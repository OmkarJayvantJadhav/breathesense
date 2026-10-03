"""Classify each station's real NO/NO2 unit from the NOx identity.

Run:  python -m scripts.classify_units            (resumable; re-run to continue)
      python -m scripts.classify_units --restart  (discard previous results)

Why: OpenAQ labels the current CPCB no/no2/so2/co series `ppb`, but Phase 1 showed
the true unit differs by station. CPCB publishes NOx in ppb; OpenAQ's `nox` value
behaves like ppm. For each hour where NO, NO2 and NOx are all present:

    ratio_if_ugm3 = NOx*1000 / (NO/1.2274 + NO2/1.8818)   (NO/NO2 really ug/m3)
    ratio_if_ppb  = NOx*1000 / (NO + NO2)                 (NO/NO2 really ppb)

A station is `ugm3` or `ppb` when the median ratio over >= 12 hours is within
5% of 1.0 for that hypothesis, else `unclassified` (its gases are then rejected).
The window is the 48 h ending at the station's last observation, so this also
works while the upstream feed is paused.

Output: data/unit_convention.csv (owner-reviewed, committed).
Requests: ~4 per station (1 latest + 3 hours). Throttled to 25/min, capped at
1,500 per run, so one run stays below OpenAQ's 2,000/hour limit.
"""

from __future__ import annotations

import argparse
import csv
import logging
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

from src.config import DATA_DIR, get_settings
from src.openaq_client import OpenAQClient, RequestBudgetExceeded
from src.sync_locations import fetch_locations, parse_ts, sensor_rows, utc_of

log = logging.getLogger("classify_units")

OUT_CSV = DATA_DIR / "unit_convention.csv"
FIELDS = ["location_id", "location_name", "convention", "n_hours",
          "median_ratio_ugm3", "median_ratio_ppb", "window_end_utc"]
PPB_PER_UGM3_NO = 30.01 / 24.45
PPB_PER_UGM3_NO2 = 46.01 / 24.45
WINDOW_HOURS = 48
MIN_HOURS = 12
TOLERANCE = 0.05
LOOKBACK_DAYS = 30


def classify(hourly: dict[str, dict[datetime, float]]) -> dict:
    """Pure: {'no': {t: v}, 'no2': {...}, 'nox': {...}} -> verdict + stats."""
    r_ug, r_ppb = [], []
    common = set(hourly.get("no", {})) & set(hourly.get("no2", {})) & set(hourly.get("nox", {}))
    for t in common:
        no, no2, nox = hourly["no"][t], hourly["no2"][t], hourly["nox"][t]
        if nox <= 0 or no < 0 or no2 < 0 or no + no2 <= 0:
            continue
        r_ug.append(nox * 1000 / (no / PPB_PER_UGM3_NO + no2 / PPB_PER_UGM3_NO2))
        r_ppb.append(nox * 1000 / (no + no2))
    n = len(r_ug)
    if n < MIN_HOURS:
        return {"convention": "unclassified", "n_hours": n,
                "median_ratio_ugm3": None, "median_ratio_ppb": None}
    m_ug, m_ppb = statistics.median(r_ug), statistics.median(r_ppb)
    if abs(m_ug - 1) <= TOLERANCE:
        conv = "ugm3"
    elif abs(m_ppb - 1) <= TOLERANCE:
        conv = "ppb"
    else:
        conv = "unclassified"
    return {"convention": conv, "n_hours": n,
            "median_ratio_ugm3": round(m_ug, 3), "median_ratio_ppb": round(m_ppb, 3)}


def load_existing() -> dict[int, dict]:
    if not OUT_CSV.exists():
        return {}
    with OUT_CSV.open(encoding="utf-8", newline="") as f:
        return {int(r["location_id"]): r for r in csv.DictReader(f)}


def save(rows: dict[int, dict]) -> None:
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for lid in sorted(rows):
            w.writerow({k: rows[lid].get(k) for k in FIELDS})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--max-requests", type=int, default=1500)
    ap.add_argument("--per-minute", type=int, default=25)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    settings = get_settings()
    client = OpenAQClient(settings.require_openaq_key(), max_requests=args.max_requests,
                          max_per_minute=args.per_minute)
    done = {} if args.restart else load_existing()
    now = datetime.now(timezone.utc)

    locations = fetch_locations(client, include_low_cost=False)
    candidates = [
        l for l in locations
        if (t := utc_of(l.get("datetimeLast"))) and now - t <= timedelta(days=LOOKBACK_DAYS)
        and {"no", "no2", "nox"} <= {s["parameter"]["name"] for s in l.get("sensors") or []}
        and l["id"] not in done
    ]
    log.info("%d candidate stations (%d already classified); est. %d requests",
             len(candidates), len(done), 4 * len(candidates))

    try:
        for i, l in enumerate(candidates, 1):
            latest = {r["sensorsId"]: parse_ts(r["datetime"]["utc"])
                      for r in client.get(f"/locations/{l['id']}/latest").get("results") or []}
            # Reuse the sync rule: live sensor = newest sensor-level timestamp per parameter.
            # Anchor "now" at the station's own last observation so a paused feed still works.
            end = utc_of(l["datetimeLast"])
            live = {r["parameter"]: r["sensor_id"]
                    for r in sensor_rows(l, latest, end) if r["is_live"]}
            if not {"no", "no2", "nox"} <= set(live):
                done[l["id"]] = {"location_id": l["id"], "location_name": l["name"],
                                 "convention": "unclassified", "n_hours": 0,
                                 "window_end_utc": end.isoformat()}
                continue
            hourly: dict[str, dict[datetime, float]] = {}
            for p in ("no", "no2", "nox"):
                body = client.get(f"/sensors/{live[p]}/hours", {
                    "datetime_from": (end - timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "datetime_to": (end + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "limit": 100})
                hourly[p] = {utc_of(r["period"]["datetimeFrom"]): r["value"]
                             for r in body.get("results") or [] if r.get("value") is not None}
            done[l["id"]] = {"location_id": l["id"], "location_name": l["name"],
                             **classify(hourly), "window_end_utc": end.isoformat()}
            if i % 20 == 0:
                save(done)
                log.info("progress %d/%d, requests %d", i, len(candidates), client.request_count)
    except RequestBudgetExceeded:
        log.warning("request budget reached; re-run to continue (results so far are saved)")
    finally:
        save(done)

    log.info("classified stations: %s; requests used: %d",
             dict(Counter(r["convention"] for r in done.values())), client.request_count)


if __name__ == "__main__":
    main()
