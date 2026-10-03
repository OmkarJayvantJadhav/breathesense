"""BreatheSense AQI (CPCB method): pure functions, no database, no API.

Not an official CPCB AQI. Implements the CPCB National AQI with the project
rules:

  * Breakpoints come from data/ref_aqi_breakpoints.csv, the same file init_db
    loads into SQL, so Python and SQL share one source of truth.
  * Concentrations are rounded to table precision before banding: integers,
    one decimal for CO and Pb. Rounding is half-up (Decimal) to match
    PostgreSQL ROUND(numeric); Python's round() is banker's rounding and would
    disagree with SQL on .5 values.
  * A value belongs to the first band with C <= conc_high. A value in a
    published gap (e.g. CO 10.05 before rounding) therefore goes to the higher
    band, and the result is clamped to that band's AQI range.
  * Open-ended top band: extrapolate with the previous band's slope from 401;
    cap at 500.
  * Overall AQI = max sub-index, rounded to an integer. Valid only with >= 3
    pollutants whose averaging windows are complete, including PM2.5 or PM10.
  * Windows: 24 h mean for PM10/PM2.5/NO2/SO2/NH3/Pb, 8 h mean for O3/CO,
    requiring >= MIN_HOURS_24H (18) / MIN_HOURS_8H (6) hourly values.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path

from src.config import DATA_DIR

BREAKPOINTS_CSV = DATA_DIR / "ref_aqi_breakpoints.csv"
AQI_MAX = 500
PM_PARAMS = ("pm25", "pm10")
MIN_POLLUTANTS = 3
DECIMALS = {"co": 1, "pb": 1}   # all others: integers
# Tie-break order for the dominant pollutant when two sub-indices are equal.
DOMINANCE_ORDER = ("pm25", "pm10", "no2", "o3", "co", "so2", "nh3", "pb")

# (upper AQI, category, colour). CPCB's PDF names category 3 "Moderately Polluted".
CATEGORIES = (
    (50, "Good", "#00B050"),
    (100, "Satisfactory", "#92D050"),
    (200, "Moderate", "#FFFF00"),
    (300, "Poor", "#FF9900"),
    (400, "Very Poor", "#FF0000"),
    (500, "Severe", "#C00000"),
)


@dataclass(frozen=True)
class Band:
    conc_low: float
    conc_high: float | None     # None = open-ended top band
    aqi_low: int
    aqi_high: int


@dataclass(frozen=True)
class Breakpoints:
    bands: dict[str, tuple[Band, ...]]
    avg_hours: dict[str, int]


@dataclass
class AqiResult:
    aqi: int | None
    category: str | None
    color: str | None
    dominant_pollutant: str | None
    sub_indices: dict[str, float] = field(default_factory=dict)
    valid: bool = False
    reason: str | None = None


def load_breakpoints(path: Path = BREAKPOINTS_CSV) -> Breakpoints:
    bands: dict[str, list[Band]] = {}
    hours: dict[str, int] = {}
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            p = r["parameter"]
            bands.setdefault(p, []).append(Band(
                conc_low=float(r["conc_low"]),
                conc_high=float(r["conc_high"]) if r["conc_high"].strip() else None,
                aqi_low=int(r["aqi_low"]), aqi_high=int(r["aqi_high"])))
            hours[p] = int(r["avg_hours"])
    return Breakpoints(
        bands={p: tuple(sorted(b, key=lambda x: x.aqi_low)) for p, b in bands.items()},
        avg_hours=hours)


@lru_cache(maxsize=1)
def default_breakpoints() -> Breakpoints:
    return load_breakpoints()


def round_half_up(value: float, decimals: int = 0) -> float:
    q = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def round_concentration(parameter: str, conc: float) -> float:
    return round_half_up(conc, DECIMALS.get(parameter, 0))


def interpolate(c: float, band: Band) -> float:
    """I = (I_high - I_low) / (B_high - B_low) * (C - B_low) + I_low, clamped to the band."""
    value = (band.aqi_high - band.aqi_low) / (band.conc_high - band.conc_low) * (c - band.conc_low) + band.aqi_low
    return min(max(value, band.aqi_low), band.aqi_high)


def sub_index(parameter: str, conc: float | None, bp: Breakpoints | None = None) -> float | None:
    """Unrounded sub-index for one pollutant average; None if no value."""
    if conc is None:
        return None
    if conc < 0:
        raise ValueError(f"negative concentration for {parameter}: {conc}")
    bp = bp or default_breakpoints()
    bands = bp.bands.get(parameter)
    if not bands:
        raise ValueError(f"no breakpoints for {parameter!r}")
    c = round_concentration(parameter, conc)
    for i, band in enumerate(bands):
        if band.conc_high is None:
            prev = bands[i - 1]
            slope = (prev.aqi_high - prev.aqi_low) / (prev.conc_high - prev.conc_low)
            value = band.aqi_low + slope * (c - band.conc_low)
            return min(max(value, band.aqi_low), AQI_MAX)
        if c <= band.conc_high:
            return interpolate(c, band)
    raise AssertionError("unreachable: top band is open-ended")


def category(aqi: int | None) -> tuple[str, str] | tuple[None, None]:
    if aqi is None:
        return None, None
    for upper, name, color in CATEGORIES:
        if aqi <= upper:
            return name, color
    return CATEGORIES[-1][1], CATEGORIES[-1][2]


def dominant_pollutant(sub_indices: dict[str, float | None]) -> str | None:
    present = {p: v for p, v in sub_indices.items() if v is not None}
    if not present:
        return None
    top = max(present.values())
    return next(p for p in DOMINANCE_ORDER if present.get(p) == top)


def validate_availability(sub_indices: dict[str, float | None]) -> str | None:
    """Reason the AQI is invalid, or None when valid."""
    present = [p for p, v in sub_indices.items() if v is not None]
    if len(present) < MIN_POLLUTANTS:
        return f"fewer_than_{MIN_POLLUTANTS}_pollutants"
    if not any(p in present for p in PM_PARAMS):
        return "no_pm"
    return None


def overall_aqi(averages: dict[str, float | None], bp: Breakpoints | None = None) -> AqiResult:
    """AQI from per-pollutant window averages (standard units). None = incomplete window."""
    subs = {p: sub_index(p, c, bp) for p, c in averages.items()}
    reason = validate_availability(subs)
    present = {p: v for p, v in subs.items() if v is not None}
    if reason:
        return AqiResult(None, None, None, None, present, valid=False, reason=reason)
    aqi = int(round_half_up(max(present.values())))
    name, color = category(aqi)
    return AqiResult(aqi, name, color, dominant_pollutant(present), present, valid=True)


def window_average(series: dict[datetime, float | None], end: datetime,
                   hours: int, min_count: int) -> float | None:
    """Mean of the hourly values with start in [end - (hours-1) h, end]; None if too few."""
    start = end - timedelta(hours=hours - 1)
    vals = [v for t, v in series.items() if v is not None and start <= t <= end]
    if len(vals) < min_count:
        return None
    return sum(vals) / len(vals)


def station_aqi(hourly: dict[str, dict[datetime, float | None]], end: datetime,
                min_hours_24h: int = 18, min_hours_8h: int = 6,
                bp: Breakpoints | None = None) -> AqiResult:
    """AQI for one station as of the hour starting at `end` (UTC).

    `hourly` maps pollutant -> {reading_time: value in standard units}.
    """
    bp = bp or default_breakpoints()
    averages = {}
    for p, series in hourly.items():
        if p not in bp.avg_hours:
            continue
        hours = bp.avg_hours[p]
        averages[p] = window_average(series, end, hours,
                                     min_hours_8h if hours == 8 else min_hours_24h)
    return overall_aqi(averages, bp)
