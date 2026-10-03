"""Daily rollup, retention and last_run.json.

Run:  python -m src.rollup                    # previous IST day
      python -m src.rollup --date 2026-09-23  # a specific IST day (idempotent)
      python -m src.rollup --no-retention

1. City summary for one IST calendar day -> daily_city_summary (source='live_rollup').
   Live rows overwrite backfill rows for the same day (ON CONFLICT DO UPDATE).
   Definitions (documented in README; this is an analytical rollup, not a CPCB AQI):
     station daily AQI   24 h pollutants: mean of the day's hours (>= MIN_HOURS_24H);
                         O3/CO: highest complete 8 h rolling mean within the day
                         (each window >= MIN_HOURS_8H), as CPCB does for 8 h pollutants.
     avg_<pollutant>     mean of each station's daily mean, over stations with a complete day
                         (>= MIN_HOURS_24H hours); the backfill applies the same rule
     median/max_station_aqi, dominant_pollutant (most frequent station dominant)
     max_hourly_pm25     highest station hourly PM2.5 in the city that day
     hours_poor_or_worse IST hours where the city's median rolling station AQI >= 201
     reporting_locations stations with a valid daily AQI
   Only stations with a city+state mapping are included.
2. Retention: fact_pollutant_hourly 30 d, pipeline_log 90 d, alert_log 30 d.
3. last_run.json: public observability + keeps scheduled workflows alive.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, text

from src.aqi import DOMINANCE_ORDER, AqiResult, default_breakpoints, overall_aqi, station_aqi
from src.config import PROJECT_ROOT, get_settings
from src.db import PipelineRun, database_size_mb, get_engine, upsert
from src.units import POLLUTANTS

log = logging.getLogger("rollup")

IST = ZoneInfo("Asia/Kolkata")
POOR = 201
RETENTION = (("fact_pollutant_hourly", "reading_time", 30),
             ("pipeline_log", "started_at", 90),
             ("alert_log", "alerted_at", 30))
LAST_RUN_JSON = PROJECT_ROOT / "last_run.json"

Series = dict[datetime, float | None]


def ist_day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC [start, end) of an IST calendar day: 18:30Z the day before to 18:30Z.
    CPCB hour starts (HH:30Z) are whole IST hours, so they align with these bounds."""
    start = datetime.combine(day, datetime.min.time(), IST).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def day_aqi(hourly: dict[str, Series], day_start: datetime, day_end: datetime,
            min_24h: int, min_8h: int) -> AqiResult:
    """Station daily AQI for the hours with day_start <= t < day_end."""
    bp = default_breakpoints()
    averages: dict[str, float | None] = {}
    for p, series in hourly.items():
        if p not in bp.avg_hours:
            continue
        in_day = {t: v for t, v in series.items() if day_start <= t < day_end and v is not None}
        if bp.avg_hours[p] == 24:
            averages[p] = sum(in_day.values()) / len(in_day) if len(in_day) >= min_24h else None
        else:  # 8 h: best complete rolling window ending within the day
            best = None
            t = day_start
            while t < day_end:
                w = [v for tt, v in in_day.items() if t - timedelta(hours=7) <= tt <= t]
                if len(w) >= min_8h:
                    m = sum(w) / len(w)
                    best = m if best is None or m > best else best
                t += timedelta(hours=1)
            averages[p] = best
    return overall_aqi(averages, bp)


def most_frequent(items: list[str]) -> str | None:
    if not items:
        return None
    counts = Counter(items)
    top = max(counts.values())
    return next(p for p in DOMINANCE_ORDER if counts.get(p) == top)


def build_city_rows(stations: dict[int, tuple[str, str]], hourly: dict[int, dict[str, Series]],
                    day: date, min_24h: int, min_8h: int) -> list[dict]:
    """Pure: station hourly data (standard units) -> daily_city_summary rows."""
    day_start, day_end = ist_day_bounds(day)
    by_city: dict[tuple[str, str], list[int]] = defaultdict(list)
    for lid, key in stations.items():
        if lid in hourly:
            by_city[key].append(lid)

    rows = []
    for (city, state), lids in sorted(by_city.items()):
        daily = {lid: day_aqi(hourly[lid], day_start, day_end, min_24h, min_8h) for lid in lids}
        valid = {lid: r for lid, r in daily.items() if r.valid}

        # City pollutant averages: mean of station daily means, counting only complete station-days
        # (>= MIN_HOURS_24H hourly values): the same rule as the station AQI and the historical
        # backfill, so the series stays comparable across the backfill-to-live boundary. A station
        # with a few hours of data would otherwise pull the average toward whichever hours it saw.
        avgs = {}
        for p in POLLUTANTS:
            means = []
            for lid in lids:
                vals = [v for t, v in hourly[lid].get(p, {}).items() if day_start <= t < day_end and v is not None]
                if len(vals) >= min_24h:
                    means.append(sum(vals) / len(vals))
            avgs[f"avg_{p}"] = round(sum(means) / len(means), 3) if means else None
        if all(v is None for v in avgs.values()):
            continue    # no complete station-day in this city: no row (missing data stays missing)

        pm25_hours = [v for lid in lids for t, v in hourly[lid].get("pm25", {}).items()
                      if day_start <= t < day_end and v is not None]

        # Hours where the city's median rolling station AQI is Poor or worse.
        poor_hours = 0
        t = day_start
        while t < day_end:
            aqis = [r.aqi for lid in lids if (r := station_aqi(hourly[lid], t, min_24h, min_8h)).valid]
            if aqis and statistics.median(aqis) >= POOR:
                poor_hours += 1
            t += timedelta(hours=1)

        aqis = [r.aqi for r in valid.values()]
        rows.append({
            "city": city, "state": state, "summary_date": day, **avgs,
            "median_station_aqi": statistics.median(aqis) if aqis else None,
            "max_station_aqi": max(aqis) if aqis else None,
            "dominant_pollutant": most_frequent([r.dominant_pollutant for r in valid.values()]),
            "max_hourly_pm25": max(pm25_hours) if pm25_hours else None,
            "hours_poor_or_worse": poor_hours,
            "reporting_locations": len(valid),
            "source": "live_rollup",
            "o3_co_approximated": False,
        })
    return rows


def load_day(engine: Engine, day: date) -> tuple[dict[int, tuple[str, str]], dict[int, dict[str, Series]]]:
    """Mapped stations and their hourly data for the day plus the 24 h before (rolling windows)."""
    day_start, day_end = ist_day_bounds(day)
    with engine.connect() as c:
        stations = {r.location_id: (r.city, r.state) for r in c.execute(text(
            "SELECT location_id, city, state FROM dim_location WHERE city IS NOT NULL AND state IS NOT NULL"))}
        rows = c.execute(text(f"""
            SELECT location_id, reading_time, {", ".join(POLLUTANTS)} FROM fact_pollutant_hourly
            WHERE reading_time >= :a AND reading_time < :b"""),
            {"a": day_start - timedelta(hours=24), "b": day_end}).mappings().all()
    hourly: dict[int, dict[str, Series]] = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        if r["location_id"] in stations:
            for p in POLLUTANTS:
                if r[p] is not None:
                    hourly[r["location_id"]][p][r["reading_time"]] = float(r[p])
    return stations, hourly


def apply_retention(engine: Engine) -> dict[str, int]:
    deleted = {}
    with engine.begin() as c:
        for table, col, days in RETENTION:
            deleted[table] = c.execute(text(
                f"DELETE FROM {table} WHERE {col} < now() - make_interval(days => :d)"), {"d": days}).rowcount
    return deleted


def write_last_run(engine: Engine, day: date, cities: int) -> dict:
    with engine.connect() as c:
        hourly_rows = c.execute(text("SELECT count(*) FROM fact_pollutant_hourly")).scalar_one()
        active = c.execute(text("SELECT count(*) FROM dim_location WHERE is_active")).scalar_one()
        last_hour = c.execute(text("SELECT max(reading_time) FROM fact_pollutant_hourly")).scalar_one()
    info = {
        "last_rollup_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rollup_ist_date": day.isoformat(),
        "cities_with_data": cities,
        "hourly_rows": hourly_rows,
        "latest_reading_utc": last_hour.isoformat() if last_hour else None,
        "active_locations": active,
        "db_size_mb": database_size_mb(engine),
    }
    LAST_RUN_JSON.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description="BreatheSense daily rollup")
    ap.add_argument("--date", help="IST day YYYY-MM-DD (default: yesterday IST)")
    ap.add_argument("--no-retention", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = get_settings()
    day = date.fromisoformat(args.date) if args.date else datetime.now(IST).date() - timedelta(days=1)
    engine = get_engine()
    try:
        with PipelineRun(engine, "daily_rollup") as pr:
            stations, hourly = load_day(engine, day)
            rows = build_city_rows(stations, hourly, day, settings.min_hours_24h, settings.min_hours_8h)
            upserted = upsert(engine, "daily_city_summary", rows, ["city", "state", "summary_date"])
            deleted = {} if args.no_retention else apply_retention(engine)
            info = write_last_run(engine, day, len(rows))
            pr.metrics.update(rows_fetched=sum(len(s) for h in hourly.values() for s in h.values()),
                              rows_upserted=upserted,
                              details={"ist_date": day.isoformat(), "cities": len(rows),
                                       "retention_deleted": deleted, "db_size_mb": info["db_size_mb"]})
    except Exception as exc:
        log.error("daily_rollup failed: %s: %s", type(exc).__name__, exc)
        return 1
    log.info("rollup %s: %d city rows; retention %s; %s", day, len(rows), deleted, info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
