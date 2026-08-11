"""Préparation, sauvegarde et transfert vérifié d'une mission DJI."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

if __package__:
    from .missions import MissionArchive, file_sha256
    from .mtp import (
        GioMTPStorage,
        LibMTPStorage,
        MountedStorage,
        RemoteEntry,
        StorageError,
        list_mission_files,
        locate_waypoint_directory,
    )
else:  # Permet aussi l'exécution directe depuis le dossier prog_vol.
    from missions import MissionArchive, file_sha256
    from mtp import (
        GioMTPStorage,
        LibMTPStorage,
        MountedStorage,
        RemoteEntry,
        StorageError,
        list_mission_files,
        locate_waypoint_directory,
    )


APP_ROOT = Path(__file__).resolve().parent
BACKUP_ROOT = APP_ROOT / "backups"
TMP_ROOT = APP_ROOT / "tmp"
LOGGER = logging.getLogger("prog_vol")


class TransferError(RuntimeError):
    """Le transfert ne peut pas être terminé et vérifié."""


@dataclass(frozen=True)
class TransferPlan:
    mission: MissionArchive
    storage: MountedStorage | GioMTPStorage | LibMTPStorage
    waypoint_directory: str
    target: RemoteEntry
    backup_path: Path


@dataclass(frozen=True)
class TransferResult:
    transferred: bool
    target: str
    backup_path: Path
    sha256: str


def _target_by_location(
    missions: list[RemoteEntry], target: str | None, storage, waypoint_directory: str
) -> RemoteEntry:
    if not missions:
        raise TransferError(
            "Aucune mission .kmz n'existe dans DJI Fly. Créez d'abord une mission "
            "brouillon dans DJI Fly, puis relancez le programme."
        )
    if not target:
        # DJI Fly stocke normalement IDENTIFIANT/IDENTIFIANT.kmz. Les autres
        # KMZ sont ignorés pour ne pas cibler une sauvegarde ou un fichier annexe.
        plausible = []
        for mission in missions:
            parent_name = mission.location.rstrip("/").rsplit("/", 2)[-2]
            if Path(mission.name).stem.casefold() == parent_name.casefold():
                plausible.append(mission)
        if not plausible:
            raise TransferError(
                "Aucune cible de forme IDENTIFIANT/IDENTIFIANT.kmz n'a été trouvée. "
                "Indiquez explicitement --target."
            )
        if len(plausible) == 1:
            return plausible[0]
        newest_time = max(item.modified for item in plausible)
        newest = [item for item in plausible if item.modified == newest_time]
        if newest_time == 0 or len(newest) != 1:
            raise TransferError(
                "Impossible d'identifier sûrement la mission la plus récente. "
                "Relancez avec --target NOM_DU_FICHIER.kmz."
            )
        return newest[0]

    requested = target if target.startswith("mtp://") else storage.join(
        waypoint_directory, *Path(target).parts
    )
    matching = [
        mission
        for mission in missions
        if mission.location == requested or mission.name == target
    ]
    if len(matching) == 1:
        return matching[0]
    if len(matching) > 1:
        raise TransferError(
            f"Plusieurs missions portent le nom {target}. Indiquez leur chemin relatif."
        )
    raise TransferError(f"Mission cible introuvable : {target}")


def build_transfer_plan(
    mission: MissionArchive,
    storage: MountedStorage | GioMTPStorage | LibMTPStorage,
    *,
    waypoint_override: str | None = None,
    target: str | None = None,
    backup_root: Path = BACKUP_ROOT,
) -> TransferPlan:
    waypoint_directory = locate_waypoint_directory(storage, waypoint_override)
    remote_missions = list_mission_files(storage, waypoint_directory)
    selected = _target_by_location(remote_missions, target, storage, waypoint_directory)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_name = selected.name.replace("/", "_")
    backup_path = backup_root / f"{timestamp}_{safe_name}"
    return TransferPlan(
        mission=mission,
        storage=storage,
        waypoint_directory=waypoint_directory,
        target=selected,
        backup_path=backup_path,
    )


def _write_manifest(plan: TransferPlan, backup_sha256: str) -> None:
    manifest = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(plan.mission.path),
        "source_sha256": plan.mission.sha256,
        "target": plan.target.location,
        "backup": str(plan.backup_path),
        "backup_sha256": backup_sha256,
    }
    path = plan.backup_path.with_suffix(plan.backup_path.suffix + ".json")
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@contextmanager
def _immutable_mission_file(mission: MissionArchive):
    """Matérialise les octets déjà validés afin d'éviter une course sur la source."""
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=TMP_ROOT,
            prefix="mission-validated-",
            suffix=".kmz",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(mission.data)
            stream.flush()
            os.fsync(stream.fileno())
        yield temporary
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def execute_transfer(plan: TransferPlan, *, dry_run: bool = False) -> TransferResult:
    """Sauvegarde la cible, la remplace puis compare taille et SHA-256."""
    if dry_run:
        LOGGER.info("Simulation : aucune écriture sur %s", plan.target.location)
        return TransferResult(
            transferred=False,
            target=plan.target.location,
            backup_path=plan.backup_path,
            sha256=plan.mission.sha256,
        )

    plan.backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_verified = False
    replacement_started = False
    backup_digest: str | None = None
    try:
        plan.storage.copy_to_local(plan.target.location, plan.backup_path)
        backup_digest = file_sha256(plan.backup_path)
        remote_digest = plan.storage.sha256(plan.target.location)
        if backup_digest != remote_digest:
            raise TransferError("La sauvegarde locale de l'ancienne mission est invalide.")
        backup_verified = True
        _write_manifest(plan, backup_digest)

        replacement_started = True
        with _immutable_mission_file(plan.mission) as immutable_source:
            plan.storage.replace_from_local(immutable_source, plan.target.location)
        if plan.storage.size(plan.target.location) != plan.mission.size:
            raise TransferError("La taille de la mission transférée est incorrecte.")
        transferred_digest = plan.storage.sha256(plan.target.location)
        if transferred_digest != plan.mission.sha256:
            raise TransferError("Le checksum de la mission transférée est incorrect.")
    except BaseException as exc:
        LOGGER.exception("Échec du transfert vers %s", plan.target.location)
        recovery_error: Exception | None = None
        if replacement_started and backup_verified and backup_digest:
            try:
                current_digest = (
                    plan.storage.sha256(plan.target.location)
                    if plan.storage.exists(plan.target.location)
                    else None
                )
                if current_digest != backup_digest:
                    plan.storage.replace_from_local(plan.backup_path, plan.target.location)
                if plan.storage.size(plan.target.location) != plan.backup_path.stat().st_size:
                    raise TransferError("La taille restaurée ne correspond pas à la sauvegarde.")
                if plan.storage.sha256(plan.target.location) != backup_digest:
                    raise TransferError("Le checksum de la restauration est incorrect.")
                LOGGER.warning("Ancienne mission restaurée et vérifiée après l'échec.")
            except Exception as restore_exc:
                recovery_error = restore_exc
                LOGGER.exception("La restauration automatique a échoué.")
        if recovery_error:
            raise TransferError(
                "Le transfert et la restauration ont échoué. Conservez la sauvegarde : "
                f"{plan.backup_path}. Détail : {recovery_error}"
            ) from exc
        if isinstance(exc, TransferError):
            raise
        if isinstance(exc, KeyboardInterrupt):
            raise TransferError("Transfert interrompu par l'utilisateur; restauration vérifiée.") from exc
        if not isinstance(exc, (OSError, StorageError)):
            raise
        raise TransferError(str(exc)) from exc

    LOGGER.info("Mission transférée et vérifiée vers %s", plan.target.location)
    return TransferResult(
        transferred=True,
        target=plan.target.location,
        backup_path=plan.backup_path,
        sha256=plan.mission.sha256,
    )
