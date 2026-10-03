"""Phase 1: explore the OpenAQ v3 API for India and benchmark hourly strategies.

Run:  python -m scripts.explore_api

Outputs
  * docs/openaq_exploration.md          verified facts only (regenerated each run)
  * data/samples/exploration_facts.json machine-readable version of the same facts
  * data/samples/*.json                 trimmed raw payloads (gitignored)

Nothing here is assumed: every number in the report is computed from responses
received during this run. Where a filter or parameter is being *tested*, the
report records what the API actually did (e.g. whether `iso=IN` really restricts
results to India).

Request budget for the whole run is capped at 400 (well under 2,000/hour).
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import DOCS_DIR, SAMPLES_DIR, get_settings
from src.openaq_client import OpenAQClient, OpenAQError

log = logging.getLogger("explore_api")

TARGET_PARAMS = ["pm25", "pm10", "no2", "so2", "o3", "co", "nh3", "pb"]
EXPLORATION_BUDGET = 400
BENCH_LOCATIONS = 10
BENCH_WINDOW_HOURS = 3

NOW = datetime.now(timezone.utc)


# ----------------------------------------------------------------- helpers
def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def utc_of(obj: dict | None) -> datetime | None:
    """OpenAQ datetimes look like {"utc": "...", "local": "..."}."""
    return parse_ts((obj or {}).get("utc")) if isinstance(obj, dict) else None


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def save_sample(name: str, payload: Any, max_results: int = 3) -> None:
    """Save a trimmed payload: only the first few `results` rows are kept."""
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        payload = {**payload, "results": payload["results"][:max_results]}
    (SAMPLES_DIR / f"{name}.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )


def try_get(client: OpenAQClient, path: str, params: dict | None = None) -> tuple[dict | None, str | None]:
    """GET that records a 4xx as a fact instead of crashing the exploration."""
    try:
        return client.get(path, params), None
    except OpenAQError as exc:
        return None, str(exc)


def pct(n: int, d: int) -> str:
    return f"{n} / {d} ({100 * n / d:.1f}%)" if d else f"{n} / 0"


# ----------------------------------------------------------------- steps
def explore_countries(client: OpenAQClient, facts: dict) -> int:
    body = client.get("/countries", {"limit": 1000})
    save_sample("countries", body, max_results=300)
    india = [c for c in body["results"] if c.get("code") == "IN"]
    if len(india) != 1:
        raise SystemExit(f"Expected exactly one country with code IN, found {len(india)}")
    c = india[0]
    facts["country"] = {
        "id": c.get("id"), "code": c.get("code"), "name": c.get("name"),
        "datetimeFirst": c.get("datetimeFirst"), "datetimeLast": c.get("datetimeLast"),
        "parameters": sorted({p.get("name") for p in c.get("parameters") or []}),
        "keys": sorted(c.keys()),
    }
    return c["id"]


def explore_parameters(client: OpenAQClient, facts: dict) -> dict[str, list[dict]]:
    rows = list(client.paginate("/parameters", limit=1000, max_pages=3))
    save_sample("parameters", {"results": rows}, max_results=100)
    by_name: dict[str, list[dict]] = defaultdict(list)
    for p in rows:
        by_name[p.get("name")].append(p)
    facts["parameters"] = [
        {"id": p["id"], "name": p["name"], "units": p.get("units"),
         "displayName": p.get("displayName")}
        for name in TARGET_PARAMS for p in by_name.get(name, [])
    ]
    facts["parameter_keys"] = sorted(rows[0].keys()) if rows else []
    return by_name


def explore_locations(client: OpenAQClient, facts: dict, india_id: int) -> list[dict]:
    # Filter behaviour: compare iso=IN with countries_id=<id> using meta.found.
    filt = {}
    for label, params in {
        "iso=IN": {"iso": "IN"},
        f"countries_id={india_id}": {"countries_id": india_id},
        "iso=IN&monitor=true": {"iso": "IN", "monitor": "true"},
        "iso=IN&mobile=false": {"iso": "IN", "mobile": "false"},
    }.items():
        body, err = try_get(client, "/locations", {**params, "limit": 1, "page": 1})
        filt[label] = {"meta_found": body["meta"].get("found") if body else None, "error": err}
    facts["location_filters"] = filt

    # Full pull of Indian locations with page-level metadata.
    pages, locations = [], []
    page = 1
    while page <= 20:  # hard bound; India is expected to need only a few pages
        body = client.get("/locations", {"iso": "IN", "limit": 1000, "page": page})
        res = body.get("results") or []
        pages.append({"page": page, "returned": len(res),
                      "meta": {k: v for k, v in body["meta"].items() if k != "website"}})
        if page == 1:
            save_sample("locations_page1", body, max_results=3)
        locations.extend(res)
        if len(res) < 1000:
            break
        page += 1
    beyond, err = try_get(client, "/locations", {"iso": "IN", "limit": 1000, "page": page + 1})
    over_limit, over_err = try_get(client, "/locations", {"iso": "IN", "limit": 1001, "page": 1})
    facts["pagination"] = {
        "pages": pages,
        "page_beyond_end": {"returned": len(beyond["results"]) if beyond else None,
                            "meta_found": beyond["meta"].get("found") if beyond else None,
                            "error": err},
        "limit_1001": {"accepted": over_limit is not None, "error": over_err},
    }
    return locations


def summarize_locations(locations: list[dict], facts: dict) -> None:
    n = len(locations)
    country_codes = Counter((l.get("country") or {}).get("code") for l in locations)
    monitors = [l for l in locations if l.get("isMonitor") is True]
    mobile = [l for l in locations if l.get("isMobile") is True]
    fixed_monitors = [l for l in monitors if l.get("isMobile") is False]

    def last_seen(l):
        return utc_of(l.get("datetimeLast"))

    def active_within(ls, hours):
        return sum(1 for l in ls if (t := last_seen(l)) and NOW - t <= timedelta(hours=hours))

    localities = [l.get("locality") for l in fixed_monitors]
    loc_filled = [x for x in localities if x]
    sensor_combo = Counter()
    sensors_per_loc = []
    for l in fixed_monitors:
        sensors = l.get("sensors") or []
        sensors_per_loc.append(sum(1 for s in sensors if s["parameter"]["name"] in TARGET_PARAMS))
        for s in sensors:
            sensor_combo[(s["parameter"]["name"], s["parameter"].get("units"), s["parameter"]["id"])] += 1

    active_24h_monitors = [l for l in fixed_monitors
                           if (t := last_seen(l)) and NOW - t <= timedelta(hours=24)]
    active_target_sensors_24h = sum(
        1 for l in active_24h_monitors for s in l.get("sensors") or []
        if s["parameter"]["name"] in TARGET_PARAMS
    )

    buckets = Counter()
    for l in fixed_monitors:
        t = last_seen(l)
        age = None if t is None else (NOW - t).total_seconds() / 3600
        buckets["never" if age is None else "<3h" if age < 3 else "3-24h" if age < 24
                else "1-7d" if age < 168 else "7-30d" if age < 720 else ">30d"] += 1
    multi = Counter()
    for l in fixed_monitors:
        for name, k in Counter(s["parameter"]["name"] for s in l.get("sensors") or []).items():
            if k > 1 and name in TARGET_PARAMS:
                multi[name] += 1
    feed_head = max((t for l in fixed_monitors if (t := last_seen(l))), default=None)
    # Newest datetimeLast per provider: shows whether one provider's feed has stalled.
    provider_head: dict[str, datetime] = {}
    for l in fixed_monitors:
        p, t = (l.get("provider") or {}).get("name"), last_seen(l)
        if t and (p not in provider_head or t > provider_head[p]):
            provider_head[p] = t
    main_provider = Counter((l.get("provider") or {}).get("name") for l in fixed_monitors).most_common(1)[0][0]

    facts["locations"] = {
        "total": n,
        "fixed_monitor_datetimeLast_age": dict(buckets),
        "feed_head_utc": feed_head,
        "provider_head_utc": {p: iso(t) for p, t in sorted(provider_head.items(), key=lambda x: str(x[0]))},
        "main_provider": main_provider,
        "main_provider_head": provider_head.get(main_provider),
        "locations_with_multiple_sensors_per_parameter": dict(multi),
        "country_codes_in_result": dict(country_codes),
        "location_keys": sorted(locations[0].keys()) if locations else [],
        "sensor_keys": sorted((locations[0].get("sensors") or [{}])[0].keys()) if locations else [],
        "isMonitor_true": len(monitors),
        "isMonitor_false": sum(1 for l in locations if l.get("isMonitor") is False),
        "isMobile_true": len(mobile),
        "fixed_monitors": len(fixed_monitors),
        "fixed_monitors_active_24h": active_within(fixed_monitors, 24),
        "fixed_monitors_active_7d": active_within(fixed_monitors, 24 * 7),
        "all_active_24h": active_within(locations, 24),
        "fixed_monitors_never_reported": sum(1 for l in fixed_monitors if not last_seen(l)),
        "active_24h_target_sensors": active_target_sensors_24h,
        "providers_fixed_monitors": Counter(
            (l.get("provider") or {}).get("name") for l in fixed_monitors).most_common(15),
        "providers_non_monitors": Counter(
            (l.get("provider") or {}).get("name") for l in locations
            if l.get("isMonitor") is False).most_common(10),
        "owners_fixed_monitors": Counter(
            (l.get("owner") or {}).get("name") for l in fixed_monitors).most_common(15),
        "timezones": Counter(l.get("timezone") for l in locations).most_common(),
        "locality_filled_fixed_monitors": pct(len(loc_filled), len(fixed_monitors)),
        "locality_distinct": len(set(loc_filled)),
        "locality_examples": Counter(loc_filled).most_common(25),
        "name_examples": sorted(l.get("name") for l in fixed_monitors)[:40:2],
        "name_with_locality_examples": [
            {"name": l.get("name"), "locality": l.get("locality")}
            for l in fixed_monitors[:: max(1, len(fixed_monitors) // 20)]
        ][:20],
        "sensor_parameter_units": sorted(
            ([p, u, i, c] for (p, u, i), c in sensor_combo.items()), key=lambda r: -r[3]),
        "target_sensors_per_fixed_monitor": {
            "min": min(sensors_per_loc, default=None),
            "median": statistics.median(sensors_per_loc) if sensors_per_loc else None,
            "max": max(sensors_per_loc, default=None),
        },
    }


def pick_bench_locations(locations: list[dict], head: datetime) -> list[dict]:
    """Deterministic spread of fixed monitors that reported within the bench window.

    The window is anchored at `head`, the newest datetimeLast in the feed. When the
    upstream feed is live, head ~= now; during an upstream outage it still lets us
    measure completeness, duplicates and request cost on the last available hours.
    """
    fresh = sorted(
        (l for l in locations
         if l.get("isMonitor") is True and l.get("isMobile") is False
         and (t := utc_of(l.get("datetimeLast"))) and head - t <= timedelta(hours=BENCH_WINDOW_HOURS)
         and any(s["parameter"]["name"] in TARGET_PARAMS for s in l.get("sensors") or [])),
        key=lambda l: l["id"],
    )
    if not fresh:
        return []
    step = max(1, len(fresh) // BENCH_LOCATIONS)
    return fresh[::step][:BENCH_LOCATIONS]


def explore_single_endpoints(client: OpenAQClient, facts: dict, loc: dict) -> None:
    ep: dict[str, Any] = {}
    detail = client.get(f"/locations/{loc['id']}")
    save_sample("location_detail", detail)

    latest = client.get(f"/locations/{loc['id']}/latest")
    save_sample("location_latest", latest, max_results=10)
    ep["location_latest_keys"] = sorted(latest["results"][0].keys()) if latest["results"] else []

    sensor = next(s for s in loc["sensors"] if s["parameter"]["name"] in ("pm25", "pm10"))
    ep["sample_location"] = {"id": loc["id"], "name": loc.get("name")}
    ep["sample_sensor"] = {"id": sensor["id"], "parameter": sensor["parameter"]}

    # Raw measurements: what is the native reporting interval?
    meas, err = try_get(client, f"/sensors/{sensor['id']}/measurements", {
        "datetime_from": iso(NOW - timedelta(hours=6)), "datetime_to": iso(NOW), "limit": 100})
    if meas:
        save_sample("sensor_measurements", meas, max_results=5)
        periods = Counter((r.get("period") or {}).get("interval") for r in meas["results"])
        ep["measurements"] = {"rows_last_6h": len(meas["results"]), "period_intervals": dict(periods),
                              "keys": sorted(meas["results"][0].keys()) if meas["results"] else []}
    else:
        ep["measurements"] = {"error": err}

    hours, err = try_get(client, f"/sensors/{sensor['id']}/hours", {
        "datetime_from": iso(NOW - timedelta(hours=48)), "datetime_to": iso(NOW), "limit": 100})
    if hours:
        save_sample("sensor_hours", hours, max_results=3)
        res = hours["results"]
        first = res[0] if res else {}
        ep["hours"] = {
            "rows_last_48h": len(res),
            "meta_found": hours["meta"].get("found"),
            "keys": sorted(first.keys()),
            "period_example": first.get("period"),
            "coverage_example": first.get("coverage"),
            "ordering": ("ascending" if len(res) > 1 and
                         utc_of(res[0]["period"]["datetimeFrom"]) < utc_of(res[-1]["period"]["datetimeFrom"])
                         else "descending_or_single"),
            "latest_hour_from": (max((utc_of(r["period"]["datetimeFrom"]) for r in res), default=None)),
        }
    else:
        ep["hours"] = {"error": err}

    # /days: which window parameters are honoured? Record, don't assume.
    req_from = (NOW - timedelta(days=10)).date()
    for label, params in {
        "datetime_from": {"datetime_from": iso(NOW - timedelta(days=10)), "limit": 5},
        "date_from/date_to": {"date_from": req_from.isoformat(),
                              "date_to": NOW.date().isoformat(), "limit": 100},
    }.items():
        body, err = try_get(client, f"/sensors/{sensor['id']}/days", params)
        if not body:
            ep[f"days[{label}]"] = {"error": err}
            continue
        res = body["results"]
        first_from = utc_of(res[0]["period"]["datetimeFrom"]) if res else None
        ep[f"days[{label}]"] = {
            "rows": len(res),
            "first_period_from": first_from,
            "honoured": bool(first_from and first_from.date() >= req_from - timedelta(days=1)),
        }
        if label == "date_from/date_to":
            save_sample("sensor_days", body, max_results=2)
            if res:
                ep["days_keys"] = sorted(res[0].keys())
                ep["days_period_example"] = res[0].get("period")
                ep["days_coverage_example"] = res[0].get("coverage")
    facts["endpoints"] = ep


def explore_parameter_latest(client: OpenAQClient, facts: dict, india_id: int,
                             param_ids: dict[str, int], india_location_ids: set[int]) -> dict:
    """Test whether /parameters/{id}/latest can be restricted to India, and at what cost."""
    out: dict[str, Any] = {}
    pm25 = param_ids.get("pm25")
    for label, extra in {"no_filter": {}, "iso=IN": {"iso": "IN"},
                         f"countries_id={india_id}": {"countries_id": india_id}}.items():
        body, err = try_get(client, f"/parameters/{pm25}/latest", {**extra, "limit": 1000, "page": 1})
        if body:
            res = body["results"]
            in_india = sum(1 for r in res if r.get("locationsId") in india_location_ids)
            out[label] = {"meta_found": body["meta"].get("found"), "returned": len(res),
                          "rows_at_indian_locations": in_india}
            if label == "no_filter":
                save_sample("parameter_latest", body, max_results=3)
        else:
            out[label] = {"error": err}
    facts["parameter_latest"] = out
    return out


def benchmark(client: OpenAQClient, facts: dict, bench_locs: list[dict],
              india_filter_param: dict | None, param_ids: dict[str, int]) -> None:
    head: datetime = facts["locations"]["main_provider_head"]
    window_start = head - timedelta(hours=BENCH_WINDOW_HOURS)
    window_end = head + timedelta(minutes=1)
    listed_sensors = [(l["id"], s["id"], s["parameter"]["name"])
                      for l in bench_locs for s in l["sensors"]
                      if s["parameter"]["name"] in TARGET_PARAMS]
    bench: dict[str, Any] = {"locations": [l["id"] for l in bench_locs],
                             "listed_target_sensors": len(listed_sensors),
                             "feed_live": NOW - head <= timedelta(hours=3),
                             "window_utc": [iso(window_start), iso(head)], "run_at_utc": iso(NOW)}

    # --- A: /locations/{id}/latest (1 request per location)
    r0, t0 = client.request_count, time.monotonic()
    latest_by_sensor: dict[int, datetime] = {}
    latest_value: dict[int, float] = {}
    for l in bench_locs:
        body = client.get(f"/locations/{l['id']}/latest")
        for r in body["results"]:
            latest_by_sensor[r["sensorsId"]] = parse_ts(r["datetime"]["utc"])
            latest_value[r["sensorsId"]] = r["value"]
    # "Live" = the sensor's own /latest timestamp falls inside the window. Legacy
    # sensors (same parameter name, dead series) drop out here, not by name.
    target_sensors = [x for x in listed_sensors
                      if (t := latest_by_sensor.get(x[1])) and t >= window_start]
    bench["live_target_sensors"] = len(target_sensors)
    bench["live_target_sensors_per_location"] = round(len(target_sensors) / len(bench_locs), 2)
    bench["live_parameters"] = dict(Counter(p for _, _, p in target_sensors))
    bench["A_location_latest"] = {
        "requests": client.request_count - r0, "seconds": round(time.monotonic() - t0, 1),
        "values_per_sensor_per_request": 1,
        "latest_age_vs_head_minutes": dict(Counter(
            round((head - latest_by_sensor[sid]).total_seconds() / 60)
            for _, sid, _ in target_sensors).most_common(8)),
    }

    # --- B: /sensors/{id}/hours trailing window (1 request per live sensor)
    r0, t0 = client.request_count, time.monotonic()
    hours_found, dup_rows, lag_hours = [], 0, []
    latest_hour: dict[int, datetime] = {}
    value_match = value_checked = 0
    for _, sid, _ in target_sensors:
        body = client.get(f"/sensors/{sid}/hours",
                          {"datetime_from": iso(window_start), "datetime_to": iso(window_end), "limit": 100})
        starts = [utc_of(r["period"]["datetimeFrom"]) for r in body["results"]]
        dup_rows += len(starts) - len(set(starts))
        hours_found.append(len(set(starts)))
        if starts:
            latest_hour[sid] = max(starts)
            lag_hours.append((head - max(starts)).total_seconds() / 3600)
        # Is the /latest value the same number as the /hours row ending at that time?
        match = [r for r in body["results"]
                 if utc_of(r["period"]["datetimeTo"]) == latest_by_sensor.get(sid)]
        if match:
            value_checked += 1
            value_match += int(abs(match[0]["value"] - latest_value[sid]) < 1e-6)
    bench["B_sensor_hours_window"] = {
        "requests": client.request_count - r0, "seconds": round(time.monotonic() - t0, 1),
        "expected_hours_per_sensor": BENCH_WINDOW_HOURS,
        "sensors_with_any_hour": sum(1 for h in hours_found if h),
        "hours_returned_distribution": dict(Counter(hours_found)),
        "duplicate_hour_rows": dup_rows,
        "newest_hour_start_before_head_hours": dict(Counter(round(x, 2) for x in lag_hours)),
    }
    bench["latest_value_equals_hours_value"] = f"{value_match} / {value_checked}"

    # A vs B: how does the /latest timestamp relate to the newest /hours bucket?
    diffs = [round((latest_by_sensor[sid] - lh).total_seconds() / 60)
             for sid, lh in latest_hour.items() if sid in latest_by_sensor]
    bench["latest_datetime_minus_newest_hour_start_minutes"] = dict(Counter(diffs).most_common(10))

    # --- C: /parameters/{id}/latest with an India filter (few requests total)
    if india_filter_param is not None:
        r0, t0 = client.request_count, time.monotonic()
        c_seen: dict[int, datetime] = {}
        for pname in TARGET_PARAMS:
            pid = param_ids.get(pname)
            if pid is None:
                continue
            for r in client.paginate(f"/parameters/{pid}/latest", india_filter_param,
                                     limit=1000, max_pages=5):
                c_seen[r["sensorsId"]] = parse_ts(r["datetime"]["utc"])
        c_fresh = sum(1 for _, sid, _ in target_sensors if sid in c_seen)
        bench["C_parameter_latest"] = {
            "requests": client.request_count - r0, "seconds": round(time.monotonic() - t0, 1),
            "rows_total": len(c_seen),
            "bench_sensors_with_value_in_window": c_fresh,
            "note": "requests are for ALL of India, not just the bench locations",
        }
    facts["benchmark"] = bench


MW_NO, MW_NO2, MOLAR_VOLUME = 30.01, 46.01, 24.45
UNIT_CHECK_LOCATIONS = 45


def unit_identity_check(client: OpenAQClient, facts: dict, locations: list[dict]) -> None:
    """Infer the real NO/NO2 unit per station from the identity NOx = NO + NO2.

    Motivation: the 2025+ CPCB series labels no/no2/nox/so2/co as `ppb`, but the
    magnitudes don't fit (e.g. CO median < 1). CPCB reports NOx in ppb and NO/NO2
    in ug/m3. If NO/NO2 are really ug/m3, then  NOx_ppb ~= NO/1.227 + NO2/1.882.
    If they're really ppb, then NOx_ppb ~= NO + NO2. We assume OpenAQ's `nox`
    value is in ppm (x1000 -> ppb); the result below shows whether that holds.
    A ratio within 5% of 1.0 classifies the station.
    """
    head = facts["locations"]["main_provider_head"]
    main = facts["locations"]["main_provider"]
    pool = sorted((l for l in locations
                   if (l.get("provider") or {}).get("name") == main
                   and (t := utc_of(l.get("datetimeLast"))) and t == head
                   and {"no", "no2", "nox"} <= {s["parameter"]["name"] for s in l.get("sensors") or []}),
                  key=lambda l: l["id"])
    sample = pool[:: max(1, len(pool) // UNIT_CHECK_LOCATIONS)][:UNIT_CHECK_LOCATIONS]
    ppb_f_no, ppb_f_no2 = MW_NO / MOLAR_VOLUME, MW_NO2 / MOLAR_VOLUME
    rows, verdicts = [], Counter()
    for l in sample:
        body = client.get(f"/locations/{l['id']}/latest")
        by_sid = {r["sensorsId"]: r for r in body["results"]}
        vals = {}
        for s in l["sensors"]:
            p = s["parameter"]["name"]
            r = by_sid.get(s["id"])
            if p in ("no", "no2", "nox") and r and parse_ts(r["datetime"]["utc"]) == head:
                vals[p] = r["value"]
        if len(vals) < 3 or vals["nox"] <= 0 or vals["no"] + vals["no2"] <= 0:
            verdicts["insufficient"] += 1
            continue
        nox_ppb = vals["nox"] * 1000
        r_ppb = nox_ppb / (vals["no"] + vals["no2"])
        r_ug = nox_ppb / (vals["no"] / ppb_f_no + vals["no2"] / ppb_f_no2)
        v = ("ugm3" if abs(r_ug - 1) < 0.05 else "ppb" if abs(r_ppb - 1) < 0.05 else "ambiguous")
        verdicts[v] += 1
        rows.append({"location_id": l["id"], "name": l.get("name"), **vals,
                     "ratio_if_ppb": round(r_ppb, 3), "ratio_if_ugm3": round(r_ug, 3), "verdict": v})
    save_sample("unit_identity_check", {"results": rows}, max_results=len(rows))
    facts["unit_identity_check"] = {
        "locations_checked": len(sample), "at_time_utc": head, "verdicts": dict(verdicts),
        "stations_ppb": [r["location_id"] for r in rows if r["verdict"] == "ppb"],
        "examples": rows[:8],
    }


# ----------------------------------------------------------------- report
def render_markdown(f: dict) -> str:
    L = f["locations"]
    b = f.get("benchmark", {})
    lines = [
        "# OpenAQ v3 exploration: India",
        "",
        f"_Generated by `python -m scripts.explore_api` at {f['run_at_utc']} UTC. "
        "Every figure below was measured in that run. Re-run to refresh._",
        "",
        "## 1. Country",
        "",
        f"- India: `id={f['country']['id']}`, `code={f['country']['code']}`, name `{f['country']['name']}`",
        f"- Country coverage: {f['country']['datetimeFirst']} to {f['country']['datetimeLast']}",
        f"- Parameters listed for India: {', '.join(p for p in f['country']['parameters'] if p)}",
        "",
        "### Location filter behaviour (`meta.found` with limit=1)",
        "",
        "| Filter | meta.found | Error |",
        "|---|---|---|",
        *[f"| `{k}` | {v['meta_found']} | {v['error'] or ''} |" for k, v in f["location_filters"].items()],
        "",
        f"Country codes present in the full `iso=IN` pull: `{L['country_codes_in_result']}`",
        "",
        "## 2. Parameters (target pollutants)",
        "",
        "| id | name | units | displayName |",
        "|---:|---|---|---|",
        *[f"| {p['id']} | {p['name']} | {p['units']} | {p['displayName']} |" for p in f["parameters"]],
        "",
        "### Parameter/unit combinations on Indian fixed reference monitors (sensor count)",
        "",
        "| parameter | units | parameter id | sensors |",
        "|---|---|---:|---:|",
        *[f"| {p} | {u} | {i} | {c} |" for p, u, i, c in L["sensor_parameter_units"]],
        "",
        "## 3. Locations",
        "",
        f"- Total Indian locations (`iso=IN`): **{L['total']}**",
        f"- `isMonitor = true`: **{L['isMonitor_true']}** · `false`: {L['isMonitor_false']}",
        f"- `isMobile = true`: {L['isMobile_true']}",
        f"- Fixed reference monitors (`isMonitor` and not `isMobile`): **{L['fixed_monitors']}**",
        f"- Newest `datetimeLast` across fixed monitors (feed head): **{L['feed_head_utc']}**",
        f"- Newest `datetimeLast` per provider: `{json.dumps(L['provider_head_utc'])}`",
        f"- `datetimeLast` age buckets (fixed monitors): `{L['fixed_monitor_datetimeLast_age']}`",
        f"- Locations listing >1 sensor for the same target parameter: `{L['locations_with_multiple_sensors_per_parameter']}`",
        f"- Fixed monitors with `datetimeLast` in last 24 h: **{L['fixed_monitors_active_24h']}** · last 7 d: {L['fixed_monitors_active_7d']} · never: {L['fixed_monitors_never_reported']}",
        f"- Target-pollutant sensors on monitors active in last 24 h: **{L['active_24h_target_sensors']}**",
        f"- Target sensors per fixed monitor: {L['target_sensors_per_fixed_monitor']}",
        f"- All locations (incl. low-cost) active in last 24 h: {L['all_active_24h']}",
        "",
        "### Providers (fixed monitors)",
        "",
        *[f"- {name}: {n}" for name, n in L["providers_fixed_monitors"]],
        "",
        "### Providers (non-monitor / low-cost locations)",
        "",
        *[f"- {name}: {n}" for name, n in L["providers_non_monitors"]],
        "",
        "### Owners (fixed monitors)",
        "",
        *[f"- {name}: {n}" for name, n in L["owners_fixed_monitors"]],
        "",
        "### Time zones",
        "",
        *[f"- `{tz}`: {n}" for tz, n in L["timezones"]],
        "",
        "### Is `locality` usable as city?",
        "",
        f"- `locality` populated on fixed monitors: {L['locality_filled_fixed_monitors']} ({L['locality_distinct']} distinct values)",
        "- Most frequent values: " + ", ".join(f"{k} ({v})" for k, v in L["locality_examples"]),
        "",
        "Sample of name vs locality:",
        "",
        "| name | locality |",
        "|---|---|",
        *[f"| {r['name']} | {r['locality']} |" for r in L["name_with_locality_examples"]],
        "",
        f"Location object keys: `{', '.join(L['location_keys'])}`",
        "",
        f"Sensor object keys: `{', '.join(L['sensor_keys'])}`",
        "",
        "## 4. Pagination",
        "",
        "| page | returned | meta |",
        "|---:|---:|---|",
        *[f"| {p['page']} | {p['returned']} | `{json.dumps(p['meta'])}` |" for p in f["pagination"]["pages"]],
        "",
        f"- Page beyond the end: `{json.dumps(f['pagination']['page_beyond_end'])}`",
        f"- `limit=1001`: `{json.dumps(f['pagination']['limit_1001'])}`",
        "",
        "## 5. Rate-limit headers",
        "",
        f"Last observed: `{json.dumps(f['rate_limit_headers'])}`",
        "",
        "## 6. Endpoint behaviour (single sensor)",
        "",
        f"```json\n{json.dumps(f['endpoints'], indent=2, default=str)}\n```",
        "",
        "## 7. `/parameters/{id}/latest` filter test (PM2.5)",
        "",
        f"```json\n{json.dumps(f['parameter_latest'], indent=2, default=str)}\n```",
        "",
        f"## 8. Hourly-acquisition benchmark ({len(b.get('locations', []))} {L['main_provider']} monitors, "
        f"{BENCH_WINDOW_HOURS} h ending at the {L['main_provider']} feed head)",
        "",
        f"```json\n{json.dumps(b, indent=2, default=str)}\n```",
        "",
        "### Extrapolation to all active fixed monitors",
        "",
        f"- Fixed monitors with data in last 7 d: {L['fixed_monitors_active_7d']}",
        f"- A (`/locations/{{id}}/latest`): ~{L['fixed_monitors_active_7d']} requests per run (1 per location)",
        f"- B (`/sensors/{{id}}/hours` window): ~{round(L['fixed_monitors_active_7d'] * b.get('live_target_sensors_per_location', 0))} requests per run (1 per live sensor)",
        f"- C (`/parameters/{{id}}/latest` + India filter): {b.get('C_parameter_latest', {}).get('requests', 'n/a')} requests per run",
        f"- Internal budget: ≤ 600 requests per hourly run, ≤ 50/min",
        "",
        "## 9. Unit check: NOx = NO + NO2 identity (per station)",
        "",
        "See `unit_identity_check()` docstring for the method.",
        "",
        f"```json\n{json.dumps(f['unit_identity_check'], indent=2, default=str)}\n```",
        "",
        f"## 10. Requests used by this exploration run: {f['client_stats']['requests']} "
        f"(retries: {f['client_stats']['retries']})",
        "",
    ]
    return "\n".join(lines)


HAND_WRITTEN_MARKER = "<!-- HAND-WRITTEN BELOW: preserved when explore_api.py re-runs -->"


def write_report(markdown: str) -> None:
    """Regenerate the measured part; keep the hand-written analysis below the marker."""
    path = DOCS_DIR / "openaq_exploration.md"
    tail = f"\n{HAND_WRITTEN_MARKER}\n"
    if path.exists():
        old = path.read_text(encoding="utf-8")
        if HAND_WRITTEN_MARKER in old:
            tail = "\n" + HAND_WRITTEN_MARKER + old.split(HAND_WRITTEN_MARKER, 1)[1]
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown + tail, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # urllib3 debug logs would include full URLs; keep them quiet.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    client = OpenAQClient(get_settings().require_openaq_key(), max_requests=EXPLORATION_BUDGET)
    facts: dict[str, Any] = {"run_at_utc": iso(NOW)}

    log.info("countries...")
    india_id = explore_countries(client, facts)
    log.info("parameters...")
    by_name = explore_parameters(client, facts)
    log.info("locations...")
    locations = explore_locations(client, facts, india_id)
    summarize_locations(locations, facts)

    # Parameter ids actually used by Indian monitors (most common per name).
    usage = Counter()
    for p, _u, pid, c in facts["locations"]["sensor_parameter_units"]:
        usage[(p, pid)] += c
    param_ids: dict[str, int] = {}
    for (p, pid), _c in usage.most_common():
        param_ids.setdefault(p, pid)
    for name in TARGET_PARAMS:  # fall back to the /parameters catalogue
        if name not in param_ids and by_name.get(name):
            param_ids[name] = by_name[name][0]["id"]
    facts["param_ids_used"] = param_ids

    # Benchmark on the dominant provider's newest window (CPCB is most monitors).
    main = facts["locations"]["main_provider"]
    bench_locs = pick_bench_locations(
        [l for l in locations if (l.get("provider") or {}).get("name") == main],
        facts["locations"]["main_provider_head"])
    if not bench_locs:
        raise SystemExit("No fixed monitor reported in the benchmark window; re-run later.")

    log.info("single-endpoint checks...")
    explore_single_endpoints(client, facts, bench_locs[0])

    log.info("parameter latest filter test...")
    india_ids = {l["id"] for l in locations}
    pl = explore_parameter_latest(client, facts, india_id, param_ids, india_ids)
    india_filter = None
    for label, params in (("iso=IN", {"iso": "IN"}), (f"countries_id={india_id}", {"countries_id": india_id})):
        r = pl.get(label, {})
        if r.get("returned") and r.get("rows_at_indian_locations") == r.get("returned"):
            india_filter = params
            break
    facts["parameter_latest_india_filter"] = india_filter

    log.info("benchmark...")
    benchmark(client, facts, bench_locs, india_filter, param_ids)

    log.info("unit identity check...")
    unit_identity_check(client, facts, locations)

    facts["rate_limit_headers"] = client.rate_limit
    facts["client_stats"] = client.stats()

    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    (SAMPLES_DIR / "exploration_facts.json").write_text(
        json.dumps(facts, indent=2, default=str), encoding="utf-8")
    write_report(render_markdown(facts))
    log.info("done: %s", client.stats())


if __name__ == "__main__":
    main()
