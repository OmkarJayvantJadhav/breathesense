"""Database access helpers (SQLAlchemy 2.x over psycopg2).

Every job gets its engine from `get_engine()`, so connection settings live in one
place. `pool_pre_ping` matters on Neon: the compute scales to zero when idle, and
a pooled connection may be dead by the time it's reused.

`upsert()` is the single idempotent write path: INSERT ...
ON CONFLICT (pk) DO UPDATE, so re-running any load never creates duplicates.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg2.extras import execute_values
from sqlalchemy import Engine, create_engine, text

from src.config import get_settings

log = logging.getLogger(__name__)

UPSERT_CHUNK = 1000


def get_engine(url: str | None = None) -> Engine:
    url = url or get_settings().require_database_url()
    # psycopg2 driver; Neon URLs carry sslmode=require.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=0)


def run_sql_file(engine: Engine, path: Path) -> None:
    """Execute a multi-statement SQL file in one transaction.

    Uses the raw cursor with no parameters, so a literal % in the SQL (e.g.
    LIKE '%x%') isn't mistaken for a psycopg2 placeholder.
    """
    sql = path.read_text(encoding="utf-8")
    with engine.begin() as conn:
        cur = conn.connection.dbapi_connection.cursor()
        try:
            cur.execute(sql)
        finally:
            cur.close()


def upsert(
    engine_or_conn,
    table: str,
    rows: Sequence[Mapping[str, Any]],
    key: Sequence[str],
    *,
    update: Iterable[str] | None = None,
    coalesce: Iterable[str] = (),
    set_expr: Mapping[str, str] | None = None,
    where: str | None = None,
) -> int:
    """INSERT rows; on key conflict update the non-key columns.

    `update`   columns to overwrite on conflict (default: all non-key columns).
               Pass an empty list for ON CONFLICT DO NOTHING.
    `coalesce` columns where a NULL in the new row must not wipe an existing value.
    `set_expr` column -> SQL expression used on conflict instead (may reference
               EXCLUDED.col and <table>.col), e.g. a recomputed count.
    `where`    condition for the DO UPDATE (e.g. only overwrite backfill rows);
               conflicting rows that fail it are left untouched.
    Returns the number of rows sent.
    """
    if not rows:
        return 0
    cols = list(rows[0].keys())
    update = [c for c in cols if c not in key] if update is None else list(update)
    coalesce = set(coalesce)
    set_expr = dict(set_expr or {})
    col_list = ", ".join(cols)
    if update:
        sets = ", ".join(
            f"{c} = {set_expr[c]}" if c in set_expr
            else f"{c} = COALESCE(EXCLUDED.{c}, {table}.{c})" if c in coalesce
            else f"{c} = EXCLUDED.{c}"
            for c in update
        )
        conflict = f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {sets}"
        if where:
            conflict += f" WHERE {where}"
    else:
        conflict = f"ON CONFLICT ({', '.join(key)}) DO NOTHING"
    # execute_values sends one multi-row INSERT per chunk. Row-by-row executemany
    # costs one network round trip per row (~200 ms to Neon Singapore).
    stmt = f"INSERT INTO {table} ({col_list}) VALUES %s {conflict}"
    tuples = [tuple(r[c] for c in cols) for r in rows]

    def _run(conn):
        cur = conn.connection.dbapi_connection.cursor()
        try:
            execute_values(cur, stmt, tuples, page_size=UPSERT_CHUNK)
        finally:
            cur.close()

    if isinstance(engine_or_conn, Engine):
        with engine_or_conn.begin() as conn:
            _run(conn)
    else:
        _run(engine_or_conn)
    return len(rows)


def database_size_mb(engine: Engine) -> float:
    with engine.connect() as conn:
        size = conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
    return round(size / 1024 / 1024, 2)


class PipelineRun:
    """Context manager that writes one pipeline_log row per job run.

    On success status='success'; on any exception status='failed' with a short
    error message, and the exception is re-raised so the process exits non-zero
.
    """

    def __init__(self, engine: Engine, job: str):
        self.engine = engine
        self.job = job
        self.metrics: dict[str, Any] = {}
        self.run_id: int | None = None

    def __enter__(self) -> "PipelineRun":
        with self.engine.begin() as conn:
            self.run_id = conn.execute(
                text("INSERT INTO pipeline_log (job, started_at, status) "
                     "VALUES (:job, :t, 'running') RETURNING run_id"),
                {"job": self.job, "t": datetime.now(timezone.utc)},
            ).scalar_one()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        fields = {k: self.metrics.get(k) for k in (
            "api_requests", "rows_fetched", "rows_valid", "rows_invalid",
            "rows_upserted", "fresh_locations", "stale_locations", "duplicates")}
        fields["details"] = json.dumps(self.metrics.get("details") or {}, default=str)
        status = "failed" if exc else self.metrics.get("status", "success")
        err = f"{exc_type.__name__}: {str(exc)[:500]}" if exc else self.metrics.get("error_message")
        with self.engine.begin() as conn:
            conn.execute(
                text("UPDATE pipeline_log SET finished_at = :t, status = :status, "
                     "error_message = :err, "
                     + ", ".join(f"{k} = CAST(:{k} AS jsonb)" if k == "details" else f"{k} = :{k}"
                                 for k in fields)
                     + " WHERE run_id = :run_id"),
                {"t": datetime.now(timezone.utc), "status": status, "err": err,
                 "run_id": self.run_id, **fields},
            )
        return False  # never swallow the exception
