import math

import pytest

from src.validation import check


@pytest.mark.parametrize("raw, reason", [
    (None, "missing_value"),
    (math.nan, "not_finite"),
    (math.inf, "not_finite"),
    ("abc", "not_finite"),
    (-0.1, "negative"),
    (1500.1, "implausible_high"),
])
def test_invalid_pm25(raw, reason):
    c = check("pm25", raw, "µg/m³", "ugm3")
    assert not c.is_valid and c.reason == reason and c.value is None


def test_pm_threshold_is_inclusive_and_configurable():
    assert check("pm10", 1500, "µg/m³", "ugm3").is_valid
    assert check("pm10", 900, "µg/m³", "ugm3", pm_max=800).reason == "implausible_high"


def test_high_but_real_gas_values_are_kept():
    # Gas caps sit at 5x the CPCB "Severe" floor, so heavy pollution passes.
    assert check("so2", 2000, "µg/m³", "ugm3").is_valid
    assert check("no2", 2000, "µg/m³", "ugm3").is_valid      # cap is inclusive
    assert check("co", 50, "mg/m³", "ugm3").is_valid


@pytest.mark.parametrize("param, raw, unit", [
    ("no2", 4e20, "µg/m³"),          # the sensor glitch seen in vw_city_daily_all
    ("no2", 2000.1, "µg/m³"),
    ("o3", 5000, "µg/m³"),
    ("co", 171, "mg/m³"),
    ("no2", 1500, "ppb"),            # 1500 ppb = 2823 ug/m3 after conversion
])
def test_implausible_gas_values_are_rejected(param, raw, unit):
    c = check(param, raw, unit, "ppb" if unit == "ppb" else "ugm3")
    assert c.reason == "implausible_high" and c.value is None


def test_zero_is_valid():
    c = check("pm25", 0, "µg/m³", "ugm3")
    assert c.is_valid and c.value == 0


def test_unclassified_station_gas_is_rejected():
    assert check("no2", 20.0, "ppb", "unclassified").reason == "unit_rejected"


def test_unknown_unit_is_rejected():
    assert check("pm25", 20.0, "particles/cm³", "ugm3").reason == "unknown_unit"
    assert check("pm25", 20.0, "ppb", "ugm3").reason == "unknown_unit"   # pm can't be ppb


def test_negative_is_checked_before_conversion():
    assert check("no2", -1, "ppb", "ppb").reason == "negative"


def test_valid_values_are_converted():
    assert check("no2", 10.0, "ppb", "ppb").value == pytest.approx(18.818, abs=1e-3)
    assert check("no2", 10.0, "ppb", "ugm3").value == 10.0
    assert check("co", 0.82, "ppb", "ugm3").value == 0.82              # mg/m3 as published
    assert check("co", 0.82, "ppb", "ppb").reason == "unit_rejected"   # CO/SO2 unverified at ppb stations
    assert check("so2", 5.0, "ppb", "ppb").reason == "unit_rejected"
