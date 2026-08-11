"""Tests du transfert avec une fausse radiocommande locale."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

PROJECT_PARENT = Path(__file__).resolve().parents[2]
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))

from prog_vol.main import run
from prog_vol.missions import MissionError, inspect_mission
from prog_vol.mtp import LibMTPStorage, MountedStorage, StorageError, detect_storage
from prog_vol.transfer import TransferError, build_transfer_plan, execute_transfer
import prog_vol.transfer as transfer_module


APP_ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = APP_ROOT / "tmp"


def create_mission(path: Path, marker: str) -> None:
    template = f"""<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="http://www.dji.com/wpmz/1.0.2">
  <Document>
    <wpml:missionConfig><wpml:droneInfo><wpml:droneEnumValue>68</wpml:droneEnumValue></wpml:droneInfo></wpml:missionConfig>
    <name>{marker}</name>
  </Document>
</kml>"""
    waylines = f"""<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="http://www.dji.com/wpmz/1.0.2">
  <Document>
    <wpml:missionConfig/>
    <Folder>
      <wpml:waylineId>0</wpml:waylineId>
      <Placemark>
        <name>{marker}</name>
        <Point><coordinates>-0.6,44.8</coordinates></Point>
        <wpml:index>0</wpml:index>
        <wpml:executeHeight>50</wpml:executeHeight>
      </Placemark>
    </Folder>
  </Document>
</kml>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("wpmz/template.kml", template)
        archive.writestr("wpmz/waylines.wpml", waylines)


class MissionTests(unittest.TestCase):
    def test_rejects_incomplete_kmz(self) -> None:
        TMP_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            path = Path(directory) / "invalid.kmz"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("doc.kml", "<kml/>")
            with self.assertRaises(MissionError):
                inspect_mission(path)


class TransferTests(unittest.TestCase):
    def test_dry_run_does_not_modify_target(self) -> None:
        TMP_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.kmz"
            create_mission(source, "new")
            target = (
                root
                / "device/Android/data/dji.go.v5/files/waypoint/mission-id/mission-id.kmz"
            )
            target.parent.mkdir(parents=True)
            create_mission(target, "old")
            before = target.read_bytes()

            plan = build_transfer_plan(
                inspect_mission(source),
                MountedStorage(root / "device"),
                backup_root=root / "backups",
            )
            result = execute_transfer(plan, dry_run=True)

            self.assertFalse(result.transferred)
            self.assertEqual(target.read_bytes(), before)
            self.assertFalse(result.backup_path.exists())

    def test_transfer_backs_up_and_verifies_target(self) -> None:
        TMP_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.kmz"
            create_mission(source, "new")
            target = (
                root
                / "device/Android/data/dji.go.v5/files/waypoint/mission-id/mission-id.kmz"
            )
            target.parent.mkdir(parents=True)
            create_mission(target, "old")
            old_content = target.read_bytes()

            plan = build_transfer_plan(
                inspect_mission(source),
                MountedStorage(root / "device"),
                backup_root=root / "backups",
            )
            result = execute_transfer(plan)

            self.assertTrue(result.transferred)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(result.backup_path.read_bytes(), old_content)
            self.assertTrue(result.backup_path.with_suffix(".kmz.json").is_file())

    def test_validated_source_is_immutable_during_transfer(self) -> None:
        TMP_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.kmz"
            create_mission(source, "validated")
            inspected = inspect_mission(source)
            target = (
                root
                / "device/Android/data/dji.go.v5/files/waypoint/mission-id/mission-id.kmz"
            )
            target.parent.mkdir(parents=True)
            create_mission(target, "old")
            source.write_bytes(b"not a valid mission anymore")

            plan = build_transfer_plan(
                inspected,
                MountedStorage(root / "device"),
                backup_root=root / "backups",
            )
            execute_transfer(plan)

            self.assertEqual(target.read_bytes(), inspected.data)

    def test_manifest_failure_does_not_rewrite_remote_target(self) -> None:
        TMP_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "source.kmz"
            create_mission(source, "new")
            target = (
                root
                / "device/Android/data/dji.go.v5/files/waypoint/mission-id/mission-id.kmz"
            )
            target.parent.mkdir(parents=True)
            create_mission(target, "old")
            old_content = target.read_bytes()
            plan = build_transfer_plan(
                inspect_mission(source),
                MountedStorage(root / "device"),
                backup_root=root / "backups",
            )

            with (
                patch("prog_vol.transfer._write_manifest", side_effect=OSError("disk full")),
                patch.object(transfer_module.LOGGER, "disabled", True),
            ):
                with self.assertRaises(TransferError):
                    execute_transfer(plan)

            self.assertEqual(target.read_bytes(), old_content)

    def test_mounted_storage_rejects_path_outside_root(self) -> None:
        TMP_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            device = root / "device"
            device.mkdir()
            storage = MountedStorage(device)
            with self.assertRaises(StorageError):
                storage.from_root("../outside")

    def test_libmtp_virtual_paths_are_confined_to_storage(self) -> None:
        storage = object.__new__(LibMTPStorage)
        storage.storage_id = 0x00010001
        storage.root = "libmtp://00010001"

        waypoint = storage.from_root("Android/data/dji.go.v5/files/waypoint")

        self.assertEqual(
            waypoint,
            "libmtp://00010001/Android/data/dji.go.v5/files/waypoint",
        )
        with self.assertRaises(StorageError):
            storage.from_root("../outside")
        with self.assertRaises(StorageError):
            storage.join("libmtp://00020002", "Android")

    def test_detection_falls_back_to_native_libmtp(self) -> None:
        native = object()
        with (
            patch("prog_vol.mtp._mounted_candidates", return_value=[]),
            patch("prog_vol.mtp._gio_executable", return_value=None),
            patch("prog_vol.mtp._libmtp_candidates", return_value=[native]),
            patch("prog_vol.mtp.locate_waypoint_directory", return_value="waypoint"),
            patch("prog_vol.mtp._close_native_candidates") as close_candidates,
        ):
            selected = detect_storage()

        self.assertIs(selected, native)
        close_candidates.assert_called_once_with([native], keep=native)

    def test_cli_dry_run_uses_relative_or_absolute_source(self) -> None:
        TMP_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as directory:
            root = Path(directory)
            source = root / "mission_waypoints.kmz"
            create_mission(source, "new")
            target = (
                root
                / "device/Android/data/dji.go.v5/files/waypoint/mission-id/mission-id.kmz"
            )
            target.parent.mkdir(parents=True)
            create_mission(target, "old")
            before = target.read_bytes()

            with (
                patch("prog_vol.main.configure_logging"),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                code = run(
                    [
                        str(source),
                        "--device-root",
                        str(root / "device"),
                        "--dry-run",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
