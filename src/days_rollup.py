"""Station-day values and city rows from OpenAQ /days data.

One implementation of the /days rules, shared by the historical backfill
(scripts/backfill_history.py, cached files) and the daily rollup (src/rollup_days.py,
fresh API results): the completeness rule, the gas-unit rule at ppb stations, unit
conversion and the city-row definitions. Pure functions: no API, no database.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime

from src.aqi import overall_aqi
from src.rollup import most_frequent
from src.units import POLLUTANTS, UnitError, canonical_unit
from src.validation import check

PARAMS = ("pm25", "pm10", "no2", "so2", "o3", "co")
# Observation timestamps carry sub-hour offsets (:00/:15/:30/:45), so N hourly readings
# span up to ~0.75 h less than N-1 hours. Measured in 2025-26: 18 readings span >= 16.25 h.
SPAN_SLACK_HOURS = 1.0
# Gas units at stations classified `ppb` (owner decision, Phase 7). Cross-era check on 56 ppb and 63 ugm3
# cities: current-converted / legacy-as-labelled is 2.04 vs 1.07 for NO2 (factor 1.88), so the legacy
# NO2 label (ug/m3) is wrong there and the series is ppb. For SO2 (2.62) and CO (1.15) the same ratios
# appear, but the test cannot say whether both eras are ppb or both ug/m3, so they are excluded.
PPB_UNVERIFIED = ("so2", "co")


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def days_row(result: dict) -> dict:
    """One OpenAQ /days result -> the flat cache-format row that day_values() reads.
    ist_date is the local (IST) date of the day's period; first_obs/last_obs are the local times
    of the day's first and last observation (coverage.datetimeFrom / datetimeTo)."""
    cov = result.get("coverage") or {}
    return {
        "ist_date": result["period"]["datetimeFrom"]["local"][:10],
        "value": result.get("value"),
        "units": (result.get("parameter") or {}).get("units"),
        "observed": cov.get("observedCount"),
        "expected": cov.get("expectedCount"),
        "first_obs": (cov.get("datetimeFrom") or {}).get("local"),
        "last_obs": (cov.get("datetimeTo") or {}).get("local"),
    }


def span_hours(r: dict) -> float | None:
    """Hours between the first and last observation of a cached day; None if not recorded."""
    try:
        first, last = datetime.fromisoformat(r["first_obs"]), datetime.fromisoformat(r["last_obs"])
    except (KeyError, TypeError, ValueError):
        return None
    return (last - first).total_seconds() / 3600


def day_values(t: dict, rows: list[dict], pm_max: float, min_count: int, stats: Counter,
               report: dict) -> dict[date, tuple[float, float]]:
    """ist_date -> (standard-unit value, observed count) for one sensor; invalid days counted.
    `rows` are cache-format /days rows (ist_date, value, units, observed, first_obs, last_obs).

    Day completeness (both must hold; rejected days are counted, not dropped silently):
      * observedCount >= MIN_HOURS_24H  (incomplete_day)
      * the observations span >= MIN_HOURS_24H - 1 - SPAN_SLACK_HOURS hours, first to last
        (short_span_day, or unknown_span_day when the cache has no timestamps)

    The current CPCB series is hourly, so the count is the number of hours, and N distinct
    hourly readings span at least N - 1 hours less a sub-hour timestamp offset: the slack in
    the threshold means no genuinely hourly day is rejected (measured minimum for 18 readings:
    16.25 h against a 16 h threshold).
    The legacy series count irregular sub-hourly readings or duplicates (Phase 7: 62% of
    legacy days report more than 24 'observations'), so the count alone says little about
    hours covered. The span check removes days whose readings sit in part of the day. Measured
    on the first 586 sensors fetched: it rejects 7.7% of the 178,299 legacy days that pass the
    count rule and none of the 93,383 such current-era days. It cannot see gaps inside the
    span (README limitation).
    """
    out = {}
    for r in rows:
        obs = _num(r["observed"]) or 0.0
        if obs < min_count:
            stats["incomplete_day"] += 1
            continue
        span = span_hours(r)
        if span is None:
            stats["unknown_span_day"] += 1
            continue
        if span < min_count - 1 - SPAN_SLACK_HOURS:
            stats["short_span_day"] += 1
            continue
        coverage = obs
        label = r["units"] or t["source_units"]
        if t["gas_unit_convention"] == "ppb":
            if t["parameter"] in PPB_UNVERIFIED:
                stats["unit_rejected"] += 1      # SO2/CO unit unverified at ppb stations, both eras
                continue
            if t["parameter"] == "no2":
                label = "ppb"                    # legacy label says ug/m3 but the series is ppb
        c = check(t["parameter"], _num(r["value"]), label, t["gas_unit_convention"], pm_max)
        if not c.is_valid:
            stats[c.reason] += 1
            continue
        stats["valid_day"] += 1
        d = date.fromisoformat(r["ist_date"])
        era = "legacy" if d < date(2023, 1, 1) else "current"
        try:
            label = canonical_unit(r["units"] or t["source_units"])
        except UnitError:
            label = "?"
        report.setdefault((t["parameter"], label, t["gas_unit_convention"], era), []).append(c.value)
        out[d] = (c.value, coverage)
    return out


# location_id -> parameter -> IST day -> (standard-unit daily mean, observed count)
StationDays = dict[int, dict[str, dict[date, tuple[float, float]]]]


def collect_station_days(sensor_rows, pm_max: float, min_count: int, stats: Counter,
                         report: dict) -> StationDays:
    """Sensors grouped by station, from (sensor, cached rows) pairs. A station can have two
    sensors for the same parameter and day (legacy + current series); the better-covered wins."""
    station: StationDays = defaultdict(lambda: defaultdict(dict))
    for t, rows in sensor_rows:
        for d, (v, cov) in day_values(t, rows, pm_max, min_count, stats, report).items():
            cur = station[t["location_id"]][t["parameter"]].get(d)
            if cur is None or cov > cur[1]:
                station[t["location_id"]][t["parameter"]][d] = (v, cov)
    return station


def build_city_rows(city: str, state: str, station: StationDays) -> list[dict]:
    """Pure: one city's station-day values -> daily_city_summary rows (source='backfill').

    Same definitions as the live rollup (src/rollup.py) except where /days cannot
    support them: O3/CO use the daily mean (o3_co_approximated), and the hourly
    metrics stay NULL. A station counts toward reporting_locations only with a valid
    AQI; it still feeds avg_<pollutant> for every pollutant it reported that day.
    """
    days = sorted({d for s in station.values() for p in s.values() for d in p})
    rows = []
    for d in days:
        daily = {}
        means: dict[str, list[float]] = defaultdict(list)
        for lid, params in station.items():
            avgs = {p: params[p][d][0] for p in PARAMS if d in params.get(p, {})}
            for p, v in avgs.items():
                means[p].append(v)
            if avgs:
                daily[lid] = overall_aqi(avgs)
        valid = [r for r in daily.values() if r.valid]
        aqis = [r.aqi for r in valid]
        rows.append({
            "city": city, "state": state, "summary_date": d,
            **{f"avg_{p}": (round(sum(means[p]) / len(means[p]), 3) if means.get(p) else None)
               for p in POLLUTANTS},
            "median_station_aqi": statistics.median(aqis) if aqis else None,
            "max_station_aqi": max(aqis) if aqis else None,
            "dominant_pollutant": most_frequent([r.dominant_pollutant for r in valid]),
            "max_hourly_pm25": None, "hours_poor_or_worse": None,
            "reporting_locations": len(valid), "source": "backfill",
            "o3_co_approximated": True,
        })
    return rows


