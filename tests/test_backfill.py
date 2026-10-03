"""Backfill aggregation pure-function tests (no API, no database, no cache files)."""

import argparse
import types
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

import pytest

from scripts import backfill_history as bh
from src.units import MOLAR_VOLUME, MOLECULAR_WEIGHT

UG = "µg/m³"
D1, D2 = date(2022, 1, 5), date(2022, 1, 6)


def sensor(parameter="pm25", convention="ugm3", units=UG, sensor_id=1, location_id=10):
    return {"sensor_id": sensor_id, "location_id": location_id, "parameter": parameter,
            "source_units": units, "gas_unit_convention": convention}


def row(day, value, observed=24, units=UG, span=23.0):
    """One cached /days row; the cache is read back from CSV, so every field is a string.
    `span` = hours between the day's first and last observation (None: not recorded)."""
    first = datetime.fromisoformat(f"{day}T00:30:00+05:30")
    return {"ist_date": day, "value": str(value), "units": units,
            "observed": "" if observed is None else str(observed), "expected": "24",
            "first_obs": "" if span is None else first.isoformat(),
            "last_obs": "" if span is None else (first + timedelta(hours=span)).isoformat()}


@pytest.fixture
def cache(monkeypatch):
    """Serve fake cached /days rows per sensor id instead of reading data/backfill_cache."""
    store: dict[int, list[dict]] = {}
    monkeypatch.setattr(bh, "read_cache", lambda sid: store.get(sid, []))
    return store


def day_values(cache, t, rows, pm_max=1500.0, min_count=18):
    cache[t["sensor_id"]] = rows
    stats, report = Counter(), defaultdict(list)
    return bh.station_day_values(t, pm_max, min_count, stats, report), stats, report


# ------------------------------------------------------------ station_day_values
def test_day_needs_min_observed_count(cache):
    rows = [row("2022-01-01", 80, observed=18), row("2022-01-02", 80, observed=17),
            row("2022-01-03", 80, observed=None)]
    out, stats, _ = day_values(cache, sensor(), rows)
    assert out == {date(2022, 1, 1): (80.0, 18.0)}
    assert stats == Counter(valid_day=1, incomplete_day=2)


def test_observations_must_be_spread_over_the_day(cache):
    rows = [row("2022-01-01", 80, observed=40, span=10.0),   # 40 readings, all within 10 h of the day
            row("2022-01-02", 80, observed=18, span=16.0),   # threshold: 18 - 1 h, less 1 h timestamp slack
            row("2022-01-03", 80, observed=18, span=15.9)]
    out, stats, _ = day_values(cache, sensor(), rows)
    assert list(out) == [date(2022, 1, 2)]
    assert stats == Counter(valid_day=1, short_span_day=2)


def test_hourly_day_with_subhour_timestamp_offsets_is_not_rejected(cache):
    # Current series: timestamps sit at :00/:15/:30/:45, and 18 hourly readings were seen to span
    # only 16.25 h (e.g. 07:00 .. 23:15). A threshold of 17 h wrongly rejected such days.
    out, stats, _ = day_values(cache, sensor(), [row("2026-03-04", 80, observed=18, span=16.25)])
    assert list(out) == [date(2026, 3, 4)] and stats == Counter(valid_day=1)


def test_days_without_recorded_timestamps_are_counted_not_trusted(cache):
    out, stats, _ = day_values(cache, sensor(), [row("2022-01-01", 80, span=None)])
    assert out == {} and stats == Counter(unknown_span_day=1)


def test_invalid_values_are_counted_not_dropped_silently(cache):
    rows = [row("2022-01-01", -5), row("2022-01-02", 2000), row("2022-01-03", ""),
            row("2022-01-04", "nan"), row("2022-01-05", 80)]
    out, stats, _ = day_values(cache, sensor(), rows)
    assert list(out) == [date(2022, 1, 5)]
    # Empty and non-finite cache cells both read back as None, so they count as missing_value.
    assert stats == Counter(negative=1, implausible_high=1, missing_value=2, valid_day=1)


def test_legacy_co_labelled_ug_m3_becomes_mg_m3_even_at_unclassified_station(cache):
    # Legacy series carry a trustworthy ug/m3 label; the unclassified-station rejection
    # only applies to the unreliable `ppb` label of the current CPCB gas series.
    out, stats, _ = day_values(cache, sensor("co", "unclassified"), [row("2018-03-01", 710)])
    assert out[date(2018, 3, 1)][0] == pytest.approx(0.71)
    assert stats["valid_day"] == 1 and "unit_rejected" not in stats


@pytest.mark.parametrize("convention, expected", [
    ("ugm3", 40.0),                                              # label wrong, value already ug/m3
    ("ppb", 40.0 * MOLECULAR_WEIGHT["no2"] / MOLAR_VOLUME),      # really ppb -> ug/m3
    ("unclassified", None),                                      # never guess
])
def test_current_ppb_labelled_no2_follows_station_convention(cache, convention, expected):
    t = sensor("no2", convention, units="ppb")
    out, stats, _ = day_values(cache, t, [row("2026-03-01", 40, units="ppb")])
    if expected is None:
        assert out == {} and stats == Counter(unit_rejected=1)
    else:
        assert out[date(2026, 3, 1)][0] == pytest.approx(expected)


def test_unit_report_splits_eras_at_2023(cache):
    rows = [row("2022-12-31", 80), row("2023-01-01", 80)]
    _, _, report = day_values(cache, sensor(), rows)
    assert set(report) == {("pm25", "ug/m3", "ugm3", "legacy"), ("pm25", "ug/m3", "ugm3", "current")}


# ----------------------------------------------------------- collect_station_days
def test_better_covered_sensor_wins_and_ties_keep_the_first(cache):
    legacy, current = sensor(sensor_id=1), sensor(sensor_id=2)
    cache[1] = [row("2022-01-05", 100, observed=20), row("2022-01-06", 100, observed=24)]
    cache[2] = [row("2022-01-05", 120, observed=24), row("2022-01-06", 120, observed=24)]
    station = bh.collect_station_days([legacy, current], 1500.0, 18, Counter(), defaultdict(list))
    assert station[10]["pm25"] == {D1: (120.0, 24.0), D2: (100.0, 24.0)}


# --------------------------------------------------------------- build_city_rows
def day(**values):
    return {p: {D1: (v, 24.0)} for p, v in values.items()}


def test_city_row_definitions():
    # Station sub-indices (CPCB bands): A pm25 90 -> 200, pm10 100 -> 100, no2 40 -> 50 => AQI 200 (pm25)
    #                                   C pm25 30 -> 50,  pm10 40 -> 40,  no2 20 -> 25 => AQI 50 (pm25)
    # B reports only PM2.5 => no valid AQI, but still feeds avg_pm25.
    station = {1: day(pm25=90.0, pm10=100.0, no2=40.0), 2: day(pm25=30.0),
               3: day(pm25=30.0, pm10=40.0, no2=20.0)}
    [r] = bh.build_city_rows("Delhi", "Delhi", station)
    assert (r["city"], r["state"], r["summary_date"]) == ("Delhi", "Delhi", D1)
    assert r["avg_pm25"] == pytest.approx(50.0)       # (90 + 30 + 30) / 3
    assert r["avg_pm10"] == pytest.approx(70.0)       # (100 + 40) / 2
    assert r["avg_no2"] == pytest.approx(30.0)        # (40 + 20) / 2
    assert r["avg_so2"] is None and r["avg_co"] is None
    assert r["median_station_aqi"] == pytest.approx(125.0) and r["max_station_aqi"] == 200
    assert r["reporting_locations"] == 2 and r["dominant_pollutant"] == "pm25"
    # What /days cannot support is flagged or left empty, never invented.
    assert r["source"] == "backfill" and r["o3_co_approximated"] is True
    assert r["max_hourly_pm25"] is None and r["hours_poor_or_worse"] is None


def test_day_without_a_valid_station_aqi_keeps_averages_but_no_aqi():
    [r] = bh.build_city_rows("Pune", "Maharashtra", {1: day(pm25=60.0), 2: day(pm25=40.0, so2=10.0)})
    assert r["avg_pm25"] == pytest.approx(50.0)
    assert r["median_station_aqi"] is None and r["max_station_aqi"] is None
    assert r["dominant_pollutant"] is None and r["reporting_locations"] == 0


def test_rows_cover_every_reported_day_in_order():
    station = {1: {"pm25": {D2: (60.0, 24.0), D1: (40.0, 24.0)}}}
    assert [r["summary_date"] for r in bh.build_city_rows("Pune", "Maharashtra", station)] == [D1, D2]


def test_no_data_means_no_rows():
    assert bh.build_city_rows("Pune", "Maharashtra", {}) == []


# ----------------------------------------------------------------- parallel workers
def todo_targets():
    return ([{"sensor_id": i, "status": None, "cached": False} for i in range(1, 11)]
            + [{"sensor_id": 11, "status": "done", "cached": True},
               {"sensor_id": 12, "status": "empty", "cached": True},
               {"sensor_id": 13, "status": "error", "cached": False},
               {"sensor_id": 14, "status": "done", "cached": False}])


def test_shards_split_the_todo_list_without_overlap_or_gaps():
    targets = todo_targets()
    ids = [t["sensor_id"] for i in range(3) for t in bh.select_todo(targets, (i, 3))]
    assert len(ids) == len(set(ids))                                           # nobody fetched twice
    assert sorted(ids) == sorted(t["sensor_id"] for t in bh.select_todo(targets))   # nobody missed


def test_done_and_empty_are_skipped_but_errors_and_old_cache_formats_are_fetched():
    ids = {t["sensor_id"] for t in bh.select_todo(todo_targets())}
    assert {11, 12}.isdisjoint(ids)
    assert {13, 14} <= ids      # 14: marked done by an older version, no current-format cache file


def test_only_selects_current_era_or_legacy_sensors():
    targets = [{"sensor_id": 1, "status": None, "cached": False, "is_live": True},
               {"sensor_id": 2, "status": None, "cached": False, "is_live": False},
               {"sensor_id": 3, "status": "done", "cached": True, "is_live": True}]
    ids = lambda only: [t["sensor_id"] for t in bh.select_todo(targets, only=only)]
    assert ids("live") == [1] and ids("legacy") == [2] and ids(None) == [1, 2]


@pytest.mark.parametrize("spec, expected", [("0/1", (0, 1)), ("1/2", (1, 2)), ("2/3", (2, 3))])
def test_parse_shard(spec, expected):
    assert bh.parse_shard(spec) == expected


@pytest.mark.parametrize("spec", ["2/2", "-1/2", "a/b", "1", "0/0", "1/2/3"])
def test_parse_shard_rejects_bad_specs(spec):
    with pytest.raises(argparse.ArgumentTypeError):
        bh.parse_shard(spec)


@pytest.mark.parametrize("per_minute, workers, allowed", [
    (25, 1, True), (15, 2, True), (11, 3, True), (35, 1, True),   # combined <= 35/min
    (18, 2, False), (25, 2, False), (0, 1, False),
])
def test_combined_throttle_stays_inside_the_openaq_budget(per_minute, workers, allowed):
    if allowed:
        bh.check_rate_budget(per_minute, workers)
    else:
        with pytest.raises(ValueError):
            bh.check_rate_budget(per_minute, workers)


# ------------------------------------------------------------------ keep awake
class FakeKernel32:
    def __init__(self):
        self.calls = []

    def SetThreadExecutionState(self, flags):
        self.calls.append(flags)


@pytest.fixture
def power(monkeypatch):
    """Patch the module's own ctypes/sys references (not the real ones) and return the fake API."""
    k32 = FakeKernel32()
    monkeypatch.setattr(bh, "ctypes", types.SimpleNamespace(windll=types.SimpleNamespace(kernel32=k32)))
    monkeypatch.setattr(bh, "sys", types.SimpleNamespace(platform="win32"))
    return k32


def test_keep_awake_requests_then_releases_system_required(power):
    with bh.keep_awake():
        assert power.calls == [bh.ES_CONTINUOUS | bh.ES_SYSTEM_REQUIRED]
    assert power.calls == [bh.ES_CONTINUOUS | bh.ES_SYSTEM_REQUIRED, bh.ES_CONTINUOUS]


def test_keep_awake_releases_even_when_the_fetch_fails(power):
    with pytest.raises(RuntimeError):
        with bh.keep_awake():
            raise RuntimeError("boom")
    assert power.calls[-1] == bh.ES_CONTINUOUS


def test_keep_awake_does_nothing_off_windows(power, monkeypatch):
    monkeypatch.setattr(bh, "sys", types.SimpleNamespace(platform="linux"))
    with bh.keep_awake():
        pass
    assert power.calls == []


# ------------------------------------------------------- hourly_etl pause window
@pytest.mark.parametrize("hh_mm_ss, expected_sleep", [
    ("06:13:59", None),          # just before the window
    ("06:14:00", 21 * 60),       # window opens: sleep until :35
    ("06:20:30", 14 * 60 + 30),
    ("06:34:59", 1),
    ("06:35:00", None),          # window closed
])
def test_backfill_sleeps_through_the_hourly_etl_window(monkeypatch, hh_mm_ss, expected_sleep):
    h, m, s = map(int, hh_mm_ss.split(":"))
    fixed = datetime(2026, 10, 2, h, m, s, tzinfo=timezone.utc)

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    slept = []
    monkeypatch.setattr(bh, "datetime", FakeDatetime)
    monkeypatch.setattr(bh.time, "sleep", slept.append)
    bh._wait_outside_hourly_window()
    assert slept == ([] if expected_sleep is None else [expected_sleep])


# ---------------------------------------------------- gas units at ppb-classified stations
def test_legacy_no2_at_ppb_station_is_converted_despite_its_ug_m3_label(cache):
    t = sensor("no2", "ppb")                                  # legacy row labelled ug/m3
    out, stats, _ = day_values(cache, t, [row("2019-01-05", 40)])
    assert out[date(2019, 1, 5)][0] == pytest.approx(40 * MOLECULAR_WEIGHT["no2"] / MOLAR_VOLUME)


@pytest.mark.parametrize("parameter", ["so2", "co"])
@pytest.mark.parametrize("units, day", [(UG, "2019-01-05"), ("ppb", "2026-03-01")])
def test_so2_and_co_at_ppb_stations_are_excluded_in_both_eras(cache, parameter, units, day):
    out, stats, _ = day_values(cache, sensor(parameter, "ppb", units=units), [row(day, 10, units=units)])
    assert out == {} and stats == Counter(unit_rejected=1)


def test_ugm3_and_unclassified_stations_are_unaffected(cache):
    for conv in ("ugm3", "unclassified"):
        out, _, _ = day_values(cache, sensor("so2", conv), [row("2019-01-05", 10)])
        assert out[date(2019, 1, 5)][0] == 10.0
