"""Tests du calcul d'autonomie indépendant de l'interface graphique."""

from __future__ import annotations

import unittest

from prog_vol.autonomy import (
    AlertLevel,
    AutonomyError,
    BatteryProfile,
    BatteryState,
    DEFAULT_BATTERY_PROFILE,
    FlightEstimate,
    assess_batteries,
    custom_battery_profile,
    estimate_flight,
    plan_mission_split,
)


WAYPOINTS = (
    (44.8000, -0.6000, 50.0),
    (44.8000, -0.5990, 50.0),
    (44.8010, -0.5990, 50.0),
)

SPLIT_WAYPOINTS = (
    (44.8000, -0.6000, 30.0),
    (44.8000, -0.5900, 30.0),
    (44.8001, -0.6000, 30.0),
    (44.8001, -0.5900, 30.0),
)


class AutonomyTests(unittest.TestCase):
    @staticmethod
    def complete_estimate(duration_s: float) -> FlightEstimate:
        return FlightEstimate(
            route_distance_m=1000,
            transit_distance_m=200,
            route_duration_s=duration_s - 60,
            transit_duration_s=30,
            vertical_duration_s=30,
            estimated_duration_s=duration_s,
            photo_count=10,
            short_photo_intervals=0,
            complete=True,
        )

    @staticmethod
    def battery(name: str, safe_time_s: float) -> BatteryState:
        return BatteryState(
            name,
            BatteryProfile(name, 1000, 1, safe_time_s),
        )

    def test_default_battery_is_fully_charged_by_default(self) -> None:
        battery = BatteryState("Batterie 1", DEFAULT_BATTERY_PROFILE)

        self.assertEqual(battery.charge_percent, 100)
        self.assertAlmostEqual(battery.safe_available_time_s(20), 16.8 * 60)

    def test_reserve_is_a_final_charge_threshold(self) -> None:
        half_charged = BatteryState("Batterie 1", DEFAULT_BATTERY_PROFILE, 50)
        at_reserve = BatteryState("Batterie 2", DEFAULT_BATTERY_PROFILE, 20)

        self.assertAlmostEqual(half_charged.safe_available_time_s(20), 6.3 * 60)
        self.assertEqual(at_reserve.safe_available_time_s(20), 0)

    def test_custom_profile_computes_energy_and_estimates_duration(self) -> None:
        profile = custom_battery_profile("Test", 3000, 7.7)

        self.assertAlmostEqual(profile.effective_energy_wh, 23.1)
        self.assertTrue(profile.estimated_reference)
        self.assertGreater(profile.reference_flight_time_s, 21 * 60)

    def test_home_makes_estimate_complete(self) -> None:
        without_home = estimate_flight(WAYPOINTS, 5)
        with_home = estimate_flight(WAYPOINTS, 5, home_point=(44.7995, -0.6005))

        self.assertFalse(without_home.complete)
        self.assertTrue(with_home.complete)
        self.assertGreater(with_home.estimated_duration_s, without_home.estimated_duration_s)
        self.assertIsNotNone(with_home.transit_distance_m)

    def test_photo_cadence_detects_segments_that_are_too_short(self) -> None:
        waypoints = (
            (44.8, -0.6, 50.0),
            (44.8, -0.59999, 50.0),
        )

        estimate = estimate_flight(waypoints, 5, photo_interval_s=2)

        self.assertEqual(estimate.short_photo_intervals, 1)

    def test_assessment_recommends_a_sufficient_battery(self) -> None:
        estimate = estimate_flight(
            WAYPOINTS,
            5,
            home_point=(44.7995, -0.6005),
        )
        batteries = (
            BatteryState("Batterie 1", DEFAULT_BATTERY_PROFILE, 20),
            BatteryState("Batterie 2", DEFAULT_BATTERY_PROFILE, 100),
        )

        assessment = assess_batteries(estimate, batteries)

        self.assertEqual(assessment.level, AlertLevel.OK)
        self.assertEqual(assessment.recommended_battery.name, "Batterie 2")

    def test_missing_home_never_returns_ok(self) -> None:
        estimate = estimate_flight(WAYPOINTS, 5)

        assessment = assess_batteries(
            estimate,
            (BatteryState("Batterie 1", DEFAULT_BATTERY_PROFILE),),
        )

        self.assertEqual(assessment.level, AlertLevel.PARTIAL)

    def test_missing_home_is_critical_when_route_already_exceeds_safe_time(self) -> None:
        estimate = FlightEstimate(
            route_distance_m=1000,
            transit_distance_m=None,
            route_duration_s=1100,
            transit_duration_s=None,
            vertical_duration_s=None,
            estimated_duration_s=1100,
            photo_count=10,
            short_photo_intervals=0,
            complete=False,
        )

        assessment = assess_batteries(
            estimate,
            (BatteryState("Batterie 1", DEFAULT_BATTERY_PROFILE),),
        )

        self.assertEqual(assessment.level, AlertLevel.CRITICAL)
        self.assertLess(assessment.margin_s, 0)

    def test_warns_when_reserve_cannot_be_respected(self) -> None:
        assessment = assess_batteries(
            self.complete_estimate(1100),
            (BatteryState("Batterie 1", DEFAULT_BATTERY_PROFILE),),
        )

        self.assertEqual(assessment.level, AlertLevel.WARNING)
        self.assertEqual(assessment.minimum_battery_count, 1)

    def test_reports_minimum_multiple_batteries_without_claiming_continuity(self) -> None:
        batteries = (
            BatteryState("Batterie 1", DEFAULT_BATTERY_PROFILE),
            BatteryState("Batterie 2", DEFAULT_BATTERY_PROFILE),
        )

        assessment = assess_batteries(self.complete_estimate(1500), batteries)

        self.assertEqual(assessment.level, AlertLevel.WARNING)
        self.assertEqual(assessment.minimum_battery_count, 2)
        self.assertIn("atterrissage", assessment.message)

    def test_reports_insufficient_total_safe_charge(self) -> None:
        batteries = (
            BatteryState("Batterie 1", DEFAULT_BATTERY_PROFILE),
            BatteryState("Batterie 2", DEFAULT_BATTERY_PROFILE),
        )

        assessment = assess_batteries(self.complete_estimate(2500), batteries)

        self.assertEqual(assessment.level, AlertLevel.CRITICAL)
        self.assertIsNone(assessment.minimum_battery_count)

    def test_rejects_invalid_charge(self) -> None:
        estimate = estimate_flight(WAYPOINTS, 5)
        with self.assertRaises(AutonomyError):
            assess_batteries(
                estimate,
                (BatteryState("Batterie 1", DEFAULT_BATTERY_PROFILE, 101),),
            )

    def test_split_plan_keeps_complete_passes_when_possible(self) -> None:
        plan = plan_mission_split(
            SPLIT_WAYPOINTS,
            (2, 4),
            10,
            (self.battery("B1", 210), self.battery("B2", 210)),
            home_point=SPLIT_WAYPOINTS[0][:2],
            reserve_percent=0,
        )

        self.assertTrue(plan.possible)
        self.assertTrue(plan.requires_split)
        self.assertFalse(plan.uses_mid_pass_cut)
        self.assertEqual([(part.start_index, part.end_index) for part in plan.parts], [(0, 2), (2, 4)])
        self.assertTrue(all(part.margin_s >= 0 for part in plan.parts))

    def test_split_plan_cuts_inside_pass_only_as_fallback(self) -> None:
        plan = plan_mission_split(
            SPLIT_WAYPOINTS,
            (4,),
            10,
            (self.battery("B1", 210), self.battery("B2", 210)),
            home_point=SPLIT_WAYPOINTS[0][:2],
            reserve_percent=0,
        )

        self.assertTrue(plan.possible)
        self.assertTrue(plan.uses_mid_pass_cut)
        self.assertFalse(plan.parts[0].ends_on_pass_boundary)

    def test_split_recalculates_overhead_instead_of_adding_safe_times(self) -> None:
        plan = plan_mission_split(
            SPLIT_WAYPOINTS,
            (2, 4),
            10,
            (self.battery("B1", 170), self.battery("B2", 170)),
            home_point=SPLIT_WAYPOINTS[0][:2],
            reserve_percent=0,
        )

        self.assertGreater(340, plan.original_estimate.estimated_duration_s)
        self.assertFalse(plan.possible)
        self.assertEqual(plan.parts, ())

    def test_split_requires_home(self) -> None:
        plan = plan_mission_split(
            SPLIT_WAYPOINTS,
            (2, 4),
            10,
            (self.battery("B1", 210), self.battery("B2", 210)),
            home_point=None,
            reserve_percent=0,
        )

        self.assertFalse(plan.possible)
        self.assertIn("Home", plan.reason)

    def test_split_uses_each_physical_battery_once_and_covers_all_waypoints(self) -> None:
        batteries = (self.battery("B1", 210), self.battery("B2", 220))
        plan = plan_mission_split(
            SPLIT_WAYPOINTS,
            (2, 4),
            10,
            batteries,
            home_point=SPLIT_WAYPOINTS[0][:2],
            reserve_percent=0,
        )

        self.assertEqual(len({part.battery.name for part in plan.parts}), len(plan.parts))
        self.assertEqual(
            tuple(waypoint for part in plan.parts for waypoint in part.waypoints),
            SPLIT_WAYPOINTS,
        )

    def test_split_balances_the_minimum_margin(self) -> None:
        waypoints = tuple(
            (44.8, -0.6 if index % 2 == 0 else -0.59, 30.0)
            for index in range(6)
        )
        plan = plan_mission_split(
            waypoints,
            tuple(range(1, 7)),
            10,
            (self.battery("B1", 380), self.battery("B2", 400)),
            home_point=waypoints[0][:2],
            reserve_percent=0,
        )

        self.assertTrue(plan.possible)
        self.assertEqual(len(plan.parts), 2)
        self.assertGreater(min(part.margin_s for part in plan.parts), 60)

    def test_split_limits_combinatorial_battery_count(self) -> None:
        batteries = tuple(self.battery(f"B{index}", 210 + index) for index in range(13))

        with self.assertRaisesRegex(AutonomyError, "12 batteries"):
            plan_mission_split(
                SPLIT_WAYPOINTS,
                (2, 4),
                10,
                batteries,
                home_point=SPLIT_WAYPOINTS[0][:2],
                reserve_percent=0,
            )


if __name__ == "__main__":
    unittest.main()
