"""Point d'entrée commun aux modes terminal et graphique."""

from __future__ import annotations

import argparse
import sys

from get_latest_drone_photo import DroneError, human_size

from .core import scan_drone_photos
from .history import HistoryError, SyncHistory
from .missions import build_import_plan
from .transfer import TransferError, import_missions


def analyze(gap_minutes: int):
    """Enchaîne scan, historique et missions sans effectuer de copie."""
    result = scan_drone_photos()
    history = SyncHistory().load()
    plan = build_import_plan(result, history, gap_minutes=gap_minutes)
    return plan, history


def print_scan(gap_minutes: int) -> int:
    """Affiche un diagnostic complet en lecture seule dans le terminal."""
    plan, _history = analyze(gap_minutes)
    result = plan.scan
    pending_missions = plan.missions
    print(f"Drone: {', '.join(device.name for device in result.devices)}")
    print(
        "Stockage: "
        + ", ".join(str(storage.mount_point) for storage in result.storages)
    )
    print(f"{len(result.photos)} photos trouvees sur le drone")
    print(f"{plan.imported_count} deja importees")
    print(f"{plan.new_count} nouvelles photos")
    for directory, count in result.photo_directories:
        print(f"- {directory}: {count} photo(s)")
    print("\n10 photos les plus recentes:")
    for photo in reversed(result.photos[-10:]):
        exif = (
            photo.exif_datetime_original.isoformat(sep=" ")
            if photo.exif_datetime_original
            else "absent"
        )
        print(
            f"- {photo.taken_at:%Y-%m-%d %H:%M:%S} | {photo.name} | "
            f"{photo.extension} | {human_size(photo.size)} | EXIF: {exif}"
        )
    if result.warnings:
        print(f"\n{len(result.warnings)} avertissement(s)", file=sys.stderr)
    print(f"\n{len(pending_missions)} mission(s) a importer:")
    for mission in pending_missions:
        print(
            f"Mission {mission.number}: {len(mission.photos)} photos - "
            f"{mission.start_at:%d/%m/%Y %H:%M:%S} -> {mission.end_at:%H:%M:%S} "
            f"- {human_size(mission.total_size)}"
        )
    return 0


def import_all(gap_minutes: int) -> int:
    """Importe toutes les nouvelles missions sans passer par l'interface."""
    plan, history = analyze(gap_minutes)
    result = plan.scan
    pending_missions = plan.missions
    print(
        f"{len(result.photos)} trouvees, {plan.imported_count} deja importees, "
        f"{plan.new_count} nouvelles, {len(pending_missions)} mission(s)."
    )
    if not pending_missions:
        print("Aucune nouvelle photo a importer.")
        return 0

    def show_progress(progress) -> None:
        print(
            f"[{progress.current}/{progress.total}] {progress.mission.folder_name}: "
            f"{progress.photo.name} ({progress.percent} %)"
        )

    summary = import_missions(
        pending_missions,
        history,
        progress_callback=show_progress,
    )
    print(f"Import termine: {summary.imported} photo(s) copiee(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Sans option, le programme lance la GUI. Les options servent surtout aux
    # tests et au diagnostic lorsque l'interface n'est pas disponible.
    parser = argparse.ArgumentParser(description="DJI Photo Importer")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--scan",
        action="store_true",
        help="scanner toutes les photos dans le terminal sans rien importer",
    )
    actions.add_argument(
        "--import-all",
        action="store_true",
        help="importer toutes les nouvelles missions sans interface graphique",
    )
    def valid_gap(value: str) -> int:
        gap = int(value)
        if not 1 <= gap <= 120:
            raise argparse.ArgumentTypeError(
                "la valeur doit etre comprise entre 1 et 120"
            )
        return gap

    parser.add_argument(
        "--gap-minutes",
        type=valid_gap,
        default=5,
        help="seuil de separation des missions (5 minutes par defaut)",
    )
    args = parser.parse_args(argv)
    if args.scan:
        return print_scan(args.gap_minutes)
    if args.import_all:
        return import_all(args.gap_minutes)
    # Import tardif : un simple --scan peut fonctionner même sans PyQt5.
    from .gui import run_gui

    return run_gui()


def run(argv: list[str] | None = None) -> int:
    # Les erreurs métier sont traduites en message lisible plutôt qu'en traceback.
    try:
        return main(argv)
    except (DroneError, HistoryError, TransferError, ValueError) as exc:
        print(f"ERREUR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
