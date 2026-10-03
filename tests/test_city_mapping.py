"""City/state mapping tests. Names are real OpenAQ station names seen in Phase 1/2."""

import pytest

from src.city_mapping import build_mapping, parse_location_name, read_mapping, write_mapping


@pytest.mark.parametrize("name, city, agency", [
    ("Anand Vihar, Delhi - DPCC", "Delhi", "DPCC"),
    ("ITO, New Delhi - CPCB", "Delhi", "CPCB"),                           # alias New Delhi -> Delhi
    ("IHBAS, Dilshad Garden,New Delhi - CPCB", "Delhi", "CPCB"),          # comma without space
    ("Sector - 62, Noida, UP - IMD", "Noida", "IMD"),                     # dash in site + state token
    ("Sector - 125, Noida, UP - UPPCB", "Noida", "UPPCB"),
    ("Corporation Ground, Thrissur- Kerala PCB", "Thrissur", "Kerala PCB"),  # no space before dash
    ("Collectorate - Gaya - BSPCB", "Gaya", "BSPCB"),                     # no comma, dash-separated
    ("Tata Stadium - Jorapokhar - JSPCB", "Jorapokhar", "JSPCB"),
    ("Talcher Coalfields,Talcher - OSPCB", "Talcher", "OSPCB"),
    ("Savta Mali Nagar, Pimpri-Chinchwad - IITM", "Pimpri Chinchwad", "IITM"),  # hyphen inside city
    ("Kalaburgi, Kalaburgi - KSPCB", "Kalaburagi", "KSPCB"),              # spelling alias
])
def test_parse_known_patterns(name, city, agency):
    p = parse_location_name(name)
    assert (p.city, p.agency) == (city, agency)


@pytest.mark.parametrize("name", [
    "Haldia - WBSPCB",               # single token: site or city? don't guess
    "Victoria Memorial - WBSPCB",
    "Collectorate Jodhpur - RSPCB",
])
def test_ambiguous_single_token_is_not_guessed(name):
    p = parse_location_name(name)
    assert p.city is None and p.agency is not None


@pytest.mark.parametrize("name", ["IGI Airport", "Lalbagh, DN Park", "US Diplomatic Post: Kolkata", "", "Chennai"])
def test_no_known_agency_suffix_returns_nothing(name):
    p = parse_location_name(name)
    assert p.city is None and p.agency is None


def locs(*pairs):
    return [{"id": i, "name": n} for i, n in pairs]


def by_id(rows):
    return {r["location_id"]: r for r in rows}


def test_state_from_agency():
    r = by_id(build_mapping(locs((1, "Bandra, Mumbai - MPCB"))))[1]
    assert (r["city"], r["state"], r["mapping_source"]) == ("Mumbai", "Maharashtra", "parsed_agency")


def test_national_agency_gets_state_from_city_lookup():
    rows = by_id(build_mapping(locs((1, "Bandra, Mumbai - MPCB"), (2, "Worli, Mumbai - IITM"))))
    assert (rows[2]["state"], rows[2]["mapping_source"]) == ("Maharashtra", "parsed_city_lookup")


def test_national_agency_without_evidence_has_no_state_and_no_city():
    r = by_id(build_mapping(locs((1, "Science Center, Surat - SMC"))))[1]
    assert r["state"] is None and r["mapping_source"] == "unmapped_state"
    assert read_mapping_roundtrip([r])[1] == (None, None)  # half mapping is excluded from rollups


def test_same_city_name_in_two_states_stays_distinct():
    rows = by_id(build_mapping(locs((1, "Waluj, Aurangabad - MPCB"), (2, "Gurdeo Nagar, Aurangabad - BSPCB"))))
    assert (rows[1]["city"], rows[1]["state"]) == ("Aurangabad", "Maharashtra")
    assert (rows[2]["city"], rows[2]["state"]) == ("Aurangabad", "Bihar")


def test_ambiguous_city_lookup_is_not_used():
    # Aurangabad exists in two states, so a CPCB station there can't borrow a state.
    rows = by_id(build_mapping(locs((1, "Waluj, Aurangabad - MPCB"),
                                    (2, "Gurdeo Nagar, Aurangabad - BSPCB"),
                                    (3, "Somewhere, Aurangabad - CPCB"))))
    assert rows[3]["state"] is None


def test_override_wins_and_can_borrow_state():
    rows = by_id(build_mapping(
        locs((1, "Bandra, Mumbai - MPCB"), (2, "Mumbai")),
        overrides={2: {"city": "Mumbai", "state": None}}))
    assert (rows[2]["city"], rows[2]["state"], rows[2]["mapping_source"]) == ("Mumbai", "Maharashtra", "override")


def read_mapping_roundtrip(rows, tmp=None):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "m.csv"
        write_mapping(rows, p)
        return read_mapping(p)


def test_csv_roundtrip():
    rows = build_mapping(locs((1, "Bandra, Mumbai - MPCB"), (2, "IGI Airport")))
    m = read_mapping_roundtrip(rows)
    assert m == {1: ("Mumbai", "Maharashtra"), 2: (None, None)}
