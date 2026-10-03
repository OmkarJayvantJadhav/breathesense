"""One-off seed: load the last 48 h of /hours for every active station,
so rolling 24 h AQI works immediately instead of after a day of hourly runs.

Run:  python -m scripts.seed_recent                       # 48 h ending now
      python -m scripts.seed_recent --end 2026-09-24T17:30:00Z
      python -m scripts.seed_recent --cohorts 4 5         # resume remaining cohorts

Cost: one request per live pollutant sensor (~2,700). Runs cohort by cohort at
30 requests/min (1,800/h, under OpenAQ's 2,000/h), about 90 minutes in total.
Each cohort is a separate pipeline_log run, so an interrupted seed resumes with
--cohorts. Idempotent: re-running upserts the same rows.

Afterwards it prints the checks: row counts, duplicate keys,
window completeness and sample station AQIs.
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict

from sqlalchemy import text

from src.aqi import station_aqi
from src.db import get_engine
from src.fetch_hourly import N_COHORTS
from src.main import parse_end, run
from src.units import POLLUTANTS

log = logging.getLogger("seed_recent")

SEED_HOURS = 48
PER_MINUTE = 30
COHORT_BUDGET = 800   # ~540 expected per cohort


def verify(engine) -> None:
    with engine.connect() as c:
        n, locs, t0, t1 = c.execute(text(
            "SELECT count(*), count(DISTINCT location_id), min(reading_time), max(reading_time) "
            "FROM fact_pollutant_hourly")).one()
        dups = c.execute(text(
            "SELECT count(*) FROM (SELECT location_id, reading_time FROM fact_pollutant_hourly "
            "GROUP BY 1, 2 HAVING count(*) > 1) d")).scalar_one()
        print(f"fact rows={n} stations={locs} range={t0} .. {t1} duplicate keys={dups}")

        # Window completeness at each station's newest hour
        rows = c.execute(text(f"""
            SELECT location_id, reading_time, {", ".join(POLLUTANTS)}
            FROM fact_pollutant_hourly
            WHERE reading_time > (SELECT max(reading_time) FROM fact_pollutant_hourly) - interval '30 hours'
            ORDER BY location_id, reading_time""")).mappings().all()
    hourly: dict[int, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        for p in POLLUTANTS:
            hourly[r["location_id"]][p][r["reading_time"]] = r[p]
    results = {lid: station_aqi(h, max(t for s in h.values() for t in s)) for lid, h in hourly.items()}
    valid = [r for r in results.values() if r.valid]
    reasons = defaultdict(int)
    for r in results.values():
        if not r.valid:
            reasons[r.reason] += 1
    print(f"stations with valid AQI: {len(valid)} / {len(results)}; invalid reasons: {dict(reasons)}")
    for lid, r in list(results.items())[:: max(1, len(results) // 8)][:8]:
        print(f"  location {lid}: AQI={r.aqi} {r.category} dominant={r.dominant_pollutant}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", help="window end (UTC ISO); default now")
    ap.add_argument("--cohorts", type=int, nargs="*", default=list(range(N_COHORTS)))
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if not args.verify_only:
        end = parse_end(args.end)
        for cohort in args.cohorts:
            run(job="seed_recent", cohort=cohort, end=end, window_hours=SEED_HOURS,
                per_minute=PER_MINUTE, budget=COHORT_BUDGET)
    verify(get_engine())


if __name__ == "__main__":
    main()
