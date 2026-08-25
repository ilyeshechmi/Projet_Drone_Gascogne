"""Calcul de trajectoires et génération de missions DJI au format KMZ."""

from __future__ import annotations

import math
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .autonomy import (
    FlightEstimate,
    MissionSplitPlan,
    estimate_flight,
    geographic_distance_m,
)
from .missions import inspect_mission


MAX_WAYPOINTS = 20_000


class GenerationError(RuntimeError):
    """Les paramètres ou la zone ne permettent pas de générer une mission."""


@dataclass(frozen=True)
class MissionParameters:
    altitude: float = 50.0
    drone_speed: float = 2.5
    gimbal_pitch: float = -45.0
    frontal_overlap: float = 0.8
    lateral_overlap: float = 0.8
    sensor_width: float = 6.17
    sensor_height: float = 4.55
    focal_length: float = 4.5
    photo_interval: float = 2.0

    def validate(self) -> None:
        if self.altitude <= 0:
            raise GenerationError("L'altitude doit être strictement positive.")
        if self.drone_speed <= 0:
            raise GenerationError("La vitesse doit être strictement positive.")
        if not -90 <= self.gimbal_pitch <= 0:
            raise GenerationError("L'angle de nacelle doit être compris entre -90° et 0°.")
        for label, overlap in (
            ("frontal", self.frontal_overlap),
            ("latéral", self.lateral_overlap),
        ):
            if not 0 <= overlap < 1:
                raise GenerationError(
                    f"Le recouvrement {label} doit être compris entre 0 inclus et 1 exclu."
                )
        if self.sensor_width <= 0 or self.sensor_height <= 0:
            raise GenerationError("Les dimensions du capteur doivent être positives.")
        if self.focal_length <= 0:
            raise GenerationError("La focale doit être strictement positive.")
        if self.photo_interval <= 0:
            raise GenerationError("L'intervalle minimal entre photos doit être positif.")


@dataclass(frozen=True)
class GenerationResult:
    output_path: Path
    waypoints: tuple[tuple[float, float, float], ...]
    line_count: int
    fov_width: float
    fov_height: float
    flight_estimate: FlightEstimate

    @property
    def waypoint_count(self) -> int:
        return len(self.waypoints)


@dataclass(frozen=True)
class PassRange:
    start_index: int
    end_index: int


@dataclass(frozen=True)
class GeneratedRoute:
    waypoints: tuple[tuple[float, float, float], ...]
    passes: tuple[PassRange, ...]
    fov_width: float
    fov_height: float

    @property
    def line_count(self) -> int:
        return len(self.passes)

    @property
    def pass_end_indices(self) -> tuple[int, ...]:
        return tuple(item.end_index for item in self.passes)


@dataclass(frozen=True)
class MissionGenerationSet:
    results: tuple[GenerationResult, ...]
    split_plan: MissionSplitPlan

    @property
    def output_paths(self) -> tuple[Path, ...]:
        return tuple(result.output_path for result in self.results)


def point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    """Teste l'appartenance avec l'algorithme de ray casting."""
    x, y = point
    inside = False
    p1x, p1y = polygon[0]
    for index in range(1, len(polygon) + 1):
        p2x, p2y = polygon[index % len(polygon)]
        if y > min(p1y, p2y) and y <= max(p1y, p2y) and x <= max(p1x, p2x):
            intersection = p1x
            if p1y != p2y:
                intersection = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
            if p1x == p2x or x <= intersection:
                inside = not inside
        p1x, p1y = p2x, p2y
    return inside


def _bounding_box(
    polygon: list[tuple[float, float]],
) -> tuple[float, float, float, float]:
    latitudes = [point[0] for point in polygon]
    longitudes = [point[1] for point in polygon]
    return min(latitudes), max(latitudes), min(longitudes), max(longitudes)


def _distance_m(point_a: tuple[float, float], point_b: tuple[float, float]) -> float:
    return geographic_distance_m(point_a, point_b)


def _orientation(
    first: tuple[float, float],
    second: tuple[float, float],
    third: tuple[float, float],
) -> float:
    return (second[1] - first[1]) * (third[0] - first[0]) - (
        second[0] - first[0]
    ) * (third[1] - first[1])


def _polygon_self_intersects(polygon: list[tuple[float, float]]) -> bool:
    edge_count = len(polygon)
    for first_index in range(edge_count):
        first_start = polygon[first_index]
        first_end = polygon[(first_index + 1) % edge_count]
        for second_index in range(first_index + 1, edge_count):
            if second_index in {
                first_index,
                (first_index + 1) % edge_count,
                (first_index - 1) % edge_count,
            }:
                continue
            second_start = polygon[second_index]
            second_end = polygon[(second_index + 1) % edge_count]
            first_side = _orientation(first_start, first_end, second_start)
            second_side = _orientation(first_start, first_end, second_end)
            third_side = _orientation(second_start, second_end, first_start)
            fourth_side = _orientation(second_start, second_end, first_end)
            if first_side * second_side < 0 and third_side * fourth_side < 0:
                return True
    return False


def generate_route_polygon(
    polygon_points: list[tuple[float, float]] | list[list[float]],
    parameters: MissionParameters,
) -> GeneratedRoute:
    """Couvre un polygone par des lignes horizontales en boustrophédon."""
    parameters.validate()
    polygon = [(float(latitude), float(longitude)) for latitude, longitude in polygon_points]
    if len(polygon) < 3:
        raise GenerationError("La zone de mission doit contenir au moins trois sommets.")
    if len(set(polygon)) < 3:
        raise GenerationError("La zone de mission doit contenir trois sommets distincts.")
    if _polygon_self_intersects(polygon):
        raise GenerationError("Le contour de la zone ne doit pas se croiser lui-même.")

    fov_width = parameters.altitude * parameters.sensor_width / parameters.focal_length
    fov_height = parameters.altitude * parameters.sensor_height / parameters.focal_length
    point_spacing = fov_width * (1 - parameters.frontal_overlap)
    line_spacing = fov_height * (1 - parameters.lateral_overlap)
    if point_spacing <= 0 or line_spacing <= 0:
        raise GenerationError("Les recouvrements produisent un espacement nul.")

    min_lat, max_lat, _min_lon, _max_lon = _bounding_box(polygon)
    line_spacing_degrees = line_spacing / 111_000
    height_degrees = max_lat - min_lat
    line_total = max(1, math.ceil(height_degrees / line_spacing_degrees)) + 1

    waypoints: list[tuple[float, float, float]] = []
    passes: list[PassRange] = []
    generated_lines = 0
    for line_index in range(line_total):
        latitude = min_lat + line_index * line_spacing_degrees
        if latitude > max_lat:
            break

        intersections: list[float] = []
        for edge_index, first in enumerate(polygon):
            second = polygon[(edge_index + 1) % len(polygon)]
            if (
                first[0] <= latitude <= second[0]
                or second[0] <= latitude <= first[0]
            ) and second[0] != first[0]:
                ratio = (latitude - first[0]) / (second[0] - first[0])
                intersections.append(first[1] + ratio * (second[1] - first[1]))
        intersections.sort()

        for pair_index in range(0, len(intersections) - 1, 2):
            start_longitude = intersections[pair_index]
            end_longitude = intersections[pair_index + 1]
            line_length = _distance_m(
                (latitude, start_longitude), (latitude, end_longitude)
            )
            point_total = max(1, math.ceil(line_length / point_spacing)) + 1
            line_waypoints: list[tuple[float, float, float]] = []
            for point_index in range(point_total):
                fraction = point_index / (point_total - 1) if point_total > 1 else 0
                longitude = start_longitude + fraction * (
                    end_longitude - start_longitude
                )
                if point_in_polygon((latitude, longitude), polygon):
                    line_waypoints.append(
                        (latitude, longitude, parameters.altitude)
                    )
            if generated_lines % 2:
                line_waypoints.reverse()
            if line_waypoints:
                if len(waypoints) + len(line_waypoints) > MAX_WAYPOINTS:
                    raise GenerationError(
                        f"La mission dépasse la limite de sécurité de {MAX_WAYPOINTS} "
                        "waypoints. Réduisez la zone ou le recouvrement."
                    )
                start_index = len(waypoints)
                waypoints.extend(line_waypoints)
                passes.append(PassRange(start_index, len(waypoints)))
                generated_lines += 1

    if not waypoints:
        raise GenerationError(
            "Aucun waypoint n'a été généré. Vérifiez la forme et la taille du polygone."
        )
    return GeneratedRoute(tuple(waypoints), tuple(passes), fov_width, fov_height)


def generate_waypoints_polygon(
    polygon_points: list[tuple[float, float]] | list[list[float]],
    parameters: MissionParameters,
) -> tuple[list[tuple[float, float, float]], int, float, float]:
    """Interface historique retournant la trajectoire sous forme de liste plate."""
    route = generate_route_polygon(polygon_points, parameters)
    return list(route.waypoints), route.line_count, route.fov_width, route.fov_height


def _heading(index: int, waypoints: list[tuple[float, float, float]]) -> int:
    if len(waypoints) < 2:
        return -90
    if index == len(waypoints) - 1:
        first, second = waypoints[index - 1], waypoints[index]
    else:
        first, second = waypoints[index], waypoints[index + 1]
    longitude_delta = second[1] - first[1]
    latitude_delta = second[0] - first[0]
    if abs(longitude_delta) > abs(latitude_delta):
        return -90 if longitude_delta > 0 else 90
    return -90


def generate_waypointmap_kmz(
    waypoints: list[tuple[float, float, float]],
    parameters: MissionParameters,
    output_path: str | Path,
    *,
    route_distance_m: float | None = None,
    route_duration_s: float | None = None,
    finish_action: str = "goHome",
) -> Path:
    """Crée l'archive WPMZ attendue par DJI Fly et WaypointMap."""
    parameters.validate()
    if not waypoints:
        raise GenerationError("Une mission KMZ doit contenir au moins un waypoint.")
    if finish_action not in {"goHome", "noAction", "autoLand", "gotoFirstWaypoint"}:
        raise GenerationError("Action de fin WPML non prise en charge.")
    if route_distance_m is None or route_duration_s is None:
        route_estimate = estimate_flight(
            waypoints,
            parameters.drone_speed,
            photo_interval_s=parameters.photo_interval,
        )
        route_distance_m = route_estimate.route_distance_m
        route_duration_s = route_estimate.route_duration_s
    path = Path(output_path).expanduser().resolve()
    if path.suffix.casefold() != ".kmz":
        path = path.with_suffix(".kmz")
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = int(time.time() * 1000)

    template_kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="http://www.dji.com/wpmz/1.0.2">
<Document>
<wpml:author>Kael</wpml:author>
<wpml:createTime>{timestamp}</wpml:createTime>
<wpml:updateTime>{timestamp}</wpml:updateTime>
<wpml:missionConfig>
<wpml:flyToWaylineMode>safely</wpml:flyToWaylineMode>
<wpml:finishAction>{finish_action}</wpml:finishAction>
<wpml:exitOnRCLost>executeLostAction</wpml:exitOnRCLost>
<wpml:executeRCLostAction>hover</wpml:executeRCLostAction>
<wpml:globalTransitionalSpeed>{parameters.drone_speed}</wpml:globalTransitionalSpeed>
<wpml:droneInfo>
<wpml:droneEnumValue>68</wpml:droneEnumValue>
<wpml:droneSubEnumValue>0</wpml:droneSubEnumValue>
</wpml:droneInfo>
</wpml:missionConfig>
</Document>
</kml>
"""

    placemarks: list[str] = []
    action_id = 1
    for index, (latitude, longitude, altitude) in enumerate(waypoints):
        heading = _heading(index, waypoints)
        turn_mode = (
            "toPointAndStopWithContinuityCurvature"
            if index == 0
            else "toPointAndPassWithContinuityCurvature"
        )
        heading_enabled = 1 if index == 0 else 0
        actions: list[str] = []
        if index == 0:
            actions.append(
                f"""<wpml:actionGroup>
<wpml:actionGroupId>1</wpml:actionGroupId>
<wpml:actionGroupStartIndex>0</wpml:actionGroupStartIndex>
<wpml:actionGroupEndIndex>0</wpml:actionGroupEndIndex>
<wpml:actionGroupMode>parallel</wpml:actionGroupMode>
<wpml:actionTrigger><wpml:actionTriggerType>reachPoint</wpml:actionTriggerType></wpml:actionTrigger>
<wpml:action>
<wpml:actionId>{action_id}</wpml:actionId>
<wpml:actionActuatorFunc>gimbalRotate</wpml:actionActuatorFunc>
<wpml:actionActuatorFuncParam>
<wpml:gimbalHeadingYawBase>aircraft</wpml:gimbalHeadingYawBase>
<wpml:gimbalRotateMode>absoluteAngle</wpml:gimbalRotateMode>
<wpml:gimbalPitchRotateEnable>1</wpml:gimbalPitchRotateEnable>
<wpml:gimbalPitchRotateAngle>{parameters.gimbal_pitch}</wpml:gimbalPitchRotateAngle>
<wpml:gimbalRollRotateEnable>0</wpml:gimbalRollRotateEnable>
<wpml:gimbalRollRotateAngle>0</wpml:gimbalRollRotateAngle>
<wpml:gimbalYawRotateEnable>0</wpml:gimbalYawRotateEnable>
<wpml:gimbalYawRotateAngle>0</wpml:gimbalYawRotateAngle>
<wpml:gimbalRotateTimeEnable>0</wpml:gimbalRotateTimeEnable>
<wpml:gimbalRotateTime>0</wpml:gimbalRotateTime>
<wpml:payloadPositionIndex>0</wpml:payloadPositionIndex>
</wpml:actionActuatorFuncParam>
</wpml:action>
</wpml:actionGroup>"""
            )
            action_id += 1

        actions.append(
            f"""<wpml:actionGroup>
<wpml:actionGroupId>{index * 2 + 2}</wpml:actionGroupId>
<wpml:actionGroupStartIndex>{index}</wpml:actionGroupStartIndex>
<wpml:actionGroupEndIndex>{index}</wpml:actionGroupEndIndex>
<wpml:actionGroupMode>parallel</wpml:actionGroupMode>
<wpml:actionTrigger><wpml:actionTriggerType>reachPoint</wpml:actionTriggerType></wpml:actionTrigger>
<wpml:action>
<wpml:actionId>{action_id}</wpml:actionId>
<wpml:actionActuatorFunc>gimbalEvenlyRotate</wpml:actionActuatorFunc>
<wpml:actionActuatorFuncParam>
<wpml:gimbalPitchRotateAngle>{parameters.gimbal_pitch}</wpml:gimbalPitchRotateAngle>
<wpml:payloadPositionIndex>0</wpml:payloadPositionIndex>
</wpml:actionActuatorFuncParam>
</wpml:action>
</wpml:actionGroup>"""
        )
        action_id += 1
        actions.append(
            f"""<wpml:actionGroup>
<wpml:actionGroupId>{index * 2 + 3}</wpml:actionGroupId>
<wpml:actionGroupStartIndex>{index}</wpml:actionGroupStartIndex>
<wpml:actionGroupEndIndex>{index}</wpml:actionGroupEndIndex>
<wpml:actionGroupMode>parallel</wpml:actionGroupMode>
<wpml:actionTrigger><wpml:actionTriggerType>reachPoint</wpml:actionTriggerType></wpml:actionTrigger>
<wpml:action>
<wpml:actionId>{action_id}</wpml:actionId>
<wpml:actionActuatorFunc>takePhoto</wpml:actionActuatorFunc>
<wpml:actionActuatorFuncParam><wpml:payloadPositionIndex>0</wpml:payloadPositionIndex></wpml:actionActuatorFuncParam>
</wpml:action>
</wpml:actionGroup>"""
        )
        action_id += 1
        placemarks.append(
            f"""<Placemark>
<Point><coordinates>{longitude},{latitude}</coordinates></Point>
<wpml:index>{index}</wpml:index>
<wpml:executeHeight>{altitude:.2f}</wpml:executeHeight>
<wpml:waypointSpeed>{parameters.drone_speed}</wpml:waypointSpeed>
<wpml:waypointHeadingParam>
<wpml:waypointHeadingMode>smoothTransition</wpml:waypointHeadingMode>
<wpml:waypointHeadingAngle>{heading}</wpml:waypointHeadingAngle>
<wpml:waypointPoiPoint>0.000000,0.000000,0.000000</wpml:waypointPoiPoint>
<wpml:waypointHeadingAngleEnable>{heading_enabled}</wpml:waypointHeadingAngleEnable>
<wpml:waypointHeadingPathMode>followBadArc</wpml:waypointHeadingPathMode>
</wpml:waypointHeadingParam>
<wpml:waypointTurnParam>
<wpml:waypointTurnMode>{turn_mode}</wpml:waypointTurnMode>
<wpml:waypointTurnDampingDist>0</wpml:waypointTurnDampingDist>
</wpml:waypointTurnParam>
<wpml:useStraightLine>0</wpml:useStraightLine>
{''.join(actions)}
</Placemark>"""
        )

    waylines_wpml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="http://www.dji.com/wpmz/1.0.2">
<Document>
<wpml:missionConfig>
<wpml:flyToWaylineMode>safely</wpml:flyToWaylineMode>
<wpml:finishAction>{finish_action}</wpml:finishAction>
<wpml:exitOnRCLost>executeLostAction</wpml:exitOnRCLost>
<wpml:executeRCLostAction>hover</wpml:executeRCLostAction>
<wpml:globalTransitionalSpeed>{parameters.drone_speed}</wpml:globalTransitionalSpeed>
<wpml:droneInfo>
<wpml:droneEnumValue>68</wpml:droneEnumValue>
<wpml:droneSubEnumValue>0</wpml:droneSubEnumValue>
</wpml:droneInfo>
</wpml:missionConfig>
<Folder>
<wpml:templateId>0</wpml:templateId>
<wpml:executeHeightMode>relativeToStartPoint</wpml:executeHeightMode>
<wpml:waylineId>0</wpml:waylineId>
<wpml:distance>{route_distance_m:.2f}</wpml:distance>
<wpml:duration>{route_duration_s:.2f}</wpml:duration>
<wpml:autoFlightSpeed>{parameters.drone_speed}</wpml:autoFlightSpeed>
{''.join(placemarks)}
</Folder>
</Document>
</kml>
"""

    try:
        with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("wpmz/template.kml", template_kml)
            archive.writestr("wpmz/waylines.wpml", waylines_wpml)
    except OSError as exc:
        raise GenerationError(f"Impossible d'écrire le fichier KMZ : {exc}") from exc
    return path


def generate_mission(
    polygon_points: list[tuple[float, float]] | list[list[float]],
    parameters: MissionParameters,
    output_path: str | Path,
    *,
    home_point: tuple[float, float] | None = None,
) -> GenerationResult:
    """Point d'entrée stable utilisé par l'interface graphique."""
    route = generate_route_polygon(
        polygon_points,
        parameters,
    )
    flight_estimate = estimate_flight(
        route.waypoints,
        parameters.drone_speed,
        home_point=home_point,
        photo_interval_s=parameters.photo_interval,
    )
    path = generate_waypointmap_kmz(
        list(route.waypoints),
        parameters,
        output_path,
        route_distance_m=flight_estimate.route_distance_m,
        route_duration_s=flight_estimate.route_duration_s,
    )
    return GenerationResult(
        output_path=path,
        waypoints=route.waypoints,
        line_count=route.line_count,
        fov_width=route.fov_width,
        fov_height=route.fov_height,
        flight_estimate=flight_estimate,
    )


def _part_output_paths(output_path: str | Path, part_count: int) -> tuple[Path, ...]:
    base = Path(output_path).expanduser().resolve()
    if base.suffix.casefold() != ".kmz":
        base = base.with_suffix(".kmz")
    if part_count == 1:
        return (base,)
    return tuple(
        base.with_name(
            f"{base.stem}_partie_{part_number:02d}_sur_{part_count:02d}.kmz"
        )
        for part_number in range(1, part_count + 1)
    )


def generate_mission_parts(
    route: GeneratedRoute,
    parameters: MissionParameters,
    output_path: str | Path,
    split_plan: MissionSplitPlan,
) -> MissionGenerationSet:
    """Génère et valide toutes les parties avant de publier les fichiers finaux."""
    if not split_plan.possible or not split_plan.parts:
        raise GenerationError(split_plan.reason)
    covered = tuple(
        waypoint for part in split_plan.parts for waypoint in part.waypoints
    )
    if covered != route.waypoints:
        raise GenerationError(
            "Le plan de découpage ne couvre pas exactement la trajectoire générée."
        )

    final_paths = _part_output_paths(output_path, len(split_plan.parts))
    for path in final_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    temporary_paths = tuple(
        path.with_name(f".{path.stem}.{token}.tmp.kmz") for path in final_paths
    )
    backup_paths: dict[Path, Path] = {}
    published_paths: set[Path] = set()
    results: list[GenerationResult] = []
    try:
        for part, temporary_path in zip(split_plan.parts, temporary_paths):
            generate_waypointmap_kmz(
                list(part.waypoints),
                parameters,
                temporary_path,
                route_distance_m=part.flight_estimate.route_distance_m,
                route_duration_s=part.flight_estimate.route_duration_s,
                finish_action="goHome",
            )
            inspect_mission(temporary_path)

        for final_path in final_paths:
            if final_path.exists():
                backup_path = final_path.with_name(f".{final_path.name}.{token}.bak")
                os.replace(final_path, backup_path)
                backup_paths[final_path] = backup_path
        for temporary_path, final_path in zip(temporary_paths, final_paths):
            os.replace(temporary_path, final_path)
            published_paths.add(final_path)

        for part, final_path in zip(split_plan.parts, final_paths):
            inspect_mission(final_path)
            line_count = sum(
                item.start_index < part.end_index
                and item.end_index > part.start_index
                for item in route.passes
            )
            results.append(
                GenerationResult(
                    output_path=final_path,
                    waypoints=part.waypoints,
                    line_count=line_count,
                    fov_width=route.fov_width,
                    fov_height=route.fov_height,
                    flight_estimate=part.flight_estimate,
                )
            )
    except Exception:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)
        for final_path in published_paths:
            final_path.unlink(missing_ok=True)
        for final_path, backup_path in backup_paths.items():
            final_path.unlink(missing_ok=True)
            if backup_path.exists():
                os.replace(backup_path, final_path)
        raise
    else:
        for backup_path in backup_paths.values():
            backup_path.unlink(missing_ok=True)

    return MissionGenerationSet(tuple(results), split_plan)
