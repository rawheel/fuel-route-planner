"""Find the fuel stations that lie along a route.

All ~6.5k stations are held in memory as numpy arrays (loaded from the DB once
per process). For a route, a KD-tree over the densified route vertices gives
each nearby station its distance from the road and its mile marker - a few
milliseconds even for a coast-to-coast trip.
"""

from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from fuel_route.models import FuelStation

from . import geometry

# Vertex spacing for the route KD-tree; bounds the mile-marker error at ~0.25 mi.
ROUTE_SAMPLE_MILES = 0.5


@dataclass(frozen=True)
class StationOnRoute:
    station: FuelStation
    price: float
    route_mile: float
    miles_off_route: float


class StationIndex:
    def __init__(self, stations: list[FuelStation]):
        self.stations = stations
        self.lat = np.array([s.latitude for s in stations], dtype=np.float64)
        self.lon = np.array([s.longitude for s in stations], dtype=np.float64)
        self.price = np.array([float(s.retail_price) for s in stations], dtype=np.float64)
        self.xyz = geometry.to_unit_vectors(self.lat, self.lon)

    def __len__(self) -> int:
        return len(self.stations)

    def along_route(self, coords: np.ndarray, corridor_miles: float) -> list[StationOnRoute]:
        if not self.stations or len(coords) < 2:
            return []

        # Cheap bounding-box prefilter before the exact KD-tree query.
        margin = corridor_miles / 69.0  # ~miles per degree of latitude
        lon_margin = margin / max(np.cos(np.radians(np.abs(coords[:, 0]).max())), 0.1)
        in_box = np.flatnonzero(
            (self.lat >= coords[:, 0].min() - margin)
            & (self.lat <= coords[:, 0].max() + margin)
            & (self.lon >= coords[:, 1].min() - lon_margin)
            & (self.lon <= coords[:, 1].max() + lon_margin)
        )
        if not len(in_box):
            return []

        samples = geometry.densify(coords, ROUTE_SAMPLE_MILES)
        sample_miles = geometry.cumulative_miles(samples)
        tree = cKDTree(geometry.to_unit_vectors(samples[:, 0], samples[:, 1]))
        chord, nearest = tree.query(
            self.xyz[in_box], distance_upper_bound=geometry.miles_to_chord(corridor_miles)
        )
        hit = np.isfinite(chord)
        off_route = geometry.chord_to_miles(chord[hit])
        return [
            StationOnRoute(
                station=self.stations[i],
                price=float(self.price[i]),
                route_mile=float(sample_miles[n]),
                miles_off_route=float(d),
            )
            for i, n, d in zip(in_box[hit], nearest[hit], off_route)
        ]


class StationsNotLoadedError(Exception):
    pass


_index: StationIndex | None = None


def get_station_index() -> StationIndex:
    global _index
    if _index is None:
        index = StationIndex(list(FuelStation.objects.all()))
        if not len(index):
            # Not cached, so the server picks the data up once it's loaded.
            raise StationsNotLoadedError(
                "No fuel stations loaded. Run `python manage.py load_fuel_stations`."
            )
        _index = index
    return _index


def reset_station_index() -> None:
    global _index
    _index = None
