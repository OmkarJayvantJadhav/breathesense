"""Daily city rollup from OpenAQ /days.

Run:  python -m src.rollup_days                    # previous IST day
      python -m src.rollup_days --date 2026-09-30  # a specific IST day (idempotent)

Why this exists. GitHub fires the "hourly" cron only ~4 times a day, so the hourly
cohort strategy collects roughly 9 of every 36 hours per station and live station-days
rarely reach the 18 complete hours the AQI needs. /sensors/{id}/days returns the whole
day's mean per sensor whatever the cron lag, so this job fetches yesterday's /days for
every live sensor of the active stations (~2,500 requests, ~85 min at 30/min) and writes
city-days with the same rules as the historical backfill (src/days_rollup.py).

Rows are written with source='backfill' (derived from daily means: O3/CO use the daily
mean, hourly metrics stay NULL) and never overwrite a live_rollup row. The hourly-based
src.rollup runs after this job and overwrites a city-day where it has complete hourly data.
A run fails (no write) if more than 5% of the sensors could not be fetched.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import requests
from sqlalchemy import Engine, text

from src.config import get_settings
from src.days_rollup import PARAMS, build_city_rows, collect_station_days, days_row
from src.db import PipelineRun, get_engine, upsert
from src.openaq_client import OpenAQClient, OpenAQError, RequestBudgetExceeded
from src.rollup import IST

log = logging.getLogger("rollup_days")

PER_MINUTE = 30            # 30 x 60 = 1,800 < OpenAQ's 2,000/hour; the workflow never overlaps hourly_etl
MAX_REQUESTS = 3500
MAX_ERROR_SHARE = 0.05


def select_sensors(engine: Engine) -> list[dict]:
    """Live pollutant sensors of active stations that have a city+state mapping."""
    sql = """
        SELECT s.sensor_id, s.location_id, s.parameter, s.source_units,
               l.gas_unit_convention, l.city, l.state
        FROM dim_sensor s JOIN dim_location l USING (location_id)
        WHERE s.is_live AND l.is_active AND s.parameter = ANY(:params)
          AND l.city IS NOT NULL AND l.state IS NOT NULL
        ORDER BY s.sensor_id"""
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), {"params": list(PARAMS)}).mappings()]


def day_rows_for(results: list[dict], day: date) -> list[dict]:
    """Cache-format rows of one sensor for exactly `day` (the API may return neighbouring days)."""
    return [r for r in map(days_row, results) if r["ist_date"] == day.isoformat()]


def build_rows(sensors: list[dict], fetched: dict[int, list[dict]], day: date, pm_max: float,
               min_hours: int, stats: Counter) -> list[dict]:
    """Pure: sensors + their /days rows for `day` -> daily_city_summary rows."""
    by_city: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in sensors:
        if t["sensor_id"] in fetched:
            by_city[(t["city"], t["state"])].append(t)
    out, report = [], defaultdict(list)
    for (city, state), ts in sorted(by_city.items()):
        station = collect_station_days(((t, fetched[t["sensor_id"]]) for t in ts),
                                       pm_max, min_hours, stats, report)
        out += [r for r in build_city_rows(city, state, station) if r["summary_date"] == day]
    return out


def fetch_day(client: OpenAQClient, sensors: list[dict], day: date) -> tuple[dict[int, list[dict]], dict[int, str]]:
    """GET /days for each sensor. Returns (sensor_id -> rows for `day`, sensor_id -> short error)."""
    fetched: dict[int, list[dict]] = {}
    errors: dict[int, str] = {}
    for n, t in enumerate(sensors, 1):
        try:
            body = client.get(f"/sensors/{t['sensor_id']}/days", {
                "date_from": day.isoformat(), "date_to": (day + timedelta(days=1)).isoformat(), "limit": 5})
            fetched[t["sensor_id"]] = day_rows_for(body.get("results") or [], day)
        except RequestBudgetExceeded:
            raise
        except (OpenAQError, requests.RequestException) as exc:
            errors[t["sensor_id"]] = f"{type(exc).__name__}: {str(exc)[:80]}"
        if n % 250 == 0:
            log.info("fetched %d/%d sensors, %d requests", n, len(sensors), client.request_count)
    return fetched, errors


def check_error_share(errors: dict, n_sensors: int) -> None:
    """Fail the run (before any write) when too many sensors could not be fetched."""
    if len(errors) > MAX_ERROR_SHARE * n_sensors:
        raise RuntimeError(f"{len(errors)} of {n_sensors} sensors failed, e.g. {next(iter(errors.values()))}")


def main() -> int:
    ap = argparse.ArgumentParser(description="BreatheSense daily /days rollup")
    ap.add_argument("--date", help="IST day YYYY-MM-DD (default: yesterday IST)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    settings = get_settings()
    day = date.fromisoformat(args.date) if args.date else datetime.now(IST).date() - timedelta(days=1)
    engine = get_engine()
    try:
        with PipelineRun(engine, "daily_days") as pr:
            sensors = select_sensors(engine)
            client = OpenAQClient(settings.require_openaq_key(), max_requests=MAX_REQUESTS,
                                  max_per_minute=PER_MINUTE)
            fetched, errors = fetch_day(client, sensors, day)
            check_error_share(errors, len(sensors))
            stats: Counter = Counter()
            rows = build_rows(sensors, fetched, day, settings.pm_max_ugm3, settings.min_hours_24h, stats)
            upserted = upsert(engine, "daily_city_summary", rows, ["city", "state", "summary_date"],
                              where="daily_city_summary.source = 'backfill'")
            pr.metrics.update(api_requests=client.request_count, rows_fetched=len(fetched),
                              rows_valid=stats["valid_day"],
                              rows_invalid=sum(v for k, v in stats.items() if k != "valid_day"),
                              rows_upserted=upserted,
                              details={"ist_date": day.isoformat(), "sensors": len(sensors),
                                       "errors": len(errors), "day_validation": dict(stats)})
    except Exception as exc:
        log.error("daily_days failed: %s: %s", type(exc).__name__, exc)
        return 1
    log.info("daily_days %s: %d sensors (%d errors), %d requests, %d city rows; %s",
             day, len(sensors), len(errors), client.request_count, len(rows), dict(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
