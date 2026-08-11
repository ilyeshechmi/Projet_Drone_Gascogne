#!/usr/bin/env python3
"""Interface en ligne de commande du transfert de missions DJI."""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

if __package__:
    from .missions import MissionError, inspect_mission
    from .mtp import StorageError, detect_storage
    from .transfer import TransferError, build_transfer_plan, execute_transfer
else:  # Chemin utilisé par la commande demandée : python3 main.py.
    from missions import MissionError, inspect_mission
    from mtp import StorageError, detect_storage
    from transfer import TransferError, build_transfer_plan, execute_transfer


APP_ROOT = Path(__file__).resolve().parent
LOG_PATH = APP_ROOT / "logs" / "mission_transfer.log"
LOGGER = logging.getLogger("prog_vol")


def configure_logging(verbose: bool = False) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Sauvegarde et remplace une mission DJI Fly depuis un fichier KMZ."
    )
    result.add_argument("mission", help="fichier KMZ source, relatif ou absolu")
    result.add_argument(
        "--dry-run",
        action="store_true",
        help="afficher le transfert prévu sans modifier la radiocommande",
    )
    location = result.add_mutually_exclusive_group()
    location.add_argument(
        "--device-root",
        help="racine locale d'un stockage monté, utile aussi pour les tests",
    )
    location.add_argument("--mtp-uri", help="URI mtp:// explicite utilisé par gio")
    result.add_argument(
        "--waypoint-dir",
        help="dossier waypoint relatif à la racine, si la détection automatique échoue",
    )
    result.add_argument(
        "--target",
        help="nom ou chemin relatif du KMZ DJI Fly à remplacer",
    )
    result.add_argument(
        "--yes",
        action="store_true",
        help="confirmer sans question, destiné aux scripts non interactifs",
    )
    result.add_argument("--verbose", action="store_true", help="journal plus détaillé")
    return result


def _confirm(target: str) -> bool:
    if not sys.stdin.isatty():
        raise TransferError("Confirmation impossible. Ajoutez --yes ou utilisez --dry-run.")
    answer = input(f"Remplacer la mission DJI Fly suivante ?\n{target}\nContinuer [o/N] : ")
    return answer.strip().casefold() in {"o", "oui", "y", "yes"}


def run(arguments: list[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    storage = None
    try:
        configure_logging(args.verbose)
        mission = inspect_mission(args.mission)
        storage = detect_storage(device_root=args.device_root, mtp_uri=args.mtp_uri)
        plan = build_transfer_plan(
            mission,
            storage,
            waypoint_override=args.waypoint_dir,
            target=args.target,
        )

        print(f"Mission source : {mission.path}")
        print(f"Waypoints      : {mission.waypoint_count}")
        print(f"Taille         : {mission.size} octets")
        print(f"SHA-256        : {mission.sha256}")
        print(f"Stockage       : {storage.description}")
        print(f"Cible          : {plan.target.location}")
        print(f"Sauvegarde     : {plan.backup_path}")

        if args.dry_run:
            execute_transfer(plan, dry_run=True)
            print(
                "Simulation terminée : aucune donnée de la radiocommande n'a été modifiée. "
                "Seul le journal local a été mis à jour."
            )
            return 0
        if not args.yes and not _confirm(plan.target.location):
            print("Transfert annulé. Aucune donnée n'a été modifiée.")
            return 2

        result = execute_transfer(plan)
        print("Mission transférée et vérifiée.")
        print(f"Ancienne mission sauvegardée : {result.backup_path}")
        return 0
    except (OSError, MissionError, StorageError, TransferError) as exc:
        if LOGGER.handlers:
            LOGGER.error("%s", exc)
        print(f"ERREUR : {exc}", file=sys.stderr)
        print(f"Diagnostic : {LOG_PATH}", file=sys.stderr)
        return 1
    finally:
        close = getattr(storage, "close", None)
        if close:
            close()


if __name__ == "__main__":
    raise SystemExit(run())
