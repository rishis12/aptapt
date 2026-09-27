"""Loads buildings + gazetteer for a city. "madison" is the real app data; "river_north" is
the Chicago test/dev fixture used by tests and scripts/try_ai_search.py. The Flask app only
ever loads "madison" -- river_north data must never reach the app DB.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_PATHS = {
    "madison": {
        "buildings": REPO_ROOT / "app" / "data" / "buildings.json",
        "gazetteer": REPO_ROOT / "app" / "data" / "gazetteer" / "madison.json",
    },
    "river_north": {
        "buildings": REPO_ROOT / "tests" / "fixtures" / "river_north" / "buildings.json",
        "gazetteer": REPO_ROOT / "tests" / "fixtures" / "river_north" / "gazetteer.json",
    },
}

CITIES = tuple(_PATHS)


class UnknownCityError(Exception):
    """Raised for any city other than the known fixture/app cities."""


def _paths_for(city):
    try:
        return _PATHS[city]
    except KeyError:
        raise UnknownCityError(f"Unknown city {city!r}. Known cities: {', '.join(CITIES)}.") from None


def load_buildings(city):
    """List of building dicts for the given city, in the shared schema (slug, name, kind,
    leasing, floor_plans, rent_min, rent_max, amenities, lat, lng, ...)."""
    return json.loads(_paths_for(city)["buildings"].read_text(encoding="utf-8"))


def load_gazetteer(city):
    """{place_key: {label, lat, lng, aliases}} for the given city."""
    return json.loads(_paths_for(city)["gazetteer"].read_text(encoding="utf-8"))
