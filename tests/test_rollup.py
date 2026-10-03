"""Rollup pure-function tests (no database)."""

from datetime import date, datetime, timedelta, timezone

import pytest

from src.rollup import build_city_rows, day_aqi, ist_day_bounds, most_frequent

DAY = date(2026, 9, 23)
START, END = ist_day_bounds(DAY)


def day_hours(value_fn, start=START, n=24):
    return {start + timedelta(hours=i): value_fn(i) for i in range(n)}


def test_ist_day_bounds_align_with_cpcb_hours():
    assert START == datetime(2026, 9, 22, 18, 30, tzinfo=timezone.utc)
    assert END == datetime(2026, 9, 23, 18, 30, tzinfo=timezone.utc)


def test_day_aqi_24h_mean_and_best_8h_window():
    hourly = {
        "pm25": day_hours(lambda i: 45.0),                    # mean 45 -> 74.66
        "pm10": day_hours(lambda i: 80.0),                    # 80
        "o3": day_hours(lambda i: 150.0 if 10 <= i < 18 else 20.0),   # best 8 h window = 150 -> 173.4
    }
    r = day_aqi(hourly, START, END, 18, 6)
    assert r.valid and r.dominant_pollutant == "o3" and r.aqi == 173


def test_day_aqi_ignores_hours_outside_the_day():
    hourly = {"pm25": {**day_hours(lambda i: 45.0), START - timedelta(hours=1): 900.0},
              "pm10": day_hours(lambda i: 80.0), "no2": day_hours(lambda i: 30.0)}
    assert day_aqi(hourly, START, END, 18, 6).aqi == 80


def test_day_aqi_incomplete_day_is_invalid():
    hourly = {"pm25": day_hours(lambda i: 45.0, n=12), "pm10": day_hours(lambda i: 80.0, n=12),
              "no2": day_hours(lambda i: 30.0, n=12)}
    assert not day_aqi(hourly, START, END, 18, 6).valid


def test_most_frequent_tie_break():
    assert most_frequent(["o3", "pm10", "o3", "pm10"]) == "pm10"
    assert most_frequent([]) is None


def station(pm25, pm10=80.0, no2=30.0):
    return {"pm25": day_hours(lambda i: pm25), "pm10": day_hours(lambda i: pm10),
            "no2": day_hours(lambda i: no2)}


def test_city_rows_median_max_and_city_state_key():
    stations = {1: ("Aurangabad", "Maharashtra"), 2: ("Aurangabad", "Maharashtra"),
                3: ("Aurangabad", "Bihar")}
    hourly = {1: station(45.0), 2: station(250.0), 3: station(20.0)}
    rows = {(r["city"], r["state"]): r for r in build_city_rows(stations, hourly, DAY, 18, 6)}
    mh = rows[("Aurangabad", "Maharashtra")]
    assert mh["reporting_locations"] == 2 and mh["max_station_aqi"] == 400
    assert mh["median_station_aqi"] == pytest.approx((80 + 400) / 2)
    assert mh["avg_pm25"] == pytest.approx(147.5) and mh["max_hourly_pm25"] == 250.0
    # Rolling 24 h windows reach 18 hours only from the 18th hour of the day (no data
    # before the day here): hours 17..23 -> 7 hours with median AQI (80+400)/2 = 240.
    assert mh["hours_poor_or_worse"] == 7 and mh["source"] == "live_rollup"
    assert rows[("Aurangabad", "Bihar")]["reporting_locations"] == 1


def test_unmapped_stations_are_excluded():
    rows = build_city_rows({1: ("Delhi", "Delhi")}, {1: station(45.0), 99: station(500.0)}, DAY, 18, 6)
    assert len(rows) == 1 and rows[0]["max_station_aqi"] == 80


def test_city_with_data_but_no_valid_aqi_keeps_averages():
    hourly = {1: {"pm25": day_hours(lambda i: 45.0)}}           # one pollutant only
    rows = build_city_rows({1: ("Gaya", "Bihar")}, hourly, DAY, 18, 6)
    assert rows[0]["avg_pm25"] == 45.0 and rows[0]["median_station_aqi"] is None
    assert rows[0]["reporting_locations"] == 0


def test_city_averages_count_only_complete_station_days():
    # Station 2 reported 12 hours of PM2.5 (< 18): it is left out of the average, but its hours
    # still count for the hourly metric.
    stations = {1: ("Pune", "Maharashtra"), 2: ("Pune", "Maharashtra")}
    hourly = {1: station(40.0), 2: {"pm25": day_hours(lambda i: 400.0, n=12)}}
    [row] = build_city_rows(stations, hourly, DAY, 18, 6)
    assert row["avg_pm25"] == pytest.approx(40.0)
    assert row["max_hourly_pm25"] == 400.0


def test_completeness_threshold_is_inclusive():
    city = {1: ("Pune", "Maharashtra")}
    exactly = build_city_rows(city, {1: {"pm25": day_hours(lambda i: 50.0, n=18)}}, DAY, 18, 6)
    one_short = build_city_rows(city, {1: {"pm25": day_hours(lambda i: 50.0, n=17)}}, DAY, 18, 6)
    assert exactly[0]["avg_pm25"] == 50.0 and one_short == []


def test_city_day_without_any_complete_station_gets_no_row():
    hourly = {1: {"pm25": day_hours(lambda i: 400.0, n=12)}}
    assert build_city_rows({1: ("Gaya", "Bihar")}, hourly, DAY, 18, 6) == []
