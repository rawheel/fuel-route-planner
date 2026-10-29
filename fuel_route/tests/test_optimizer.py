import math
import random

from django.test import SimpleTestCase

from fuel_route.services.optimizer import Candidate, NoFuelInRangeError, plan_fuel_stops

TANK, MPG = 50, 10  # 500 mile range


def plan(stations, trip, penalty=0.0, initial=TANK):
    candidates = [Candidate(mile, price) for mile, price in stations]
    return plan_fuel_stops(candidates, trip, TANK, MPG, initial, penalty)


def reference_cost(stations, trip, initial=TANK):
    """Textbook greedy (optimal when stops are free), for cross-checking the DP."""
    stations = sorted(stations)
    positions = [0] + [m for m, _ in stations] + [trip]
    prices = [math.inf] + [p for _, p in stations] + [-math.inf]
    fuel, cost, i, reach = initial * MPG, 0.0, 0, TANK * MPG
    while i < len(positions) - 1:
        ahead = [j for j in range(i + 1, len(positions)) if positions[j] - positions[i] <= reach]
        if not ahead:
            raise NoFuelInRangeError(positions[i], positions[i + 1], reach)
        cheaper = [j for j in ahead if prices[j] < prices[i]]
        if i == 0:
            target, need = i + 1, 0
        elif cheaper:
            target = cheaper[0]
            need = max(positions[target] - positions[i] - fuel, 0)
        else:
            target = min(ahead, key=lambda j: (prices[j], -positions[j]))
            need = reach - fuel
        cost += need * prices[i] / MPG if need else 0
        fuel += need - (positions[target] - positions[i])
        if fuel < -1e-9:
            raise NoFuelInRangeError(positions[i], positions[target], reach)
        i = target
    return cost


def simulate(purchases, trip, initial=TANK):
    """Replay a plan and return the fuel level on arrival at each point (gallons)."""
    fuel, here, levels = initial, 0.0, []
    for p in purchases:
        fuel -= (p.candidate.mile - here) / MPG
        levels.append(fuel)
        fuel += p.gallons
        here = p.candidate.mile
        assert fuel <= TANK + 1e-9, "overfilled the tank"
    levels.append(fuel - (trip - here) / MPG)
    return levels


class PlanFuelStopsTests(SimpleTestCase):
    def test_trip_within_one_tank_needs_no_stops(self):
        self.assertEqual(plan([(100, 3.0), (300, 2.5)], trip=480), [])

    def test_buys_only_what_is_needed_to_finish(self):
        [stop] = plan([(400, 3.0)], trip=700)
        self.assertAlmostEqual(stop.gallons, 20)  # 300 miles left, 100 in the tank
        self.assertAlmostEqual(stop.fuel_on_arrival, 10)
        self.assertAlmostEqual(stop.cost, 60)

    def test_prefers_cheaper_station_within_range(self):
        [stop] = plan([(200, 4.0), (450, 3.0)], trip=900)
        self.assertEqual(stop.candidate.mile, 450)

    def test_fills_up_at_cheap_station_before_expensive_stretch(self):
        stops = plan([(300, 2.0), (700, 5.0)], trip=1000)
        self.assertEqual([s.candidate.mile for s in stops], [300, 700])
        self.assertAlmostEqual(stops[0].gallons, 30)  # top up to a full tank at $2
        self.assertAlmostEqual(stops[1].gallons, 20)  # only the remainder at $5

    def test_gap_longer_than_range_raises(self):
        with self.assertRaises(NoFuelInRangeError) as ctx:
            plan([(400, 3.0), (950, 3.0)], trip=1300)
        self.assertEqual(ctx.exception.from_mile, 400)

    def test_first_station_out_of_reach_raises(self):
        with self.assertRaises(NoFuelInRangeError):
            plan([(520, 3.0)], trip=900)

    def test_matches_reference_greedy_without_stop_penalty(self):
        rng = random.Random(7)
        for _ in range(200):
            trip = rng.randint(300, 3000)
            stations = sorted(
                {(rng.randint(0, trip), round(rng.uniform(2.5, 4.5), 3)) for _ in range(rng.randint(5, 80))}
            )
            try:
                expected = reference_cost(stations, trip)
            except NoFuelInRangeError:
                with self.assertRaises(NoFuelInRangeError):
                    plan(stations, trip)
                continue
            purchases = plan(stations, trip)
            self.assertAlmostEqual(sum(p.cost for p in purchases), expected, places=6)
            self.assertTrue(all(level >= -1e-9 for level in simulate(purchases, trip)))

    def test_stop_penalty_trades_pennies_for_fewer_stops(self):
        rng = random.Random(3)
        stations = [(m, round(rng.uniform(3.0, 3.1), 3)) for m in range(10, 2500, 15)]
        free = plan(stations, 2500, penalty=0)
        penalised = plan(stations, 2500, penalty=10)
        self.assertLess(len(penalised), len(free))
        self.assertLessEqual(sum(p.cost for p in free), sum(p.cost for p in penalised))
        self.assertTrue(all(level >= -1e-9 for level in simulate(penalised, 2500)))
        self.assertAlmostEqual(sum(p.gallons for p in penalised), (2500 - 500) / MPG)
