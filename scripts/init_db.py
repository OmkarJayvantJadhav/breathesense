"""Create the schema and load reference data. Idempotent; safe to re-run.

Run:  python -m scripts.init_db

  * sql/01_schema.sql                  tables + indexes (CREATE ... IF NOT EXISTS)
  * ref_settings + sql/02_views.sql    AQI SQL functions and analytical views
  * data/ref_aqi_breakpoints.csv  ->   ref_aqi_breakpoints   (same file src/aqi.py reads)
  * dim_date 2016-01-01 .. 2030-12-31  (OpenAQ India data starts 2016-01-30)
  * data/festivals.csv            ->   dim_date.is_festival / festival_name
"""

from __future__ import annotations

import csv
import logging
from datetime import date, timedelta

from sqlalchemy import text

from src.config import DATA_DIR, PROJECT_ROOT, get_settings
from src.db import database_size_mb, get_engine, run_sql_file, upsert
from src.load import FRESH_HOURS

log = logging.getLogger("init_db")

DIM_DATE_START = date(2016, 1, 1)
DIM_DATE_END = date(2030, 12, 31)


def season_for(month: int) -> str:
    """Indian meteorological seasons (winter, summer, monsoon, post-monsoon)."""
    if month in (12, 1, 2):
        return "Winter"
    if month in (3, 4, 5):
        return "Summer"
    if month in (6, 7, 8, 9):
        return "Monsoon"
    return "Post-monsoon"


def read_breakpoints() -> list[dict]:
    with (DATA_DIR / "ref_aqi_breakpoints.csv").open(encoding="utf-8", newline="") as f:
        return [{
            "parameter": r["parameter"],
            "avg_hours": int(r["avg_hours"]),
            "conc_low": float(r["conc_low"]),
            "conc_high": float(r["conc_high"]) if r["conc_high"].strip() else None,
            "aqi_low": int(r["aqi_low"]),
            "aqi_high": int(r["aqi_high"]),
        } for r in csv.DictReader(f)]


def read_festivals() -> dict[date, str]:
    with (DATA_DIR / "festivals.csv").open(encoding="utf-8", newline="") as f:
        return {date.fromisoformat(r["date"]): r["festival_name"] for r in csv.DictReader(f)}


def date_rows(start: date = DIM_DATE_START, end: date = DIM_DATE_END,
              festivals: dict[date, str] | None = None) -> list[dict]:
    festivals = festivals or {}
    rows, d = [], start
    while d <= end:
        rows.append({
            "date": d, "year": d.year, "month": d.month, "month_name": d.strftime("%B"),
            "day_of_week": d.strftime("%A"), "season": season_for(d.month),
            "is_festival": d in festivals, "festival_name": festivals.get(d),
        })
        d += timedelta(days=1)
    return rows


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    engine = get_engine()

    run_sql_file(engine, PROJECT_ROOT / "sql" / "01_schema.sql")
    log.info("schema applied")

    # Settings the SQL views need, from the same config Python uses (one source of truth).
    s = get_settings()
    settings_rows = [
        {"key": "min_hours_24h", "value": s.min_hours_24h},
        {"key": "min_hours_8h", "value": s.min_hours_8h},
        {"key": "aqi_alert_threshold", "value": s.aqi_alert_threshold},
        {"key": "fresh_hours", "value": FRESH_HOURS},
    ]
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS ref_settings (key TEXT PRIMARY KEY, value NUMERIC NOT NULL)")
    upsert(engine, "ref_settings", settings_rows, ["key"])
    run_sql_file(engine, PROJECT_ROOT / "sql" / "02_views.sql")
    log.info("ref_settings %s; views applied", {r["key"]: r["value"] for r in settings_rows})

    bp = read_breakpoints()
    upsert(engine, "ref_aqi_breakpoints", bp, ["parameter", "aqi_low"])
    log.info("ref_aqi_breakpoints: %d rows upserted", len(bp))

    dates = date_rows(festivals=read_festivals())
    upsert(engine, "dim_date", dates, ["date"])
    log.info("dim_date: %d rows upserted (%s .. %s)", len(dates), DIM_DATE_START, DIM_DATE_END)

    with engine.connect() as conn:
        counts = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
                  for t in ("ref_aqi_breakpoints", "dim_date")}
        fest = conn.execute(text("SELECT count(*) FROM dim_date WHERE is_festival")).scalar_one()
    log.info("verify: %s, festival days=%d, db size=%.2f MB", counts, fest, database_size_mb(engine))


if __name__ == "__main__":
    main()
