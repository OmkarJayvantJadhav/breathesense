"""Reference data and pure helpers used by init_db, sync_locations and classify_units."""

from datetime import date, datetime, timedelta, timezone

import pytest

from scripts.classify_units import classify
from scripts.init_db import date_rows, read_breakpoints, read_festivals, season_for
from src.sync_locations import location_row, sensor_rows

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- breakpoints / dates
def test_breakpoints_csv_is_well_formed():
    bp = read_breakpoints()
    assert len(bp) == 48
    by_param: dict[str, list] = {}
    for r in bp:
        by_param.setdefault(r["parameter"], []).append(r)
    assert set(by_param) == {"pm10", "pm25", "no2", "o3", "co", "so2", "nh3", "pb"}
    for p, rows in by_param.items():
        rows.sort(key=lambda r: r["aqi_low"])
        assert [r["aqi_low"] for r in rows] == [0, 51, 101, 201, 301, 401], p
        assert rows[-1]["conc_high"] is None, f"{p}: top band must be open-ended"
        assert all(r["conc_high"] is not None for r in rows[:-1]), p
        assert len({r["avg_hours"] for r in rows}) == 1, p
        for a, b in zip(rows, rows[1:]):
            assert a["conc_high"] < b["conc_low"] or a["conc_high"] == b["conc_low"], p
    assert by_param["o3"][0]["avg_hours"] == 8 and by_param["co"][0]["avg_hours"] == 8
    assert by_param["pm25"][0]["avg_hours"] == 24


@pytest.mark.parametrize("month, season", [
    (12, "Winter"), (1, "Winter"), (2, "Winter"), (3, "Summer"), (5, "Summer"),
    (6, "Monsoon"), (9, "Monsoon"), (10, "Post-monsoon"), (11, "Post-monsoon"),
])
def test_season(month, season):
    assert season_for(month) == season


def test_date_rows_marks_festivals():
    fest = read_festivals()
    rows = {r["date"]: r for r in date_rows(date(2024, 10, 30), date(2024, 11, 2), fest)}
    assert len(rows) == 4
    assert rows[date(2024, 10, 31)]["is_festival"] and rows[date(2024, 11, 1)]["is_festival"]
    assert not rows[date(2024, 10, 30)]["is_festival"]
    assert rows[date(2024, 11, 1)]["day_of_week"] == "Friday"


def test_festivals_cover_every_diwali_2018_2026():
    years = {d.year for d in read_festivals()}
    assert years == set(range(2018, 2027))


# ---------------------------------------------------------------- sensor selection
def loc(sensors, last=NOW - timedelta(hours=1)):
    return {"id": 17, "name": "R K Puram, Delhi - DPCC", "isMonitor": True, "isMobile": False,
            "datetimeLast": {"utc": last.isoformat()}, "sensors": sensors,
            "coordinates": {"latitude": 28.56, "longitude": 77.18},
            "provider": {"name": "CPCB"}, "owner": {"name": "DPCC"}, "timezone": "Asia/Kolkata"}


def sensor(sid, p, units="µg/m³"):
    return {"id": sid, "name": p, "parameter": {"name": p, "units": units}}


def test_legacy_sensor_is_not_live():
    # Real Phase 1 case: sensor 35 died in 2018, 12234787 is the current pm25 series.
    l = loc([sensor(35, "pm25"), sensor(12234787, "pm25")])
    latest = {35: datetime(2018, 2, 21, 21, 15, tzinfo=timezone.utc), 12234787: NOW - timedelta(hours=1)}
    live = {r["sensor_id"]: r["is_live"] for r in sensor_rows(l, latest, NOW)}
    assert live == {35: False, 12234787: True}


def test_one_live_sensor_per_parameter_even_if_both_fresh():
    l = loc([sensor(1, "pm25"), sensor(2, "pm25")])
    latest = {1: NOW - timedelta(hours=2), 2: NOW - timedelta(hours=1)}
    assert [r["sensor_id"] for r in sensor_rows(l, latest, NOW) if r["is_live"]] == [2]


def test_stale_sensor_is_not_live_and_unknown_params_ignored():
    l = loc([sensor(1, "pm25"), sensor(2, "temperature", "c")])
    latest = {1: NOW - timedelta(days=8), 2: NOW}
    assert not any(r["is_live"] for r in sensor_rows(l, latest, NOW))


def test_location_active_flag():
    assert location_row(loc([]), ("Delhi", "Delhi"), "ugm3", NOW)["is_active"] is True
    old = loc([], last=NOW - timedelta(days=8))
    assert location_row(old, ("Delhi", "Delhi"), "ugm3", NOW)["is_active"] is False
    assert location_row({**old, "datetimeLast": None}, (None, None), "unclassified", NOW)["is_active"] is False


# ---------------------------------------------------------------- unit classification
def series(no, no2, nox, hours=24):
    t0 = datetime(2026, 9, 23, 0, 30, tzinfo=timezone.utc)
    ts = [t0 + timedelta(hours=i) for i in range(hours)]
    return {"no": {t: no for t in ts}, "no2": {t: no2 for t in ts}, "nox": {t: nox for t in ts}}


def test_classify_ugm3_station():
    # Real R K Puram values: NO 2.1, NO2 26.6 (ug/m3), NOx 0.0159 (ppm)
    assert classify(series(2.1, 26.6, 0.0159))["convention"] == "ugm3"


def test_classify_ppb_station():
    # NO 15.16 + NO2 35.49 = 50.65 ppb = NOx 0.05065 ppm
    assert classify(series(15.16, 35.49, 0.05065))["convention"] == "ppb"


def test_classify_inconsistent_is_unclassified():
    assert classify(series(10, 10, 0.1))["convention"] == "unclassified"


def test_classify_needs_enough_hours():
    r = classify(series(2.1, 26.6, 0.0159, hours=5))
    assert r["convention"] == "unclassified" and r["n_hours"] == 5


def test_classify_skips_invalid_hours():
    s = series(2.1, 26.6, 0.0159)
    first = min(s["nox"])
    s["nox"][first] = 0
    assert classify(s)["n_hours"] == 23
