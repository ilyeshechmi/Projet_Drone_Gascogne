"""Tests des transformations géométriques de zone."""

from __future__ import annotations

import unittest

from prog_vol.generator import MissionParameters, generate_route_polygon
from prog_vol.geometry import ZoneGeometryError, buffer_zone_points


POLYGON = (
    (44.8000, -0.6000),
    (44.8000, -0.5990),
    (44.8010, -0.5990),
    (44.8010, -0.6000),
)


class GeometryTests(unittest.TestCase):
    def test_zero_margin_preserves_original_points(self) -> None:
        self.assertEqual(buffer_zone_points(POLYGON, 0), POLYGON)

    def test_positive_margin_expands_bounds_and_generates_route(self) -> None:
        buffered = buffer_zone_points(POLYGON, 5)

        self.assertGreater(len(buffered), len(POLYGON))
        self.assertLess(min(point[0] for point in buffered), min(point[0] for point in POLYGON))
        self.assertGreater(max(point[0] for point in buffered), max(point[0] for point in POLYGON))
        self.assertLess(min(point[1] for point in buffered), min(point[1] for point in POLYGON))
        self.assertGreater(max(point[1] for point in buffered), max(point[1] for point in POLYGON))

        route = generate_route_polygon(
            buffered,
            MissionParameters(frontal_overlap=0.6, lateral_overlap=0.6),
        )

        self.assertGreater(len(route.waypoints), 0)
        self.assertGreater(route.line_count, 0)

    def test_negative_margin_is_rejected(self) -> None:
        with self.assertRaisesRegex(ZoneGeometryError, "positive ou nulle"):
            buffer_zone_points(POLYGON, -1)


if __name__ == "__main__":
    unittest.main()
