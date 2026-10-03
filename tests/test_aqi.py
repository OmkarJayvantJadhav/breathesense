"""AQI tests: every documented edge case."""

from datetime import datetime, timedelta, timezone

import pytest

from src.aqi import (category, default_breakpoints, dominant_pollutant, overall_aqi,
                     round_half_up, station_aqi, sub_index, validate_availability,
                     window_average)

BP = default_breakpoints()
ALL_BANDS = [(p, b) for p, bands in BP.bands.items() for b in bands]


# ---------------------------------------------------------------- CPCB's own worked example
@pytest.mark.parametrize("conc, expected", [(31, 51), (60, 100), (45, 75)])
def test_cpcb_published_example_pm25(conc, expected):
    # About_AQI.pdf: "sub-index for PM2.5 will be 51 at 31, 100 at 60, and 75 at 45 ug/m3"
    assert round_half_up(sub_index("pm25", conc)) == expected


# ---------------------------------------------------------------- every breakpoint edge
@pytest.mark.parametrize("param, band", ALL_BANDS, ids=lambda x: str(x))
def test_band_lower_edge_maps_to_aqi_low(param, band):
    assert sub_index(param, band.conc_low) == pytest.approx(band.aqi_low)


@pytest.mark.parametrize("param, band", [(p, b) for p, b in ALL_BANDS if b.conc_high is not None],
                         ids=lambda x: str(x))
def test_band_upper_edge_maps_to_aqi_high(param, band):
    assert sub_index(param, band.conc_high) == pytest.approx(band.aqi_high)


def test_zero_concentration():
    assert sub_index("pm10", 0) == 0


# ---------------------------------------------------------------- rounding and gaps
@pytest.mark.parametrize("param, conc, expected", [
    ("pm25", 30.4, 50),    # rounds to 30 -> Good
    ("pm25", 30.5, 51),    # half-up to 31 -> Satisfactory (banker's rounding would give 30)
    ("pm25", 60.5, 101),
    ("co", 1.04, 50),      # CO: one decimal -> 1.0
    ("co", 1.05, 51),      # -> 1.1
    ("co", 10.04, 200),    # -> 10.0, Moderate upper edge
    ("co", 10.05, 201),    # -> 10.1, Poor
    ("pb", 0.54, 50),
    ("pb", 0.55, 51),
])
def test_rounding_before_banding(param, conc, expected):
    assert round_half_up(sub_index(param, conc)) == expected


def test_value_in_gap_goes_to_higher_band_and_is_clamped():
    # Bands are only defined at table precision, so every rounded value lands in
    # a band. Feeding an unrounded gap value directly still clamps to aqi_low.
    from src.aqi import Band, interpolate
    band = Band(conc_low=31, conc_high=60, aqi_low=51, aqi_high=100)
    assert interpolate(30.6, band) == 51


# ---------------------------------------------------------------- top band
@pytest.mark.parametrize("param, conc, expected", [
    ("pm25", 251, 401),
    ("pm25", 300, 439),      # 401 + (99/129) * 49 = 438.6
    ("pm25", 380, 500),      # 401 + 99 = 500
    ("pm25", 1000, 500),     # capped
    ("pm10", 431, 401),
    ("pm10", 2000, 500),
    ("co", 34.1, 401),
    ("co", 60, 500),
])
def test_top_band_extrapolation_and_cap(param, conc, expected):
    assert round_half_up(sub_index(param, conc)) == expected


def test_negative_concentration_is_an_error():
    with pytest.raises(ValueError):
        sub_index("pm25", -1)


def test_missing_concentration_gives_none():
    assert sub_index("pm25", None) is None


# ---------------------------------------------------------------- categories
@pytest.mark.parametrize("aqi, name", [
    (0, "Good"), (50, "Good"), (51, "Satisfactory"), (100, "Satisfactory"),
    (101, "Moderate"), (200, "Moderate"), (201, "Poor"), (300, "Poor"),
    (301, "Very Poor"), (400, "Very Poor"), (401, "Severe"), (500, "Severe"),
])
def test_category_boundaries(aqi, name):
    assert category(aqi)[0] == name


def test_category_colors_and_none():
    assert category(450) == ("Severe", "#C00000")
    assert category(None) == (None, None)


# ---------------------------------------------------------------- overall AQI and validity
def test_overall_is_max_subindex_rounded():
    r = overall_aqi({"pm25": 45, "pm10": 80, "no2": 30})
    # pm25 45 -> 74.66, pm10 80 -> 80, no2 30 -> 37.5
    assert r.valid and r.aqi == 80 and r.dominant_pollutant == "pm10" and r.category == "Satisfactory"


def test_fewer_than_three_pollutants_is_invalid():
    r = overall_aqi({"pm25": 45, "pm10": 80, "no2": None, "o3": None})
    assert not r.valid and r.aqi is None and r.reason == "fewer_than_3_pollutants"


def test_no_pm_is_invalid():
    r = overall_aqi({"no2": 30, "o3": 40, "co": 1.0, "so2": 10})
    assert not r.valid and r.reason == "no_pm"


def test_only_pm10_counts_as_pm():
    assert overall_aqi({"pm10": 80, "o3": 40, "co": 1.0}).valid


def test_missing_pollutants_are_ignored_not_zero():
    r = overall_aqi({"pm25": 250, "pm10": None, "no2": 10, "o3": 10, "nh3": None, "pb": None})
    assert r.valid and r.aqi == 400 and set(r.sub_indices) == {"pm25", "no2", "o3"}


def test_dominant_tie_break_is_deterministic():
    assert dominant_pollutant({"o3": 100.0, "pm10": 100.0, "no2": 50.0}) == "pm10"
    assert dominant_pollutant({}) is None


def test_validate_availability_counts_only_present():
    assert validate_availability({"pm25": 10.0, "o3": None, "co": 5.0}) == "fewer_than_3_pollutants"


# ---------------------------------------------------------------- windows and completeness
T0 = datetime(2026, 9, 24, 17, 30, tzinfo=timezone.utc)   # a real CPCB hour start (HH:30Z)


def hours(values, end=T0):
    """values[0] is the oldest hour; the last value is at `end`."""
    n = len(values)
    return {end - timedelta(hours=n - 1 - i): v for i, v in enumerate(values)}


def test_window_average_requires_min_count():
    s = hours([10.0] * 17 + [None] * 7)
    assert window_average(s, T0, 24, 18) is None          # 17 < 18
    s = hours([10.0] * 18 + [None] * 6)
    assert window_average(s, T0, 24, 18) == 10.0


def test_window_average_ignores_hours_outside_window():
    s = hours([1000.0] + [10.0] * 24)                        # 25 hours; oldest is outside
    assert window_average(s, T0, 24, 18) == 10.0


def test_window_average_null_series():
    assert window_average({}, T0, 8, 6) is None


def test_station_aqi_uses_8h_for_o3_and_co():
    hourly = {
        "pm25": hours([45.0] * 24),
        "pm10": hours([80.0] * 24),
        "o3": hours([500.0] * 16 + [40.0] * 8),              # only the last 8 h count
        "co": hours([1.0] * 5, ),                            # 5 < 6 hours -> incomplete
    }
    r = station_aqi(hourly, T0)
    assert r.valid and r.aqi == 80 and "co" not in r.sub_indices
    assert r.sub_indices["o3"] == pytest.approx(40.0)


def test_station_aqi_invalid_when_windows_incomplete():
    # PM2.5 has only 10 of 24 hours, leaving 2 complete pollutants: below the minimum of 3.
    hourly = {"pm25": hours([45.0] * 10), "pm10": hours([80.0] * 24), "no2": hours([30.0] * 24)}
    r = station_aqi(hourly, T0)
    assert not r.valid and r.aqi is None and r.reason == "fewer_than_3_pollutants"
    assert set(r.sub_indices) == {"pm10", "no2"}


def test_station_aqi_completeness_thresholds_are_configurable():
    hourly = {"pm25": hours([45.0] * 12), "pm10": hours([80.0] * 12), "no2": hours([30.0] * 12)}
    assert not station_aqi(hourly, T0).valid
    assert station_aqi(hourly, T0, min_hours_24h=12).valid
