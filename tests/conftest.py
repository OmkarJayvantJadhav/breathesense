"""Shared test helpers. Fixtures are trimmed real /sensors/{id}/hours payloads
(tests/fixtures/hours_<convention>_<location_id>.json), fetched once from OpenAQ.
Unit tests never call the API or the database."""

import json
from pathlib import Path

import pytest

from src.transform import parse_hours

FIXTURES = Path(__file__).parent / "fixtures"


def load_station(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def observations_from(station: dict):
    obs = []
    for s in station["sensors"]:
        obs += parse_hours(s["results"], location_id=station["location_id"], sensor_id=s["sensor_id"],
                           parameter=s["parameter"], unit_label=s["source_units"],
                           convention=station["gas_unit_convention"])
    return obs


@pytest.fixture
def rk_puram():            # DPCC Delhi, classified ugm3
    return load_station("hours_ugm3_17.json")


@pytest.fixture
def jalandhar():           # PPCB Punjab, classified ppb
    return load_station("hours_ppb_5542.json")


@pytest.fixture
def vikas_sadan():         # HSPCB Gurugram, unclassified
    return load_station("hours_unclassified_301.json")
