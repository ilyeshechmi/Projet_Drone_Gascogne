"""Tests du moteur de génération indépendant de l'interface Qt."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from prog_vol.generator import (
    GenerationError,
    MissionParameters,
    generate_mission,
    generate_waypoints_polygon,
)
from prog_vol.missions import inspect_mission


POLYGON = [
    (44.8000, -0.6000),
    (44.8000, -0.5988),
    (44.8010, -0.5988),
    (44.8010, -0.6000),
]


class GenerationTests(unittest.TestCase):
    def test_generated_mission_is_accepted_by_transfer_validator(self) -> None:
        parameters = MissionParameters(frontal_overlap=0.6, lateral_overlap=0.6)
        with tempfile.TemporaryDirectory() as directory:
            result = generate_mission(
                POLYGON,
                parameters,
                Path(directory) / "generated.kmz",
            )
            archive = inspect_mission(result.output_path)

        self.assertGreater(result.waypoint_count, 0)
        self.assertGreater(result.line_count, 0)
        self.assertEqual(archive.waypoint_count, result.waypoint_count)
        self.assertGreater(result.fov_width, 0)
        self.assertGreater(result.fov_height, 0)

    def test_output_extension_is_added(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = generate_mission(
                POLYGON,
                MissionParameters(),
                Path(directory) / "generated",
            )

            self.assertEqual(result.output_path.suffix, ".kmz")
            self.assertTrue(result.output_path.is_file())

    def test_rejects_polygon_with_less_than_three_vertices(self) -> None:
        with self.assertRaises(GenerationError):
            generate_waypoints_polygon(
                [(44.8, -0.6), (44.81, -0.59)],
                MissionParameters(),
            )

    def test_rejects_invalid_overlap(self) -> None:
        with self.assertRaises(GenerationError):
            generate_waypoints_polygon(
                POLYGON,
                MissionParameters(frontal_overlap=1),
            )

    def test_rejects_self_intersecting_polygon(self) -> None:
        with self.assertRaises(GenerationError):
            generate_waypoints_polygon(
                [
                    (44.8000, -0.6000),
                    (44.8010, -0.5990),
                    (44.8000, -0.5990),
                    (44.8010, -0.6000),
                ],
                MissionParameters(),
            )


if __name__ == "__main__":
    unittest.main()
