"""fetch_hourly tests with a fake client (no network, no database)."""

from datetime import datetime, timedelta, timezone

from src.fetch_hourly import (MAX_CATCHUP_HOURS, N_COHORTS, Target, catchup_start, cohort_for, fetch,
                              window)
from src.openaq_client import OpenAQError, RequestBudgetExceeded
from tests.conftest import load_station

END = datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, responses):
        self.responses = responses       # sensor_id -> body or exception
        self.calls = []

    def get(self, path, params=None):
        sid = int(path.split("/")[2])
        self.calls.append((sid, params))
        r = self.responses[sid]
        if isinstance(r, Exception):
            raise r
        return r


def test_cohorts_rotate_through_all_every_day():
    hours = [cohort_for(datetime(2026, 9, 28, h, 15, tzinfo=timezone.utc)) for h in range(24)]
    assert set(hours) == set(range(N_COHORTS))
    assert all(hours.count(c) == 24 // N_COHORTS for c in range(N_COHORTS))   # equal share


def test_window():
    start, end = window(END, 9)
    assert (end - start).total_seconds() == 9 * 3600 and end == END


def targets_from(station):
    return [Target(station["location_id"], s["sensor_id"], s["parameter"], s["source_units"],
                   station["gas_unit_convention"]) for s in station["sensors"]]


def test_fetch_parses_all_sensors():
    st = load_station("hours_ugm3_17.json")
    client = FakeClient({s["sensor_id"]: {"results": s["results"]} for s in st["sensors"]})
    res = fetch(client, targets_from(st), *window(END))
    assert res.sensors_ok == 6 and not res.errors and not res.budget_exhausted
    assert len(res.observations) == sum(len(s["results"]) for s in st["sensors"])
    assert client.calls[0][1]["datetime_from"] == "2026-09-24T09:00:00Z"


def test_one_sensor_error_does_not_stop_the_run():
    st = load_station("hours_ugm3_17.json")
    responses = {s["sensor_id"]: {"results": s["results"]} for s in st["sensors"]}
    bad = st["sensors"][0]["sensor_id"]
    responses[bad] = OpenAQError("HTTP 404 for /sensors", status=404)
    res = fetch(FakeClient(responses), targets_from(st), *window(END))
    assert res.sensors_ok == 5 and list(res.errors) == [bad]


def test_budget_exhaustion_stops_and_keeps_fetched_data():
    st = load_station("hours_ugm3_17.json")
    responses = {s["sensor_id"]: {"results": s["results"]} for s in st["sensors"]}
    third = st["sensors"][2]["sensor_id"]
    responses[third] = RequestBudgetExceeded("budget", status=None)
    client = FakeClient(responses)
    res = fetch(client, targets_from(st), *window(END))
    assert res.budget_exhausted and res.sensors_ok == 2 and len(client.calls) == 3
    assert len(res.observations) > 0


def test_empty_results_during_upstream_outage():
    t = [Target(1, 99, "pm25", "µg/m³", "ugm3")]
    res = fetch(FakeClient({99: {"results": []}}), t, *window(END))
    assert res.sensors_ok == 1 and res.observations == [] and not res.errors


def test_catchup_keeps_normal_window_for_up_to_date_station():
    start, end = window(END, 9)
    assert catchup_start(END - timedelta(hours=2), start, end) == start


def test_catchup_refetches_from_newest_stored_hour():
    start, end = window(END, 9)
    last = END - timedelta(days=3)
    assert catchup_start(last, start, end) == last


def test_catchup_is_capped():
    start, end = window(END, 9)
    floor = END - timedelta(hours=MAX_CATCHUP_HOURS)
    assert catchup_start(END - timedelta(days=30), start, end) == floor
    assert catchup_start(None, start, end) == floor


def test_fetch_uses_per_location_start():
    st = load_station("hours_ugm3_17.json")
    client = FakeClient({s["sensor_id"]: {"results": s["results"]} for s in st["sensors"]})
    earlier = END - timedelta(days=2)
    res = fetch(client, targets_from(st), *window(END), starts={st["location_id"]: earlier})
    assert res.sensors_ok == 6
    assert {c[1]["datetime_from"] for c in client.calls} == {"2026-09-22T18:00:00Z"}
