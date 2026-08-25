"""Tests du moteur de génération indépendant de l'interface Qt."""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from prog_vol.autonomy import BatteryProfile, BatteryState, plan_mission_split
from prog_vol.generator import (
    GeneratedRoute,
    GenerationError,
    MissionParameters,
    PassRange,
    generate_mission_parts,
    generate_mission,
    generate_route_polygon,
    generate_waypoints_polygon,
)
from prog_vol.missions import inspect_mission


POLYGON = [
    (44.8000, -0.6000),
    (44.8000, -0.5988),
    (44.8010, -0.5988),
    (44.8010, -0.6000),
]

SPLIT_WAYPOINTS = (
    (44.8000, -0.6000, 30.0),
    (44.8000, -0.5900, 30.0),
    (44.8001, -0.6000, 30.0),
    (44.8001, -0.5900, 30.0),
)


class GenerationTests(unittest.TestCase):
    @staticmethod
    def split_route_and_plan():
        route = GeneratedRoute(
            SPLIT_WAYPOINTS,
            (PassRange(0, 2), PassRange(2, 4)),
            100,
            80,
        )
        profile = BatteryProfile("Test", 1000, 1, 210)
        plan = plan_mission_split(
            route.waypoints,
            route.pass_end_indices,
            10,
            (BatteryState("B1", profile), BatteryState("B2", profile)),
            home_point=SPLIT_WAYPOINTS[0][:2],
            reserve_percent=0,
        )
        return route, plan

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
        self.assertGreater(result.flight_estimate.route_distance_m, 0)
        self.assertGreater(result.flight_estimate.route_duration_s, 0)

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

    def test_home_is_included_in_estimate_without_changing_waypoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            without_home = generate_mission(
                POLYGON,
                MissionParameters(frontal_overlap=0.6, lateral_overlap=0.6),
                Path(directory) / "without-home.kmz",
            )
            with_home = generate_mission(
                POLYGON,
                MissionParameters(frontal_overlap=0.6, lateral_overlap=0.6),
                Path(directory) / "with-home.kmz",
                home_point=(44.7995, -0.6005),
            )

        self.assertEqual(without_home.waypoints, with_home.waypoints)
        self.assertFalse(without_home.flight_estimate.complete)
        self.assertTrue(with_home.flight_estimate.complete)
        self.assertGreater(
            with_home.flight_estimate.estimated_duration_s,
            without_home.flight_estimate.estimated_duration_s,
        )

    def test_wpml_contains_computed_distance_and_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = generate_mission(
                POLYGON,
                MissionParameters(frontal_overlap=0.6, lateral_overlap=0.6),
                Path(directory) / "metrics.kmz",
            )
            with zipfile.ZipFile(result.output_path) as archive:
                waylines = archive.read("wpmz/waylines.wpml").decode("utf-8")

        self.assertNotIn("<wpml:distance>0</wpml:distance>", waylines)
        self.assertNotIn("<wpml:duration>0</wpml:duration>", waylines)

    def test_generated_route_preserves_pass_boundaries(self) -> None:
        route = generate_route_polygon(
            POLYGON,
            MissionParameters(frontal_overlap=0.6, lateral_overlap=0.6),
        )

        self.assertEqual(route.pass_end_indices[-1], len(route.waypoints))
        self.assertEqual(route.line_count, len(route.passes))
        self.assertTrue(all(item.start_index < item.end_index for item in route.passes))

    def test_generates_and_validates_numbered_split_missions(self) -> None:
        route, plan = self.split_route_and_plan()
        with tempfile.TemporaryDirectory() as directory:
            generated = generate_mission_parts(
                route,
                MissionParameters(altitude=30, drone_speed=10),
                Path(directory) / "mission.kmz",
                plan,
            )
            names = [result.output_path.name for result in generated.results]
            archives = [inspect_mission(result.output_path) for result in generated.results]
            with zipfile.ZipFile(generated.results[0].output_path) as archive:
                waylines = archive.read("wpmz/waylines.wpml").decode("utf-8")

        self.assertEqual(
            names,
            ["mission_partie_01_sur_02.kmz", "mission_partie_02_sur_02.kmz"],
        )
        self.assertEqual([archive.waypoint_count for archive in archives], [2, 2])
        self.assertIn("<wpml:index>0</wpml:index>", waylines)
        self.assertIn("<wpml:finishAction>goHome</wpml:finishAction>", waylines)

    def test_split_generation_preserves_existing_files_on_validation_failure(self) -> None:
        route, plan = self.split_route_and_plan()
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "mission_partie_01_sur_02.kmz"
            second = Path(directory) / "mission_partie_02_sur_02.kmz"
            first.write_bytes(b"existing one")
            second.write_bytes(b"existing two")

            with patch("prog_vol.generator.inspect_mission", side_effect=RuntimeError("invalid")):
                with self.assertRaises(RuntimeError):
                    generate_mission_parts(
                        route,
                        MissionParameters(altitude=30, drone_speed=10),
                        Path(directory) / "mission.kmz",
                        plan,
                    )

            self.assertEqual(first.read_bytes(), b"existing one")
            self.assertEqual(second.read_bytes(), b"existing two")

    def test_split_generation_restores_all_files_after_partial_publication(self) -> None:
        route, plan = self.split_route_and_plan()
        real_replace = os.replace
        replace_count = 0

        def fail_during_publication(source, destination):
            nonlocal replace_count
            replace_count += 1
            if replace_count == 4:
                raise OSError("publication interrupted")
            return real_replace(source, destination)

        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "mission_partie_01_sur_02.kmz"
            second = Path(directory) / "mission_partie_02_sur_02.kmz"
            first.write_bytes(b"existing one")
            second.write_bytes(b"existing two")

            with patch("prog_vol.generator.os.replace", side_effect=fail_during_publication):
                with self.assertRaises(OSError):
                    generate_mission_parts(
                        route,
                        MissionParameters(altitude=30, drone_speed=10),
                        Path(directory) / "mission.kmz",
                        plan,
                    )

            self.assertEqual(first.read_bytes(), b"existing one")
            self.assertEqual(second.read_bytes(), b"existing two")
            self.assertFalse(any(Path(directory).glob(".*.bak")))
            self.assertFalse(any(Path(directory).glob("*.tmp.kmz")))


if __name__ == "__main__":
    unittest.main()
