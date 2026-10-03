"""Daily location/sensor sync.

Run:  python -m src.sync_locations

  1. GET /locations?iso=IN (one page holds all 764 Indian locations; Phase 1).
     Keep fixed reference monitors (isMonitor, not isMobile) unless INCLUDE_LOW_COST.
  2. For locations seen in the last 30 days, GET /locations/{id}/latest to learn
     each sensor's own last timestamp. Phase 1 found ~310 stations with a dead
     legacy sensor next to the live one for the same parameter, so the live sensor
     is chosen by that timestamp, never by parameter name.
  3. Upsert dim_location (city/state from data/city_mapping.csv, gas unit
     convention from data/unit_convention.csv) and dim_sensor.
  4. Mark inactive (no observation in 7 days); never delete. Log unmapped stations.

Budget: 1 + ~480 requests, under the 600 per-run cap.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from src.city_mapping import read_mapping
from src.config import DATA_DIR, get_settings
from src.db import PipelineRun, get_engine, upsert
from src.openaq_client import OpenAQClient

log = logging.getLogger("sync_locations")

# Parameters we keep a live sensor for: AQI pollutants + no/nox (unit classification).
LIVE_PARAMS = ("pm25", "pm10", "no2", "so2", "o3", "co", "nh3", "pb", "no", "nox")
ACTIVE_DAYS = 7
LATEST_LOOKBACK_DAYS = 30
REQUEST_BUDGET = 600
UNIT_CONVENTION_CSV = DATA_DIR / "unit_convention.csv"


def parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def utc_of(obj: Any) -> datetime | None:
    return parse_ts(obj.get("utc")) if isinstance(obj, dict) else None


def fetch_locations(client: OpenAQClient, include_low_cost: bool) -> list[dict]:
    params: dict[str, Any] = {"iso": "IN"}
    if not include_low_cost:
        params["monitor"] = "true"
    locs = list(client.paginate("/locations", params, limit=1000, max_pages=5))
    return [l for l in locs if l.get("isMobile") is False
            and (include_low_cost or l.get("isMonitor") is True)]


def read_unit_conventions() -> dict[int, str]:
    if not UNIT_CONVENTION_CSV.exists():
        return {}
    with UNIT_CONVENTION_CSV.open(encoding="utf-8", newline="") as f:
        return {int(r["location_id"]): r["convention"] for r in csv.DictReader(f)}


def location_row(loc: dict, city_state: tuple[str | None, str | None],
                 convention: str, now: datetime) -> dict:
    last = utc_of(loc.get("datetimeLast"))
    coords = loc.get("coordinates") or {}
    return {
        "location_id": loc["id"],
        "location_name": loc["name"],
        "city": city_state[0],
        "state": city_state[1],
        "latitude": coords.get("latitude"),
        "longitude": coords.get("longitude"),
        "provider": (loc.get("provider") or {}).get("name"),
        "owner": (loc.get("owner") or {}).get("name"),
        "timezone": loc.get("timezone"),
        "is_monitor": loc.get("isMonitor"),
        "is_mobile": loc.get("isMobile"),
        "is_active": bool(last and now - last <= timedelta(days=ACTIVE_DAYS)),
        "gas_unit_convention": convention,
        "datetime_first": utc_of(loc.get("datetimeFirst")),
        "datetime_last": last,
        "updated_at": now,
    }


def sensor_rows(loc: dict, latest_by_sensor: dict[int, datetime], now: datetime) -> list[dict]:
    """One row per sensor; is_live marks the newest sensor per parameter (if active)."""
    newest: dict[str, tuple[datetime, int]] = {}
    for s in loc.get("sensors") or []:
        p, t = s["parameter"]["name"], latest_by_sensor.get(s["id"])
        if p in LIVE_PARAMS and t and now - t <= timedelta(days=ACTIVE_DAYS):
            if p not in newest or (t, s["id"]) > newest[p]:
                newest[p] = (t, s["id"])
    live_ids = {sid for _, sid in newest.values()}
    return [{
        "sensor_id": s["id"],
        "location_id": loc["id"],
        "parameter": s["parameter"]["name"],
        "source_units": s["parameter"].get("units"),
        "datetime_first": None,
        "datetime_last": latest_by_sensor.get(s["id"]),
        "is_live": s["id"] in live_ids,
        "updated_at": now,
    } for s in loc.get("sensors") or []]


def sync(engine, client: OpenAQClient, include_low_cost: bool, run: PipelineRun | None = None) -> dict:
    now = datetime.now(timezone.utc)
    locations = fetch_locations(client, include_low_cost)
    mapping = read_mapping()
    conventions = read_unit_conventions()

    latest_by_sensor: dict[int, datetime] = {}
    recent = [l for l in locations
              if (t := utc_of(l.get("datetimeLast"))) and now - t <= timedelta(days=LATEST_LOOKBACK_DAYS)]
    for l in recent:
        for r in client.get(f"/locations/{l['id']}/latest").get("results") or []:
            latest_by_sensor[r["sensorsId"]] = parse_ts(r["datetime"]["utc"])

    loc_rows = [location_row(l, mapping.get(l["id"], (None, None)),
                             conventions.get(l["id"], "unclassified"), now) for l in locations]
    sen_rows = [r for l in locations for r in sensor_rows(l, latest_by_sensor, now)]

    with engine.begin() as conn:
        upsert(conn, "dim_location", loc_rows, ["location_id"],
               coalesce=["datetime_first", "datetime_last"])
        # Sensors of locations we didn't query keep their stored datetime_last.
        # Clear is_live first so a sensor that died stops being selected.
        conn.execute(text("UPDATE dim_sensor SET is_live = FALSE WHERE is_live"))
        upsert(conn, "dim_sensor", sen_rows, ["sensor_id"],
               coalesce=["datetime_first", "datetime_last"])

    active = [r for r in loc_rows if r["is_active"]]
    unmapped_active = [r["location_id"] for r in active if not r["city"]]
    live_by_param: dict[str, int] = defaultdict(int)
    for r in sen_rows:
        if r["is_live"]:
            live_by_param[r["parameter"]] += 1
    summary = {
        "locations": len(loc_rows), "active": len(active), "inactive": len(loc_rows) - len(active),
        "queried_latest": len(recent), "sensors": len(sen_rows),
        "live_sensors_by_parameter": dict(live_by_param),
        "unmapped_active": unmapped_active, "requests": client.request_count,
    }
    if unmapped_active:
        log.warning("%d active locations have no city/state mapping: %s",
                    len(unmapped_active), unmapped_active)
    if run is not None:
        run.metrics.update(api_requests=client.request_count, rows_fetched=len(locations),
                           rows_valid=len(locations), rows_invalid=0,
                           rows_upserted=len(loc_rows) + len(sen_rows),
                           fresh_locations=len(active), stale_locations=len(loc_rows) - len(active))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync Indian locations and sensors from OpenAQ.")
    ap.add_argument("--per-minute", type=int, default=50,
                    help="request rate (lower it when another OpenAQ job runs at the same time)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    settings = get_settings()
    engine = get_engine()
    client = OpenAQClient(settings.require_openaq_key(), max_requests=REQUEST_BUDGET,
                          max_per_minute=args.per_minute)
    try:
        with PipelineRun(engine, "sync_locations") as run:
            summary = sync(engine, client, settings.include_low_cost, run)
    except Exception as exc:  # already logged to pipeline_log by PipelineRun
        log.error("sync_locations failed: %s", exc)
        return 1
    log.info("sync_locations done: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
