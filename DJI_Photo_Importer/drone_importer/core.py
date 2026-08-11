"""Détection du drone et construction des objets représentant les photos."""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Ces fonctions ont déjà été validées avec le vrai drone. Le nouvel outil les
# réutilise au lieu de dupliquer la détection USB et le lecteur EXIF.
from get_latest_drone_photo import (
    IMAGE_EXTENSIONS,
    DroneDevice,
    DroneError,
    MountedStorage,
    disk_info,
    exif_datetime_original,
    filename_details,
    find_dji_devices,
    mounted_storages,
    mounted_volume_diagnostic,
)


@dataclass(frozen=True)
class DronePhoto:
    """Métadonnées utiles d'une photo originale encore présente sur le drone."""

    name: str
    source_path: Path
    relative_path: str
    storage_name: str
    extension: str
    size: int
    modified_at: datetime
    modified_ns: int
    exif_datetime_original: datetime | None
    taken_at: datetime
    timestamp_source: str
    sequence: int

    @property
    def identity(self) -> str:
        """Construit une identité stable sans relire tout le contenu de l'image."""
        # Lire entièrement plusieurs centaines de fichiers ralentirait fortement
        # le scan USB. Ces informations combinées sont suffisamment discriminantes.
        parts = (
            self.storage_name.casefold(),
            self.relative_path.casefold(),
            str(self.size),
            str(self.modified_ns),
            self.taken_at.isoformat(timespec="seconds"),
        )
        return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()

    @property
    def sort_key(self) -> tuple[datetime, int, str]:
        return self.taken_at, self.sequence, self.name.casefold()


@dataclass(frozen=True)
class ScanResult:
    """Résultat complet du scan, y compris les diagnostics non bloquants."""

    devices: tuple[DroneDevice, ...]
    storages: tuple[MountedStorage, ...]
    photos: tuple[DronePhoto, ...]
    photo_directories: tuple[tuple[Path, int], ...]
    warnings: tuple[str, ...]


def _allowed_extensions(include_jpeg: bool, include_dng: bool) -> set[str]:
    extensions: set[str] = set()
    if include_jpeg:
        extensions.update({".jpg", ".jpeg"})
    if include_dng:
        extensions.add(".dng")
    return extensions


def _photo_from_path(
    path: Path,
    storage: MountedStorage,
    storage_identity: str,
) -> DronePhoto:
    stat = path.stat()
    modified_at = datetime.fromtimestamp(stat.st_mtime)
    exif_date = exif_datetime_original(path, stat.st_size)
    filename_date, sequence = filename_details(path.name)

    # La date EXIF correspond à la prise de vue réelle. La modification du
    # fichier reste le repli principal demandé lorsqu'elle est absente.
    if exif_date is not None:
        taken_at = exif_date
        source = "EXIF DateTimeOriginal"
    elif modified_at is not None:
        taken_at = modified_at
        source = "date de modification"
    elif filename_date is not None:  # pragma: no cover - stat always provides mtime on macOS
        taken_at = filename_date
        source = "nom DJI"
    else:  # pragma: no cover - kept for unusual filesystems
        raise ValueError("aucune date exploitable")

    return DronePhoto(
        name=path.name,
        source_path=path,
        relative_path=path.relative_to(storage.mount_point).as_posix(),
        storage_name=storage_identity,
        extension=path.suffix.upper(),
        size=stat.st_size,
        modified_at=modified_at,
        modified_ns=stat.st_mtime_ns,
        exif_datetime_original=exif_date,
        taken_at=taken_at,
        timestamp_source=source,
        sequence=sequence,
    )


def scan_drone_photos(
    *,
    include_jpeg: bool = True,
    include_dng: bool = True,
) -> ScanResult:
    """Détecte les stockages DJI et retourne toutes les photos demandées."""
    allowed = _allowed_extensions(include_jpeg, include_dng)
    if not allowed:
        raise DroneError("Aucun format d'image n'est selectionne.")
    if not allowed.issubset(IMAGE_EXTENSIONS):  # Defensive check if legacy constants change.
        raise DroneError("Configuration de formats invalide.")

    devices = find_dji_devices()
    if not devices:
        raise DroneError(
            "Aucun drone DJI detecte. Verifiez l'alimentation, le cable USB de "
            f"donnees et le mode de transfert. {mounted_volume_diagnostic()}"
        )

    storages = mounted_storages(devices)
    if not storages:
        media = ", ".join(
            name for device in devices for name in device.bsd_names
        ) or "aucun media USB"
        raise DroneError(
            "Le drone DJI est detecte, mais son stockage n'est pas accessible. "
            f"Media detecte: {media}. {mounted_volume_diagnostic()}"
        )

    photos: list[DronePhoto] = []
    directories: Counter[Path] = Counter()
    warnings: list[str] = []

    for storage in storages:
        root = storage.mount_point
        info = disk_info(storage.bsd_name) or {}
        # L'UUID distingue notamment deux cartes qui auraient la même arborescence.
        volume_uuid = str(info.get("VolumeUUID") or "uuid-inconnu")
        storage_identity = f"{storage.device_name}|{volume_uuid}"
        if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
            warnings.append(f"Volume inaccessible en lecture: {root}")
            continue
        try:
            def walk_error(exc: OSError) -> None:
                warnings.append(f"Dossier inaccessible pendant le scan: {exc}")

            for current, dir_names, file_names in os.walk(
                root,
                followlinks=False,
                onerror=walk_error,
            ):
                # Les caches macOS et dossiers système ne contiennent pas les
                # originaux DJI et peuvent provoquer des erreurs de permission.
                dir_names[:] = [
                    name
                    for name in dir_names
                    if not name.startswith(".")
                    and name not in {"System Volume Information"}
                ]
                current_path = Path(current)
                for file_name in file_names:
                    # Les fichiers ._ créés par macOS sont des métadonnées
                    # AppleDouble de quelques kilo-octets, pas des photos DJI.
                    if file_name.startswith("."):
                        continue
                    path = current_path / file_name
                    if path.suffix.casefold() not in allowed:
                        continue
                    try:
                        if not path.is_file():
                            continue
                        photos.append(_photo_from_path(path, storage, storage_identity))
                        directories[current_path] += 1
                    except (OSError, ValueError) as exc:
                        warnings.append(f"Photo ignoree ({path}): {exc}")
        except OSError as exc:
            warnings.append(f"Parcours interrompu sur {root}: {exc}")

    photos.sort(key=lambda photo: photo.sort_key)
    return ScanResult(
        devices=tuple(devices),
        storages=tuple(storages),
        photos=tuple(photos),
        photo_directories=tuple(sorted(directories.items(), key=lambda item: str(item[0]))),
        warnings=tuple(warnings),
    )
