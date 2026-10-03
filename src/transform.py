"""Transform OpenAQ /hours results into wide hourly rows for fact_pollutant_hourly.

Pipeline for one run:
    parse_hours()  payload -> Observation per (sensor, hour)
    transform()    dedupe -> validate + convert (validation.py) -> pivot wide

Design decisions
  * reading_time = period.datetimeFrom in UTC, unchanged. CPCB hours start at
    HH:30Z (whole IST hours); rounding would shift every value by 30 min.
  * One row per (location_id, reading_time), one column per pollutant, so the
    AQI views read a single row per station-hour.
  * Duplicates of (location, parameter, hour) are counted, and the copy with the
    most underlying 15-min readings is kept. That makes the result deterministic
    and independent of input order.
  * quality_flag tokens (sorted, ';'-separated):
      low_coverage       an hour built from < 75% of its expected readings
                         (e.g. 2 of 4 quarter-hours). Kept, but visible.
      gas_units_inferred SO2/CO unit taken from the station's NO2-based
                         classification (the documented assumption)
  * A station-hour whose every value is invalid produces no row. Its values
    are still counted as invalid.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from src.units import PPB, POLLUTANTS, UnitError, canonical_unit
from src.validation import DEFAULT_PM_MAX_UGM3, check

LOW_COVERAGE_RATIO = 0.75
INFERRED_UNIT_PARAMS = ("so2", "co")


@dataclass(frozen=True)
class Observation:
    location_id: int
    sensor_id: int
    parameter: str
    reading_time: datetime          # UTC, start of the hour
    value: float | None             # as published
    unit_label: str | None          # as published
    convention: str                 # station gas unit convention
    observed_count: int | None = None
    expected_count: int | None = None


@dataclass
class TransformStats:
    fetched: int = 0
    duplicates: int = 0
    valid: int = 0
    invalid: int = 0
    invalid_by_reason: Counter = field(default_factory=Counter)
    low_coverage: int = 0
    rows: int = 0


def _utc(obj: dict) -> datetime:
    return datetime.fromisoformat(obj["utc"].replace("Z", "+00:00"))


def parse_hours(results: list[dict], *, location_id: int, sensor_id: int, parameter: str,
                unit_label: str | None, convention: str) -> list[Observation]:
    """Turn one sensor's /sensors/{id}/hours `results` into Observations.

    The unit label comes from dim_sensor (the /locations metadata). Each result's
    own `parameter.units` must agree, and a disagreement is surfaced as an
    unknown unit rather than trusted.
    """
    out = []
    for r in results:
        label = unit_label
        row_units = (r.get("parameter") or {}).get("units")
        if row_units is not None and label is not None and row_units != label:
            label = f"conflict:{label}|{row_units}"
        cov = r.get("coverage") or {}
        out.append(Observation(
            location_id=location_id, sensor_id=sensor_id, parameter=parameter,
            reading_time=_utc(r["period"]["datetimeFrom"]), value=r.get("value"),
            unit_label=label, convention=convention,
            observed_count=cov.get("observedCount"), expected_count=cov.get("expectedCount"),
        ))
    return out


def transform(observations: list[Observation],
              pm_max: float = DEFAULT_PM_MAX_UGM3) -> tuple[list[dict], TransformStats]:
    stats = TransformStats(fetched=len(observations))

    # 1. Deduplicate per (location, parameter, hour): keep the best-covered copy.
    best: dict[tuple, Observation] = {}
    for o in observations:
        if o.parameter not in POLLUTANTS:
            continue
        key = (o.location_id, o.parameter, o.reading_time)
        if key in best:
            stats.duplicates += 1
            cur = best[key]
            if ((o.observed_count or 0), o.sensor_id) <= ((cur.observed_count or 0), cur.sensor_id):
                continue
        best[key] = o

    # 2. Validate + convert, 3. pivot wide.
    values: dict[tuple, dict[str, float]] = defaultdict(dict)
    flags: dict[tuple, set[str]] = defaultdict(set)
    for (loc, param, t), o in sorted(best.items(), key=lambda kv: (kv[0][0], kv[0][2], kv[0][1])):
        c = check(param, o.value, o.unit_label, o.convention, pm_max)
        if not c.is_valid:
            stats.invalid += 1
            stats.invalid_by_reason[c.reason] += 1
            continue
        stats.valid += 1
        values[(loc, t)][param] = round(c.value, 4)
        if o.expected_count and (o.observed_count or 0) / o.expected_count < LOW_COVERAGE_RATIO:
            flags[(loc, t)].add("low_coverage")
            stats.low_coverage += 1
        if param in INFERRED_UNIT_PARAMS and _label_is_ppb(o.unit_label):
            flags[(loc, t)].add("gas_units_inferred")

    rows = []
    for (loc, t), vals in sorted(values.items()):
        rows.append({
            "location_id": loc,
            "reading_time": t,
            **{p: vals.get(p) for p in POLLUTANTS},
            "valid_observation_count": len(vals),
            "quality_flag": ";".join(sorted(flags[(loc, t)])) or None,
        })
    stats.rows = len(rows)
    return rows, stats


def _label_is_ppb(label: str | None) -> bool:
    try:
        return canonical_unit(label) == PPB
    except UnitError:
        return False
