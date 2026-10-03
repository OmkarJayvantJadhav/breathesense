"""One-off repair: apply the gas plausibility caps (src.validation.GAS_MAX) to stored rows.

    python -m scripts.repair_gas_caps            # report only (no changes)
    python -m scripts.repair_gas_caps --apply    # repair in one transaction

Applied to Neon on 2026-10-03 (23 daily rows). The caps come from GAS_MAX so they can't drift,
and the connection comes from .env (DATABASE_URL) like the rest of the pipeline.
Nothing is deleted: hourly values over a cap become NULL and the row gets the quality_flag
token 'implausible_high_<param>'; daily gas means over a cap become NULL. --apply re-checks
inside the transaction and rolls back unless every count is 0.

Afterwards, recompute the affected city-days:
    python -m scripts.backfill_history --aggregate
    python -m src.rollup --date <day> --no-retention     # for each live day printed below
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from src.db import get_engine
from src.validation import GAS_MAX

PARAMS = list(GAS_MAX)


def _counts(conn) -> dict[str, dict[str, int]]:
    out = {}
    for table, prefix in (("fact_pollutant_hourly", ""), ("daily_city_summary", "avg_")):
        cols = ", ".join(f"count(*) FILTER (WHERE {prefix}{p} > {GAS_MAX[p]}) AS {p}" for p in PARAMS)
        row = conn.execute(text(f"SELECT {cols} FROM {table}")).mappings().one()
        out[table] = {p: int(row[p]) for p in PARAMS}
    return out


def _any_over(prefix: str) -> str:
    return " OR ".join(f"{prefix}{p} > {GAS_MAX[p]}" for p in PARAMS)


def _live_days(conn) -> list[str]:
    """IST days whose live rollup must be recomputed (hourly rows still within retention)."""
    rows = conn.execute(text(f"""
        SELECT DISTINCT (reading_time AT TIME ZONE 'Asia/Kolkata')::date AS d
        FROM fact_pollutant_hourly WHERE {_any_over('')}
        UNION
        SELECT DISTINCT summary_date FROM daily_city_summary
        WHERE source = 'live_rollup' AND ({_any_over('avg_')})
        ORDER BY 1""")).all()
    return [r[0].isoformat() for r in rows]


def _print(counts: dict[str, dict[str, int]]) -> None:
    print(f"{'table':<24}" + "".join(f"{p:>8}" for p in PARAMS))
    for table, c in counts.items():
        print(f"{table:<24}" + "".join(f"{c[p]:>8}" for p in PARAMS))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="repair (default: report only)")
    args = ap.parse_args()

    engine = get_engine()
    with engine.connect() as conn:
        before = _counts(conn)
        days = _live_days(conn)
    print("Values over the caps:", ", ".join(f"{p} {GAS_MAX[p]:g}" for p in PARAMS))
    _print(before)
    print("Live days to re-roll after repair:", ", ".join(days) or "none")
    if not args.apply:
        print("\nReport only. Re-run with --apply to repair.")
        return 0
    if not any(v for c in before.values() for v in c.values()):
        print("\nNothing to repair.")
        return 0

    set_hourly = ", ".join(f"{p} = CASE WHEN f.{p} > {GAS_MAX[p]} THEN NULL ELSE f.{p} END" for p in PARAMS)
    tokens = ", ".join(f"CASE WHEN f.{p} > {GAS_MAX[p]} THEN 'implausible_high_{p}' END" for p in PARAMS)
    set_daily = ", ".join(f"avg_{p} = CASE WHEN avg_{p} > {GAS_MAX[p]} THEN NULL ELSE avg_{p} END"
                          for p in PARAMS)
    with engine.begin() as conn:
        n_hourly = conn.execute(text(f"""
            UPDATE fact_pollutant_hourly f SET {set_hourly},
                quality_flag = (
                    SELECT string_agg(tok, ';' ORDER BY tok) FROM (
                        SELECT DISTINCT tok FROM unnest(
                            string_to_array(coalesce(f.quality_flag, ''), ';') || ARRAY[{tokens}]
                        ) AS t(tok) WHERE tok IS NOT NULL AND tok <> '') s)
            WHERE {_any_over('f.')}""")).rowcount
        n_daily = conn.execute(text(
            f"UPDATE daily_city_summary SET {set_daily} WHERE {_any_over('avg_')}")).rowcount
        after = _counts(conn)
        if any(v for c in after.values() for v in c.values()):
            _print(after)
            raise SystemExit("Counts not zero after repair - rolled back.")
    print(f"\nRepaired {n_hourly} hourly rows and {n_daily} daily rows (committed).")
    print("Next: python -m scripts.backfill_history --aggregate")
    for d in days:
        print(f"      python -m src.rollup --date {d} --no-retention")
    return 0


if __name__ == "__main__":
    sys.exit(main())
