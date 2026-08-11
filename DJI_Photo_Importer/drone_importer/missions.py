"""Regroupement chronologique des photos en missions, sans dépendance à la GUI."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from .core import DronePhoto
from .core import ScanResult
from .history import SyncHistory


@dataclass(frozen=True)
class Mission:
    """Mission détectée et photos de cette mission encore proposées à l'import."""

    identifier: str
    number: int
    photos: tuple[DronePhoto, ...]
    start_at: datetime
    end_at: datetime
    first_photo: DronePhoto
    last_photo: DronePhoto
    total_size: int

    @property
    def duration(self) -> timedelta:
        return self.end_at - self.start_at

    @property
    def folder_name(self) -> str:
        return f"Mission_{self.start_at:%Y-%m-%d_%H-%M}"


@dataclass(frozen=True)
class ImportPlan:
    """Informations prêtes à être affichées dans le terminal ou l'interface."""

    scan: ScanResult
    imported_count: int
    new_count: int
    missions: tuple[Mission, ...]


def _build_mission(photos: list[DronePhoto], number: int) -> Mission:
    first = photos[0]
    last = photos[-1]
    # Le suffixe rend l'identifiant stable même si deux missions commencent à
    # la même seconde. Il reste interne et n'alourdit pas le nom du dossier.
    digest = hashlib.sha256(
        f"{first.identity}\0{last.identity}".encode("ascii")
    ).hexdigest()[:12]
    return Mission(
        identifier=f"mission-{first.taken_at:%Y%m%d-%H%M%S}-{digest}",
        number=number,
        photos=tuple(photos),
        start_at=first.taken_at,
        end_at=last.taken_at,
        first_photo=first,
        last_photo=last,
        total_size=sum(photo.size for photo in photos),
    )


def group_photos_into_missions(
    photos: tuple[DronePhoto, ...] | list[DronePhoto],
    gap_minutes: int | float = 5,
) -> list[Mission]:
    """Crée une mission lorsque l'écart avec la photo précédente dépasse le seuil."""
    if gap_minutes <= 0:
        raise ValueError("Le seuil entre missions doit etre superieur a zero.")
    ordered = sorted(photos, key=lambda photo: photo.sort_key)
    if not ordered:
        return []

    maximum_gap = timedelta(minutes=gap_minutes)
    groups: list[list[DronePhoto]] = [[ordered[0]]]
    for photo in ordered[1:]:
        # L'écart est toujours mesuré entre deux photos consécutives, pas entre
        # la première et la dernière photo de la mission.
        if photo.taken_at - groups[-1][-1].taken_at > maximum_gap:
            groups.append([photo])
        else:
            groups[-1].append(photo)
    return [_build_mission(group, number) for number, group in enumerate(groups, 1)]


def missions_to_import(
    missions: list[Mission],
    history: SyncHistory,
    *,
    ignore_imported: bool = True,
) -> list[Mission]:
    """Conserve les bornes de mission mais retire les photos déjà importées."""
    selected: list[Mission] = []
    for mission in missions:
        pending = (
            [photo for photo in mission.photos if not history.is_imported(photo)]
            if ignore_imported
            else list(mission.photos)
        )
        if not pending:
            continue
        # Garder les heures de la mission complète stabilise le nom du dossier
        # lorsqu'un transfert interrompu reprend avec seulement quelques photos.
        selected.append(
            Mission(
                identifier=mission.identifier,
                number=mission.number,
                photos=tuple(pending),
                start_at=mission.start_at,
                end_at=mission.end_at,
                first_photo=mission.first_photo,
                last_photo=mission.last_photo,
                total_size=sum(photo.size for photo in pending),
            )
        )
    return selected


def build_import_plan(
    scan: ScanResult,
    history: SyncHistory,
    *,
    gap_minutes: int = 5,
    ignore_imported: bool = True,
) -> ImportPlan:
    # Les missions sont formées avec toutes les photos avant d'enlever celles de
    # l'historique. Cela évite de découper différemment une mission lors d'une reprise.
    imported, new = history.split_photos(scan.photos)
    all_missions = group_photos_into_missions(scan.photos, gap_minutes)
    pending = missions_to_import(
        all_missions,
        history,
        ignore_imported=ignore_imported,
    )
    return ImportPlan(
        scan=scan,
        imported_count=len(imported),
        new_count=len(new),
        missions=tuple(pending),
    )
