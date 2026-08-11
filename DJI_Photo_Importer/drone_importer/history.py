"""Historique persistant des photos importées, enregistré de façon atomique."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .core import DronePhoto


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HISTORY_PATH = PROJECT_ROOT / "drone_sync.json"


class HistoryError(RuntimeError):
    """Signale que l'historique ne peut pas être lu ou écrit sans risque."""


class SyncHistory:
    def __init__(self, path: Path = DEFAULT_HISTORY_PATH) -> None:
        self.path = path
        self.records: dict[str, dict[str, object]] = {}

    def load(self) -> "SyncHistory":
        # L'absence du fichier est normale avant le premier import.
        if not self.path.exists():
            self.records = {}
            return self
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise HistoryError(
                f"L'historique {self.path} est illisible. Il n'a pas ete modifie."
            ) from exc

        if not isinstance(data, dict) or data.get("version") != 1:
            raise HistoryError(f"Format d'historique non reconnu: {self.path}")
        records = data.get("photos")
        if not isinstance(records, dict) or not all(
            isinstance(key, str) and isinstance(value, dict)
            for key, value in records.items()
        ):
            raise HistoryError(f"Contenu d'historique invalide: {self.path}")
        self.records = records
        return self

    def is_imported(self, photo: DronePhoto) -> bool:
        record = self.records.get(photo.identity)
        if not record:
            return False
        destination = record.get("destination")
        expected_size = record.get("size")
        if not isinstance(destination, str) or not isinstance(expected_size, int):
            return False
        # Une entrée JSON seule ne suffit pas : si la copie locale a été effacée,
        # la photo doit être proposée à nouveau à l'utilisateur.
        try:
            path = Path(destination)
            return path.is_file() and path.stat().st_size == expected_size
        except OSError:
            return False

    def split_photos(
        self, photos: tuple[DronePhoto, ...] | list[DronePhoto]
    ) -> tuple[list[DronePhoto], list[DronePhoto]]:
        imported: list[DronePhoto] = []
        new: list[DronePhoto] = []
        for photo in photos:
            (imported if self.is_imported(photo) else new).append(photo)
        return imported, new

    def mark_imported(
        self,
        photo: DronePhoto,
        *,
        mission_id: str,
        destination: Path,
        sha256: str | None = None,
    ) -> None:
        # Cette méthode n'est appelée qu'après la copie et son checksum.
        self.records[photo.identity] = {
            "name": photo.name,
            "source_path": str(photo.source_path),
            "source_relative_path": photo.relative_path,
            "storage_name": photo.storage_name,
            "size": photo.size,
            "date_taken": photo.taken_at.isoformat(timespec="seconds"),
            "date_modified": photo.modified_at.isoformat(timespec="seconds"),
            "date_transferred": datetime.now().astimezone().isoformat(timespec="seconds"),
            "mission": mission_id,
            "destination": str(destination),
            "sha256": sha256,
        }
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "photos": self.records,
        }
        temporary_name: str | None = None
        # Le JSON est écrit dans le même dossier puis remplacé d'un seul coup.
        # Un plantage ne peut donc pas laisser la moitié d'un fichier JSON.
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_name = stream.name
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.path)
            temporary_name = None
        except OSError as exc:
            raise HistoryError(f"Impossible d'ecrire l'historique {self.path}: {exc}") from exc
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink()
                except OSError:
                    pass
