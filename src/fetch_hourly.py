"""Fetch hourly observations for one cohort of stations.

Strategy (Phase 1 decision): /sensors/{id}/hours is the only source of true
hourly means, and it's per sensor. Active stations are split into 6 cohorts by
`location_id % 6`; each run fetches every live pollutant sensor of one cohort
over a trailing window (default 9 h). Each station is refreshed every 6 h, and
the 3 h overlap picks up late-arriving hours.

Why 6, not 5: with 5, cohort 0 had 629 live sensors (over the 600 budget); with 6
the largest is 485 (Phase 4 measurement). 24 / 6 also gives each cohort the same
four UTC hours every day.

Catch-up: a station whose newest stored hour is older than the window start is fetched
from that hour instead (at most MAX_CATCHUP_HOURS back). It costs no extra requests
(one /hours call per sensor either way, 168 rows fit one page), so hours that OpenAQ
publishes late, or that a skipped GitHub run missed, are filled in on the next run
instead of leaving a permanent gap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Engine, text

from src.openaq_client import OpenAQClient, OpenAQError, RequestBudgetExceeded, RetryableHTTPError
from src.transform import Observation, parse_hours
from src.units import POLLUTANTS

log = logging.getLogger(__name__)

N_COHORTS = 6
DEFAULT_WINDOW_HOURS = 9
MAX_CATCHUP_HOURS = 168     # 7 days; fact_pollutant_hourly keeps 30


@dataclass(frozen=True)
class Target:
    location_id: int
    sensor_id: int
    parameter: str
    source_units: str | None
    convention: str


@dataclass
class FetchResult:
    observations: list[Observation] = field(default_factory=list)
    sensors_ok: int = 0
    errors: dict[int, str] = field(default_factory=dict)   # sensor_id -> short error
    budget_exhausted: bool = False


def cohort_for(when: datetime) -> int:
    """The cohort a run at `when` (UTC) fetches: rotates hourly through 0..5."""
    return when.hour % N_COHORTS


def select_targets(engine: Engine, cohort: int | None) -> list[Target]:
    """Live pollutant sensors of active stations, optionally for one cohort only."""
    sql = """
        SELECT s.location_id, s.sensor_id, s.parameter, s.source_units, l.gas_unit_convention
        FROM dim_sensor s JOIN dim_location l USING (location_id)
        WHERE s.is_live AND l.is_active AND s.parameter = ANY(:params)
          AND (CAST(:cohort AS int) IS NULL OR l.location_id % :n = CAST(:cohort AS int))
        ORDER BY s.location_id, s.parameter
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"params": list(POLLUTANTS), "cohort": cohort, "n": N_COHORTS})
        return [Target(*r) for r in rows]


def catchup_start(last_reading: datetime | None, start: datetime, end: datetime,
                  max_hours: int = MAX_CATCHUP_HOURS) -> datetime:
    """Window start for one station: the normal start, or earlier if its data is older.

    Refetches from the newest stored hour (that hour included, it may have been partial),
    but never further back than `max_hours` before `end`.
    """
    floor = end - timedelta(hours=max_hours)
    if last_reading is None:
        return max(floor, min(start, floor))   # never seen: go back the full catch-up span
    if last_reading >= start:
        return start
    return max(floor, last_reading)


def last_readings(engine: Engine, location_ids: list[int]) -> dict[int, datetime]:
    """Newest stored reading_time per location (locations with none are absent)."""
    if not location_ids:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT location_id, max(reading_time) FROM fact_pollutant_hourly
            WHERE location_id = ANY(:ids) GROUP BY location_id"""), {"ids": location_ids})
        return {int(loc): ts for loc, ts in rows}


def fetch(client: OpenAQClient, targets: list[Target], start: datetime, end: datetime,
          starts: dict[int, datetime] | None = None) -> FetchResult:
    """GET /sensors/{id}/hours for each target over [start, end].

    `starts` overrides the window start per location_id (catch-up, see catchup_start).

    A 4xx for one sensor (or 5xx after retries) is recorded and the run moves on.
    Hitting the request budget stops fetching; what was fetched is still returned.
    """
    result = FetchResult()
    starts = starts or {}
    for t in targets:
        params = {"datetime_from": starts.get(t.location_id, start).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "datetime_to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "limit": 1000}   # 9 h = 9 rows; 7-day catch-up = 168 rows; one page either way
        try:
            body = client.get(f"/sensors/{t.sensor_id}/hours", params)
        except RequestBudgetExceeded:
            result.budget_exhausted = True
            log.warning("request budget reached after %d of %d sensors",
                        result.sensors_ok + len(result.errors), len(targets))
            break
        except (OpenAQError, RetryableHTTPError) as exc:
            result.errors[t.sensor_id] = str(exc)[:120]
            continue
        except Exception as exc:  # network errors after retries
            result.errors[t.sensor_id] = f"{type(exc).__name__}: {str(exc)[:100]}"
            continue
        result.sensors_ok += 1
        result.observations += parse_hours(
            body.get("results") or [], location_id=t.location_id, sensor_id=t.sensor_id,
            parameter=t.parameter, unit_label=t.source_units, convention=t.convention)
    return result


def window(end: datetime, hours: int = DEFAULT_WINDOW_HOURS) -> tuple[datetime, datetime]:
    return end - timedelta(hours=hours), end
