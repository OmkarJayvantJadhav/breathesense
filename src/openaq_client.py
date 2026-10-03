"""The only module that talks to the OpenAQ v3 REST API.

Responsibilities:
  * auth via the `X-API-Key` header (the key is never logged)
  * client-side throttling below the published limit (60/min -> we use <= 50/min)
  * a hard per-run request budget, so a bug can never turn into an unbounded loop
  * retries with exponential backoff for HTTP 429 / transient 5xx / network errors
  * finite timeouts on every call
  * request counting and capture of the `x-ratelimit-*` response headers

Key design decision: throttling is a fixed minimum interval between requests
(60 s / max_per_minute). It is simpler than a token bucket, trivially provable to
stay under the per-minute limit, and our jobs are I/O-light, so the lost burst
capacity doesn't matter.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterator
from typing import Any

import requests
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
)

log = logging.getLogger(__name__)

BASE_URL = "https://api.openaq.org/v3"
DEFAULT_TIMEOUT = (10, 60)  # (connect, read) seconds
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5
MAX_BACKOFF_SECONDS = 120


class OpenAQError(RuntimeError):
    """Non-retryable API failure (4xx other than 429, bad JSON, ...)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RetryableHTTPError(OpenAQError):
    """429 / transient 5xx. Carries the server's Retry-After hint if present."""

    def __init__(self, message: str, status: int, retry_after: float | None):
        super().__init__(message, status)
        self.retry_after = retry_after


class RequestBudgetExceeded(OpenAQError):
    """The per-run request budget is used up. Deliberately not retried."""


def _wait_strategy(state: RetryCallState) -> float:
    """Honour Retry-After when the server sends it, else 2, 4, 8, 16 s + jitter."""
    exc = state.outcome.exception() if state.outcome else None
    if isinstance(exc, RetryableHTTPError) and exc.retry_after is not None:
        return min(exc.retry_after, MAX_BACKOFF_SECONDS)
    return min(2 ** state.attempt_number, MAX_BACKOFF_SECONDS) + random.uniform(0, 1)


def _log_retry(state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome else None
    log.warning("OpenAQ request failed (attempt %d/%d): %s",
                state.attempt_number, MAX_ATTEMPTS, exc)


class OpenAQClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        max_per_minute: int = 50,
        max_requests: int = 600,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        self.base_url = base_url.rstrip("/")
        self.min_interval = 60.0 / max_per_minute
        self.max_requests = max_requests
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"X-API-Key": api_key, "Accept": "application/json"})
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None

        self.request_count = 0          # every HTTP attempt, including retries
        self.retry_count = 0
        self.rate_limit: dict[str, str] = {}  # latest x-ratelimit-* headers

    # ------------------------------------------------------------------ core
    def _throttle(self) -> None:
        if self._last_request_at is not None:
            wait = self.min_interval - (self._clock() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = self._clock()

    def _respect_server_limit(self) -> None:
        """If the server says we have no requests left, wait for its reset."""
        remaining = self.rate_limit.get("x-ratelimit-remaining")
        reset = self.rate_limit.get("x-ratelimit-reset")
        if remaining is not None and remaining.isdigit() and int(remaining) == 0:
            try:
                wait = min(float(reset), MAX_BACKOFF_SECONDS) if reset else 60.0
            except ValueError:
                wait = 60.0
            log.warning("Server rate limit exhausted; sleeping %.0f s", wait)
            self._sleep(wait)

    @retry(
        retry=retry_if_exception_type(
            (RetryableHTTPError, requests.ConnectionError, requests.Timeout)
        ),
        stop=stop_after_attempt(MAX_ATTEMPTS),
        wait=_wait_strategy,
        before_sleep=_log_retry,
        reraise=True,
    )
    def _request(self, path: str, params: dict[str, Any] | None) -> requests.Response:
        if self.request_count >= self.max_requests:
            raise RequestBudgetExceeded(
                f"request budget of {self.max_requests} exhausted", status=None
            )
        self._respect_server_limit()
        self._throttle()
        self.request_count += 1

        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = self.session.get(url, params=params, timeout=self.timeout)
        self.rate_limit = {
            k.lower(): v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")
        }
        log.debug("GET %s %s -> %s (req #%d, rate=%s)",
                  path, params, resp.status_code, self.request_count, self.rate_limit)

        if resp.status_code in RETRYABLE_STATUS:
            raise RetryableHTTPError(
                f"HTTP {resp.status_code} for {path}",
                status=resp.status_code,
                retry_after=_parse_retry_after(resp.headers.get("Retry-After")),
            )
        if resp.status_code >= 400:
            raise OpenAQError(
                f"HTTP {resp.status_code} for {path}: {resp.text[:300]}",
                status=resp.status_code,
            )
        return resp

    # ---------------------------------------------------------------- public
    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET one resource page and return the decoded JSON body."""
        before = self.request_count
        try:
            resp = self._request(path, params)
        finally:
            self.retry_count += max(0, self.request_count - before - 1)
        try:
            return resp.json()
        except ValueError as exc:
            raise OpenAQError(f"Invalid JSON from {path}", status=resp.status_code) from exc

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int = 1000,
        max_pages: int = 50,
    ) -> Iterator[dict[str, Any]]:
        """Yield result rows across pages.

        Stops when a page comes back shorter than `limit` (OpenAQ's `meta.found`
        can be a string such as ">1000", so it is not a reliable stop signal).
        `max_pages` is a hard bound; hitting it with a full last page is logged
        as possible truncation rather than looping forever.
        """
        params = dict(params or {})
        for page in range(1, max_pages + 1):
            body = self.get(path, {**params, "limit": limit, "page": page})
            results = body.get("results") or []
            yield from results
            if len(results) < limit:
                return
        log.warning("paginate(%s) stopped at max_pages=%d; results may be truncated",
                    path, max_pages)

    def stats(self) -> dict[str, Any]:
        return {
            "requests": self.request_count,
            "retries": self.retry_count,
            "rate_limit": dict(self.rate_limit),
        }


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form; fall back to exponential backoff
