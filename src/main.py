"""Hourly ETL entry point.

Run:  python -m src.main                     # cohort = current UTC hour % 6, 9 h window (+ catch-up)
      python -m src.main --cohort 2          # a specific cohort
      python -m src.main --end 2026-09-24T17:30:00Z   # re-run a past window

  start log -> select cohort's live sensors -> fetch /hours (within budget)
  -> validate + convert + pivot (transform.py) -> upsert fact_pollutant_hourly
  -> log counts, freshness, status

Status: success = all sensors fetched; partial = some sensors failed or the
budget was reached, but what was fetched is loaded; failed = exception (nothing
fabricated, exit code 1). Only `failed` exits non-zero.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from src.config import get_settings
from src.db import PipelineRun, get_engine
from src.fetch_hourly import (DEFAULT_WINDOW_HOURS, catchup_start, cohort_for, fetch, last_readings,
                               select_targets, window)
from src.load import freshness, load_rows
from src.openaq_client import OpenAQClient
from src.transform import transform

log = logging.getLogger("main")

REQUEST_BUDGET = 600


def run(*, job: str, cohort: int | None, end: datetime, window_hours: int,
        per_minute: int = 50, budget: int = REQUEST_BUDGET) -> dict:
    settings = get_settings()
    engine = get_engine()
    client = OpenAQClient(settings.require_openaq_key(), max_requests=budget,
                          max_per_minute=per_minute)
    with PipelineRun(engine, job) as pr:
        targets = select_targets(engine, cohort)
        start, end = window(end, window_hours)
        locations = sorted({t.location_id for t in targets})
        last = last_readings(engine, locations)
        starts = {loc: catchup_start(last.get(loc), start, end) for loc in locations}
        catchup = {loc: s for loc, s in starts.items() if s < start}
        log.info("%s: cohort=%s sensors=%d stations=%d window=%s..%s catch-up stations=%d (from %s)",
                 job, cohort, len(targets), len(locations), start.isoformat(), end.isoformat(),
                 len(catchup), min(catchup.values()).isoformat() if catchup else "-")

        fetched = fetch(client, targets, start, end, starts=starts)
        rows, stats = transform(fetched.observations, pm_max=settings.pm_max_ugm3)
        upserted = load_rows(engine, rows)
        fresh, stale = freshness(engine)

        status = "partial" if (fetched.errors or fetched.budget_exhausted) else "success"
        message = None
        if status == "partial":
            message = (f"{len(fetched.errors)} sensor fetch errors"
                       + ("; request budget reached" if fetched.budget_exhausted else ""))
        details = {
            "cohort": cohort, "window_hours": window_hours,
            "window_utc": [start.isoformat(), end.isoformat()],
            "catchup_stations": len(catchup),
            "catchup_from_utc": min(catchup.values()).isoformat() if catchup else None,
            "sensors_targeted": len(targets), "sensors_fetched": fetched.sensors_ok,
            "stations_targeted": len({t.location_id for t in targets}),
            "stations_with_rows": len({r["location_id"] for r in rows}),
            "invalid_by_reason": dict(stats.invalid_by_reason),
            "low_coverage_values": stats.low_coverage,
            "fetch_errors": dict(list(fetched.errors.items())[:20]),
            "retries": client.retry_count,
            "rate_limit": client.rate_limit,
        }
        pr.metrics.update(
            api_requests=client.request_count, rows_fetched=stats.fetched,
            rows_valid=stats.valid, rows_invalid=stats.invalid, duplicates=stats.duplicates,
            rows_upserted=upserted, fresh_locations=fresh, stale_locations=stale,
            status=status, error_message=message, details=details)
    summary = {"status": status, "requests": client.request_count, "observations": stats.fetched,
               "valid": stats.valid, "invalid": stats.invalid, "duplicates": stats.duplicates,
               "rows_upserted": upserted, "fresh": fresh, "stale": stale, **details}
    log.info("%s done: %s", job, {k: summary[k] for k in (
        "status", "requests", "observations", "valid", "invalid", "duplicates",
        "rows_upserted", "fresh", "stale", "invalid_by_reason")})
    return summary


def parse_end(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def main() -> int:
    ap = argparse.ArgumentParser(description="BreatheSense hourly ingestion")
    ap.add_argument("--cohort", type=int, choices=range(6), help="default: current UTC hour %% 6")
    ap.add_argument("--end", help="window end (UTC ISO time); default now")
    ap.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    ap.add_argument("--per-minute", type=int, default=50)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    end = parse_end(args.end)
    cohort = args.cohort if args.cohort is not None else cohort_for(datetime.now(timezone.utc))
    try:
        run(job="hourly_etl", cohort=cohort, end=end, window_hours=args.window_hours,
            per_minute=args.per_minute)
    except Exception as exc:   # PipelineRun already wrote status='failed'
        log.error("hourly_etl failed: %s: %s", type(exc).__name__, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
