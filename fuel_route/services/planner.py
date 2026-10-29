"""Plan a trip: geocode -> route (1 API call) -> stations on route -> cheapest fuel plan."""

from dataclasses import dataclass

from django.conf import settings
from django.db import DatabaseError

from . import geometry
from .geocoding import Location, geocode, get_place_index
from .optimizer import Candidate, Purchase, plan_fuel_stops
from .routing import Route, get_route
from .stations import StationOnRoute, StationsNotLoadedError, get_station_index

# ~50 m: plenty for drawing the route, and keeps coast-to-coast responses small.
RESPONSE_SIMPLIFY_TOLERANCE_DEG = 0.0005


@dataclass(frozen=True)
class TripPlan:
    start: Location
    finish: Location
    route: Route
    candidates: list[StationOnRoute]
    purchases: list[Purchase]
    tank_gallons: float
    mpg: float
    stop_penalty: float

    @property
    def external_api_calls(self) -> dict:
        return {
            "geocoding": sum(loc.source == "nominatim" for loc in (self.start, self.finish)),
            "routing": 0 if self.route.from_cache else 1,
        }

    def fuel_stops(self) -> list[dict]:
        stops = []
        for number, purchase in enumerate(self.purchases, start=1):
            on_route: StationOnRoute = purchase.candidate.station
            station = on_route.station
            stops.append(
                {
                    "stop_number": number,
                    "station_id": station.opis_id,
                    "name": station.name,
                    "address": station.address,
                    "city": station.city,
                    "state": station.state,
                    "lat": station.latitude,
                    "lon": station.longitude,
                    "price_per_gallon": round(on_route.price, 3),
                    "route_mile": round(purchase.candidate.mile, 1),
                    "miles_off_route": round(on_route.miles_off_route, 1),
                    "fuel_on_arrival_gallons": round(purchase.fuel_on_arrival, 2),
                    "gallons_purchased": round(purchase.gallons, 2),
                    "cost": round(purchase.cost, 2),
                }
            )
        return stops

    def as_dict(self) -> dict:
        gallons = sum(p.gallons for p in self.purchases)
        cost = sum(p.cost for p in self.purchases)
        simplified = geometry.simplify(self.route.coordinates, RESPONSE_SIMPLIFY_TOLERANCE_DEG)
        return {
            "start": self.start.as_dict(),
            "finish": self.finish.as_dict(),
            "summary": {
                "total_distance_miles": round(self.route.distance_miles, 1),
                "estimated_drive_time_hours": round(self.route.duration_seconds / 3600, 1),
                "total_fuel_cost": round(cost, 2),
                "total_gallons_purchased": round(gallons, 2),
                "fuel_used_gallons": round(self.route.distance_miles / self.mpg, 2),
                "average_price_per_gallon": round(cost / gallons, 3) if gallons else None,
                "number_of_fuel_stops": len(self.purchases),
                "stations_considered": len(self.candidates),
            },
            "fuel_stops": self.fuel_stops(),
            "assumptions": {
                "vehicle_range_miles": settings.VEHICLE_RANGE_MILES,
                "miles_per_gallon": self.mpg,
                "tank_capacity_gallons": self.tank_gallons,
                "starts_with_full_tank": True,
                "stop_penalty_usd": self.stop_penalty,
                "station_corridor_miles": settings.STATION_CORRIDOR_MILES,
            },
            "route": {
                "type": "Feature",
                "properties": {"source": "OSRM", "simplified_points": len(simplified)},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[round(lon, 5), round(lat, 5)] for lat, lon in simplified],
                },
            },
            "external_api_calls": self.external_api_calls,
        }


def plan_trip(
    start_query: str, finish_query: str, stop_penalty: float | None = None
) -> TripPlan:
    if stop_penalty is None:
        stop_penalty = settings.DEFAULT_STOP_PENALTY
    station_index = get_station_index()  # Fail fast, before any API calls.
    start, finish = geocode(start_query), geocode(finish_query)
    route = get_route(start, finish)

    mpg = settings.VEHICLE_MPG
    tank_gallons = settings.VEHICLE_RANGE_MILES / mpg
    stations = station_index.along_route(route.coordinates, settings.STATION_CORRIDOR_MILES)
    # Mile markers come from the polyline; rescale them to OSRM's road distance.
    polyline_miles = geometry.cumulative_miles(route.coordinates)[-1]
    scale = route.distance_miles / polyline_miles if polyline_miles else 1.0
    purchases = plan_fuel_stops(
        [Candidate(s.route_mile * scale, s.price, s) for s in stations],
        trip_miles=route.distance_miles,
        tank_gallons=tank_gallons,
        mpg=mpg,
        initial_gallons=tank_gallons,
        stop_penalty=stop_penalty,
    )
    return TripPlan(
        start, finish, route, stations, purchases, tank_gallons, mpg, stop_penalty
    )


def warm_up() -> None:
    """Build the in-memory indexes before the first request instead of during it."""
    get_place_index()
    try:
        get_station_index()
    except (DatabaseError, StationsNotLoadedError):
        pass  # Not migrated/loaded yet; the first request will report it.
