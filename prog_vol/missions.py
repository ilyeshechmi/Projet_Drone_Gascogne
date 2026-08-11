"""Validation et description des archives de mission DJI au format KMZ."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree


REQUIRED_MEMBERS = {"wpmz/template.kml", "wpmz/waylines.wpml"}
MAX_UNCOMPRESSED_SIZE = 100 * 1024 * 1024
MAX_MEMBER_SIZE = 50 * 1024 * 1024
MAX_MEMBER_COUNT = 1000
KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
WPML_NAMESPACE_PREFIX = "http://www.dji.com/wpmz/"


class MissionError(RuntimeError):
    """Le fichier fourni ne peut pas être utilisé comme mission DJI."""


@dataclass(frozen=True)
class MissionArchive:
    path: Path
    size: int
    sha256: str
    waypoint_count: int
    members: tuple[str, ...]
    data: bytes = field(repr=False)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_member_name(name: str) -> None:
    if "\\" in name or re.search(r"[\x00-\x1f]", name):
        raise MissionError(f"Nom de fichier interdit dans le KMZ : {name!r}")
    member = PurePosixPath(name)
    if (
        member.is_absolute()
        or ".." in member.parts
        or not member.parts
        or member.parts[0] != "wpmz"
        or re.match(r"^[A-Za-z]:", name)
    ):
        raise MissionError(f"Chemin dangereux dans le KMZ : {name}")


def _is_wpml_tag(element: ElementTree.Element, local_name: str) -> bool:
    return (
        element.tag.startswith("{" + WPML_NAMESPACE_PREFIX)
        and element.tag.endswith("}" + local_name)
    )


def _find_wpml(root: ElementTree.Element, local_name: str) -> ElementTree.Element | None:
    return next(
        (element for element in root.iter() if _is_wpml_tag(element, local_name)),
        None,
    )


def inspect_mission(source: str | Path) -> MissionArchive:
    """Valide la structure DJI et retourne les informations utiles au transfert."""
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise MissionError(f"Fichier KMZ introuvable : {path}")
    if path.suffix.casefold() != ".kmz":
        raise MissionError("La mission doit être un fichier avec l'extension .kmz.")
    if path.stat().st_size > MAX_UNCOMPRESSED_SIZE:
        raise MissionError("Le fichier KMZ dépasse la taille maximale de 100 Mo.")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MissionError(f"Impossible de lire le KMZ : {exc}") from exc
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise MissionError(f"Le fichier n'est pas une archive KMZ valide : {path}")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBER_COUNT:
                raise MissionError("Le KMZ contient plus de 1000 entrées.")
            names = {info.filename for info in infos}
            if len(names) != len(infos):
                raise MissionError("Le KMZ contient plusieurs entrées portant le même nom.")
            for info in infos:
                _validate_member_name(info.filename)
                if info.flag_bits & 0x1:
                    raise MissionError(f"Entrée ZIP chiffrée non prise en charge : {info.filename}")
                if info.file_size > MAX_MEMBER_SIZE:
                    raise MissionError(f"Entrée KMZ trop volumineuse : {info.filename}")
            total_size = sum(info.file_size for info in infos)
            if total_size > MAX_UNCOMPRESSED_SIZE:
                raise MissionError("Le contenu décompressé du KMZ dépasse 100 Mo.")

            missing = REQUIRED_MEMBERS - names
            if missing:
                raise MissionError(
                    "Structure DJI incomplète. Fichier(s) absent(s) : "
                    + ", ".join(sorted(missing))
                )

            template_data = archive.read("wpmz/template.kml")
            waylines_data = archive.read("wpmz/waylines.wpml")
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile, KeyError) as exc:
        raise MissionError(f"Impossible de lire le KMZ : {exc}") from exc

    try:
        template_root = ElementTree.fromstring(template_data)
        waylines_root = ElementTree.fromstring(waylines_data)
    except ElementTree.ParseError as exc:
        raise MissionError(f"XML DJI invalide dans le KMZ : {exc}") from exc

    expected_root = "{" + KML_NAMESPACE + "}kml"
    if template_root.tag != expected_root or waylines_root.tag != expected_root:
        raise MissionError("Les fichiers DJI doivent utiliser la racine KML 2.2.")
    for root, label in ((template_root, "template.kml"), (waylines_root, "waylines.wpml")):
        if _find_wpml(root, "missionConfig") is None:
            raise MissionError(f"Configuration wpml:missionConfig absente de {label}.")
    if _find_wpml(template_root, "droneInfo") is None:
        raise MissionError("Information wpml:droneInfo absente de template.kml.")
    if _find_wpml(waylines_root, "waylineId") is None:
        raise MissionError("Information wpml:waylineId absente de waylines.wpml.")

    placemarks = waylines_root.findall(".//{" + KML_NAMESPACE + "}Placemark")
    waypoint_count = len(placemarks)
    if waypoint_count == 0:
        raise MissionError("La mission ne contient aucun waypoint Placemark.")
    for index, placemark in enumerate(placemarks):
        coordinates = placemark.find(".//{" + KML_NAMESPACE + "}coordinates")
        if coordinates is None or not (coordinates.text or "").strip():
            raise MissionError(f"Coordonnées absentes du waypoint {index}.")
        if _find_wpml(placemark, "index") is None:
            raise MissionError(f"Index WPML absent du waypoint {index}.")
        if _find_wpml(placemark, "executeHeight") is None:
            raise MissionError(f"Altitude WPML absente du waypoint {index}.")

    return MissionArchive(
        path=path,
        size=path.stat().st_size,
        sha256=hashlib.sha256(data).hexdigest(),
        waypoint_count=waypoint_count,
        members=tuple(sorted(names)),
        data=data,
    )
