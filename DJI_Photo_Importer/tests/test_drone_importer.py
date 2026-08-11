from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from drone_importer.core import DronePhoto
from drone_importer.history import SyncHistory
from drone_importer.missions import group_photos_into_missions
from drone_importer.transfer import TransferError, import_missions


def make_photo(path: Path, taken_at: datetime, sequence: int = 1) -> DronePhoto:
    # Ce petit constructeur évite de répéter toutes les métadonnées dans les tests.
    stat = path.stat() if path.exists() else None
    return DronePhoto(
        name=path.name,
        source_path=path,
        relative_path=f"DCIM/DJI_001/{path.name}",
        storage_name="DJI Test-123",
        extension=path.suffix.upper(),
        size=stat.st_size if stat else 100,
        modified_at=datetime.fromtimestamp(stat.st_mtime) if stat else taken_at,
        modified_ns=stat.st_mtime_ns if stat else int(taken_at.timestamp() * 1_000_000_000),
        exif_datetime_original=taken_at,
        taken_at=taken_at,
        timestamp_source="EXIF DateTimeOriginal",
        sequence=sequence,
    )


class HistoryTests(unittest.TestCase):
    def test_first_second_and_new_photo_scans(self) -> None:
        # Le dossier temporaire garantit que le vrai drone_sync.json n'est pas touché.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history_path = root / "drone_sync.json"
            start = datetime(2026, 8, 6, 9, 14)
            first = make_photo(root / "DJI_0001.JPG", start, 1)
            second = make_photo(root / "DJI_0002.JPG", start + timedelta(seconds=3), 2)

            history = SyncHistory(history_path).load()
            imported, new = history.split_photos([first, second])
            self.assertEqual(imported, [])
            self.assertEqual(new, [first, second])

            first_destination = root / f"imported-{first.name}"
            second_destination = root / f"imported-{second.name}"
            first_destination.write_bytes(b"x" * first.size)
            second_destination.write_bytes(b"x" * second.size)
            history.mark_imported(
                first,
                mission_id="mission-test",
                destination=first_destination,
            )
            history.mark_imported(
                second,
                mission_id="mission-test",
                destination=second_destination,
            )

            reloaded = SyncHistory(history_path).load()
            imported, new = reloaded.split_photos([first, second])
            self.assertEqual(imported, [first, second])
            self.assertEqual(new, [])

            third = make_photo(root / "DJI_0003.JPG", start + timedelta(seconds=6), 3)
            imported, new = reloaded.split_photos([first, second, third])
            self.assertEqual(imported, [first, second])
            self.assertEqual(new, [third])

            temporary_files = list(root.glob(".drone_sync.json.*.tmp"))
            self.assertEqual(temporary_files, [])

            first_destination.unlink()
            imported, new = reloaded.split_photos([first, second])
            self.assertEqual(imported, [second])
            self.assertEqual(new, [first])


class MissionTests(unittest.TestCase):
    def test_groups_photos_after_configured_gap(self) -> None:
        # Trois photos proches puis une photo deux heures plus tard donnent 2 missions.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            start = datetime(2026, 8, 6, 9, 14, 21)
            photos = [
                make_photo(root / "DJI_0001.JPG", start, 1),
                make_photo(root / "DJI_0002.JPG", start + timedelta(seconds=3), 2),
                make_photo(root / "DJI_0003.JPG", start + timedelta(minutes=4), 3),
                make_photo(root / "DJI_0004.JPG", start + timedelta(hours=2), 4),
            ]

            missions = group_photos_into_missions(photos, gap_minutes=5)

            self.assertEqual(len(missions), 2)
            self.assertEqual(len(missions[0].photos), 3)
            self.assertEqual(len(missions[1].photos), 1)
            self.assertEqual(missions[0].start_at, start)
            self.assertEqual(missions[0].end_at, start + timedelta(minutes=4))
            self.assertEqual(missions[0].folder_name, "Mission_2026-08-06_09-14")


class TransferTests(unittest.TestCase):
    def test_verified_transfer_preserves_sources_and_updates_history(self) -> None:
        # Les octets sources doivent rester identiques après l'import.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            first_path = source / "DJI_0001.JPG"
            second_path = source / "DJI_0002.JPG"
            first_path.write_bytes(b"first-original-photo")
            second_path.write_bytes(b"second-original-photo")
            start = datetime(2026, 8, 6, 9, 14)
            photos = [
                make_photo(first_path, start, 1),
                make_photo(second_path, start + timedelta(seconds=3), 2),
            ]
            mission = group_photos_into_missions(photos)[0]
            history = SyncHistory(root / "drone_sync.json").load()

            summary = import_missions([mission], history, destination_root=destination)

            mission_directory = destination / "Mission_2026-08-06_09-14"
            self.assertEqual(summary.imported, 2)
            self.assertEqual((mission_directory / first_path.name).read_bytes(), first_path.read_bytes())
            self.assertEqual((mission_directory / second_path.name).read_bytes(), second_path.read_bytes())
            self.assertEqual(first_path.read_bytes(), b"first-original-photo")
            self.assertEqual(len(SyncHistory(history.path).load().records), 2)

    def test_interruption_records_only_completed_files(self) -> None:
        # La deuxième source absente simule une déconnexion en cours de mission.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = root / "DJI_0001.JPG"
            first_path.write_bytes(b"complete-photo")
            start = datetime(2026, 8, 6, 9, 14)
            first = make_photo(first_path, start, 1)
            missing = make_photo(root / "DJI_0002.JPG", start + timedelta(seconds=3), 2)
            mission = group_photos_into_missions([first, missing])[0]
            history = SyncHistory(root / "drone_sync.json").load()

            with self.assertRaises(TransferError):
                import_missions([mission], history, destination_root=root / "destination")

            reloaded = SyncHistory(history.path).load()
            self.assertTrue(reloaded.is_imported(first))
            self.assertFalse(reloaded.is_imported(missing))

    def test_rejects_changed_source_after_scan(self) -> None:
        # Une source modifiée ne doit pas être importée avec les anciennes métadonnées.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DJI_0001.JPG"
            source.write_bytes(b"original")
            photo = make_photo(source, datetime(2026, 8, 6, 9, 14), 1)
            source.write_bytes(b"modified")
            mission = group_photos_into_missions([photo])[0]

            with self.assertRaises(TransferError):
                import_missions(
                    [mission],
                    SyncHistory(root / "drone_sync.json").load(),
                    destination_root=root / "destination",
                )

    def test_rejects_same_size_corrupted_existing_destination(self) -> None:
        # Une corruption au milieu du fichier vérifie que le checksum est complet.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "DJI_0001.JPG"
            source.write_bytes(b"A" * 70_000 + b"B" * 70_000 + b"C" * 70_000)
            photo = make_photo(source, datetime(2026, 8, 6, 9, 14), 1)
            mission = group_photos_into_missions([photo])[0]
            mission_directory = root / "destination" / mission.folder_name
            mission_directory.mkdir(parents=True)
            destination = mission_directory / source.name
            destination.write_bytes(b"A" * 70_000 + b"X" * 70_000 + b"C" * 70_000)

            with self.assertRaises(TransferError):
                import_missions(
                    [mission],
                    SyncHistory(root / "drone_sync.json").load(),
                    destination_root=root / "destination",
                )

            self.assertEqual(destination.read_bytes()[70_000:140_000], b"X" * 70_000)


if __name__ == "__main__":
    unittest.main()
"""Tests du cœur métier sans dépendre d'un vrai drone ni de l'interface."""
