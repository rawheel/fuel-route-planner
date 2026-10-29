"""Resolve free-text US locations to coordinates.

Lookup order, cheapest first:

1. ``"lat,lon"`` literals.
2. ``"City, ST"`` / ``"City, State"`` against the bundled US Census gazetteer
   (``data/us_places.csv``). Offline and instant - no API call.
3. Nominatim (OpenStreetMap) for anything else, e.g. full street addresses.
   Restricted to the US and cached, so each distinct query costs one call ever.

The same offline index geocodes the fuel station CSV, whose rows only carry a
highway-exit style address plus city and state.
"""

import csv
import functools
import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass

import requests
from django.conf import settings
from django.core.cache import cache

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}  # fmt: skip
US_STATES = frozenset(STATE_NAMES.values())

# Generous boxes around the lower 48 and Alaska; Nominatim results are already
# restricted to the US, so this only guards raw coordinate input.
_US_BOUNDS = ((24.0, 50.0, -125.5, -66.5), (51.0, 72.0, -180.0, -129.5))

_COORDINATES = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_ABBREVIATIONS = {"saint": "st", "sainte": "ste", "fort": "ft", "mount": "mt"}


class GeocodingError(Exception):
    """The input can't be resolved to a US location."""


class GeocodingServiceError(GeocodingError):
    """The upstream geocoder failed."""


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float
    label: str
    source: str  # "coordinates" | "gazetteer" | "nominatim"

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_place_name(name: str) -> str:
    """Canonical form for matching: lower-case ASCII, no punctuation, common abbreviations."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9]+", " ", re.sub(r"[.'`]", "", name)).split()
    return " ".join(_ABBREVIATIONS.get(word, word) for word in words)


def is_in_us(lat: float, lon: float) -> bool:
    return any(s <= lat <= n and w <= lon <= e for s, n, w, e in _US_BOUNDS)


class PlaceIndex:
    """In-memory ``(city, state) -> (lat, lon)`` lookup built from the gazetteer.

    Keys are tried exactly and with spaces removed, which covers spellings like
    "Mc Calla"/"McCalla" and "De Forest"/"DeForest". Consolidated city-county
    names ("Nashville-Davidson") are also indexed under their first part.
    """

    def __init__(self, rows):
        self._exact: dict[tuple[str, str], tuple[float, float]] = {}
        self._compact: dict[tuple[str, str], tuple[float, float]] = {}
        aliases = []
        for name, state, lat, lon in rows:
            point = (float(lat), float(lon))
            self._add(normalize_place_name(name), state, point)
            if "-" in name:
                aliases.append((normalize_place_name(name.split("-")[0]), state, point))
        for key, state, point in aliases:
            self._add(key, state, point)

    def _add(self, key: str, state: str, point: tuple[float, float]) -> None:
        # Rows arrive in priority order, so the first one for a key wins.
        self._exact.setdefault((key, state), point)
        self._compact.setdefault((key.replace(" ", ""), state), point)

    def lookup(self, city: str, state: str) -> tuple[float, float] | None:
        key = normalize_place_name(city)
        state = state.strip().upper()
        return self._exact.get((key, state)) or self._compact.get(
            (key.replace(" ", ""), state)
        )

    def __len__(self) -> int:
        return len(self._exact)


def _read_rows(path, columns):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [tuple(row[c] for c in columns) for row in csv.DictReader(f)]


@functools.cache
def get_place_index() -> PlaceIndex:
    columns = ("name", "state", "lat", "lon")
    # Hand-resolved overrides take precedence over the gazetteer.
    return PlaceIndex(
        _read_rows(settings.DATA_DIR / "geocode_overrides.csv", columns)
        + _read_rows(settings.DATA_DIR / "us_places.csv", columns)
    )


def _split_city_state(query: str) -> tuple[str, str] | None:
    parts = [p.strip() for p in query.split(",")]
    if len(parts) == 3 and parts[2].lower() in {"us", "usa", "united states"}:
        parts = parts[:2]
    if len(parts) != 2 or not parts[0]:
        return None
    city, state = parts
    state = STATE_NAMES.get(state.lower(), state.upper())
    return (city, state) if state in US_STATES else None


def geocode(query: str) -> Location:
    query = " ".join(query.split())
    if not query:
        raise GeocodingError("Location is empty.")

    if match := _COORDINATES.match(query):
        lat, lon = float(match[1]), float(match[2])
        if not is_in_us(lat, lon):
            raise GeocodingError(f"Coordinates {query!r} are outside the USA.")
        return Location(lat, lon, query, "coordinates")

    if city_state := _split_city_state(query):
        if point := get_place_index().lookup(*city_state):
            return Location(*point, f"{city_state[0]}, {city_state[1]}", "gazetteer")

    return _geocode_with_nominatim(query)


def _geocode_with_nominatim(query: str) -> Location:
    cache_key = "geocode:" + hashlib.sha1(query.lower().encode()).hexdigest()
    if cached := cache.get(cache_key):
        return Location(**{**cached, "source": "cache"})

    try:
        response = requests.get(
            f"{settings.NOMINATIM_BASE_URL}/search",
            params={"q": query, "format": "jsonv2", "countrycodes": "us", "limit": 1},
            headers={"User-Agent": settings.EXTERNAL_API_USER_AGENT},
            timeout=settings.EXTERNAL_API_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        results = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise GeocodingServiceError(f"Geocoding service unavailable: {exc}") from exc

    if not results:
        raise GeocodingError(f"Could not find a US location matching {query!r}.")
    best = results[0]
    location = Location(
        float(best["lat"]), float(best["lon"]), best["display_name"], "nominatim"
    )
    cache.set(cache_key, location.as_dict())
    return location
