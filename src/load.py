"""Load wide hourly rows into fact_pollutant_hourly, and compute freshness.

Idempotency: upsert on (location_id, reading_time).
  * Pollutant columns use COALESCE(new, existing): a later re-fetch where one
    sensor failed can't wipe a value stored by an earlier run.
  * valid_observation_count is recomputed in SQL from the merged row, so it
    always matches the stored columns.
  * Trade-off: a value can be corrected by a re-fetch, but not removed. OpenAQ
    doesn't retract hourly values in practice, and removals would be visible in
    the source anyway.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Engine, text

from src.db import upsert
from src.units import POLLUTANTS

TABLE = "fact_pollutant_hourly"
# A station counts as fresh if its newest stored hour started within this many
# hours: the 6 h cohort cycle plus 1 h of slack.
FRESH_HOURS = 7

_MERGED = ", ".join(f"COALESCE(EXCLUDED.{p}, {TABLE}.{p})" for p in POLLUTANTS)


def load_rows(engine_or_conn, rows: list[dict]) -> int:
    if not rows:
        return 0
    now = datetime.now(timezone.utc)
    payload = [{**r, "loaded_at": now} for r in rows]
    return upsert(
        engine_or_conn, TABLE, payload, ["location_id", "reading_time"],
        coalesce=[*POLLUTANTS, "quality_flag"],
        set_expr={"valid_observation_count": f"num_nonnulls({_MERGED})"},
    )


def freshness(engine: Engine, now: datetime | None = None) -> tuple[int, int]:
    """(fresh, stale) counts over active stations, based on stored hourly data."""
    now = now or datetime.now(timezone.utc)
    sql = """
        SELECT count(*) FILTER (WHERE f.last_hour >= :cutoff),
               count(*) FILTER (WHERE f.last_hour IS NULL OR f.last_hour < :cutoff)
        FROM dim_location l
        LEFT JOIN (SELECT location_id, max(reading_time) AS last_hour
                   FROM fact_pollutant_hourly GROUP BY location_id) f USING (location_id)
        WHERE l.is_active
    """
    with engine.connect() as conn:
        fresh, stale = conn.execute(text(sql), {"cutoff": now - timedelta(hours=FRESH_HOURS)}).one()
    return int(fresh), int(stale)
