"""OpenAQ client tests. No network: a fake session returns canned responses,
and sleep/clock are injected so throttling and backoff run instantly."""

import pytest
import requests

import src.openaq_client as oc
from src.openaq_client import OpenAQClient, OpenAQError, RequestBudgetExceeded


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {"meta": {}, "results": []}
        self.headers = headers or {}
        self.text = str(self._body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


@pytest.fixture(autouse=True)
def no_real_backoff(monkeypatch):
    # tenacity sleeps via its own sleep function; make retries instant.
    monkeypatch.setattr(oc.OpenAQClient._request.retry, "sleep", lambda s: None)


def make_client(responses, **kw):
    clock = FakeClock()
    session = FakeSession(responses)
    client = OpenAQClient("test-key", session=session, sleep=clock.sleep, clock=clock.clock, **kw)
    return client, session, clock


def test_sets_auth_header_and_timeout():
    client, session, _ = make_client([FakeResponse()])
    client.get("/countries")
    assert session.headers["X-API-Key"] == "test-key"
    assert session.calls[0]["timeout"] == oc.DEFAULT_TIMEOUT
    assert session.calls[0]["url"] == "https://api.openaq.org/v3/countries"


def test_api_key_not_in_repr():
    client, _, _ = make_client([])
    assert "test-key" not in repr(client.stats())


def test_throttle_spaces_requests():
    client, _, clock = make_client([FakeResponse(), FakeResponse()], max_per_minute=50)
    client.get("/a")
    client.get("/b")
    assert clock.sleeps == [pytest.approx(1.2)]


def test_retries_429_then_succeeds():
    client, session, _ = make_client([
        FakeResponse(429, headers={"Retry-After": "1"}),
        FakeResponse(503),
        FakeResponse(200, {"results": [1]}),
    ])
    assert client.get("/x") == {"results": [1]}
    assert client.request_count == 3
    assert client.retry_count == 2


def test_retries_connection_error():
    client, _, _ = make_client([requests.ConnectionError("boom"), FakeResponse()])
    client.get("/x")
    assert client.request_count == 2


def test_gives_up_after_max_attempts():
    client, _, _ = make_client([FakeResponse(500)] * oc.MAX_ATTEMPTS)
    with pytest.raises(oc.RetryableHTTPError):
        client.get("/x")
    assert client.request_count == oc.MAX_ATTEMPTS


def test_404_not_retried():
    client, _, _ = make_client([FakeResponse(404)])
    with pytest.raises(OpenAQError) as e:
        client.get("/missing")
    assert e.value.status == 404
    assert client.request_count == 1


def test_request_budget_is_hard_stop():
    client, _, _ = make_client([FakeResponse(), FakeResponse()], max_requests=1)
    client.get("/a")
    with pytest.raises(RequestBudgetExceeded):
        client.get("/b")


def test_paginate_stops_on_short_page():
    client, session, _ = make_client([
        FakeResponse(body={"results": [1, 2]}),
        FakeResponse(body={"results": [3]}),
    ])
    assert list(client.paginate("/locations", {"iso": "IN"}, limit=2)) == [1, 2, 3]
    assert [c["params"]["page"] for c in session.calls] == [1, 2]
    assert session.calls[0]["params"]["iso"] == "IN"


def test_paginate_respects_max_pages():
    client, session, _ = make_client([FakeResponse(body={"results": [1]})] * 5)
    assert list(client.paginate("/x", limit=1, max_pages=3)) == [1, 1, 1]
    assert len(session.calls) == 3


def test_captures_rate_limit_headers_and_waits_when_exhausted():
    client, _, clock = make_client([
        FakeResponse(headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "7",
                              "Content-Type": "application/json"}),
        FakeResponse(),
    ], max_per_minute=6000)
    client.get("/a")
    assert client.rate_limit == {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "7"}
    client.get("/b")
    assert 7 in clock.sleeps
