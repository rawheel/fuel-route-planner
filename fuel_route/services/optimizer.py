"""Cheapest refuelling plan along a fixed route.

Minimises ``fuel cost + stop_penalty * number_of_stops``. With no penalty the
pure price optimum happily stops for 1 gallon to save 2 cents; a small fixed
cost per stop (time, braking, the detour off the highway) gives plans a
dispatcher would actually accept, while still chasing cheap fuel.

Dynamic programme over the stations in route order. Fuel is measured in
whole miles of range (0.1 gal at 10 mpg), so the state is a vector of
``tank_miles + 1`` fuel levels and each station is a handful of numpy ops:

* drive to the station: shift the vector down by the leg length;
* buy any amount: ``best[f] = min_{g <= f}(cost[g] + (f - g) * price) + penalty``,
  which is a prefix-min of ``cost[g] - g * price`` - O(levels), not O(levels^2).

Total work is O(stations * tank_miles) - a few milliseconds coast to coast.
"""

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

# Ignore "savings" that are only floating-point noise.
EPSILON = 1e-9


@dataclass(frozen=True)
class Candidate:
    mile: float
    price: float
    station: Any = None


@dataclass(frozen=True)
class Purchase:
    candidate: Candidate
    gallons: float
    fuel_on_arrival: float

    @property
    def cost(self) -> float:
        return self.gallons * self.candidate.price


class NoFuelInRangeError(Exception):
    """Part of the route is longer than the vehicle's range and has no station."""

    def __init__(self, from_mile: float, to_mile: float, range_miles: float):
        self.from_mile = from_mile
        self.to_mile = to_mile
        super().__init__(
            f"No fuel station within {range_miles:g} miles after route mile "
            f"{from_mile:.0f} (next option is at mile {to_mile:.0f})."
        )


def _drive(cost: np.ndarray, miles: int) -> np.ndarray:
    """Arriving with fuel ``f`` means having left with ``f + miles``."""
    arrived = np.full_like(cost, np.inf)
    if miles < len(cost):
        arrived[: len(cost) - miles] = cost[miles:]
    return arrived


def plan_fuel_stops(
    candidates: Sequence[Candidate],
    trip_miles: float,
    tank_gallons: float,
    mpg: float,
    initial_gallons: float,
    stop_penalty: float = 0.0,
) -> list[Purchase]:
    tank_miles = round(tank_gallons * mpg)
    levels = np.arange(tank_miles + 1)
    destination = round(trip_miles)
    stations = sorted(
        (c for c in candidates if 0 <= c.mile <= trip_miles), key=lambda c: c.mile
    )
    positions = [min(round(c.mile), destination) for c in stations]

    # cost[f]: cheapest way to be at the current point holding f miles of fuel.
    cost = np.full(tank_miles + 1, np.inf)
    cost[min(round(initial_gallons * mpg), tank_miles)] = 0.0
    choices = []  # per station: (bought?, fuel on arrival) indexed by fuel on departure
    here = 0
    for station, position in zip(stations, positions):
        cost = _drive(cost, position - here)
        if not np.isfinite(cost).any():
            raise NoFuelInRangeError(here, position, tank_miles)
        here = position

        price_per_mile = station.price / mpg
        shifted = cost - levels * price_per_mile
        best = np.minimum.accumulate(shifted)
        arrival = np.maximum.accumulate(np.where(shifted == best, levels, 0))
        with_purchase = best + levels * price_per_mile + stop_penalty
        bought = with_purchase < cost - EPSILON
        cost = np.where(bought, with_purchase, cost)
        choices.append((bought, arrival))

    cost = _drive(cost, destination - here)
    if not np.isfinite(cost).any():
        raise NoFuelInRangeError(here, destination, tank_miles)

    # Walk back from the cheapest arrival state to recover the purchases.
    purchases = []
    fuel = int(np.argmin(cost)) + destination - here  # on leaving the last station
    for index in range(len(stations) - 1, -1, -1):
        bought, arrival = choices[index]
        if bought[fuel]:
            on_arrival = int(arrival[fuel])
            purchases.append(
                Purchase(stations[index], (fuel - on_arrival) / mpg, on_arrival / mpg)
            )
            fuel = on_arrival
        if index:
            fuel += positions[index] - positions[index - 1]
    purchases.reverse()
    return purchases
