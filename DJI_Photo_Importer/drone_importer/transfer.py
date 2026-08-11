"""Copie vérifiée des missions et validation progressive de l'historique."""

from __future__ import annotations

import hashlib
import fcntl
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from get_latest_drone_photo import DESTINATION

from .core import DronePhoto
from .history import SyncHistory
from .missions import Mission


class TransferError(RuntimeError):
    """Signale qu'une photo ne peut pas être copiée et validée sans risque."""


@dataclass(frozen=True)
class TransferProgress:
    mission: Mission
    photo: DronePhoto
    current: int
    total: int

    @property
    def percent(self) -> int:
        return round(self.current * 100 / self.total) if self.total else 100


@dataclass(frozen=True)
class TransferSummary:
    imported: int
    already_present: int
    mission_directories: tuple[Path, ...]


ProgressCallback = Callable[[TransferProgress], None]


def _full_digest(path: Path) -> bytes:
    # Le hash complet n'est calculé que pendant un import sélectionné, jamais
    # pendant le scan général du drone.
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.digest()


def _files_match(source: Path, destination: Path, expected_size: int) -> bool:
    try:
        if source.stat().st_size != expected_size:
            return False
        if destination.stat().st_size != expected_size:
            return False
        return _full_digest(source) == _full_digest(destination)
    except OSError:
        return False


def _source_matches_scan(photo: DronePhoto) -> bool:
    try:
        stat = photo.source_path.stat()
    except OSError:
        return False
    # Une modification entre l'analyse et l'import impose une nouvelle analyse.
    return stat.st_size == photo.size and stat.st_mtime_ns == photo.modified_ns


def _copy_verified(photo: DronePhoto, destination: Path) -> tuple[bool, str]:
    source = photo.source_path
    if not source.is_file():
        raise TransferError(
            f"Le drone a ete deconnecte ou la photo est inaccessible: {photo.name}"
        )
    if not _source_matches_scan(photo):
        raise TransferError(
            f"La photo {photo.name} a change depuis l'analyse; relancez l'analyse."
        )

    if destination.exists():
        # Une reprise peut retrouver un fichier déjà copié avant l'écriture de
        # l'historique. On le valide au lieu de le recopier.
        if _files_match(source, destination, photo.size):
            return False, _full_digest(source).hex()
        raise TransferError(
            f"Un fichier different porte deja le nom {photo.name} dans "
            f"{destination.parent}. Aucun fichier n'a ete remplace."
        )

    temporary: Path | None = None
    try:
        # La copie porte un nom temporaire unique. Le nom DJI final n'apparaît
        # qu'après synchronisation et vérification complète du contenu.
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".part",
            delete=False,
        ) as target:
            temporary = Path(target.name)
            source_digest = hashlib.sha256()
            with source.open("rb") as source_stream:
                while chunk := source_stream.read(1024 * 1024):
                    target.write(chunk)
                    source_digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        shutil.copystat(source, temporary)
        if not _source_matches_scan(photo):
            raise TransferError(
                f"La photo {photo.name} a change pendant la copie; relancez l'analyse."
            )
        if temporary.stat().st_size != photo.size:
            raise TransferError(f"La verification de la copie a echoue: {photo.name}")
        if source_digest.digest() != _full_digest(temporary):
            raise TransferError(f"La verification de la copie a echoue: {photo.name}")
        os.replace(temporary, destination)
        temporary = None
        return True, source_digest.hexdigest()
    except TransferError:
        raise
    except OSError as exc:
        raise TransferError(f"Impossible de copier {photo.name}: {exc}") from exc
    finally:
        try:
            if temporary:
                temporary.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _import_lock(destination_root: Path):
    # flock est libéré automatiquement si l'application s'arrête brutalement.
    # Il empêche deux fenêtres d'écrire dans le même historique en parallèle.
    lock_path = destination_root / ".drone_import.lock"
    try:
        with lock_path.open("a+") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TransferError(
                    "Un autre import DJI est deja en cours. Attendez sa fin."
                ) from exc
            yield
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise TransferError(f"Impossible de verrouiller l'import: {exc}") from exc


def _check_destination_collisions(
    missions: list[Mission] | tuple[Mission, ...],
) -> None:
    # Toutes les collisions sont recherchées avant la première copie pour ne
    # jamais écraser deux originaux DJI qui partageraient le même nom.
    paths: set[tuple[str, str]] = set()
    for mission in missions:
        for photo in mission.photos:
            key = mission.folder_name.casefold(), photo.name.casefold()
            if key in paths:
                raise TransferError(
                    f"Deux photos utilisent le meme nom dans {mission.folder_name}: "
                    f"{photo.name}. L'import est annule sans ecrasement."
                )
            paths.add(key)


def import_missions(
    missions: list[Mission] | tuple[Mission, ...],
    history: SyncHistory,
    *,
    destination_root: Path = DESTINATION,
    progress_callback: ProgressCallback | None = None,
) -> TransferSummary:
    """Copie les missions et enregistre chaque photo seulement après vérification."""
    total = sum(len(mission.photos) for mission in missions)
    current = 0
    imported = 0
    already_present = 0
    directories: list[Path] = []

    try:
        destination_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TransferError(
            f"Impossible de creer le dossier de destination {destination_root}: {exc}"
        ) from exc

    _check_destination_collisions(missions)
    with _import_lock(destination_root):
        for mission in missions:
            mission_directory = destination_root / mission.folder_name
            try:
                mission_directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise TransferError(
                    f"Impossible de creer le dossier {mission_directory}: {exc}"
                ) from exc
            directories.append(mission_directory)

            for photo in mission.photos:
                destination = mission_directory / photo.name
                copied, checksum = _copy_verified(photo, destination)
                # Sauvegarder après chaque photo permet de reprendre exactement
                # au bon endroit si le drone est débranché pendant la mission.
                history.mark_imported(
                    photo,
                    mission_id=mission.identifier,
                    destination=destination,
                    sha256=checksum,
                )
                if copied:
                    imported += 1
                else:
                    already_present += 1
                current += 1
                if progress_callback:
                    progress_callback(
                        TransferProgress(
                            mission=mission,
                            photo=photo,
                            current=current,
                            total=total,
                        )
                    )

    return TransferSummary(
        imported=imported,
        already_present=already_present,
        mission_directories=tuple(directories),
    )
