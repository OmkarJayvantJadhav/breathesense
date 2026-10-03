"""Central configuration.

Every setting (and every secret) is read here from environment variables,
with `.env` loaded for local development. Other modules import `get_settings()`
instead of touching `os.environ` directly, so there is exactly one place that
knows where secrets come from. Secret fields use `repr=False` so they can never
leak into a log line via `print(settings)`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SAMPLES_DIR = DATA_DIR / "samples"
DOCS_DIR = PROJECT_ROOT / "docs"

# Local runs read .env; in GitHub Actions the variables come from GitHub Secrets.
# override=False: a real environment variable always wins over the file.
load_dotenv(PROJECT_ROOT / ".env", override=False)


class ConfigError(RuntimeError):
    """A required setting is missing or malformed."""


def _str(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _str(name)
    if raw is None:
        return default
    if raw.lower() in {"1", "true", "yes", "y"}:
        return True
    if raw.lower() in {"0", "false", "no", "n"}:
        return False
    raise ConfigError(f"{name} must be true/false, got {raw!r}")


def _date(name: str, default: str) -> date:
    raw = _str(name) or default
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be YYYY-MM-DD, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    openaq_api_key: str | None = field(repr=False)
    database_url: str | None = field(repr=False)
    test_database_url: str | None = field(repr=False)
    telegram_token: str | None = field(repr=False)
    telegram_chat_id: str | None = field(repr=False)
    aqi_alert_threshold: int
    include_low_cost: bool
    backfill_start: date
    min_hours_24h: int
    min_hours_8h: int
    pm_max_ugm3: float

    def require_openaq_key(self) -> str:
        if not self.openaq_api_key:
            raise ConfigError("OPENAQ_API_KEY is not set (add it to .env or GitHub Secrets)")
        return self.openaq_api_key

    def require_database_url(self) -> str:
        if not self.database_url:
            raise ConfigError("DATABASE_URL is not set (add it to .env or GitHub Secrets)")
        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        openaq_api_key=_str("OPENAQ_API_KEY"),
        database_url=_str("DATABASE_URL"),
        test_database_url=_str("TEST_DATABASE_URL"),
        telegram_token=_str("TELEGRAM_TOKEN"),
        telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
        aqi_alert_threshold=_int("AQI_ALERT_THRESHOLD", 300),
        include_low_cost=_bool("INCLUDE_LOW_COST", False),
        backfill_start=_date("BACKFILL_START", "2018-01-01"),
        min_hours_24h=_int("MIN_HOURS_24H", 18),
        min_hours_8h=_int("MIN_HOURS_8H", 6),
        pm_max_ugm3=float(_int("PM_MAX_UGM3", 1500)),
    )
