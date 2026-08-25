"""Tests des services employés par l'interface sans ouvrir de fenêtre."""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

try:
    from prog_vol.gui import (
        DetectedMission,
        DetectionWorker,
        TransferWorker,
        split_plan_summary,
        _relative_remote_target,
    )
    from prog_vol.autonomy import BatteryProfile, BatteryState, plan_mission_split
    from prog_vol.missions import inspect_mission
    from prog_vol.mtp import MountedStorage
    from prog_vol.transfer import build_transfer_plan
except ImportError as exc:  # L'interface est optionnelle pour les tests CLI seuls.
    raise unittest.SkipTest(f"Dépendances Qt indisponibles : {exc}") from exc


def create_mission(path: Path, marker: str) -> None:
    template = f"""<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="http://www.dji.com/wpmz/1.0.2">
<Document><wpml:missionConfig><wpml:droneInfo/></wpml:missionConfig><name>{marker}</name></Document>
</kml>"""
    waylines = f"""<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:wpml="http://www.dji.com/wpmz/1.0.2">
<Document><wpml:missionConfig/><Folder><wpml:waylineId>0</wpml:waylineId>
<Placemark><name>{marker}</name><Point><coordinates>-0.6,44.8</coordinates></Point>
<wpml:index>0</wpml:index><wpml:executeHeight>50</wpml:executeHeight></Placemark>
</Folder></Document></kml>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("wpmz/template.kml", template)
        archive.writestr("wpmz/waylines.wpml", waylines)


class GuiServiceTests(unittest.TestCase):
    def test_split_summary_lists_parts_batteries_and_mid_pass_warning(self) -> None:
        waypoints = (
            (44.8000, -0.6000, 30.0),
            (44.8000, -0.5900, 30.0),
            (44.8001, -0.6000, 30.0),
            (44.8001, -0.5900, 30.0),
        )
        profile = BatteryProfile("Test", 1000, 1, 210)
        plan = plan_mission_split(
            waypoints,
            (4,),
            10,
            (BatteryState("B1", profile), BatteryState("B2", profile)),
            home_point=waypoints[0][:2],
            reserve_percent=0,
        )

        summary = split_plan_summary(plan)

        self.assertIn("Partie 1/2", summary)
        self.assertIn("B1", summary)
        self.assertIn("milieu de passe", summary)

    def test_remote_target_is_decoded_once(self) -> None:
        relative = _relative_remote_target(
            "libmtp://00010001/Android/data/dji.go.v5/files/waypoint",
            "libmtp://00010001/Android/data/dji.go.v5/files/waypoint/mission%20test/mission%20test.kmz",
            "mission test.kmz",
        )

        self.assertEqual(relative, "mission test/mission test.kmz")

    def test_detection_worker_returns_relative_targets_sorted_by_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory) / "device"
            waypoint = device / "Android/data/dji.go.v5/files/waypoint"
            older = waypoint / "older/older.kmz"
            newest = waypoint / "newest/newest.kmz"
            older.parent.mkdir(parents=True)
            newest.parent.mkdir(parents=True)
            older.write_bytes(b"old")
            newest.write_bytes(b"new")
            older.touch()
            newest.touch()
            older_stat = older.stat()
            # Les valeurs explicites évitent une égalité de timestamps sur les FS rapides.
            os.utime(older, (older_stat.st_atime, 1000))
            os.utime(newest, (older_stat.st_atime, 2000))

            with patch(
                "prog_vol.gui.detect_storage",
                return_value=MountedStorage(device),
            ):
                result = DetectionWorker().work()

        self.assertEqual([item.name for item in result.missions], ["newest.kmz", "older.kmz"])
        self.assertEqual(result.missions[0].relative_target, "newest/newest.kmz")
        self.assertIn("volume monté", result.storage_description)

    def test_transfer_worker_uses_confirmed_archive_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.kmz"
            create_mission(source, "confirmed")
            confirmed = inspect_mission(source)
            device = root / "device"
            target = (
                device
                / "Android/data/dji.go.v5/files/waypoint/mission-id/mission-id.kmz"
            )
            target.parent.mkdir(parents=True)
            create_mission(target, "old")
            stat = target.stat()
            detected = DetectedMission(
                name=target.name,
                relative_target="mission-id/mission-id.kmz",
                location=str(target),
                size=stat.st_size,
                modified=int(stat.st_mtime),
            )
            source.write_bytes(b"source modified after confirmation")
            storage = MountedStorage(device)

            def plan_with_test_backup(mission, selected_storage, *, target):
                return build_transfer_plan(
                    mission,
                    selected_storage,
                    target=target,
                    backup_root=root / "backups",
                )

            with (
                patch("prog_vol.gui.detect_storage", return_value=storage),
                patch(
                    "prog_vol.gui.build_transfer_plan",
                    side_effect=plan_with_test_backup,
                ),
            ):
                TransferWorker(confirmed, detected, storage.description).work()

            self.assertEqual(target.read_bytes(), confirmed.data)


if __name__ == "__main__":
    unittest.main()
