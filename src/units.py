"""Unit resolution and conversion to BreatheSense standard units.

Standard units: CO in mg/m3; every other pollutant in ug/m3.

Two steps, kept separate so each is testable:

1. `effective_unit()` decides what unit a value is *really* in. OpenAQ's label is
   trusted except for one case Phase 1 proved unreliable: CPCB gas sensors
   (no2, so2, co) labelled `ppb`. For those, the station's classified convention
   from data/unit_convention.csv decides:
       ugm3          -> no2/so2 ug/m3, co mg/m3   (CPCB's publishing units)
       ppb           -> no2 ppb; so2 and co None (rejected, see below)
       unclassified  -> None (value rejected; never guess)
   SO2 and CO at `ppb` stations are rejected too (owner decision, Phase 7): the cross-era
   check (docs/openaq_exploration.md G) proved the NO2 unit there but could not tell whether
   SO2/CO are ppb or ug/m3, so the old assumption (they follow NO2) is not used.
2. `to_standard()` converts a value from that unit, using the 25 degC / 1 atm
   molar volume of 24.45 L/mol. Any unit not listed here raises UnitError, so an
   unexpected label is rejected and logged instead of silently mis-scaled.
"""

from __future__ import annotations

MOLAR_VOLUME = 24.45  # L/mol at 25 degC, 1 atm
MOLECULAR_WEIGHT = {"no2": 46.01, "so2": 64.07, "o3": 48.00, "co": 28.01, "nh3": 17.03}

POLLUTANTS = ("pm25", "pm10", "no2", "so2", "co", "o3", "nh3", "pb")
UGM3, MGM3, PPB, PPM = "ug/m3", "mg/m3", "ppb", "ppm"
STANDARD_UNIT = {p: (MGM3 if p == "co" else UGM3) for p in POLLUTANTS}

# Gas parameters whose `ppb` label is overridden by the station convention.
CONVENTION_PARAMS = ("no2", "so2", "co")
CONVENTIONS = ("ugm3", "ppb", "unclassified")

# OpenAQ writes micro as U+00B5 (micro sign); U+03BC (greek mu) is accepted too.
_ALIASES = {
    "µg/m³": UGM3, "μg/m³": UGM3, "ug/m3": UGM3, "µg/m3": UGM3, "μg/m3": UGM3,
    "mg/m³": MGM3, "mg/m3": MGM3,
    "ppb": PPB, "ppm": PPM,
}


class UnitError(ValueError):
    """Unknown unit label or a conversion we don't support."""


def canonical_unit(label: str | None) -> str:
    if label is None:
        raise UnitError("missing unit label")
    unit = _ALIASES.get(label.strip())
    if unit is None:
        raise UnitError(f"unknown unit label {label!r}")
    return unit


def effective_unit(parameter: str, label: str | None, convention: str) -> str | None:
    """The unit a value is really in, or None if it must be rejected."""
    if convention not in CONVENTIONS:
        raise ValueError(f"unknown gas unit convention {convention!r}")
    unit = canonical_unit(label)
    if parameter in CONVENTION_PARAMS and unit == PPB:
        if convention == "ugm3":
            return MGM3 if parameter == "co" else UGM3
        if convention == "ppb":
            return PPB if parameter == "no2" else None   # so2/co unverified at ppb stations
        return None
    return unit


def to_standard(parameter: str, value: float, unit: str) -> float:
    """Convert `value` in canonical `unit` to the standard unit for `parameter`."""
    if parameter not in STANDARD_UNIT:
        raise UnitError(f"unsupported parameter {parameter!r}")
    target = STANDARD_UNIT[parameter]
    if unit == target:
        return value
    if target == UGM3 and unit == MGM3:
        return value * 1000.0
    if target == MGM3 and unit == UGM3:
        return value / 1000.0
    if unit in (PPB, PPM):
        mw = MOLECULAR_WEIGHT.get(parameter)
        if mw is None:  # particulates and Pb have no ppb form
            raise UnitError(f"cannot convert {unit} for {parameter}")
        ppb = value * 1000.0 if unit == PPM else value
        ugm3 = ppb * mw / MOLAR_VOLUME
        return ugm3 / 1000.0 if target == MGM3 else ugm3
    raise UnitError(f"cannot convert {unit} to {target} for {parameter}")
