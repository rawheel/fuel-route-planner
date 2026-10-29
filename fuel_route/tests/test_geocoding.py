from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from fuel_route.services import geocoding
from fuel_route.services.geocoding import GeocodingError, PlaceIndex, geocode, normalize_place_name

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class PlaceIndexTests(SimpleTestCase):
    def test_normalization(self):
        self.assertEqual(normalize_place_name("St. Louis"), normalize_place_name("Saint Louis"))
        self.assertEqual(normalize_place_name("Cañon City"), "canon city")
        self.assertEqual(normalize_place_name("Fort Worth"), "ft worth")

    def test_lookup_variants(self):
        index = PlaceIndex([
            ("McCalla", "AL", "33.3", "-87.0"),
            ("Nashville-Davidson", "TN", "36.1", "-86.7"),
            ("Springfield", "IL", "39.8", "-89.6"),
            ("Springfield", "IL", "0", "0"),  # lower-priority duplicate
        ])  # fmt: skip
        self.assertEqual(index.lookup("Mc Calla", "al"), (33.3, -87.0))
        self.assertEqual(index.lookup("Nashville", "TN"), (36.1, -86.7))
        self.assertEqual(index.lookup("Springfield", "IL"), (39.8, -89.6))
        self.assertIsNone(index.lookup("Springfield", "MO"))


@override_settings(CACHES=LOCMEM)
class GeocodeTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_coordinates(self):
        location = geocode(" 41.88, -87.63 ")
        self.assertEqual((location.lat, location.lon, location.source), (41.88, -87.63, "coordinates"))

    def test_coordinates_outside_us(self):
        with self.assertRaises(GeocodingError):
            geocode("48.85,2.35")

    def test_city_state_uses_bundled_gazetteer(self):
        for query in ("Chicago, IL", "chicago, illinois", "Chicago, IL, USA"):
            location = geocode(query)
            self.assertEqual(location.source, "gazetteer")
            self.assertAlmostEqual(location.lat, 41.84, delta=0.1)

    @mock.patch.object(geocoding.requests, "get")
    def test_free_text_falls_back_to_nominatim_once(self, get):
        get.return_value.json.return_value = [
            {"lat": "38.8977", "lon": "-77.0365", "display_name": "White House"}
        ]
        first = geocode("1600 Pennsylvania Ave NW, Washington, DC")
        second = geocode("1600 pennsylvania ave nw, washington, dc")
        self.assertEqual((first.source, second.source), ("nominatim", "cache"))
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.kwargs["params"]["countrycodes"], "us")

    @mock.patch.object(geocoding.requests, "get")
    def test_unknown_place(self, get):
        get.return_value.json.return_value = []
        with self.assertRaises(GeocodingError):
            geocode("Atlantis, Ocean")
