from decimal import Decimal
from unittest import mock

import numpy as np
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from fuel_route.models import FuelStation
from fuel_route.services import routing
from fuel_route.services.stations import reset_station_index

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
START, FINISH = "40.0,-100.0", "40.0,-80.0"  # ~1,050 miles due east


def encode_polyline(coords, precision=6):
    out, previous = [], (0, 0)
    for point in np.round(np.asarray(coords) * 10**precision).astype(int):
        for value, last in zip(point, previous):
            delta = int(value - last)
            delta = ~(delta << 1) if delta < 0 else delta << 1
            while delta >= 0x20:
                out.append(chr((0x20 | (delta & 0x1F)) + 63))
                delta >>= 5
            out.append(chr(delta + 63))
        previous = point
    return "".join(out)


def osrm_response(distance_miles=1050.0):
    line = np.column_stack((np.full(201, 40.0), np.linspace(-100.0, -80.0, 201)))
    response = mock.Mock()
    response.json.return_value = {
        "code": "Ok",
        "routes": [{
            "distance": distance_miles * routing.METERS_PER_MILE,
            "duration": distance_miles / 60 * 3600,
            "geometry": encode_polyline(line),
        }],
    }  # fmt: skip
    return response


@override_settings(CACHES=LOCMEM)
class TripPlanApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        stations = [
            # opis_id, lon, price
            (1, -95.0, "3.50"),  # ~265 mi
            (2, -92.0, "2.90"),  # ~425 mi - cheapest within the first tank
            (3, -88.0, "3.80"),  # ~635 mi
            (4, -86.0, "3.10"),  # ~740 mi
            (5, -90.0, "1.00"),  # cheap, but 30+ miles off the route
        ]
        FuelStation.objects.bulk_create(
            FuelStation(
                opis_id=opis_id, name=f"Station {opis_id}", address="I-00 Exit 1",
                city="Town", state="KS", retail_price=Decimal(price),
                latitude=40.5 if opis_id == 5 else 40.0, longitude=lon,
            )
            for opis_id, lon, price in stations
        )  # fmt: skip

    def setUp(self):
        cache.clear()
        reset_station_index()
        patcher = mock.patch.object(routing.requests, "get", return_value=osrm_response())
        self.osrm = patcher.start()
        self.addCleanup(patcher.stop)

    def test_plans_cheapest_stops_with_one_routing_call(self):
        response = self.client.get(reverse("trip-plan"), {"start": START, "finish": FINISH})
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual([s["station_id"] for s in body["fuel_stops"]], [2, 4])
        self.assertEqual(body["summary"]["number_of_fuel_stops"], 2)
        self.assertAlmostEqual(body["summary"]["total_gallons_purchased"], 55, delta=0.2)
        self.assertAlmostEqual(
            body["summary"]["total_fuel_cost"],
            sum(s["cost"] for s in body["fuel_stops"]),
            delta=0.02,
        )
        self.assertEqual(body["summary"]["stations_considered"], 4)
        self.assertEqual(body["route"]["geometry"]["type"], "LineString")
        self.assertIn("/api/route/map/?", body["map_url"])
        self.assertEqual(body["external_api_calls"], {"geocoding": 0, "routing": 1})
        self.osrm.assert_called_once()

    def test_post_json_and_cached_route(self):
        payload = {"start": START, "finish": FINISH, "stop_penalty": 0}
        first = self.client.post(reverse("trip-plan"), payload, content_type="application/json")
        second = self.client.post(reverse("trip-plan"), payload, content_type="application/json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.json()["external_api_calls"]["routing"], 0)
        self.assertEqual(second.json()["assumptions"]["stop_penalty_usd"], 0)
        self.osrm.assert_called_once()

    def test_map_view_renders_without_extra_routing_calls(self):
        self.client.get(reverse("trip-plan"), {"start": START, "finish": FINISH})
        response = self.client.get(reverse("trip-map"), {"start": START, "finish": FINISH})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="plan-data"')
        self.assertContains(response, "Station 2")
        self.osrm.assert_called_once()

    def test_validation_errors(self):
        response = self.client.get(reverse("trip-plan"), {"start": START})
        self.assertEqual(response.status_code, 400)
        self.assertIn("finish", response.json())

        response = self.client.get(reverse("trip-plan"), {"start": "48.85,2.35", "finish": FINISH})
        self.assertEqual(response.status_code, 400)
        self.osrm.assert_not_called()

    def test_no_route(self):
        self.osrm.return_value.json.return_value = {"code": "NoRoute", "routes": []}
        response = self.client.get(reverse("trip-plan"), {"start": START, "finish": FINISH})
        self.assertEqual(response.status_code, 422)

    def test_routing_service_down(self):
        self.osrm.side_effect = routing.requests.ConnectionError("boom")
        response = self.client.get(reverse("trip-plan"), {"start": START, "finish": FINISH})
        self.assertEqual(response.status_code, 502)

    def test_unreachable_fuel(self):
        FuelStation.objects.filter(opis_id__in=[1, 2]).delete()
        reset_station_index()
        response = self.client.get(reverse("trip-plan"), {"start": START, "finish": FINISH})
        self.assertEqual(response.status_code, 422)
        self.assertIn("No fuel station within 500 miles", response.json()["detail"])

    def test_no_stations_loaded(self):
        FuelStation.objects.all().delete()
        reset_station_index()
        response = self.client.get(reverse("trip-plan"), {"start": START, "finish": FINISH})
        self.assertEqual(response.status_code, 503)
        self.osrm.assert_not_called()
