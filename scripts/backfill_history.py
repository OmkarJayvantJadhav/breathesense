"""Historical daily backfill. Runs locally, never in GitHub Actions.

    python -m scripts.backfill_history --estimate        # no API calls; prints plan
    python -m scripts.backfill_history --fetch           # resumable; stops at --max-requests
    python -m scripts.backfill_history --fetch --shard 0/2 --per-minute 15   # worker 1 of 2
    python -m scripts.backfill_history --fetch --shard 1/2 --per-minute 15   # worker 2 of 2
    python -m scripts.backfill_history --aggregate --dry-run   # unit sanity report only
    python -m scripts.backfill_history --aggregate       # city-day rows -> daily_city_summary

One worker is latency-bound (~15-19 requests/min: a /days call takes 1-3 s plus a 0.5 s
progress write), well below the 25/min throttle. Shards split the sensors by
sensor_id % N so N workers can run side by side; the combined throttle is capped at
MAX_TOTAL_PER_MINUTE so the 60/min and 2,000/hour limits hold by construction.

Scope: every pollutant sensor (live and legacy) at mapped locations. Phase 7
found CPCB history on OpenAQ in two eras: legacy series (mostly 2018 -> 2022-10-31)
and the current series (2025-02-18 -> now). In between only five metros (Delhi, Mumbai,
Hyderabad, Kolkata, Chennai) have data.
Much of 2018-2022 sits under legacy location ids, which carry city mappings too.

Storage: raw daily series are cached locally (data/backfill_cache/<sensor>.v2.csv.gz,
gitignored). Only city-day aggregates go to Neon, as source='backfill'. Live rollup rows
always win: the upsert only overwrites rows that are still 'backfill'. A sensor that has
no current-format cache file is fetched again, whatever backfill_progress says.

Backfill AQI limitation (README): /days gives daily means only, so the station
daily AQI uses the daily mean of O3/CO instead of the max 8 h mean
(o3_co_approximated = TRUE). max_hourly_pm25 and hours_poor_or_worse stay NULL.
A sensor-day counts only if it passes the completeness rule in station_day_values
(enough observations AND observations spread over the day). Missing history is never
fabricated.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import gzip
import logging
import statistics
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timezone

from sqlalchemy import text

from src.config import DATA_DIR, get_settings
from src.db import get_engine, upsert
from src.openaq_client import OpenAQClient, OpenAQError, RequestBudgetExceeded
from src.days_rollup import PARAMS, StationDays, build_city_rows, day_values, days_row
from src.days_rollup import collect_station_days as _collect_station_days

log = logging.getLogger("backfill")

CACHE_DIR = DATA_DIR / "backfill_cache"
# first_obs / last_obs: local time of the first and last observation inside the day
# (OpenAQ coverage.datetimeFrom / datetimeTo); they let the completeness rule check
# that readings are spread over the day, not just counted.
CACHE_COLUMNS = ["ist_date", "value", "units", "observed", "expected", "first_obs", "last_obs"]
PER_MINUTE = 25           # leaves room for hourly_etl within OpenAQ's 2,000/hour
# Phase 7 measurement: one worker manages ~17/min, not 25. Each sensor costs a /days call
# (median 1.7 s, up to 3.6 s) plus a 0.5 s progress write, so the loop is latency-bound.
EFFECTIVE_PER_MINUTE = 17
# Combined throttle of all workers. 35/min for the 39 minutes outside the hourly_etl
# window is 1,365 requests, plus hourly_etl's <= 485 = 1,850 < 2,000 per rolling hour.
MAX_TOTAL_PER_MINUTE = 35
DEFAULT_MAX_REQUESTS = 1400
PAGE = 1000


# ------------------------------------------------------------------ targets
def load_targets(engine) -> list[dict]:
    with engine.connect() as c:
        targets = [dict(r) for r in c.execute(text("""
            SELECT s.sensor_id, s.location_id, s.parameter, s.source_units, s.is_live,
                   l.city, l.state, l.gas_unit_convention, p.status, p.last_date_loaded
            FROM dim_sensor s
            JOIN dim_location l USING (location_id)
            LEFT JOIN backfill_progress p USING (sensor_id)
            WHERE s.parameter = ANY(:params) AND l.city IS NOT NULL AND l.state IS NOT NULL
            ORDER BY s.sensor_id"""), {"params": list(PARAMS)}).mappings()]
    for t in targets:
        t["cached"] = cache_path(t["sensor_id"]).exists()   # current-format file on disk
    return targets


def is_fetched(t: dict) -> bool:
    """Done (or confirmed empty) AND cached in the current format. A sensor fetched by an
    older version of this script has no usable cache and is fetched again."""
    return t["status"] in ("done", "empty") and t["cached"]


def select_todo(targets: list[dict], shard: tuple[int, int] = (0, 1),
                only: str | None = None) -> list[dict]:
    """Sensors still to fetch that belong to this worker (sensor_id % N == I), optionally only
    the current-era ("live") or only the legacy ones. Shards are disjoint and together cover
    everything, so N workers never duplicate."""
    i, n = shard
    return [t for t in targets if not is_fetched(t) and t["sensor_id"] % n == i
            and (only is None or t["is_live"] == (only == "live"))]


def parse_shard(spec: str) -> tuple[int, int]:
    """'I/N' -> (I, N)."""
    try:
        i, n = (int(x) for x in spec.split("/"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"--shard must look like 0/2, got {spec!r}") from None
    if not 0 <= i < n:
        raise argparse.ArgumentTypeError(f"--shard {spec}: need 0 <= I < N")
    return i, n


def check_rate_budget(per_minute: int, workers: int) -> None:
    """Refuse a combined throttle that could breach OpenAQ's limits."""
    if per_minute < 1 or per_minute * workers > MAX_TOTAL_PER_MINUTE:
        raise ValueError(f"{workers} worker(s) x {per_minute}/min = {per_minute * workers}/min; "
                         f"the combined throttle must be 1-{MAX_TOTAL_PER_MINUTE}/min")


def estimate(targets: list[dict], start: date) -> None:
    todo = select_todo(targets)
    live = sum(1 for t in todo if t["is_live"])
    legacy = len(todo) - live
    days_span = (date.today() - start).days
    low, likely, high = live + legacy, live + round(legacy * 1.5), live + legacy * 2
    print(f"Backfill from {start}: {len(targets)} sensors in scope, {len(todo)} still to fetch "
          f"({live} live, {legacy} legacy); max span {days_span} days")
    print(f"Estimated requests: {low:,} - {high:,} (likely ~{likely:,}; "
          f"1 page = {PAGE} days: live series ~1 page, legacy 1-2 pages)")
    lo, hi = HOURLY_WINDOW_MINUTES
    active_minutes = 60 - (hi - lo)
    print(f"Wall-clock (workers pause :{lo:02d}-:{hi:02d} each hour for hourly_etl, so {active_minutes} min/h):")
    for label, rate in (("1 worker, as measured", EFFECTIVE_PER_MINUTE),
                        ("2 workers x 15/min (--shard i/2 --per-minute 15)", 30)):
        hours = [n / (rate * active_minutes) for n in (low, likely, high)]
        print(f"  {label:50} {rate:>2}/min: {hours[0]:4.1f} - {hours[2]:4.1f} h (likely {hours[1]:4.1f} h)")
    print(f"Runs in chunks of --max-requests (default {DEFAULT_MAX_REQUESTS}); resumable.")


# ------------------------------------------------------------------ fetch
def cache_path(sensor_id: int):
    return CACHE_DIR / f"{sensor_id}.v2.csv.gz"


ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


@contextmanager
def keep_awake():
    """Ask Windows not to go to idle sleep while a long fetch runs. Network traffic does not
    count as activity, so an unattended PC sleeps after its power-plan timeout and freezes the
    run (Phase 7 lost 45 minutes that way). This is a per-process request, released on exit;
    it changes no power setting. Closing the lid or pressing sleep still suspends the PC."""
    kernel32 = getattr(getattr(ctypes, "windll", None), "kernel32", None) if sys.platform == "win32" else None
    if kernel32 is not None:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        if kernel32 is not None:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)


# OpenAQ's 60/min limit is per API key and shared with hourly_etl (cron :15, 50/min
# for ~10 min). The backfill sleeps through that window so the two never overlap.
HOURLY_WINDOW_MINUTES = (14, 35)


def _wait_outside_hourly_window() -> None:
    now = datetime.now(timezone.utc)
    lo, hi = HOURLY_WINDOW_MINUTES
    if lo <= now.minute < hi:
        secs = (hi - now.minute) * 60 - now.second
        log.info("pausing %d s while hourly_etl runs (minutes :%02d-:%02d UTC)", secs, lo, hi)
        time.sleep(secs)


def fetch(engine, targets: list[dict], start: date, max_requests: int,
          pause_for_hourly: bool = True, per_minute: int = PER_MINUTE,
          shard: tuple[int, int] = (0, 1), only: str | None = None) -> None:
    check_rate_budget(per_minute, shard[1])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    client = OpenAQClient(get_settings().require_openaq_key(), max_requests=max_requests,
                          max_per_minute=per_minute)
    todo = select_todo(targets, shard, only)
    log.info("%d sensors to fetch (shard %d/%d, %d/min, only=%s)",
             len(todo), shard[0], shard[1], per_minute, only or "all")
    done = 0
    try:
        for t in todo:
            if pause_for_hourly:
                _wait_outside_hourly_window()
            rows, page = [], 1
            try:
                while page <= 5:
                    body = client.get(f"/sensors/{t['sensor_id']}/days", {
                        "date_from": start.isoformat(), "date_to": date.today().isoformat(),
                        "limit": PAGE, "page": page})
                    res = body.get("results") or []
                    rows += res
                    if len(res) < PAGE:
                        break
                    page += 1
            except RequestBudgetExceeded:
                raise
            except OpenAQError as exc:
                log.warning("sensor %s: %s", t["sensor_id"], str(exc)[:120])
                _progress(engine, t["sensor_id"], None, "error")
                continue
            with gzip.open(cache_path(t["sensor_id"]), "wt", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(CACHE_COLUMNS)
                for r in rows:
                    row = days_row(r)
                    w.writerow([row[c] for c in CACHE_COLUMNS])
            last = max((r["period"]["datetimeFrom"]["local"][:10] for r in rows), default=None)
            _progress(engine, t["sensor_id"], last, "done" if rows else "empty")
            done += 1
            if done % 50 == 0:
                log.info("fetched %d/%d sensors, %d requests", done, len(todo), client.request_count)
    except RequestBudgetExceeded:
        log.warning("request budget reached; re-run --fetch to continue")
    log.info("fetch: %d sensors this run, %d requests, %d retries",
             done, client.request_count, client.retry_count)


def _progress(engine, sensor_id: int, last: str | None, status: str) -> None:
    upsert(engine, "backfill_progress", [{
        "sensor_id": sensor_id, "last_date_loaded": date.fromisoformat(last) if last else None,
        "status": status, "updated_at": datetime.now(timezone.utc)}], ["sensor_id"])


# ------------------------------------------------------------------ aggregate
def read_cache(sensor_id: int) -> list[dict]:
    p = cache_path(sensor_id)
    if not p.exists():
        return []
    with gzip.open(p, "rt", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def station_day_values(t: dict, pm_max: float, min_count: int, stats: Counter,
                       report: dict) -> dict[date, tuple[float, float]]:
    """day_values() over the sensor's cached /days file."""
    return day_values(t, read_cache(t["sensor_id"]), pm_max, min_count, stats, report)


def collect_station_days(ts: list[dict], pm_max: float, min_count: int, stats: Counter,
                         report: dict) -> StationDays:
    """One city's sensors grouped by station (see src.days_rollup.collect_station_days)."""
    return _collect_station_days(((t, read_cache(t["sensor_id"])) for t in ts),
                                 pm_max, min_count, stats, report)


def aggregate(engine, targets: list[dict], dry_run: bool) -> None:
    settings = get_settings()
    by_city: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in targets:
        if t["status"] == "done" and t["cached"]:
            by_city[(t["city"], t["state"])].append(t)
    stale_cache = sum(1 for t in targets if t["status"] == "done" and not t["cached"])
    if stale_cache:
        print(f"NOTE: {stale_cache} sensors were fetched by an older version and have no current "
              f"cache file; they are excluded until --fetch fetches them again.")
    stats: Counter = Counter()
    report: dict[tuple, list[float]] = defaultdict(list)
    total_rows = 0

    for (city, state), ts in sorted(by_city.items()):
        station = collect_station_days(ts, settings.pm_max_ugm3, settings.min_hours_24h, stats, report)
        rows = build_city_rows(city, state, station)
        total_rows += len(rows)
        if rows and not dry_run:
            upsert(engine, "daily_city_summary", rows, ["city", "state", "summary_date"],
                   where="daily_city_summary.source = 'backfill'")
    print(f"\nday-level validation: {dict(stats)}")
    print("\nunit sanity report (standard units; CO mg/m3, others ug/m3):")
    print(f"  {'param':5} {'label':6} {'conv':12} {'era':8} {'n':>8} {'p10':>8} {'median':>8} {'p90':>8}")
    for (p, label, conv, era), vals in sorted(report.items()):
        if len(vals) >= 20:
            q = statistics.quantiles(vals, n=10)
            print(f"  {p:5} {label:6} {conv:12} {era:8} {len(vals):8,} {q[0]:8.2f} "
                  f"{statistics.median(vals):8.2f} {q[-1]:8.2f}")
    print(f"\n{'would write' if dry_run else 'upserted'} {total_rows:,} city-day rows "
          f"for {len(by_city)} cities")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--estimate", action="store_true")
    g.add_argument("--fetch", action="store_true")
    g.add_argument("--aggregate", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="with --aggregate: report only")
    ap.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    ap.add_argument("--no-pause", action="store_true",
                    help="don't pause during the hourly_etl window (only if Actions are off)")
    ap.add_argument("--shard", type=parse_shard, default=(0, 1), metavar="I/N",
                    help="with --fetch: this worker takes sensors with sensor_id %% N == I")
    ap.add_argument("--only", choices=("live", "legacy"),
                    help="with --fetch: only the current-era (live) or only the legacy sensors")
    ap.add_argument("--per-minute", type=int, default=None,
                    help=f"with --fetch: this worker's throttle (default {PER_MINUTE} for one worker; "
                         f"with N shards the default is {MAX_TOTAL_PER_MINUTE}//N). "
                         f"N x per-minute must stay <= {MAX_TOTAL_PER_MINUTE}")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    per_minute = args.per_minute or min(PER_MINUTE, MAX_TOTAL_PER_MINUTE // args.shard[1])
    if args.fetch:
        try:
            check_rate_budget(per_minute, args.shard[1])
        except ValueError as exc:
            ap.error(str(exc))

    engine = get_engine()
    start = get_settings().backfill_start
    targets = load_targets(engine)
    if args.estimate:
        estimate(targets, start)
    elif args.fetch:
        with keep_awake():
            fetch(engine, targets, start, args.max_requests, pause_for_hourly=not args.no_pause,
                  per_minute=per_minute, shard=args.shard, only=args.only)
    else:
        aggregate(engine, targets, args.dry_run)


if __name__ == "__main__":
    main()
