"""Regenerate data/city_mapping.csv from the current OpenAQ location list.

Run:  python -m scripts.generate_city_mapping

Uses 1 API request. Applies data/city_mapping_overrides.csv. Prints a summary
and the monitors left unmapped, so the owner can review the diff before
committing (`git diff data/city_mapping.csv`).
"""

from __future__ import annotations

import logging
from collections import Counter

from src.city_mapping import MAPPING_CSV, build_mapping, read_overrides, write_mapping
from src.config import get_settings
from src.openaq_client import OpenAQClient
from src.sync_locations import fetch_locations


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    client = OpenAQClient(settings.require_openaq_key(), max_requests=5)
    locations = fetch_locations(client, settings.include_low_cost)
    rows = build_mapping(locations, read_overrides())
    write_mapping(rows)

    print(f"wrote {MAPPING_CSV} ({len(rows)} locations)")
    print("mapping_source:", dict(Counter(r["mapping_source"] for r in rows)))
    cities = Counter((r["city"], r["state"]) for r in rows if r["state"])
    print(f"distinct city+state keys: {len(cities)}")
    dup = Counter(c for c, _ in cities)
    print("city names in more than one state:",
          {c: sorted(s for (cc, s) in cities if cc == c) for c, k in dup.items() if k > 1})
    print("unmapped:")
    for r in rows:
        if not r["state"]:
            print(f"  {r['location_id']:>8}  {r['location_name']}")


if __name__ == "__main__":
    main()
