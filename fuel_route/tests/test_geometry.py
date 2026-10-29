import numpy as np
from django.test import SimpleTestCase

from fuel_route.services import geometry


class GeometryTests(SimpleTestCase):
    def test_decode_polyline(self):
        # Example from Google's polyline algorithm documentation (precision 5).
        coords = geometry.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
        np.testing.assert_allclose(coords, [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]])

    def test_haversine(self):
        # Chicago -> New York is ~711 miles great-circle.
        self.assertAlmostEqual(geometry.haversine_miles(41.8781, -87.6298, 40.7128, -74.0060), 711, delta=3)

    def test_densify_caps_segment_length(self):
        line = np.array([[40.0, -100.0], [40.0, -99.0], [40.0, -98.99]])
        dense = geometry.densify(line, 0.5)
        self.assertLessEqual(geometry.segment_miles(dense).max(), 0.5 + 1e-9)
        np.testing.assert_allclose(dense[[0, -1]], line[[0, -1]])
        self.assertAlmostEqual(
            geometry.cumulative_miles(dense)[-1], geometry.cumulative_miles(line)[-1], places=2
        )

    def test_chord_round_trip(self):
        self.assertAlmostEqual(float(geometry.chord_to_miles(geometry.miles_to_chord(10))), 10)

    def test_simplify_keeps_endpoints_and_corners(self):
        line = np.array([[0, 0], [0, 1], [0, 2], [1, 2], [2, 2]], dtype=float)
        np.testing.assert_allclose(geometry.simplify(line, 0.01), [[0, 0], [0, 2], [2, 2]])
