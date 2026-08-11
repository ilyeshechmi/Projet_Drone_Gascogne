"""Interface graphique unifiée de génération et de transfert des missions DJI."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from geopy.geocoders import Nominatim
from PyQt5.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QCloseEvent, QFont
from PyQt5.QtWidgets import (
    QApplication,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .generator import (
    GenerationError,
    GenerationResult,
    MissionParameters,
    generate_mission,
)
from .main import configure_logging
from .map_widget import MissionMapWidget
from .missions import MissionArchive, MissionError, inspect_mission
from .mtp import (
    StorageError,
    detect_storage,
    list_mission_files,
    locate_waypoint_directory,
)
from .transfer import TransferError, build_transfer_plan, execute_transfer


APP_ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger("prog_vol")


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("o", "Ko", "Mo", "Go"):
        if value < 1024 or unit == "Go":
            return f"{value:.0f} {unit}" if unit == "o" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} o"


def friendly_error(exc: Exception) -> str:
    if isinstance(
        exc,
        (GenerationError, MissionError, StorageError, TransferError, OSError),
    ):
        return str(exc)
    return "Une erreur inattendue est survenue. Consultez le journal de l'application."


class Worker(QObject):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    done = pyqtSignal()

    def work(self):
        raise NotImplementedError

    @pyqtSlot()
    def run(self) -> None:
        try:
            self.succeeded.emit(self.work())
        except Exception as exc:
            LOGGER.exception("Échec d'une opération graphique")
            self.failed.emit(friendly_error(exc))
        finally:
            self.done.emit()


class GeocodeWorker(Worker):
    def __init__(self, place: str) -> None:
        super().__init__()
        self.place = place

    def work(self) -> tuple[float, float, str]:
        geocoder = Nominatim(user_agent="kael-mission-planner", timeout=15)
        location = geocoder.geocode(self.place)
        if location is None:
            raise GenerationError(f"Lieu introuvable : {self.place}")
        return float(location.latitude), float(location.longitude), location.address


class ValidationWorker(Worker):
    def __init__(self, source: Path) -> None:
        super().__init__()
        self.source = source

    def work(self) -> MissionArchive:
        return inspect_mission(self.source)


class GenerationWorker(Worker):
    def __init__(
        self,
        points: list[tuple[float, float]],
        parameters: MissionParameters,
        output_path: Path,
    ) -> None:
        super().__init__()
        self.points = points
        self.parameters = parameters
        self.output_path = output_path

    def work(self) -> tuple[GenerationResult, MissionArchive]:
        result = generate_mission(self.points, self.parameters, self.output_path)
        return result, inspect_mission(result.output_path)


@dataclass(frozen=True)
class DetectedMission:
    name: str
    relative_target: str
    location: str
    size: int
    modified: int


@dataclass(frozen=True)
class DetectionResult:
    storage_description: str
    waypoint_directory: str
    missions: tuple[DetectedMission, ...]


def _relative_remote_target(waypoint_directory: str, location: str, name: str) -> str:
    base = waypoint_directory.rstrip("/") + "/"
    relative = location[len(base) :] if location.startswith(base) else name
    if "://" in waypoint_directory:
        return "/".join(unquote(part) for part in relative.split("/"))
    return relative


class DetectionWorker(Worker):
    def work(self) -> DetectionResult:
        storage = None
        try:
            storage = detect_storage()
            waypoint_directory = locate_waypoint_directory(storage)
            remote_entries = list_mission_files(storage, waypoint_directory)
            missions = []
            for entry in sorted(
                remote_entries,
                key=lambda item: (item.modified, item.name.casefold()),
                reverse=True,
            ):
                relative = _relative_remote_target(
                    waypoint_directory,
                    entry.location,
                    entry.name,
                )
                missions.append(
                    DetectedMission(
                        name=entry.name,
                        relative_target=relative,
                        location=entry.location,
                        size=entry.size,
                        modified=entry.modified,
                    )
                )
            return DetectionResult(
                storage_description=storage.description,
                waypoint_directory=waypoint_directory,
                missions=tuple(missions),
            )
        finally:
            close = getattr(storage, "close", None)
            if close:
                close()


@dataclass(frozen=True)
class TransferSummary:
    target: str
    backup_path: Path
    sha256: str


class TransferWorker(Worker):
    def __init__(
        self,
        mission: MissionArchive,
        target: DetectedMission,
        storage_description: str,
    ) -> None:
        super().__init__()
        self.mission = mission
        self.target = target
        self.storage_description = storage_description

    def work(self) -> TransferSummary:
        storage = None
        try:
            storage = detect_storage()
            if storage.description != self.storage_description:
                raise TransferError(
                    "La radiocommande détectée n'est plus celle qui a été confirmée. "
                    "Actualisez la liste avant de transférer."
                )
            waypoint_directory = locate_waypoint_directory(storage)
            matching = []
            for entry in list_mission_files(storage, waypoint_directory):
                relative = _relative_remote_target(
                    waypoint_directory,
                    entry.location,
                    entry.name,
                )
                if relative == self.target.relative_target:
                    matching.append(entry)
            if len(matching) != 1:
                raise TransferError(
                    "La mission cible a disparu ou est devenue ambiguë. Actualisez la liste."
                )
            current = matching[0]
            if current.size != self.target.size or (
                self.target.modified
                and current.modified
                and current.modified != self.target.modified
            ):
                raise TransferError(
                    "La mission cible a changé depuis la détection. Actualisez la liste "
                    "et confirmez à nouveau."
                )
            plan = build_transfer_plan(
                self.mission,
                storage,
                target=self.target.relative_target,
            )
            result = execute_transfer(plan)
            return TransferSummary(
                target=result.target,
                backup_path=result.backup_path,
                sha256=result.sha256,
            )
        finally:
            close = getattr(storage, "close", None)
            if close:
                close()


class ThreadedPanel(QWidget):
    """Base minimale pour exécuter une opération à la fois par onglet."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.thread: QThread | None = None
        self.worker: Worker | None = None
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def start_worker(self, worker: Worker, success_slot) -> bool:
        if self.busy:
            return False
        self.thread = QThread(self)
        self.worker = worker
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker.run)
        worker.succeeded.connect(success_slot)
        worker.failed.connect(self.worker_failed)
        worker.done.connect(self.thread.quit)
        worker.done.connect(worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.worker_finished)
        self._busy = True
        self.set_busy(True)
        self.thread.start()
        return True

    def set_busy(self, busy: bool) -> None:
        del busy

    @pyqtSlot(str)
    def worker_failed(self, message: str) -> None:
        QMessageBox.critical(self, "Projet Kael", message)

    @pyqtSlot()
    def worker_finished(self) -> None:
        self.thread = None
        self.worker = None
        self._busy = False
        self.set_busy(False)


class GenerationTab(ThreadedPanel):
    mission_generated = pyqtSignal(object, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _spin(
        self,
        minimum: float,
        maximum: float,
        value: float,
        decimals: int,
        suffix: str = "",
        step: float | None = None,
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setValue(value)
        spin.setSuffix(suffix)
        if step is not None:
            spin.setSingleStep(step)
        return spin

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 20)
        layout.setSpacing(12)

        heading = QLabel("Composer une mission de vol")
        heading.setObjectName("pageTitle")
        heading.setFont(QFont("Arial", 21, QFont.Bold))
        layout.addWidget(heading)
        subtitle = QLabel(
            "Localisez la zone, dessinez son contour puis générez une mission DJI validée."
        )
        subtitle.setObjectName("muted")
        layout.addWidget(subtitle)

        splitter = QSplitter(Qt.Horizontal)
        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFrameShape(QFrame.NoFrame)
        controls = QWidget()
        controls.setObjectName("controlsPanel")
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(16, 16, 16, 16)
        controls_layout.setSpacing(12)

        controls_layout.addWidget(self._section_label("Localisation"))
        place_row = QHBoxLayout()
        self.place_input = QLineEdit()
        self.place_input.setPlaceholderText("Ex. ENSEIRB-MATMECA, Bordeaux")
        self.locate_button = QPushButton("Localiser")
        self.locate_button.setObjectName("secondaryButton")
        self.locate_button.clicked.connect(self.locate)
        self.place_input.returnPressed.connect(self.locate)
        place_row.addWidget(self.place_input, 1)
        place_row.addWidget(self.locate_button)
        controls_layout.addLayout(place_row)
        self.location_status = QLabel("La carte est centrée sur Bordeaux.")
        self.location_status.setObjectName("mutedSmall")
        self.location_status.setWordWrap(True)
        controls_layout.addWidget(self.location_status)

        controls_layout.addWidget(self._section_label("Paramètres de vol"))
        form = QFormLayout()
        form.setSpacing(9)
        self.altitude = self._spin(10, 500, 50, 1, " m", 5)
        self.speed = self._spin(0.5, 15, 2.5, 1, " m/s", 0.5)
        self.gimbal = self._spin(-90, 0, -45, 0, "°", 5)
        self.frontal = self._spin(0, 95, 80, 0, " %", 5)
        self.lateral = self._spin(0, 95, 80, 0, " %", 5)
        self.sensor_width = self._spin(0.1, 100, 6.17, 2, " mm", 0.1)
        self.sensor_height = self._spin(0.1, 100, 4.55, 2, " mm", 0.1)
        self.focal_length = self._spin(0.1, 200, 4.5, 2, " mm", 0.1)
        for label, widget in (
            ("Altitude", self.altitude),
            ("Vitesse", self.speed),
            ("Nacelle", self.gimbal),
            ("Recouvrement frontal", self.frontal),
            ("Recouvrement latéral", self.lateral),
            ("Largeur du capteur", self.sensor_width),
            ("Hauteur du capteur", self.sensor_height),
            ("Longueur focale", self.focal_length),
        ):
            form.addRow(label, widget)
        controls_layout.addLayout(form)

        controls_layout.addWidget(self._section_label("Zone de mission"))
        self.point_status = QLabel("Aucun sommet placé.")
        self.point_status.setObjectName("mutedSmall")
        controls_layout.addWidget(self.point_status)
        polygon_buttons = QHBoxLayout()
        self.close_button = QPushButton("Fermer le polygone")
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self.close_polygon)
        self.reset_button = QPushButton("Réinitialiser")
        self.reset_button.clicked.connect(self.reset_map)
        polygon_buttons.addWidget(self.close_button)
        polygon_buttons.addWidget(self.reset_button)
        controls_layout.addLayout(polygon_buttons)

        self.generate_button = QPushButton("Générer le fichier KMZ")
        self.generate_button.setObjectName("primaryButton")
        self.generate_button.setMinimumHeight(44)
        self.generate_button.setEnabled(False)
        self.generate_button.clicked.connect(self.choose_output_and_generate)
        controls_layout.addWidget(self.generate_button)

        self.result_label = QLabel("La mission générée sera résumée ici.")
        self.result_label.setObjectName("resultBox")
        self.result_label.setWordWrap(True)
        controls_layout.addWidget(self.result_label)
        controls_layout.addStretch()
        controls_scroll.setWidget(controls)

        self.map_widget = MissionMapWidget()
        self.map_widget.setMinimumSize(520, 480)
        self.map_widget.polygon_changed.connect(self.polygon_changed)
        self.map_widget.polygon_closed_changed.connect(self.polygon_closed_changed)
        splitter.addWidget(controls_scroll)
        splitter.addWidget(self.map_widget)
        splitter.setSizes([360, 900])
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        return label

    def parameters(self) -> MissionParameters:
        return MissionParameters(
            altitude=self.altitude.value(),
            drone_speed=self.speed.value(),
            gimbal_pitch=self.gimbal.value(),
            frontal_overlap=self.frontal.value() / 100,
            lateral_overlap=self.lateral.value() / 100,
            sensor_width=self.sensor_width.value(),
            sensor_height=self.sensor_height.value(),
            focal_length=self.focal_length.value(),
        )

    def locate(self) -> None:
        place = self.place_input.text().strip()
        if not place:
            QMessageBox.information(self, "Localisation", "Saisissez un lieu à rechercher.")
            return
        self.location_status.setText("Recherche du lieu...")
        self.start_worker(GeocodeWorker(place), self.location_found)

    @pyqtSlot(object)
    def location_found(self, result: object) -> None:
        latitude, longitude, address = result
        self.map_widget.set_center(latitude, longitude)
        self.location_status.setText(address)

    @pyqtSlot(object)
    def polygon_changed(self, points: object) -> None:
        count = len(points)
        self.point_status.setText(
            "Aucun sommet placé." if count == 0 else f"{count} sommet(s) placé(s)."
        )
        self.close_button.setEnabled(count >= 3 and not self.map_widget.is_closed)

    @pyqtSlot(bool)
    def polygon_closed_changed(self, closed: bool) -> None:
        self.generate_button.setEnabled(closed)
        self.close_button.setEnabled(not closed and len(self.map_widget.points) >= 3)
        if closed:
            self.point_status.setText(
                f"Polygone fermé avec {len(self.map_widget.points)} sommets."
            )

    def close_polygon(self) -> None:
        if not self.map_widget.close_polygon():
            QMessageBox.information(
                self,
                "Zone de mission",
                "Placez au moins trois sommets avant de fermer le polygone.",
            )

    def reset_map(self) -> None:
        self.map_widget.reset()
        self.result_label.setText("La mission générée sera résumée ici.")

    def choose_output_and_generate(self) -> None:
        default = APP_ROOT / "mission_waypoints.kmz"
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Enregistrer la mission DJI",
            str(default),
            "Mission DJI (*.kmz)",
        )
        if selected:
            self.generate_to(Path(selected))

    def generate_to(self, output_path: Path) -> None:
        self.result_label.setText("Calcul de la trajectoire et création du KMZ...")
        self.start_worker(
            GenerationWorker(
                list(self.map_widget.points),
                self.parameters(),
                output_path,
            ),
            self.generation_completed,
        )

    @pyqtSlot(object)
    def generation_completed(self, payload: object) -> None:
        result, archive = payload
        self.map_widget.show_waypoints(result.waypoints)
        self.result_label.setText(
            f"Mission prête : {result.waypoint_count} waypoints sur "
            f"{result.line_count} passe(s).\n"
            f"FOV : {result.fov_width:.1f} × {result.fov_height:.1f} m\n"
            f"Fichier : {result.output_path}"
        )
        self.mission_generated.emit(result.output_path, archive)
        QMessageBox.information(
            self,
            "Mission générée",
            f"Le KMZ a été généré et validé avec {archive.waypoint_count} waypoints.\n\n"
            "Il est maintenant sélectionné dans l'onglet Radiocommande et transfert.",
        )

    def set_busy(self, busy: bool) -> None:
        self.locate_button.setEnabled(not busy)
        self.place_input.setEnabled(not busy)
        self.close_button.setEnabled(
            not busy and not self.map_widget.is_closed and len(self.map_widget.points) >= 3
        )
        self.reset_button.setEnabled(not busy)
        self.generate_button.setEnabled(not busy and self.map_widget.is_closed)

    @pyqtSlot(str)
    def worker_failed(self, message: str) -> None:
        if isinstance(self.worker, GeocodeWorker):
            self.location_status.setText(message)
        else:
            self.result_label.setText(message)
        super().worker_failed(message)


class TransferTab(ThreadedPanel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.source_archive: MissionArchive | None = None
        self.detection: DetectionResult | None = None
        self.refresh_after_task = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 24)
        layout.setSpacing(15)
        title_row = QHBoxLayout()
        title = QLabel("Radiocommande et transfert")
        title.setObjectName("pageTitle")
        title.setFont(QFont("Arial", 21, QFont.Bold))
        title_row.addWidget(title)
        title_row.addStretch()
        self.connection_label = QLabel("● Non détectée")
        self.connection_label.setObjectName("connectionOffline")
        title_row.addWidget(self.connection_label)
        layout.addLayout(title_row)
        subtitle = QLabel(
            "Choisissez une mission DJI Fly existante, puis remplacez son KMZ avec sauvegarde et vérification."
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        connection_frame = QFrame()
        connection_frame.setObjectName("card")
        connection_layout = QHBoxLayout(connection_frame)
        self.storage_label = QLabel("Branchez la radiocommande en mode transfert de fichiers.")
        self.storage_label.setWordWrap(True)
        connection_layout.addWidget(self.storage_label, 1)
        self.detect_button = QPushButton("Détecter / Actualiser")
        self.detect_button.setObjectName("secondaryButton")
        self.detect_button.clicked.connect(self.detect_remote)
        connection_layout.addWidget(self.detect_button)
        layout.addWidget(connection_frame)

        mission_header = QHBoxLayout()
        mission_title = QLabel("Missions trouvées")
        mission_title.setObjectName("sectionTitle")
        mission_header.addWidget(mission_title)
        mission_header.addStretch()
        self.mission_count = QLabel("0 mission")
        self.mission_count.setObjectName("mutedSmall")
        mission_header.addWidget(self.mission_count)
        layout.addLayout(mission_header)
        self.mission_list = QListWidget()
        self.mission_list.setSelectionMode(QListWidget.SingleSelection)
        self.mission_list.currentItemChanged.connect(self.update_transfer_button)
        layout.addWidget(self.mission_list, 1)

        source_frame = QFrame()
        source_frame.setObjectName("card")
        source_layout = QVBoxLayout(source_frame)
        source_header = QHBoxLayout()
        source_title = QLabel("Fichier KMZ à transférer")
        source_title.setObjectName("sectionTitle")
        self.choose_source_button = QPushButton("Changer le fichier KMZ")
        self.choose_source_button.clicked.connect(self.choose_source)
        source_header.addWidget(source_title)
        source_header.addStretch()
        source_header.addWidget(self.choose_source_button)
        source_layout.addLayout(source_header)
        self.source_label = QLabel("Aucun fichier sélectionné.")
        self.source_label.setWordWrap(True)
        source_layout.addWidget(self.source_label)
        self.source_info = QLabel("")
        self.source_info.setObjectName("mutedSmall")
        source_layout.addWidget(self.source_info)
        layout.addWidget(source_frame)

        self.progress_label = QLabel("")
        self.progress_label.setObjectName("mutedSmall")
        self.progress_label.hide()
        layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        self.transfer_button = QPushButton("Transférer vers DJI Fly")
        self.transfer_button.setObjectName("primaryButton")
        self.transfer_button.setMinimumHeight(48)
        self.transfer_button.setEnabled(False)
        self.transfer_button.clicked.connect(self.confirm_transfer)
        layout.addWidget(self.transfer_button)
        backup_note = QLabel(
            f"Sauvegardes locales : {APP_ROOT / 'backups'}"
        )
        backup_note.setObjectName("mutedSmall")
        backup_note.setWordWrap(True)
        layout.addWidget(backup_note)

    def set_connection(self, connected: bool, text: str) -> None:
        self.connection_label.setObjectName(
            "connectionOnline" if connected else "connectionOffline"
        )
        self.connection_label.setText(text)
        self.connection_label.style().unpolish(self.connection_label)
        self.connection_label.style().polish(self.connection_label)

    def detect_remote(self) -> None:
        self.detection = None
        self.mission_list.clear()
        self.mission_count.setText("0 mission")
        self.set_connection(False, "● Détection...")
        self.storage_label.setText("Recherche d'un stockage DJI Fly accessible...")
        self.progress_label.setText("Détection de la radiocommande et lecture des missions...")
        self.start_worker(DetectionWorker(), self.detection_completed)

    @pyqtSlot(object)
    def detection_completed(self, result: object) -> None:
        self.detection = result
        self.progress_label.setText("")
        self.set_connection(True, "● Connectée")
        self.storage_label.setText(result.storage_description)
        self.mission_count.setText(f"{len(result.missions)} mission(s)")
        for mission in result.missions:
            date = (
                datetime.fromtimestamp(mission.modified).strftime("%d/%m/%Y %H:%M:%S")
                if mission.modified
                else "date indisponible"
            )
            item = QListWidgetItem(
                f"{Path(mission.name).stem}\n"
                f"{date}   •   {human_size(mission.size)}   •   {mission.relative_target}"
            )
            item.setData(Qt.UserRole, mission)
            item.setToolTip(mission.location)
            self.mission_list.addItem(item)
        if result.missions:
            self.mission_list.setCurrentRow(0)
        else:
            self.storage_label.setText(
                result.storage_description
                + "\nAucun KMZ trouvé. Créez d'abord une mission brouillon dans DJI Fly."
            )

    def choose_source(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Choisir une mission DJI",
            str(APP_ROOT),
            "Mission DJI (*.kmz)",
        )
        if selected:
            self.progress_label.setText("Validation du fichier KMZ...")
            self.start_worker(ValidationWorker(Path(selected)), self.source_validated)

    @pyqtSlot(object)
    def source_validated(self, archive: MissionArchive) -> None:
        self.progress_label.setText("")
        self.set_source_archive(archive)

    @pyqtSlot(object, object)
    def set_generated_source(self, path: object, archive: object) -> None:
        del path
        self.set_source_archive(archive)

    def set_source_archive(self, archive: MissionArchive) -> None:
        self.source_archive = archive
        self.source_label.setText(str(archive.path))
        self.source_info.setText(
            f"{archive.waypoint_count} waypoints   •   {human_size(archive.size)}   •   "
            f"SHA-256 {archive.sha256[:16]}…"
        )
        self.update_transfer_button()

    def selected_mission(self) -> DetectedMission | None:
        item = self.mission_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def update_transfer_button(self, *_args) -> None:
        self.transfer_button.setEnabled(
            not self.busy
            and self.source_archive is not None
            and self.selected_mission() is not None
        )

    def confirm_transfer(self) -> None:
        mission = self.selected_mission()
        if not self.source_archive or not mission:
            return
        answer = QMessageBox.question(
            self,
            "Confirmer le remplacement",
            "La mission DJI Fly sélectionnée va être remplacée.\n\n"
            f"Source : {self.source_archive.path}\n"
            f"Waypoints : {self.source_archive.waypoint_count}\n"
            f"Cible : {mission.relative_target}\n\n"
            "L'ancienne mission sera sauvegardée localement avant toute modification.\n"
            "Continuer ?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.progress_label.setText(
            "Sauvegarde, remplacement et vérification de la mission en cours..."
        )
        self.refresh_after_task = True
        assert self.detection is not None
        self.start_worker(
            TransferWorker(
                self.source_archive,
                mission,
                self.detection.storage_description,
            ),
            self.transfer_completed,
        )

    @pyqtSlot(object)
    def transfer_completed(self, summary: TransferSummary) -> None:
        self.progress_label.setText("Mission transférée et vérifiée.")
        QMessageBox.information(
            self,
            "Transfert terminé",
            "La mission a été transférée et son checksum a été vérifié.\n\n"
            f"Cible : {summary.target}\n"
            f"Sauvegarde : {summary.backup_path}\n"
            f"SHA-256 : {summary.sha256}",
        )

    def set_busy(self, busy: bool) -> None:
        self.detect_button.setEnabled(not busy)
        self.choose_source_button.setEnabled(not busy)
        self.mission_list.setEnabled(not busy)
        self.progress_bar.setVisible(busy)
        self.progress_label.setVisible(busy or bool(self.progress_label.text()))
        self.update_transfer_button()

    @pyqtSlot(str)
    def worker_failed(self, message: str) -> None:
        if isinstance(self.worker, DetectionWorker):
            self.set_connection(False, "● Non détectée")
            self.storage_label.setText(message)
        self.refresh_after_task = False
        self.progress_label.setText(message)
        super().worker_failed(message)

    @pyqtSlot()
    def worker_finished(self) -> None:
        refresh = self.refresh_after_task
        self.refresh_after_task = False
        super().worker_finished()
        if refresh:
            QTimer.singleShot(150, self.detect_remote)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Planificateur de mission DJI Fly")
        self.setMinimumSize(980, 700)
        self.resize(1320, 840)
        self.tabs = QTabWidget()
        self.generation_tab = GenerationTab()
        self.transfer_tab = TransferTab()
        self.tabs.addTab(self.generation_tab, "1. Génération de mission")
        self.tabs.addTab(self.transfer_tab, "2. Radiocommande et transfert")
        self.setCentralWidget(self.tabs)
        self.generation_tab.mission_generated.connect(
            self.transfer_tab.set_generated_source
        )
        self._apply_style()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #f3f5f4; color: #18313d; }
            QTabWidget::pane { border: none; }
            QTabBar::tab { background: #dfe6e5; padding: 11px 20px; margin-right: 2px; font-weight: 600; }
            QTabBar::tab:selected { background: #ffffff; color: #0d6679; }
            QLabel#pageTitle { color: #153441; }
            QLabel#muted { color: #62757d; font-size: 13px; }
            QLabel#mutedSmall { color: #6c7d84; font-size: 12px; }
            QLabel#sectionTitle { color: #214957; font-size: 15px; font-weight: 700; margin-top: 4px; }
            QLabel#connectionOnline { color: #147d59; font-weight: 700; }
            QLabel#connectionOffline { color: #aa4a41; font-weight: 700; }
            QLabel#resultBox { background: #e4eceb; border-radius: 8px; padding: 11px; }
            QWidget#controlsPanel, QFrame#card { background: #ffffff; border: 1px solid #d7dfde; border-radius: 10px; }
            QLineEdit, QDoubleSpinBox { background: white; border: 1px solid #bdc9c8; border-radius: 6px; padding: 6px; }
            QListWidget { background: #ffffff; border: 1px solid #d2dcda; border-radius: 9px; padding: 7px; }
            QListWidget::item { border-bottom: 1px solid #e8edec; padding: 10px 8px; }
            QListWidget::item:selected { background: #d9edf0; color: #173b48; }
            QPushButton { background: #ffffff; border: 1px solid #b9c8c8; border-radius: 7px; padding: 7px 12px; }
            QPushButton:hover { background: #edf4f3; }
            QPushButton:disabled { color: #98a5a6; background: #e9edec; }
            QPushButton#primaryButton { background: #0e7185; color: white; border: none; font-size: 14px; font-weight: 700; }
            QPushButton#primaryButton:hover { background: #0a6071; }
            QPushButton#secondaryButton { color: #0d6576; border-color: #74a2aa; font-weight: 600; }
            QProgressBar { border: none; border-radius: 5px; background: #dce4e3; height: 11px; }
            QProgressBar::chunk { border-radius: 5px; background: #168361; }
            QSplitter::handle { background: #d6dfdd; width: 4px; }
            """
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.generation_tab.busy or self.transfer_tab.busy:
            QMessageBox.warning(
                self,
                "Opération en cours",
                "Attendez la fin de l'opération avant de fermer l'application.",
            )
            event.ignore()
            return
        event.accept()


def run_gui() -> int:
    configure_logging()
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Planificateur de mission DJI Fly")
    window = MainWindow()
    window.show()
    return app.exec_()
