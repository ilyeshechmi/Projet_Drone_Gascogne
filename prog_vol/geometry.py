"""Opérations géométriques communes sur les zones de mission."""

from __future__ import annotations

import math


class ZoneGeometryError(RuntimeError):
    """La zone de mission ne peut pas être transformée proprement."""


def _dependencies():
    try:
        from pyproj import Transformer
        from shapely.geometry import Polygon
        from shapely.ops import transform
        from shapely.validation import make_valid
    except ImportError as exc:
        raise ZoneGeometryError(
            "Les bibliothèques shapely et pyproj sont nécessaires pour appliquer "
            "une marge de couverture métrique."
        ) from exc
    return Transformer, Polygon, transform, make_valid


def _clean_points(
    points: list[tuple[float, float]] | tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    cleaned = tuple((float(latitude), float(longitude)) for latitude, longitude in points)
    if len(cleaned) > 1 and cleaned[0] == cleaned[-1]:
        cleaned = cleaned[:-1]
    if len(cleaned) < 3 or len(set(cleaned)) < 3:
        raise ZoneGeometryError("La zone de mission doit contenir au moins trois sommets distincts.")
    if any(
        not math.isfinite(latitude) or not math.isfinite(longitude)
        for latitude, longitude in cleaned
    ):
        raise ZoneGeometryError("La zone de mission contient des coordonnées invalides.")
    return cleaned


def _polygon_from_points(points):
    _Transformer, Polygon, _transform, make_valid = _dependencies()
    cleaned = _clean_points(points)
    polygon = Polygon((longitude, latitude) for latitude, longitude in cleaned)
    if polygon.is_empty or polygon.area <= 0:
        raise ZoneGeometryError("Le contour de la zone est vide ou invalide.")
    if not polygon.is_valid:
        polygon = make_valid(polygon)
    if polygon.geom_type != "Polygon":
        raise ZoneGeometryError("Le contour de la zone doit former un polygone unique.")
    if polygon.interiors:
        raise ZoneGeometryError("Les zones avec trou intérieur ne sont pas prises en charge.")
    return polygon, cleaned


def _points_from_polygon(polygon) -> tuple[tuple[float, float], ...]:
    if polygon.is_empty or polygon.geom_type != "Polygon":
        raise ZoneGeometryError("La marge produit une géométrie inexploitable.")
    if polygon.interiors:
        raise ZoneGeometryError("La marge produit une zone avec trou intérieur non prise en charge.")
    coordinates = list(polygon.exterior.coords)
    if len(coordinates) > 1 and coordinates[0] == coordinates[-1]:
        coordinates.pop()
    points = tuple((float(latitude), float(longitude)) for longitude, latitude in coordinates)
    if len(points) < 3 or len(set(points)) < 3:
        raise ZoneGeometryError("La marge produit un contour trop petit pour générer une mission.")
    return points


def buffer_zone_points(
    points: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    margin_m: float,
) -> tuple[tuple[float, float], ...]:
    """Retourne le contour de vol en WGS84 après marge métrique.

    Les points d'entrée et de sortie sont au format applicatif ``(latitude,
    longitude)``. Le buffer est calculé en Lambert-93, donc en mètres, puis
    reprojeté vers WGS84 pour rester compatible avec Leaflet et le générateur.
    """
    margin = float(margin_m)
    if not math.isfinite(margin):
        raise ZoneGeometryError("La marge de couverture doit être un nombre valide.")
    if margin < 0:
        raise ZoneGeometryError("La marge de couverture doit être positive ou nulle.")
    original = _clean_points(points)
    if margin == 0:
        return original

    Transformer, _Polygon, transform, make_valid = _dependencies()
    polygon_wgs84, _cleaned = _polygon_from_points(original)
    to_lambert93 = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    to_wgs84 = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
    polygon_m = transform(to_lambert93.transform, polygon_wgs84)
    buffered_m = polygon_m.buffer(margin)
    if not buffered_m.is_valid:
        buffered_m = make_valid(buffered_m)
    if buffered_m.geom_type != "Polygon":
        raise ZoneGeometryError("La marge produit plusieurs zones disjointes non prises en charge.")
    buffered_wgs84 = transform(to_wgs84.transform, buffered_m)
    return _points_from_polygon(buffered_wgs84)
