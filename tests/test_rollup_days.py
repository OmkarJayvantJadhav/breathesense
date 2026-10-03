"""Daily /days rollup pure-function tests (no API, no database)."""

import json
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from src import days_rollup as dr
from src import rollup_days as rd

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "days_current_12234787.json").read_text(encoding="utf-8"))
DAY = date(2026, 9, 20)


def result_for(day: str, value: float, observed: int = 24, units: str = "µg/m³", span_h: float = 23.5):
    """A /days result shaped like the real fixture, with the value and coverage changed."""
    r = json.loads(json.dumps(FIXTURE["results"][0]))
    r["period"]["datetimeFrom"]["local"] = f"{day}T00:00:00+05:30"
    r["value"], r["parameter"]["units"] = value, units
    cov = r["coverage"]
    cov["observedCount"] = observed
    cov["datetimeFrom"]["local"] = f"{day}T00:15:00+05:30"
    end = 0.25 + span_h
    cov["datetimeTo"]["local"] = f"{day}T{int(end):02d}:{int(round((end % 1) * 60)):02d}:00+05:30"
    return r


def sensor(sensor_id, parameter, convention="ugm3", units="µg/m³", city="Delhi", state="Delhi", location_id=1):
    return {"sensor_id": sensor_id, "location_id": location_id, "parameter": parameter, "source_units": units,
            "gas_unit_convention": convention, "city": city, "state": state}


# ---------------------------------------------------------------- real payload
def test_days_row_from_a_real_payload():
    row = dr.days_row(FIXTURE["results"][0])
    assert row["ist_date"] == "2026-09-20" and row["value"] == 89.4 and row["units"] == "µg/m³"
    assert (row["observed"], row["expected"]) == (24, 24)
    assert row["first_obs"].startswith("2026-09-20T00:15") and row["last_obs"].startswith("2026-09-21T00:00")


def test_days_row_has_exactly_the_cache_columns():
    from scripts.backfill_history import CACHE_COLUMNS
    assert list(dr.days_row(FIXTURE["results"][0])) == CACHE_COLUMNS


def test_a_real_day_passes_the_completeness_rule():
    rows = rd.day_rows_for(FIXTURE["results"], DAY)
    out = dr.day_values(sensor(1, "pm25"), rows, 1500.0, 18, Counter(), {})
    assert out == {DAY: (89.4, 24.0)}


def test_only_the_requested_day_is_kept():
    assert [r["ist_date"] for r in rd.day_rows_for(FIXTURE["results"], date(2026, 9, 21))] == ["2026-09-21"]
    assert rd.day_rows_for(FIXTURE["results"], date(2030, 1, 1)) == []


# ------------------------------------------------------------------- city rows
def station_rows(day=DAY, pm25=90.0, pm10=100.0, no2=40.0, **kw):
    """Sensors and fetched rows for one station: PM2.5 90 -> AQI 200, PM10 100 -> 100, NO2 40 -> 50."""
    sensors = [sensor(1, "pm25", **kw), sensor(2, "pm10", **kw), sensor(3, "no2", **kw)]
    fetched = {1: [dr.days_row(result_for(day.isoformat(), pm25))], 2: [dr.days_row(result_for(day.isoformat(), pm10))],
               3: [dr.days_row(result_for(day.isoformat(), no2))]}
    return sensors, fetched


def test_city_row_for_the_day():
    sensors, fetched = station_rows()
    [row] = rd.build_rows(sensors, fetched, DAY, 1500.0, 18, Counter())
    assert (row["city"], row["state"], row["summary_date"]) == ("Delhi", "Delhi", DAY)
    assert row["avg_pm25"] == 90.0 and row["median_station_aqi"] == 200 and row["reporting_locations"] == 1
    assert row["source"] == "backfill" and row["o3_co_approximated"] is True
    assert row["max_hourly_pm25"] is None and row["hours_poor_or_worse"] is None


def test_incomplete_day_gives_no_row_and_is_counted():
    sensors, fetched = station_rows()
    fetched[1] = [dr.days_row(result_for("2026-09-20", 90.0, observed=12))]
    fetched[2] = [dr.days_row(result_for("2026-09-20", 100.0, observed=12))]
    fetched[3] = [dr.days_row(result_for("2026-09-20", 40.0, observed=12))]
    stats = Counter()
    assert rd.build_rows(sensors, fetched, DAY, 1500.0, 18, stats) == []
    assert stats["incomplete_day"] == 3


def test_so2_at_a_ppb_station_is_excluded_but_no2_is_converted():
    sensors, fetched = station_rows(convention="ppb", units="ppb")
    sensors.append(sensor(4, "so2", convention="ppb", units="ppb"))
    fetched[4] = [dr.days_row(result_for("2026-09-20", 9.0, units="ppb"))]
    fetched[3] = [dr.days_row(result_for("2026-09-20", 40.0, units="ppb"))]
    stats = Counter()
    [row] = rd.build_rows(sensors, fetched, DAY, 1500.0, 18, stats)
    assert row["avg_so2"] is None and stats["unit_rejected"] == 1
    assert row["avg_no2"] == pytest.approx(40.0 * 46.01 / 24.45, abs=1e-3)


def test_cities_with_the_same_name_in_different_states_stay_separate():
    a, fa = station_rows(city="Aurangabad", state="Maharashtra")
    b, fb = station_rows(city="Aurangabad", state="Bihar", pm25=30.0)
    for t in b:
        t["sensor_id"] += 10
        t["location_id"] = 2
    fb = {k + 10: v for k, v in fb.items()}
    rows = rd.build_rows(a + b, {**fa, **fb}, DAY, 1500.0, 18, Counter())
    assert {(r["city"], r["state"]) for r in rows} == {("Aurangabad", "Maharashtra"), ("Aurangabad", "Bihar")}


def test_sensors_that_failed_to_fetch_are_left_out():
    sensors, fetched = station_rows()
    del fetched[1], fetched[2], fetched[3]
    assert rd.build_rows(sensors, fetched, DAY, 1500.0, 18, Counter()) == []


# ------------------------------------------------------------------ failure rule
def test_error_share_limit():
    errors = {i: "boom" for i in range(5)}
    rd.check_error_share(errors, 100)            # 5% is allowed
    with pytest.raises(RuntimeError):
        rd.check_error_share({i: "boom" for i in range(6)}, 100)
