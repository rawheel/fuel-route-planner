"""Driving routes from OSRM (https://project-osrm.org) - free, no API key.

One HTTP call per distinct origin/destination pair; results are cached.
"""

from dataclasses import dataclass

import numpy as np
import requests
from django.conf import settings
from django.core.cache import cache

from . import geometry
from .geocoding import Location

METERS_PER_MILE = 1609.344


class RoutingError(Exception):
    pass


class NoRouteError(RoutingError):
    pass


@dataclass(frozen=True)
class Route:
    distance_miles: float
    duration_seconds: float
    coordinates: np.ndarray  # (N, 2) lat/lon along the road
    from_cache: bool = False


def get_route(start: Location, finish: Location) -> Route:
    waypoints = f"{start.lon:.5f},{start.lat:.5f};{finish.lon:.5f},{finish.lat:.5f}"
    cache_key = f"osrm:route:{waypoints}"
    if cached := cache.get(cache_key):
        return Route(**cached, from_cache=True)

    try:
        response = requests.get(
            f"{settings.OSRM_BASE_URL}/route/v1/driving/{waypoints}",
            params={"overview": "full", "geometries": "polyline6", "steps": "false"},
            headers={"User-Agent": settings.EXTERNAL_API_USER_AGENT},
            timeout=settings.EXTERNAL_API_TIMEOUT_SECONDS,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RoutingError(f"Routing service unavailable: {exc}") from exc

    code = payload.get("code")
    if code in {"NoRoute", "NoSegment"}:
        raise NoRouteError("No drivable route exists between these locations.")
    if code != "Ok" or not payload.get("routes"):
        raise RoutingError(f"Routing service error: {payload.get('message') or code}")

    best = payload["routes"][0]
    route = {
        "distance_miles": best["distance"] / METERS_PER_MILE,
        "duration_seconds": best["duration"],
        "coordinates": geometry.decode_polyline(best["geometry"], precision=6),
    }
    cache.set(cache_key, route)
    return Route(**route)
