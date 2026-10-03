"""Observation validation.

Flow for every hourly value:  raw -> validation reason -> valid: converted + loaded
                                                       -> invalid: excluded + counted
Nothing is silently dropped: each rejected value gets one reason code, and the
counts end up in pipeline_log.

Checks run in this order (the first failing check is the reason):
  missing_value     value is None
  not_finite        NaN / inf
  unit_rejected     station's gas unit convention is `unclassified` (units.py)
  unknown_unit      a unit label we have no verified conversion for
  negative          value < 0 (physically impossible concentration)
  implausible_high  PM2.5 or PM10 above PM_MAX_UGM3 (default 1500 ug/m3), or a gas above
                    GAS_MAX (5x the lower bound of CPCB's "Severe" band), in standard units.
                    The gas caps reject sensor glitches such as NO2 ~1e20 ug/m3 that
                    otherwise reach daily averages and the AQI.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.units import UnitError, effective_unit, to_standard

DEFAULT_PM_MAX_UGM3 = 1500.0
PM_PARAMS = ("pm25", "pm10")

# Gas plausibility caps in standard units (CO mg/m3, others ug/m3): 5x the lower bound of
# the CPCB "Severe" band in data/ref_aqi_breakpoints.csv, rounded. Far above any real
# Indian reading, so only instrument/feed errors are rejected.
GAS_MAX = {"no2": 2000.0, "so2": 8000.0, "o3": 3750.0, "co": 170.0, "nh3": 9000.0, "pb": 18.0}

REASONS = ("missing_value", "not_finite", "unit_rejected", "unknown_unit",
           "negative", "implausible_high")


@dataclass(frozen=True)
class Checked:
    value: float | None      # standard units when valid, else None
    reason: str | None       # None when valid

    @property
    def is_valid(self) -> bool:
        return self.reason is None


def check(parameter: str, raw_value, unit_label: str | None, convention: str,
          pm_max: float = DEFAULT_PM_MAX_UGM3) -> Checked:
    """Validate one raw value and convert it to standard units."""
    if raw_value is None:
        return Checked(None, "missing_value")
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return Checked(None, "not_finite")
    if not math.isfinite(value):
        return Checked(None, "not_finite")
    try:
        unit = effective_unit(parameter, unit_label, convention)
    except UnitError:
        return Checked(None, "unknown_unit")
    if unit is None:
        return Checked(None, "unit_rejected")
    if value < 0:
        return Checked(None, "negative")
    try:
        std = to_standard(parameter, value, unit)
    except UnitError:
        return Checked(None, "unknown_unit")
    if parameter in PM_PARAMS and std > pm_max:
        return Checked(None, "implausible_high")
    if parameter in GAS_MAX and std > GAS_MAX[parameter]:
        return Checked(None, "implausible_high")
    return Checked(std, None)
