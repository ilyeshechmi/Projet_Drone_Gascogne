"""Tests du calcul d'autonomie indépendant de l'interface graphique."""

from __future__ import annotations

import unittest

from prog_vol.autonomy import (
    AlertLevel,
    AutonomyError,
    BatteryState,
    DEFAULT_BATTERY_PROFILE,
    FlightEstimate,
    assess_batteries,
    custom_battery_profile,
    estimate_flight,
)


WAYPOINTS = (
    (44.8000, -0.6000, 50.0),
    (44.8000, -0.5990, 50.0),
    (44.8010, -0.5990, 50.0),
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


if __name__ == "__main__":
    unittest.main()
