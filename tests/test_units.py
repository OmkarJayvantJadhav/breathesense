import pytest

from src.units import (MGM3, PPB, PPM, UGM3, UnitError, canonical_unit, effective_unit,
                       to_standard)


@pytest.mark.parametrize("label, unit", [
    ("µg/m³", UGM3),   # U+00B5 micro sign, as OpenAQ sends it
    ("μg/m³", UGM3),   # U+03BC greek mu
    ("ug/m3", UGM3), ("mg/m³", MGM3), ("ppb", PPB), ("ppm", PPM), (" ppb ", PPB),
])
def test_canonical_unit(label, unit):
    assert canonical_unit(label) == unit


@pytest.mark.parametrize("label", ["c", "%", "deg", "particles/cm³", None, ""])
def test_unknown_unit_raises(label):
    with pytest.raises(UnitError):
        canonical_unit(label)


@pytest.mark.parametrize("param, value, unit, expected", [
    ("no2", 100, PPB, 188.18),        # 100 * 46.01 / 24.45
    ("so2", 100, PPB, 262.04),        # 100 * 64.07 / 24.45
    ("o3", 100, PPB, 196.32),         # 100 * 48.00 / 24.45
    ("nh3", 100, PPB, 69.65),         # 100 * 17.03 / 24.45
    ("o3", 0.05, PPM, 98.16),         # 50 ppb
    ("co", 1.0, PPM, 1.1456),         # 28.01 / 24.45 mg/m3
    ("co", 1000, PPB, 1.1456),
    ("co", 1500, UGM3, 1.5),
    ("pm25", 42.0, UGM3, 42.0),
    ("co", 0.82, MGM3, 0.82),
])
def test_to_standard(param, value, unit, expected):
    assert to_standard(param, value, unit) == pytest.approx(expected, abs=1e-2)


@pytest.mark.parametrize("param, unit", [("pm25", PPB), ("pb", PPM), ("temperature", UGM3)])
def test_impossible_conversions_raise(param, unit):
    with pytest.raises(UnitError):
        to_standard(param, 1.0, unit)


# --- station convention overrides only the unreliable CPCB `ppb` gas label
@pytest.mark.parametrize("param, convention, expected", [
    ("no2", "ugm3", UGM3), ("so2", "ugm3", UGM3), ("co", "ugm3", MGM3),
    ("no2", "ppb", PPB), ("so2", "ppb", None), ("co", "ppb", None),    # SO2/CO unverified at ppb stations
    ("no2", "unclassified", None), ("so2", "unclassified", None), ("co", "unclassified", None),
])
def test_effective_unit_for_ppb_labelled_gases(param, convention, expected):
    assert effective_unit(param, "ppb", convention) == expected


@pytest.mark.parametrize("convention", ["ugm3", "ppb", "unclassified"])
def test_other_labels_are_trusted(convention):
    assert effective_unit("pm25", "µg/m³", convention) == UGM3
    assert effective_unit("o3", "µg/m³", convention) == UGM3
    assert effective_unit("o3", "ppm", convention) == PPM          # 2 real o3 ppm sensors
    assert effective_unit("no2", "µg/m³", convention) == UGM3      # legacy ug/m3 gas sensors


def test_unknown_convention_is_an_error():
    with pytest.raises(ValueError):
        effective_unit("no2", "ppb", "guess")
