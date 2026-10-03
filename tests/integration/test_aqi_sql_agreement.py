"""Python vs SQL AQI agreement. Runs only with TEST_DATABASE_URL.

    pytest -v -m integration

Both checks are read-only (the schema/views are re-applied idempotently first):
  1. fn_sub_index (SQL) == aqi.sub_index (Python) on a dense concentration grid
     covering every band edge, gap values, .5 rounding cases and the top-band cap.
  2. For every station in vw_location_aqi_now: AQI, dominant pollutant and
     validity equal aqi.station_aqi() computed in Python from the same hourly rows.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

import pytest
from sqlalchemy import text

from src.aqi import default_breakpoints, station_aqi, sub_index
from src.config import PROJECT_ROOT, get_settings
from src.db import get_engine, run_sql_file
from src.units import POLLUTANTS

pytestmark = pytest.mark.integration

URL = get_settings().test_database_url
if not URL:
    pytest.skip("TEST_DATABASE_URL not set", allow_module_level=True)


@pytest.fixture(scope="module")
def engine():
    e = get_engine(URL)
    run_sql_file(e, PROJECT_ROOT / "sql" / "01_schema.sql")
    run_sql_file(e, PROJECT_ROOT / "sql" / "02_views.sql")
    return e


def concentration_grid(param: str) -> list[float]:
    bands = default_breakpoints().bands[param]
    step = 0.05 if param in ("co", "pb") else 0.5
    top = bands[-1].conc_low * 3
    grid = {round(i * step, 4) for i in range(int(top / step) + 1)}
    for b in bands:  # edges and just around them
        for x in (b.conc_low, b.conc_high):
            if x is not None:
                grid |= {x, round(x - 0.04, 4), round(x + 0.04, 4), round(x - 0.05, 4), round(x + 0.05, 4)}
    return sorted(g for g in grid if g >= 0)


@pytest.mark.parametrize("param", sorted(default_breakpoints().bands))
def test_sub_index_function_agrees(engine, param):
    grid = concentration_grid(param)
    with engine.connect() as c:
        rows = c.execute(text("SELECT x, fn_sub_index(:p, x) FROM unnest(CAST(:xs AS numeric[])) x"),
                         {"p": param, "xs": [Decimal(str(x)) for x in grid]}).all()
    mismatches = []
    for x, sql_val in rows:
        py = sub_index(param, float(x))
        if abs(float(sql_val) - py) > 1e-6:
            mismatches.append((float(x), py, float(sql_val)))
    assert not mismatches, f"{param}: {len(mismatches)} mismatches, e.g. {mismatches[:5]}"
    assert len(rows) == len(grid) > 50


def test_station_view_agrees_with_python(engine):
    with engine.connect() as c:
        settings = dict(c.execute(text("SELECT key, value FROM ref_settings")).all())
        view = {r["Location ID"]: r for r in c.execute(text(
            'SELECT "Location ID", last_observed_utc, "BreatheSense AQI", "Dominant Pollutant", '
            '"AQI Invalid Reason" FROM vw_location_aqi_now')).mappings()}
        facts = c.execute(text(f"""
            SELECT f.location_id, f.reading_time, {", ".join(POLLUTANTS)}
            FROM fact_pollutant_hourly f
            JOIN (SELECT location_id, max(reading_time) AS as_of FROM fact_pollutant_hourly
                  GROUP BY location_id) l USING (location_id)
            WHERE f.reading_time > l.as_of - interval '24 hours'""")).mappings().all()
    assert view, "vw_location_aqi_now is empty; seed data first"

    hourly: dict[int, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))
    for r in facts:
        for p in POLLUTANTS:
            if r[p] is not None:
                hourly[r["location_id"]][p][r["reading_time"]] = float(r[p])

    mismatches = []
    for lid, v in view.items():
        py = station_aqi(hourly[lid], v["last_observed_utc"],
                         int(settings["min_hours_24h"]), int(settings["min_hours_8h"]))
        sql = (v["BreatheSense AQI"], v["Dominant Pollutant"] if v["BreatheSense AQI"] is not None else None,
               v["AQI Invalid Reason"])
        mine = (py.aqi, py.dominant_pollutant, py.reason)
        if sql != mine:
            mismatches.append((lid, sql, mine))
    assert not mismatches, f"{len(mismatches)}/{len(view)} stations differ, e.g. {mismatches[:5]}"
