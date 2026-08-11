#!/usr/bin/env python3
"""Détecte un drone DJI en USB et copie uniquement sa dernière photo."""

from __future__ import annotations

import io
import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".dng"}
# Le dossier suit le script afin que toute l'application reste déplaçable.
DESTINATION = Path(__file__).resolve().parent / "DronePhotos"
DJI_DATE_RE = re.compile(r"(?:^|_)(\d{14})(?:_|\.)", re.IGNORECASE)
DJI_SEQUENCE_RE = re.compile(r"_(\d+)(?:_[^.]+)?\.[^.]+$", re.IGNORECASE)
TIFF_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


class DroneError(RuntimeError):
    """Erreur prévue de détection ou d'accès au stockage du drone."""


@dataclass(frozen=True)
class DroneDevice:
    name: str
    manufacturer: str
    bsd_names: tuple[str, ...]


@dataclass(frozen=True)
class MountedStorage:
    device_name: str
    bsd_name: str
    mount_point: Path
    filesystem: str
    size: int


@dataclass(frozen=True)
class Photo:
    path: Path
    size: int
    taken_at: datetime
    timestamp_source: str
    filename_timestamp: float
    sequence: int

    @property
    def sort_key(self) -> tuple[float, float, int, str]:
        return (
            self.taken_at.timestamp(),
            self.filename_timestamp,
            self.sequence,
            self.path.name.casefold(),
        )


def run_command(command: list[str], *, timeout: int = 30) -> bytes:
    """Lance un outil macOS et transforme ses erreurs en messages lisibles."""
    try:
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise DroneError(f"Outil macOS introuvable: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DroneError(f"Commande trop longue: {' '.join(command)}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise DroneError(
            f"Echec de {' '.join(command)}" + (f": {detail}" if detail else "")
        ) from exc
    return result.stdout


def macos_version() -> str:
    try:
        output = run_command(["/usr/bin/sw_vers", "-productVersion"])
        return output.decode().strip()
    except DroneError:
        return "inconnue"


def collect_bsd_names(value: object) -> set[str]:
    # system_profiler renvoie un arbre JSON dont la profondeur dépend des hubs USB.
    # Cette récursion récupère tous les noms diskN situés sous le périphérique DJI.
    names: set[str] = set()
    if isinstance(value, dict):
        bsd_name = value.get("bsd_name")
        if isinstance(bsd_name, str):
            names.add(bsd_name)
        for child in value.values():
            names.update(collect_bsd_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(collect_bsd_names(child))
    return names


def find_dji_devices() -> list[DroneDevice]:
    """Trouve les périphériques DJI dans l'inventaire USB de macOS."""
    raw = run_command(
        ["/usr/sbin/system_profiler", "SPUSBDataType", "-json"], timeout=60
    )
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DroneError("La liste USB de macOS est illisible.") from exc

    devices: list[DroneDevice] = []

    def visit(value: object) -> None:
        # Le fabricant est le meilleur indice, mais le nom et le vendor_id sont
        # aussi examinés pour tolérer les différences entre modèles DJI.
        if isinstance(value, dict):
            name = str(value.get("_name", ""))
            manufacturer = str(value.get("manufacturer", ""))
            vendor = str(value.get("vendor_id", ""))
            is_dji = "dji" in f"{name} {manufacturer} {vendor}".casefold()
            if is_dji:
                devices.append(
                    DroneDevice(
                        name=name or "DJI USB",
                        manufacturer=manufacturer or "DJI",
                        bsd_names=tuple(sorted(collect_bsd_names(value))),
                    )
                )
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(data.get("SPUSBDataType", []))
    return devices


def disk_info(bsd_name: str) -> dict[str, object] | None:
    try:
        raw = run_command(
            ["/usr/sbin/diskutil", "info", "-plist", f"/dev/{bsd_name}"]
        )
        value = plistlib.loads(raw)
        return value if isinstance(value, dict) else None
    except (DroneError, plistlib.InvalidFileException):
        return None


def related_disk_names(bsd_name: str) -> list[str]:
    # Un support peut apparaître comme disk2 dans l'USB mais être monté via une
    # partition disk2s1. Les deux formes doivent donc être inspectées.
    names = {bsd_name}
    info = disk_info(bsd_name)
    if info:
        parent = info.get("ParentWholeDisk")
        if isinstance(parent, str):
            names.add(parent)

    whole_disks = set(names)
    try:
        listing = plistlib.loads(run_command(["/usr/sbin/diskutil", "list", "-plist"]))
        all_disks = listing.get("AllDisks", []) if isinstance(listing, dict) else []
        for candidate in all_disks:
            if not isinstance(candidate, str):
                continue
            if any(re.fullmatch(re.escape(root) + r"(?:s\d+)?", candidate) for root in whole_disks):
                names.add(candidate)
    except (DroneError, plistlib.InvalidFileException):
        pass
    return sorted(names)


def mounted_storages(devices: list[DroneDevice]) -> list[MountedStorage]:
    """Associe les médias USB DJI à leurs points de montage dans /Volumes."""
    storages: list[MountedStorage] = []
    seen_mounts: set[Path] = set()
    for device in devices:
        for media_bsd_name in device.bsd_names:
            for bsd_name in related_disk_names(media_bsd_name):
                info = disk_info(bsd_name)
                if not info:
                    continue
                mount_value = info.get("MountPoint")
                if not isinstance(mount_value, str) or not mount_value:
                    continue
                mount_point = Path(mount_value)
                if mount_point in seen_mounts:
                    continue
                seen_mounts.add(mount_point)
                storages.append(
                    MountedStorage(
                        device_name=device.name,
                        bsd_name=bsd_name,
                        mount_point=mount_point,
                        filesystem=str(
                            info.get("FilesystemUserVisibleName")
                            or info.get("FilesystemType")
                            or "inconnu"
                        ),
                        size=int(info.get("TotalSize") or info.get("Size") or 0),
                    )
                )
    return storages


def mounted_volume_diagnostic() -> str:
    try:
        volumes = sorted(path.name for path in Path("/Volumes").iterdir())
    except OSError as exc:
        return f"Volumes montes illisibles: {exc}"
    return "Volumes montes: " + (", ".join(volumes) if volumes else "aucun")


def read_exact_at(stream: BinaryIO, offset: int, size: int, limit: int) -> bytes:
    # Les limites évitent qu'une métadonnée endommagée provoque une lecture en
    # dehors du bloc EXIF ou du fichier DNG.
    if offset < 0 or size < 0 or offset + size > limit:
        raise ValueError("Offset TIFF invalide")
    stream.seek(offset)
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("Metadonnees TIFF tronquees")
    return data


def tiff_datetime_original(stream: BinaryIO, limit: int) -> datetime | None:
    """Lit DateTimeOriginal dans une structure TIFF utilisée aussi par les DNG."""
    header = read_exact_at(stream, 0, 8, limit)
    if header[:2] == b"II":
        endian = "<"
    elif header[:2] == b"MM":
        endian = ">"
    else:
        return None
    if struct.unpack(endian + "H", header[2:4])[0] != 42:
        return None

    first_ifd = struct.unpack(endian + "I", header[4:8])[0]
    pending = [first_ifd]
    visited: set[int] = set()

    # Les IFD forment un petit graphe. La liste pending permet de suivre les
    # sous-IFD EXIF tout en empêchant les boucles de métadonnées corrompues.
    while pending and len(visited) < 32:
        ifd_offset = pending.pop()
        if ifd_offset in visited or ifd_offset <= 0:
            continue
        visited.add(ifd_offset)
        count_data = read_exact_at(stream, ifd_offset, 2, limit)
        count = struct.unpack(endian + "H", count_data)[0]
        if count > 4096:
            raise ValueError("IFD TIFF anormalement grand")
        entries = read_exact_at(stream, ifd_offset + 2, count * 12 + 4, limit)

        for index in range(count):
            entry = entries[index * 12 : index * 12 + 12]
            tag, value_type, value_count = struct.unpack(endian + "HHI", entry[:8])
            type_size = TIFF_TYPE_SIZES.get(value_type)
            if not type_size or value_count > 1_000_000:
                continue
            value_size = type_size * value_count
            if value_size <= 4:
                value = entry[8 : 8 + value_size]
            else:
                value_offset = struct.unpack(endian + "I", entry[8:12])[0]
                value = read_exact_at(stream, value_offset, value_size, limit)

            if tag == 0x9003 and value_type == 2:
                text = value.split(b"\0", 1)[0].decode("ascii", errors="strict")
                try:
                    return datetime.strptime(text[:19], "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    continue

            if tag in {0x8769, 0x014A} and value_type in {3, 4}:
                item_format = "H" if value_type == 3 else "I"
                item_size = 2 if value_type == 3 else 4
                for item_offset in range(0, len(value), item_size):
                    pending.append(
                        struct.unpack(
                            endian + item_format,
                            value[item_offset : item_offset + item_size],
                        )[0]
                    )

        next_ifd = struct.unpack(endian + "I", entries[count * 12 : count * 12 + 4])[0]
        if next_ifd:
            pending.append(next_ifd)
    return None


def jpeg_datetime_original(stream: BinaryIO, file_size: int) -> datetime | None:
    """Parcourt les segments JPEG jusqu'au bloc APP1 qui contient l'EXIF."""
    if stream.read(2) != b"\xff\xd8":
        return None
    while stream.tell() < file_size:
        byte = stream.read(1)
        while byte and byte != b"\xff":
            byte = stream.read(1)
        if not byte:
            return None
        marker = stream.read(1)
        while marker == b"\xff":
            marker = stream.read(1)
        if not marker or marker in {b"\xd9", b"\xda"}:
            return None
        marker_value = marker[0]
        if marker_value == 0x01 or 0xD0 <= marker_value <= 0xD7:
            continue
        length_data = stream.read(2)
        if len(length_data) != 2:
            return None
        segment_size = struct.unpack(">H", length_data)[0] - 2
        if segment_size < 0 or segment_size > file_size - stream.tell():
            return None
        # APP1 est le segment standard qui contient l'en-tête Exif\0\0.
        if marker_value == 0xE1:
            payload = stream.read(segment_size)
            if payload.startswith(b"Exif\0\0"):
                tiff = io.BytesIO(payload[6:])
                return tiff_datetime_original(tiff, len(payload) - 6)
        else:
            stream.seek(segment_size, os.SEEK_CUR)
    return None


def exif_datetime_original(path: Path, file_size: int) -> datetime | None:
    """Choisit le lecteur EXIF adapté à l'extension sans dépendance externe."""
    try:
        with path.open("rb") as stream:
            if path.suffix.casefold() in {".jpg", ".jpeg"}:
                return jpeg_datetime_original(stream, file_size)
            if path.suffix.casefold() == ".dng":
                return tiff_datetime_original(stream, file_size)
    except (OSError, UnicodeDecodeError, ValueError, struct.error):
        return None
    return None


def filename_details(name: str) -> tuple[datetime | None, int]:
    # Les noms récents DJI contiennent souvent YYYYMMDDHHMMSS puis un compteur.
    # Ces valeurs servent uniquement de repli ou de départage.
    timestamp_match = DJI_DATE_RE.search(name)
    sequence_match = DJI_SEQUENCE_RE.search(name)
    filename_date = None
    if timestamp_match:
        try:
            filename_date = datetime.strptime(timestamp_match.group(1), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    sequence = int(sequence_match.group(1)) if sequence_match else -1
    return filename_date, sequence


def scan_photos(storages: list[MountedStorage]) -> tuple[list[Photo], Counter[Path], list[str]]:
    """Parcourt récursivement les stockages et construit les métadonnées utiles."""
    photos: list[Photo] = []
    directories: Counter[Path] = Counter()
    warnings: list[str] = []

    for storage in storages:
        root = storage.mount_point
        if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
            warnings.append(f"Volume inaccessible en lecture: {root}")
            continue
        try:
            for current, dir_names, file_names in os.walk(root, followlinks=False):
                # Les dossiers cachés de macOS ne contiennent pas les originaux.
                dir_names[:] = [
                    name
                    for name in dir_names
                    if not name.startswith(".") and name not in {"System Volume Information"}
                ]
                current_path = Path(current)
                for file_name in file_names:
                    # Un fichier caché ._Nom.JPG est un compagnon AppleDouble
                    # de macOS et ne doit jamais être traité comme un original.
                    if file_name.startswith("."):
                        continue
                    path = current_path / file_name
                    if path.suffix.casefold() not in IMAGE_EXTENSIONS:
                        continue
                    try:
                        stat = path.stat()
                    except OSError as exc:
                        warnings.append(f"Fichier ignore ({path}): {exc}")
                        continue
                    if not path.is_file():
                        continue

                    exif_date = exif_datetime_original(path, stat.st_size)
                    filename_date, sequence = filename_details(file_name)
                    # La date EXIF est prioritaire, puis vient la modification du
                    # fichier et enfin la date encodée dans le nom DJI.
                    if exif_date is not None:
                        taken_at = exif_date
                        source = "EXIF DateTimeOriginal"
                    else:
                        try:
                            taken_at = datetime.fromtimestamp(stat.st_mtime)
                            source = "date de modification"
                        except (OSError, OverflowError, ValueError):
                            if filename_date is None:
                                warnings.append(f"Date introuvable, fichier ignore: {path}")
                                continue
                            taken_at = filename_date
                            source = "nom DJI"

                    photos.append(
                        Photo(
                            path=path,
                            size=stat.st_size,
                            taken_at=taken_at,
                            timestamp_source=source,
                            filename_timestamp=(
                                filename_date.timestamp() if filename_date else 0.0
                            ),
                            sequence=sequence,
                        )
                    )
                    directories[current_path] += 1
        except OSError as exc:
            warnings.append(f"Parcours interrompu sur {root}: {exc}")
    return photos, directories, warnings


def human_size(size: int) -> str:
    """Convertit un nombre d'octets en valeur lisible pour l'utilisateur."""
    value = float(size)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if value < 1024 or unit == "To":
            return f"{value:.0f} {unit}" if unit == "o" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} o"


def relative_to_storage(path: Path, storages: list[MountedStorage]) -> str:
    for storage in storages:
        try:
            return str(path.relative_to(storage.mount_point))
        except ValueError:
            continue
    return str(path)


def print_diagnostics(
    devices: list[DroneDevice],
    storages: list[MountedStorage],
    directories: Counter[Path],
) -> None:
    """Affiche ce que macOS expose réellement avant de présenter les photos."""
    print("=== Detection DJI ===")
    print(f"macOS: {macos_version()}")
    for device in devices:
        media = ", ".join(device.bsd_names) if device.bsd_names else "aucun disque"
        print(f"Drone USB: {device.name} ({device.manufacturer}), media: {media}")
    for storage in storages:
        print(
            f"Stockage monte: /dev/{storage.bsd_name} -> {storage.mount_point} "
            f"[{storage.filesystem}, {human_size(storage.size)}]"
        )
    print(
        "Type physique: macOS expose un stockage de masse USB; "
        "interne/microSD non precise par le peripherique."
    )
    print("\n=== Dossiers contenant des originaux ===")
    for directory, count in sorted(directories.items(), key=lambda item: str(item[0])):
        print(f"- {relative_to_storage(directory, storages)} ({count} image(s))")


def main() -> int:
    # Ce mode historique reste volontairement simple : il analyse, trie toutes
    # les photos puis copie seulement la plus récente.
    if sys.platform != "darwin":
        raise DroneError("Ce script necessite macOS.")

    devices = find_dji_devices()
    if not devices:
        raise DroneError(
            "Aucun peripherique USB DJI detecte. Verifiez que le drone est allume, "
            "que le cable transporte les donnees et que le mode USB est actif.\n"
            + mounted_volume_diagnostic()
        )

    storages = mounted_storages(devices)
    if not storages:
        details = "; ".join(
            f"{device.name}: "
            + (", ".join(device.bsd_names) if device.bsd_names else "aucun media USB")
            for device in devices
        )
        raise DroneError(
            "Le DJI est detecte, mais aucun de ses stockages n'est monte et accessible. "
            "Il peut etre en mode MTP ou attendre l'autorisation USB.\n"
            f"USB: {details}\n{mounted_volume_diagnostic()}"
        )

    photos, directories, warnings = scan_photos(storages)
    print_diagnostics(devices, storages, directories)

    if warnings:
        print("\n=== Avertissements ===", file=sys.stderr)
        for warning in warnings[:10]:
            print(f"- {warning}", file=sys.stderr)
        if len(warnings) > 10:
            print(f"- ... {len(warnings) - 10} autre(s)", file=sys.stderr)

    if not photos:
        searched = ", ".join(str(storage.mount_point) for storage in storages)
        raise DroneError(
            "Aucune photo .JPG, .JPEG ou .DNG trouvee sur le stockage DJI. "
            f"Volumes parcourus recursivement: {searched}"
        )

    photos.sort(key=lambda photo: photo.sort_key, reverse=True)
    print(f"\n=== 10 images les plus recentes (sur {len(photos)}) ===")
    for index, photo in enumerate(photos[:10], start=1):
        print(
            f"{index:2}. {photo.taken_at:%Y-%m-%d %H:%M:%S} "
            f"[{photo.timestamp_source}] {photo.path.name} "
            f"({human_size(photo.size)})"
        )

    latest = photos[0]
    DESTINATION.mkdir(parents=True, exist_ok=True)
    destination_path = DESTINATION / latest.path.name
    # copy2 conserve les dates du fichier et ne modifie jamais la source.
    shutil.copy2(latest.path, destination_path)

    print("\n=== Derniere photo ===")
    print(f"Nom: {latest.path.name}")
    print(f"Chemin sur le drone: {latest.path}")
    print(f"Date de prise de vue: {latest.taken_at:%Y-%m-%d %H:%M:%S}")
    print(f"Source de la date: {latest.timestamp_source}")
    print(f"Taille: {human_size(latest.size)} ({latest.size} octets)")
    print(f"Format: {latest.path.suffix.lstrip('.').upper()}")
    print(f"Fichier copie: {destination_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DroneError as exc:
        print(f"ERREUR: {exc}", file=sys.stderr)
        raise SystemExit(1)
