"""Transform tests on real /hours payloads (tests/fixtures)."""

import random
from datetime import datetime, timezone

import pytest

from src.aqi import station_aqi
from src.transform import Observation, parse_hours, transform
from tests.conftest import observations_from

T_1630 = datetime(2026, 9, 24, 16, 30, tzinfo=timezone.utc)   # 22:00 IST


def by_time(rows):
    return {r["reading_time"]: r for r in rows}


def test_reading_time_is_period_start_unrounded(rk_puram):
    rows, _ = transform(observations_from(rk_puram))
    assert all(r["reading_time"].minute == 30 for r in rows)      # HH:30Z = whole IST hours
    assert all(r["reading_time"].tzinfo is not None for r in rows)


def test_ugm3_station_values_pass_through(rk_puram):
    # Real R K Puram hour 16:30-17:30Z (seen in Phase 1 probes): pm25 28.5, no2 25.4, co 0.823
    rows, stats = transform(observations_from(rk_puram))
    r = by_time(rows)[T_1630]
    assert (r["pm25"], r["no2"], r["co"]) == (28.5, 25.4, 0.823)
    assert stats.invalid == 0 and stats.duplicates == 0
    assert r["valid_observation_count"] == 6
    assert r["quality_flag"] == "gas_units_inferred"               # SO2/CO unit from NO2 test


def test_ppb_station_no2_is_converted_and_so2_co_are_rejected(jalandhar):
    obs = observations_from(jalandhar)
    raw = {(o.parameter, o.reading_time): o.value for o in obs}
    rows, _ = transform(obs)
    for r in rows:
        t = r["reading_time"]
        if r["no2"] is not None:
            assert r["no2"] == pytest.approx(raw[("no2", t)] * 46.01 / 24.45, abs=1e-3)
        assert r["so2"] is None and r["co"] is None             # unit unverified at ppb stations
        assert r["pm25"] == raw.get(("pm25", t))                 # PM untouched
    n_rejected = sum(1 for o in obs if o.parameter in ("so2", "co"))
    assert n_rejected > 0 and transform(obs)[1].invalid_by_reason["unit_rejected"] == n_rejected


def test_unclassified_station_gases_rejected_and_counted(vikas_sadan):
    obs = observations_from(vikas_sadan)
    n_gas = sum(1 for o in obs if o.parameter in ("no2", "so2", "co"))
    rows, stats = transform(obs)
    assert stats.invalid_by_reason["unit_rejected"] == n_gas and n_gas > 0
    assert all(r["no2"] is None and r["so2"] is None and r["co"] is None for r in rows)
    assert any(r["pm25"] is not None and r["o3"] is not None for r in rows)
    assert stats.valid + stats.invalid == stats.fetched


def test_deterministic_regardless_of_input_order(jalandhar):
    obs = observations_from(jalandhar)
    shuffled = obs[:]
    random.Random(42).shuffle(shuffled)
    assert transform(obs)[0] == transform(shuffled)[0]


def test_duplicates_are_counted_and_removed(rk_puram):
    obs = observations_from(rk_puram)
    rows_once, _ = transform(obs)
    rows_twice, stats = transform(obs + obs)
    assert rows_twice == rows_once
    assert stats.duplicates == len(obs)


def test_duplicate_keeps_best_covered_copy():
    t = T_1630
    a = Observation(1, 10, "pm25", t, 50.0, "µg/m³", "ugm3", observed_count=1, expected_count=4)
    b = Observation(1, 10, "pm25", t, 40.0, "µg/m³", "ugm3", observed_count=4, expected_count=4)
    for order in ([a, b], [b, a]):
        rows, stats = transform(order)
        assert rows[0]["pm25"] == 40.0 and stats.duplicates == 1
        assert rows[0]["quality_flag"] is None


def test_low_coverage_is_flagged_not_dropped():
    o = Observation(1, 10, "pm25", T_1630, 50.0, "µg/m³", "ugm3", observed_count=2, expected_count=4)
    rows, stats = transform([o])
    assert rows[0]["pm25"] == 50.0 and rows[0]["quality_flag"] == "low_coverage"
    assert stats.low_coverage == 1


def test_invalid_values_counted_by_reason_and_all_invalid_hour_has_no_row():
    t = T_1630
    obs = [
        Observation(1, 10, "pm25", t, -3.0, "µg/m³", "ugm3"),
        Observation(1, 11, "pm10", t, 2000.0, "µg/m³", "ugm3"),
        Observation(1, 12, "no2", t, None, "ppb", "ugm3"),
        Observation(1, 13, "temperature", t, 30.0, "c", "ugm3"),    # not a pollutant: ignored
    ]
    rows, stats = transform(obs)
    assert rows == []
    assert dict(stats.invalid_by_reason) == {"negative": 1, "implausible_high": 1, "missing_value": 1}


def test_unit_label_conflict_is_rejected():
    results = [{"value": 10.0, "parameter": {"units": "ppm"},
                "period": {"datetimeFrom": {"utc": "2026-09-24T16:30:00Z"}},
                "coverage": {"observedCount": 4, "expectedCount": 4}}]
    obs = parse_hours(results, location_id=1, sensor_id=1, parameter="pm25",
                      unit_label="µg/m³", convention="ugm3")
    rows, stats = transform(obs)
    assert rows == [] and stats.invalid_by_reason["unknown_unit"] == 1


def test_fixture_gives_valid_station_aqi(rk_puram):
    rows, _ = transform(observations_from(rk_puram))
    hourly = {p: {r["reading_time"]: r[p] for r in rows} for p in ("pm25", "pm10", "no2", "so2", "co", "o3")}
    end = max(r["reading_time"] for r in rows)
    result = station_aqi(hourly, end)
    assert result.valid and 0 <= result.aqi <= 500 and result.category is not None
