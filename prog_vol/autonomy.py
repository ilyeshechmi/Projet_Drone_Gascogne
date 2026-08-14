"""Estimation de durée de vol et évaluation des batteries disponibles."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


DEFAULT_RESERVE_PERCENT = 20.0
DEFAULT_VERTICAL_SPEED_MPS = 3.0


class AutonomyError(ValueError):
    """Les données de vol ou de batterie ne permettent pas une estimation."""


class AlertLevel(Enum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    PARTIAL = "partial"


@dataclass(frozen=True)
class BatteryProfile:
    model: str
    capacity_mah: float
    nominal_voltage_v: float
    reference_flight_time_s: float
    energy_wh: float | None = None
    estimated_reference: bool = False

    @property
    def calculated_energy_wh(self) -> float:
        return self.capacity_mah * self.nominal_voltage_v / 1000

    @property
    def effective_energy_wh(self) -> float:
        return self.energy_wh if self.energy_wh is not None else self.calculated_energy_wh

    def validate(self) -> None:
        if not self.model.strip():
            raise AutonomyError("Le modèle de batterie doit être renseigné.")
        if self.capacity_mah <= 0:
            raise AutonomyError("La capacité de la batterie doit être positive.")
        if self.nominal_voltage_v <= 0:
            raise AutonomyError("La tension de la batterie doit être positive.")
        if self.reference_flight_time_s <= 0:
            raise AutonomyError("L'autonomie de référence doit être positive.")
        if self.energy_wh is not None and self.energy_wh <= 0:
            raise AutonomyError("L'énergie de la batterie doit être positive.")


DEFAULT_BATTERY_PROFILE = BatteryProfile(
    model="BWXNN3-2587-7.0",
    capacity_mah=2788,
    nominal_voltage_v=7.70,
    energy_wh=21.4,
    reference_flight_time_s=21 * 60,
)


def custom_battery_profile(
    model: str,
    capacity_mah: float,
    nominal_voltage_v: float,
    reference_flight_time_s: float | None = None,
) -> BatteryProfile:
    """Crée un profil, avec extrapolation énergétique si aucune durée n'est connue."""
    energy_wh = capacity_mah * nominal_voltage_v / 1000
    estimated = reference_flight_time_s is None
    if reference_flight_time_s is None:
        reference_flight_time_s = (
            DEFAULT_BATTERY_PROFILE.reference_flight_time_s
            * energy_wh
            / DEFAULT_BATTERY_PROFILE.effective_energy_wh
        )
    profile = BatteryProfile(
        model=model,
        capacity_mah=capacity_mah,
        nominal_voltage_v=nominal_voltage_v,
        energy_wh=energy_wh,
        reference_flight_time_s=reference_flight_time_s,
        estimated_reference=estimated,
    )
    profile.validate()
    return profile


@dataclass(frozen=True)
class BatteryState:
    name: str
    profile: BatteryProfile
    charge_percent: float = 100.0

    def validate(self) -> None:
        self.profile.validate()
        if not self.name.strip():
            raise AutonomyError("Chaque batterie doit avoir un nom.")
        if not 0 <= self.charge_percent <= 100:
            raise AutonomyError("Le niveau de charge doit être compris entre 0 et 100 %.")

    @property
    def raw_available_time_s(self) -> float:
        return self.profile.reference_flight_time_s * self.charge_percent / 100

    def safe_available_time_s(self, reserve_percent: float) -> float:
        usable_charge = max(0.0, self.charge_percent - reserve_percent)
        return self.profile.reference_flight_time_s * usable_charge / 100


@dataclass(frozen=True)
class FlightEstimate:
    route_distance_m: float
    transit_distance_m: float | None
    route_duration_s: float
    transit_duration_s: float | None
    vertical_duration_s: float | None
    estimated_duration_s: float
    photo_count: int
    short_photo_intervals: int
    complete: bool

    @property
    def total_distance_m(self) -> float | None:
        if self.transit_distance_m is None:
            return None
        return self.route_distance_m + self.transit_distance_m


@dataclass(frozen=True)
class BatteryAssessment:
    level: AlertLevel
    required_duration_s: float
    recommended_battery: BatteryState | None
    recommended_safe_time_s: float | None
    margin_s: float | None
    minimum_battery_count: int | None
    message: str
    estimated_profile_used: bool


def geographic_distance_m(
    point_a: tuple[float, float], point_b: tuple[float, float]
) -> float:
    """Distance de grand cercle entre deux coordonnées latitude/longitude."""
    latitude_a, longitude_a = map(math.radians, point_a)
    latitude_b, longitude_b = map(math.radians, point_b)
    latitude_delta = latitude_b - latitude_a
    longitude_delta = longitude_b - longitude_a
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(latitude_a)
        * math.cos(latitude_b)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 6_371_000 * 2 * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine))


def estimate_flight(
    waypoints: list[tuple[float, float, float]]
    | tuple[tuple[float, float, float], ...],
    drone_speed_mps: float,
    *,
    home_point: tuple[float, float] | None = None,
    photo_interval_s: float = 2.0,
    vertical_speed_mps: float = DEFAULT_VERTICAL_SPEED_MPS,
) -> FlightEstimate:
    if not waypoints:
        raise AutonomyError("La trajectoire doit contenir au moins un waypoint.")
    if drone_speed_mps <= 0:
        raise AutonomyError("La vitesse de vol doit être positive.")
    if photo_interval_s <= 0:
        raise AutonomyError("L'intervalle photo doit être positif.")
    if vertical_speed_mps <= 0:
        raise AutonomyError("La vitesse verticale doit être positive.")

    segment_distances = [
        geographic_distance_m(first[:2], second[:2])
        for first, second in zip(waypoints, waypoints[1:])
    ]
    route_distance = sum(segment_distances)
    route_duration = route_distance / drone_speed_mps
    short_intervals = sum(
        distance / drone_speed_mps < photo_interval_s for distance in segment_distances
    )

    if home_point is None:
        return FlightEstimate(
            route_distance_m=route_distance,
            transit_distance_m=None,
            route_duration_s=route_duration,
            transit_duration_s=None,
            vertical_duration_s=None,
            estimated_duration_s=route_duration,
            photo_count=len(waypoints),
            short_photo_intervals=short_intervals,
            complete=False,
        )

    transit_distance = geographic_distance_m(home_point, waypoints[0][:2])
    transit_distance += geographic_distance_m(waypoints[-1][:2], home_point)
    transit_duration = transit_distance / drone_speed_mps
    altitude = max(0.0, float(waypoints[0][2]))
    vertical_duration = 2 * altitude / vertical_speed_mps
    return FlightEstimate(
        route_distance_m=route_distance,
        transit_distance_m=transit_distance,
        route_duration_s=route_duration,
        transit_duration_s=transit_duration,
        vertical_duration_s=vertical_duration,
        estimated_duration_s=route_duration + transit_duration + vertical_duration,
        photo_count=len(waypoints),
        short_photo_intervals=short_intervals,
        complete=True,
    )


def _minimum_battery_count(
    batteries: list[BatteryState], reserve_percent: float, required_s: float
) -> int | None:
    accumulated = 0.0
    for count, battery in enumerate(
        sorted(
            batteries,
            key=lambda item: item.safe_available_time_s(reserve_percent),
            reverse=True,
        ),
        start=1,
    ):
        accumulated += battery.safe_available_time_s(reserve_percent)
        if accumulated >= required_s:
            return count
    return None


def assess_batteries(
    estimate: FlightEstimate,
    batteries: list[BatteryState] | tuple[BatteryState, ...],
    reserve_percent: float = DEFAULT_RESERVE_PERCENT,
) -> BatteryAssessment:
    if not 0 <= reserve_percent < 100:
        raise AutonomyError("La réserve doit être comprise entre 0 inclus et 100 % exclu.")
    battery_list = list(batteries)
    if not battery_list:
        raise AutonomyError("Ajoutez au moins une batterie à la mission.")
    for battery in battery_list:
        battery.validate()

    required = estimate.estimated_duration_s
    estimated_profile = any(item.profile.estimated_reference for item in battery_list)
    safe_sorted = sorted(
        battery_list,
        key=lambda item: item.safe_available_time_s(reserve_percent),
        reverse=True,
    )
    best = safe_sorted[0]
    best_safe = best.safe_available_time_s(reserve_percent)

    if not estimate.complete:
        if required > best_safe:
            return BatteryAssessment(
                level=AlertLevel.CRITICAL,
                required_duration_s=required,
                recommended_battery=None,
                recommended_safe_time_s=best_safe,
                margin_s=best_safe - required,
                minimum_battery_count=None,
                message=(
                    "La trajectoire dans la zone dépasse déjà l'autonomie sûre de "
                    "chaque batterie, sans même compter le décollage et le retour."
                ),
                estimated_profile_used=estimated_profile,
            )
        return BatteryAssessment(
            level=AlertLevel.PARTIAL,
            required_duration_s=required,
            recommended_battery=best,
            recommended_safe_time_s=best_safe,
            margin_s=best_safe - required,
            minimum_battery_count=None,
            message=(
                "Estimation partielle : placez le point Home pour inclure le transit, "
                "la montée, le retour et l'atterrissage."
            ),
            estimated_profile_used=estimated_profile,
        )

    safe_candidates = [
        item
        for item in safe_sorted
        if item.safe_available_time_s(reserve_percent) >= required
    ]
    if safe_candidates:
        selected = safe_candidates[0]
        safe_time = selected.safe_available_time_s(reserve_percent)
        return BatteryAssessment(
            level=AlertLevel.OK,
            required_duration_s=required,
            recommended_battery=selected,
            recommended_safe_time_s=safe_time,
            margin_s=safe_time - required,
            minimum_battery_count=1,
            message=f"Mission réalisable avec {selected.name} et la réserve demandée.",
            estimated_profile_used=estimated_profile,
        )

    raw_candidates = [
        item for item in battery_list if item.raw_available_time_s >= required
    ]
    if raw_candidates:
        selected = max(raw_candidates, key=lambda item: item.raw_available_time_s)
        return BatteryAssessment(
            level=AlertLevel.WARNING,
            required_duration_s=required,
            recommended_battery=selected,
            recommended_safe_time_s=selected.safe_available_time_s(reserve_percent),
            margin_s=selected.safe_available_time_s(reserve_percent) - required,
            minimum_battery_count=1,
            message=(
                f"{selected.name} couvre la durée théorique, mais pas la réserve de "
                f"{reserve_percent:.0f} %."
            ),
            estimated_profile_used=estimated_profile,
        )

    minimum_count = _minimum_battery_count(battery_list, reserve_percent, required)
    if minimum_count is not None:
        return BatteryAssessment(
            level=AlertLevel.WARNING,
            required_duration_s=required,
            recommended_battery=None,
            recommended_safe_time_s=best_safe,
            margin_s=best_safe - required,
            minimum_battery_count=minimum_count,
            message=(
                f"Aucune batterie ne suffit seule. Au moins {minimum_count} batteries "
                "seraient nécessaires, hors surcoûts des transits supplémentaires, "
                "avec atterrissage et découpage de la mission."
            ),
            estimated_profile_used=estimated_profile,
        )

    return BatteryAssessment(
        level=AlertLevel.CRITICAL,
        required_duration_s=required,
        recommended_battery=None,
        recommended_safe_time_s=best_safe,
        margin_s=best_safe - required,
        minimum_battery_count=None,
        message="La charge totale sûre des batteries est insuffisante pour cette mission.",
        estimated_profile_used=estimated_profile,
    )
