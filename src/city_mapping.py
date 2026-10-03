"""Deterministic city/state mapping for Indian monitoring locations.

Phase 1 showed OpenAQ's `locality` is empty for 636/642 monitors, so the city
comes from the CPCB naming convention  "Site, City - AGENCY":

  1. Strip a trailing " - AGENCY" if AGENCY is a known agency code.
  2. City = last comma-separated part (or last " - " part) of what remains,
     after dropping a trailing state abbreviation ("Sector - 62, Noida, UP").
  3. State = the agency's state for state bodies (MPCB -> Maharashtra), because
     each AGENCY_STATE entry was confirmed against the OpenAQ `owner` name.
     National bodies (CPCB, IITM, IMD) and companies say nothing about the state,
     so their state comes from other stations in the same city (data-driven).
  4. Anything that can't be parsed stays NULL. Manual fixes go in
     data/city_mapping_overrides.csv, which the owner reviews. Nothing is guessed.

City + state together form the city key; city names are not unique in India.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from src.config import DATA_DIR

MAPPING_CSV = DATA_DIR / "city_mapping.csv"
OVERRIDES_CSV = DATA_DIR / "city_mapping_overrides.csv"

# State pollution bodies. Each entry is confirmed by the OpenAQ `owner` of at least
# one station carrying that suffix, or the suffix itself names the state
# ("Manipur PCB"). See docs/openaq_exploration.md, Phase 2 notes.
AGENCY_STATE: dict[str, str] = {
    "APPCB": "Andhra Pradesh",
    "APSPCB": "Arunachal Pradesh",
    "BSPCB": "Bihar",
    "CECB": "Chhattisgarh",
    "CPCC": "Chandigarh",
    "DPCC": "Delhi",
    "GPCB": "Gujarat",
    "HPPCB": "Himachal Pradesh",
    "HSPCB": "Haryana",
    "JKSPCB": "Jammu and Kashmir",
    "JSPCB": "Jharkhand",
    "KSPCB": "Karnataka",
    "Kerala PCB": "Kerala",
    "MPCB": "Maharashtra",
    "MPPCB": "Madhya Pradesh",
    "Manipur PCB": "Manipur",
    "Meghalaya PCB": "Meghalaya",
    "Mizoram PCB": "Mizoram",
    "NPCB": "Nagaland",
    "OSPCB": "Odisha",
    "PCBA": "Assam",
    "PPCB": "Punjab",
    "PPCC": "Puducherry",
    "RSPCB": "Rajasthan",
    "TNPCB": "Tamil Nadu",
    "TSPCB": "Telangana",
    "Tripura SPCB": "Tripura",
    "UKPCB": "Uttarakhand",
    "UPPCB": "Uttar Pradesh",
    "WBPCB": "West Bengal",
}

# Suffixes that are agencies but don't identify a state on their own (national
# bodies, municipal corporations, companies). State comes from the city lookup.
# Some (APCB, WBSPCB, SSPCB) have no confirming owner name in the data, so they're
# deliberately kept here rather than guessed into AGENCY_STATE.
OTHER_AGENCIES: set[str] = {
    "CPCB", "IITM", "IMD", "IITK", "IIT Kanpur", "APCB", "WBSPCB", "SSPCB", "ANPCC",
    "BMC", "IMC", "JMC", "SMC",
    "Ambuja Cements", "Bhilai Steel Plant", "Birla Cement", "Glenmark", "IPCA Lab",
    "KJS Cements", "Mondelez Ind. Food", "Nandesari Ind. Association",
}
KNOWN_AGENCIES = set(AGENCY_STATE) | OTHER_AGENCIES

# Trailing state abbreviations seen inside names, e.g. "Sector - 62, Noida, UP - IMD".
STATE_TOKENS = {"UP": "Uttar Pradesh"}

# Spelling variants of the same city. Keep minimal; each entry is visible in review.
CITY_ALIASES: dict[str, str] = {
    "New Delhi": "Delhi",
    "Bangalore": "Bengaluru",
    "Gurgaon": "Gurugram",
    # Variants observed in OpenAQ station names (Phase 2):
    "Kalaburgi": "Kalaburagi",
    "Tirupathi": "Tirupati",
    "Pimpri-Chinchwad": "Pimpri Chinchwad",
}

_AGENCY_RE = re.compile(r"^(?P<rest>.*?)\s*-\s*(?P<agency>[^-]+?)\s*$")


@dataclass(frozen=True)
class ParsedName:
    city: str | None
    agency: str | None


def normalize_city(city: str | None) -> str | None:
    if not city:
        return None
    city = re.sub(r"\s+", " ", city).strip(" ,-")
    return CITY_ALIASES.get(city, city) or None


def parse_location_name(name: str) -> ParsedName:
    """Extract (city, agency) from a CPCB-style station name. Pure function."""
    name = (name or "").strip()
    m = _AGENCY_RE.match(name)
    if not m or m.group("agency").strip() not in KNOWN_AGENCIES:
        return ParsedName(city=None, agency=None)
    agency = m.group("agency").strip()
    rest = m.group("rest").strip()

    parts = [p.strip() for p in rest.split(",") if p.strip()]
    if len(parts) >= 2 and parts[-1] in STATE_TOKENS:
        parts = parts[:-1]
    if len(parts) >= 2:
        return ParsedName(city=normalize_city(parts[-1]), agency=agency)

    # No comma: "Collectorate - Gaya - BSPCB" -> "Gaya". A single bare token
    # ("Haldia - WBSPCB", "Victoria Memorial - WBSPCB") is ambiguous: site or city?
    # Leave it for the owner rather than guessing.
    dash_parts = [p.strip() for p in re.split(r"\s+-\s+", rest) if p.strip()]
    if len(dash_parts) >= 2:
        return ParsedName(city=normalize_city(dash_parts[-1]), agency=agency)
    return ParsedName(city=None, agency=agency)


def read_overrides(path: Path = OVERRIDES_CSV) -> dict[int, dict[str, str | None]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {
            int(r["location_id"]): {"city": r["city"].strip() or None,
                                    "state": r["state"].strip() or None}
            for r in csv.DictReader(f)
        }


def build_mapping(locations: list[dict], overrides: dict[int, dict] | None = None) -> list[dict]:
    """Return one mapping row per location: location_id, location_name, city, state, mapping_source.

    `locations` are OpenAQ /locations objects (only `id` and `name` are used).
    """
    overrides = overrides or {}
    parsed = {l["id"]: parse_location_name(l["name"]) for l in locations}

    # City -> state evidence from state-body stations only.
    evidence: dict[str, Counter] = defaultdict(Counter)
    for p in parsed.values():
        if p.city and p.agency in AGENCY_STATE:
            evidence[p.city][AGENCY_STATE[p.agency]] += 1
    for o in overrides.values():
        if o["city"] and o["state"]:
            evidence[o["city"]][o["state"]] += 1
    # Only use the lookup when the city maps to exactly one state.
    city_state = {c: next(iter(s)) for c, s in evidence.items() if len(s) == 1}

    rows = []
    for l in sorted(locations, key=lambda l: l["id"]):
        lid, p = l["id"], parsed[l["id"]]
        if lid in overrides:
            city = normalize_city(overrides[lid]["city"])
            state = overrides[lid]["state"] or city_state.get(city)
            source = "override"
        elif p.city and p.agency in AGENCY_STATE:
            city, state, source = p.city, AGENCY_STATE[p.agency], "parsed_agency"
        elif p.city and p.city in city_state:
            city, state, source = p.city, city_state[p.city], "parsed_city_lookup"
        elif p.city:
            city, state, source = p.city, None, "unmapped_state"
        else:
            city, state, source = None, None, "unmapped"
        if not (city and state):  # a half mapping is useless for city+state rollups
            city = city if source == "unmapped_state" else None
        rows.append({"location_id": lid, "location_name": l["name"], "city": city,
                     "state": state, "mapping_source": source})
    return rows


def write_mapping(rows: list[dict], path: Path = MAPPING_CSV) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["location_id", "location_name", "city", "state",
                                          "mapping_source"])
        w.writeheader()
        w.writerows(rows)


def read_mapping(path: Path = MAPPING_CSV) -> dict[int, tuple[str | None, str | None]]:
    """location_id -> (city, state). Rows missing either value map to (None, None)."""
    if not path.exists():
        return {}
    out = {}
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            city, state = (r.get("city") or "").strip() or None, (r.get("state") or "").strip() or None
            out[int(r["location_id"])] = (city, state) if city and state else (None, None)
    return out
