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
    QComboBox,
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

from .autonomy import (
    AlertLevel,
    AutonomyError,
    BatteryAssessment,
    BatteryState,
    DEFAULT_BATTERY_PROFILE,
    assess_batteries,
    custom_battery_profile,
)
from .cadastre import (
    CadastreGironde,
    ErreurCadastre,
    ParcelleCadastrale,
    ResultatFusion,
    collection_geojson,
    fusionner_parcelles,
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


def human_duration(seconds: float) -> str:
    total = max(0, round(seconds))
    minutes, remaining_seconds = divmod(total, 60)
    if minutes:
        return f"{minutes} min {remaining_seconds:02d} s"
    return f"{remaining_seconds} s"


def friendly_error(exc: Exception) -> str:
    if isinstance(
        exc,
        (
            AutonomyError,
            ErreurCadastre,
            GenerationError,
            MissionError,
            StorageError,
            TransferError,
            OSError,
        ),
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
        home_point: tuple[float, float] | None,
        batteries: tuple[BatteryState, ...],
        reserve_percent: float,
    ) -> None:
        super().__init__()
        self.points = points
        self.parameters = parameters
        self.output_path = output_path
        self.home_point = home_point
        self.batteries = batteries
        self.reserve_percent = reserve_percent

    def work(self) -> tuple[GenerationResult, MissionArchive, BatteryAssessment]:
        result = generate_mission(
            self.points,
            self.parameters,
            self.output_path,
            home_point=self.home_point,
        )
        assessment = assess_batteries(
            result.flight_estimate,
            self.batteries,
            self.reserve_percent,
        )
        return result, inspect_mission(result.output_path), assessment


class InstallationCadastreWorker(Worker):
    progression = pyqtSignal(str, int)

    def __init__(self, cadastre: CadastreGironde) -> None:
        super().__init__()
        self.cadastre = cadastre

    def work(self) -> int:
        return self.cadastre.installer(
            lambda message, pourcentage: self.progression.emit(message, pourcentage)
        )


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
        QMessageBox.critical(self, "Projet Drone Gascogne", message)

    @pyqtSlot()
    def worker_finished(self) -> None:
        self.thread = None
        self.worker = None
        self._busy = False
        self.set_busy(False)


class BatteryRowWidget(QFrame):
    """Saisie temporaire d'une batterie disponible pour la mission."""

    remove_requested = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("batteryCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(7)

        header = QHBoxLayout()
        self.title = QLabel("Batterie")
        self.title.setObjectName("sectionTitle")
        self.remove_button = QPushButton("Supprimer")
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self))
        header.addWidget(self.title)
        header.addStretch()
        header.addWidget(self.remove_button)
        layout.addLayout(header)

        form = QFormLayout()
        form.setSpacing(6)
        self.model = QComboBox()
        self.model.addItem(
            f"{DEFAULT_BATTERY_PROFILE.model} - "
            f"{DEFAULT_BATTERY_PROFILE.capacity_mah:.0f} mAh",
            "default",
        )
        self.model.addItem("Batterie personnalisée...", "custom")
        self.model.currentIndexChanged.connect(self._profile_changed)
        form.addRow("Modèle", self.model)

        self.custom_name = QLineEdit("Batterie personnalisée")
        form.addRow("Nom", self.custom_name)
        self.capacity = QDoubleSpinBox()
        self.capacity.setRange(1, 50_000)
        self.capacity.setDecimals(0)
        self.capacity.setSuffix(" mAh")
        self.capacity.setValue(DEFAULT_BATTERY_PROFILE.capacity_mah)
        form.addRow("Capacité", self.capacity)
        self.voltage = QDoubleSpinBox()
        self.voltage.setRange(0.1, 100)
        self.voltage.setDecimals(2)
        self.voltage.setSuffix(" V")
        self.voltage.setValue(DEFAULT_BATTERY_PROFILE.nominal_voltage_v)
        form.addRow("Tension", self.voltage)
        self.reference_minutes = QDoubleSpinBox()
        self.reference_minutes.setRange(0, 180)
        self.reference_minutes.setDecimals(1)
        self.reference_minutes.setSuffix(" min")
        self.reference_minutes.setSpecialValueText("Auto selon les Wh")
        self.reference_minutes.setValue(0)
        form.addRow("Autonomie mesurée", self.reference_minutes)
        self.charge = QDoubleSpinBox()
        self.charge.setRange(0, 100)
        self.charge.setDecimals(0)
        self.charge.setSuffix(" %")
        self.charge.setValue(100)
        form.addRow("Charge", self.charge)
        layout.addLayout(form)

        self.energy_label = QLabel()
        self.energy_label.setObjectName("mutedSmall")
        layout.addWidget(self.energy_label)
        self.capacity.valueChanged.connect(self._update_energy)
        self.voltage.valueChanged.connect(self._update_energy)
        self.reference_minutes.valueChanged.connect(self._update_energy)
        self._profile_changed()

    def set_number(self, number: int) -> None:
        self.title.setText(f"Batterie {number}")

    def _profile_changed(self, *_args) -> None:
        custom = self.model.currentData() == "custom"
        for widget in (
            self.custom_name,
            self.capacity,
            self.voltage,
            self.reference_minutes,
        ):
            widget.setEnabled(custom)
        if not custom:
            self.capacity.setValue(DEFAULT_BATTERY_PROFILE.capacity_mah)
            self.voltage.setValue(DEFAULT_BATTERY_PROFILE.nominal_voltage_v)
        self._update_energy()

    def _update_energy(self, *_args) -> None:
        if self.model.currentData() == "default":
            energy = DEFAULT_BATTERY_PROFILE.effective_energy_wh
            reference = DEFAULT_BATTERY_PROFILE.reference_flight_time_s / 60
            self.energy_label.setText(
                f"Énergie : {energy:.1f} Wh • autonomie de référence : {reference:.0f} min"
            )
            return
        energy = self.capacity.value() * self.voltage.value() / 1000
        if self.reference_minutes.value() > 0:
            reference = f"{self.reference_minutes.value():.1f} min mesurées"
        else:
            estimated = (
                DEFAULT_BATTERY_PROFILE.reference_flight_time_s
                * energy
                / DEFAULT_BATTERY_PROFILE.effective_energy_wh
                / 60
            )
            reference = f"environ {estimated:.1f} min par extrapolation"
        self.energy_label.setText(f"Énergie calculée : {energy:.2f} Wh • {reference}")

    def battery_state(self, number: int) -> BatteryState:
        if self.model.currentData() == "default":
            profile = DEFAULT_BATTERY_PROFILE
        else:
            reference = self.reference_minutes.value()
            profile = custom_battery_profile(
                self.custom_name.text().strip(),
                self.capacity.value(),
                self.voltage.value(),
                reference * 60 if reference > 0 else None,
            )
        return BatteryState(
            name=f"Batterie {number}",
            profile=profile,
            charge_percent=self.charge.value(),
        )


class GenerationTab(ThreadedPanel):
    mission_generated = pyqtSignal(object, object, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cadastre = CadastreGironde()
        self.parcelles_selectionnees: dict[str, ParcelleCadastrale] = {}
        self._build_ui()
        self.actualiser_etat_cadastre()
        self.changer_mode_zone(0)

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
            "Dessinez une zone ou sélectionnez des parcelles cadastrales, puis générez une mission DJI validée."
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

        controls_layout.addWidget(self._section_label("Parcelles cadastrales de Gironde"))
        self.cadastre_status = QLabel()
        self.cadastre_status.setObjectName("mutedSmall")
        self.cadastre_status.setWordWrap(True)
        controls_layout.addWidget(self.cadastre_status)
        self.cadastre_progress = QProgressBar()
        self.cadastre_progress.setRange(0, 100)
        self.cadastre_progress.setVisible(False)
        controls_layout.addWidget(self.cadastre_progress)
        self.install_cadastre_button = QPushButton("Installer les données cadastrales")
        self.install_cadastre_button.setObjectName("secondaryButton")
        self.install_cadastre_button.clicked.connect(self.installer_cadastre)
        controls_layout.addWidget(self.install_cadastre_button)

        self.mode_zone = QComboBox()
        self.mode_zone.addItems(("Dessin libre", "Sélection cadastrale"))
        controls_layout.addWidget(self.mode_zone)

        reference_form = QFormLayout()
        reference_form.setSpacing(7)
        self.commune_cadastrale = QLineEdit()
        self.commune_cadastrale.setPlaceholderText("Ex. 33522 pour Talence")
        self.prefixe_cadastral = QLineEdit()
        self.prefixe_cadastral.setPlaceholderText("Optionnel, ex. 000")
        self.section_cadastrale = QLineEdit()
        self.section_cadastrale.setPlaceholderText("Ex. AB")
        self.numero_parcelle = QLineEdit()
        self.numero_parcelle.setPlaceholderText("Ex. 402")
        reference_form.addRow("Code INSEE de la commune", self.commune_cadastrale)
        reference_form.addRow("Préfixe cadastral", self.prefixe_cadastral)
        reference_form.addRow("Section cadastrale", self.section_cadastrale)
        reference_form.addRow("Numéro de parcelle", self.numero_parcelle)
        controls_layout.addLayout(reference_form)
        self.search_parcelle_button = QPushButton("Rechercher et ajouter la parcelle")
        self.search_parcelle_button.clicked.connect(self.rechercher_parcelle)
        controls_layout.addWidget(self.search_parcelle_button)

        self.parcelles_status = QLabel("Aucune parcelle cadastrale sélectionnée.")
        self.parcelles_status.setObjectName("mutedSmall")
        self.parcelles_status.setWordWrap(True)
        controls_layout.addWidget(self.parcelles_status)
        self.parcelles_list = QListWidget()
        self.parcelles_list.setMaximumHeight(125)
        controls_layout.addWidget(self.parcelles_list)
        parcelles_buttons = QHBoxLayout()
        self.remove_parcelle_button = QPushButton("Retirer la parcelle")
        self.remove_parcelle_button.clicked.connect(self.retirer_parcelle)
        self.clear_parcelles_button = QPushButton("Effacer la sélection")
        self.clear_parcelles_button.clicked.connect(self.effacer_parcelles)
        parcelles_buttons.addWidget(self.remove_parcelle_button)
        parcelles_buttons.addWidget(self.clear_parcelles_button)
        controls_layout.addLayout(parcelles_buttons)

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
        self.photo_mode = QComboBox()
        self.photo_mode.addItem("Cadence supposée 12 MP - 2 s", 2.0)
        self.photo_mode.addItem("Cadence supposée 50 MP - 5 s", 5.0)
        for label, widget in (
            ("Altitude", self.altitude),
            ("Vitesse", self.speed),
            ("Nacelle", self.gimbal),
            ("Recouvrement frontal", self.frontal),
            ("Recouvrement latéral", self.lateral),
            ("Largeur du capteur", self.sensor_width),
            ("Hauteur du capteur", self.sensor_height),
            ("Longueur focale", self.focal_length),
            ("Contrôle photo", self.photo_mode),
        ):
            form.addRow(label, widget)
        controls_layout.addLayout(form)

        controls_layout.addWidget(self._section_label("Batteries de cette mission"))
        battery_note = QLabel(
            "Les charges sont temporaires et ne sont pas enregistrées. Une mission "
            "continue doit tenir sur une seule batterie."
        )
        battery_note.setObjectName("mutedSmall")
        battery_note.setWordWrap(True)
        controls_layout.addWidget(battery_note)
        self.battery_rows: list[BatteryRowWidget] = []
        self.battery_container = QWidget()
        self.battery_layout = QVBoxLayout(self.battery_container)
        self.battery_layout.setContentsMargins(0, 0, 0, 0)
        self.battery_layout.setSpacing(7)
        controls_layout.addWidget(self.battery_container)
        self.add_battery_button = QPushButton("Ajouter une batterie")
        self.add_battery_button.setObjectName("secondaryButton")
        self.add_battery_button.clicked.connect(self.add_battery)
        controls_layout.addWidget(self.add_battery_button)
        reserve_form = QFormLayout()
        self.reserve = self._spin(0, 80, 20, 0, " %", 5)
        reserve_form.addRow("Charge réservée à l'atterrissage", self.reserve)
        controls_layout.addLayout(reserve_form)
        self.add_battery()

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

        self.home_status = QLabel(
            "Home non défini : l'estimation d'autonomie sera partielle."
        )
        self.home_status.setObjectName("mutedSmall")
        self.home_status.setWordWrap(True)
        controls_layout.addWidget(self.home_status)
        home_buttons = QHBoxLayout()
        self.place_home_button = QPushButton("Placer le point Home")
        self.place_home_button.clicked.connect(self.place_home)
        self.clear_home_button = QPushButton("Supprimer Home")
        self.clear_home_button.setEnabled(False)
        home_buttons.addWidget(self.place_home_button)
        home_buttons.addWidget(self.clear_home_button)
        controls_layout.addLayout(home_buttons)

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
        self.map_widget.parcelle_demandee.connect(self.selectionner_parcelle_sur_carte)
        self.map_widget.home_changed.connect(self.home_changed)
        self.clear_home_button.clicked.connect(self.map_widget.clear_home)
        self.mode_zone.currentIndexChanged.connect(self.changer_mode_zone)
        splitter.addWidget(controls_scroll)
        splitter.addWidget(self.map_widget)
        splitter.setSizes([360, 900])
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        return label

    def add_battery(self) -> None:
        row = BatteryRowWidget()
        row.remove_requested.connect(self.remove_battery)
        self.battery_rows.append(row)
        self.battery_layout.addWidget(row)
        self._refresh_battery_rows()

    @pyqtSlot(object)
    def remove_battery(self, row: object) -> None:
        if len(self.battery_rows) <= 1 or row not in self.battery_rows:
            return
        self.battery_rows.remove(row)
        self.battery_layout.removeWidget(row)
        row.deleteLater()
        self._refresh_battery_rows()

    def reset_batteries(self) -> None:
        for row in self.battery_rows:
            self.battery_layout.removeWidget(row)
            row.deleteLater()
        self.battery_rows.clear()
        self.add_battery()
        self.reserve.setValue(20)

    def _refresh_battery_rows(self) -> None:
        for number, row in enumerate(self.battery_rows, start=1):
            row.set_number(number)
            row.remove_button.setEnabled(len(self.battery_rows) > 1 and not self.busy)

    def battery_states(self) -> tuple[BatteryState, ...]:
        states = tuple(
            row.battery_state(number)
            for number, row in enumerate(self.battery_rows, start=1)
        )
        for state in states:
            state.validate()
        return states

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
            photo_interval=float(self.photo_mode.currentData()),
        )

    def place_home(self) -> None:
        self.map_widget.begin_home_placement()
        self.home_status.setText(
            "Cliquez sur la carte pour placer le point de décollage et de retour."
        )

    @pyqtSlot(object)
    def home_changed(self, point: object) -> None:
        if point is None:
            self.home_status.setText(
                "Home non défini : l'estimation d'autonomie sera partielle."
            )
            self.clear_home_button.setEnabled(False)
            return
        latitude, longitude = point
        self.home_status.setText(
            f"Home : {latitude:.6f}, {longitude:.6f}. "
            "Utilisé pour l'estimation uniquement, sans modifier la trajectoire."
        )
        self.clear_home_button.setEnabled(not self.busy)

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

    def actualiser_etat_cadastre(self) -> None:
        installe = self.cadastre.est_installe
        if installe:
            informations = self.cadastre.informations()
            nombre = int(informations.get("nombre_parcelles", "0"))
            version = informations.get("version_donnees", "inconnue")
            self.cadastre_status.setText(
                f"Données installées : {nombre:,} parcelles de Gironde, "
                f"version {version}.".replace(",", " ")
            )
            self.install_cadastre_button.setText("Mettre à jour les données cadastrales")
        else:
            self.cadastre_status.setText(
                "Données non installées. Téléchargement initial : environ 236 Mo; "
                "prévoir 3 Go d'espace libre pendant l'indexation."
            )
            self.install_cadastre_button.setText("Installer les données cadastrales")
        self.search_parcelle_button.setEnabled(
            not self.busy and installe and self.mode_zone.currentIndex() == 1
        )

    def installer_cadastre(self) -> None:
        action = "mettre à jour" if self.cadastre.est_installe else "installer"
        confirmation = QMessageBox.question(
            self,
            "Données cadastrales de Gironde",
            f"Voulez-vous {action} la base officielle des parcelles de Gironde ?\n\n"
            "Le téléchargement représente environ 236 Mo et l'indexation peut durer "
            "plusieurs minutes.",
        )
        if confirmation != QMessageBox.Yes:
            return
        worker = InstallationCadastreWorker(self.cadastre)
        worker.progression.connect(self.progression_cadastre)
        self.cadastre_progress.setVisible(True)
        self.cadastre_progress.setValue(0)
        self.start_worker(worker, self.cadastre_installe)

    @pyqtSlot(str, int)
    def progression_cadastre(self, message: str, pourcentage: int) -> None:
        self.cadastre_status.setText(message)
        self.cadastre_progress.setValue(pourcentage)

    @pyqtSlot(object)
    def cadastre_installe(self, nombre: object) -> None:
        self.cadastre_progress.setValue(100)
        self.cadastre_progress.setVisible(False)
        self.parcelles_selectionnees.clear()
        self.parcelles_list.clear()
        self.map_widget.reset()
        self.reset_batteries()
        self.map_widget.set_mode_selection(self.mode_zone.currentIndex() == 1)
        self.parcelles_status.setText("Aucune parcelle cadastrale sélectionnée.")
        self.result_label.setText("La mission générée sera résumée ici.")
        self.actualiser_etat_cadastre()
        QMessageBox.information(
            self,
            "Données cadastrales prêtes",
            f"La base locale contient {int(nombre):,} parcelles de Gironde.".replace(",", " "),
        )

    @pyqtSlot(int)
    def changer_mode_zone(self, index: int) -> None:
        cadastral = index == 1
        if cadastral and not self.cadastre.est_installe:
            QMessageBox.information(
                self,
                "Sélection cadastrale",
                "Installez d'abord les données cadastrales de Gironde.",
            )
            self.mode_zone.blockSignals(True)
            self.mode_zone.setCurrentIndex(0)
            self.mode_zone.blockSignals(False)
            cadastral = False
        self.parcelles_selectionnees.clear()
        self.parcelles_list.clear()
        self.map_widget.reset()
        self.reset_batteries()
        self.map_widget.set_mode_selection(cadastral)
        self.parcelles_status.setText("Aucune parcelle cadastrale sélectionnée.")
        self.result_label.setText("La mission générée sera résumée ici.")
        self.search_parcelle_button.setEnabled(cadastral)
        for widget in (
            self.commune_cadastrale,
            self.prefixe_cadastral,
            self.section_cadastrale,
            self.numero_parcelle,
            self.parcelles_list,
            self.remove_parcelle_button,
            self.clear_parcelles_button,
        ):
            widget.setEnabled(cadastral)
        self.close_button.setVisible(not cadastral)

    def rechercher_parcelle(self) -> None:
        try:
            parcelle = self.cadastre.rechercher_reference(
                self.commune_cadastrale.text(),
                self.section_cadastrale.text(),
                self.numero_parcelle.text(),
                self.prefixe_cadastral.text(),
            )
            self.ajouter_ou_retirer_parcelle(parcelle)
        except ErreurCadastre as exc:
            QMessageBox.warning(self, "Recherche cadastrale", str(exc))

    @pyqtSlot(float, float)
    def selectionner_parcelle_sur_carte(self, latitude: float, longitude: float) -> None:
        if self.busy or self.mode_zone.currentIndex() != 1:
            return
        try:
            parcelle = self.cadastre.rechercher_point(latitude, longitude)
            self.ajouter_ou_retirer_parcelle(parcelle)
        except ErreurCadastre as exc:
            QMessageBox.information(self, "Sélection cadastrale", str(exc))

    def ajouter_ou_retirer_parcelle(self, parcelle: ParcelleCadastrale) -> None:
        nouvelle_selection = dict(self.parcelles_selectionnees)
        if parcelle.identifiant in nouvelle_selection:
            nouvelle_selection.pop(parcelle.identifiant)
        else:
            nouvelle_selection[parcelle.identifiant] = parcelle
        fusion: ResultatFusion | None = None
        if nouvelle_selection:
            try:
                fusion = fusionner_parcelles(list(nouvelle_selection.values()))
            except ErreurCadastre as exc:
                QMessageBox.warning(self, "Sélection cadastrale refusée", str(exc))
                return
        self.parcelles_selectionnees = nouvelle_selection
        self.actualiser_selection_cadastrale(fusion)

    def actualiser_selection_cadastrale(self, fusion: ResultatFusion | None = None) -> None:
        parcelles = list(self.parcelles_selectionnees.values())
        self.parcelles_list.clear()
        for parcelle in sorted(
            parcelles,
            key=lambda item: (item.commune, item.prefixe, item.section, item.numero),
        ):
            item = QListWidgetItem(parcelle.description)
            item.setData(Qt.UserRole, parcelle.identifiant)
            self.parcelles_list.addItem(item)
        self.map_widget.afficher_parcelles_selectionnees(collection_geojson(parcelles))
        if not parcelles:
            self.map_widget.reset()
            self.map_widget.set_mode_selection(True)
            self.parcelles_status.setText("Aucune parcelle cadastrale sélectionnée.")
            return
        fusion = fusion or fusionner_parcelles(parcelles)
        self.map_widget.set_polygon(fusion.points)
        self.map_widget.set_mode_selection(True)
        surface = f"{fusion.surface_cadastrale:,}".replace(",", " ")
        selection = (
            "1 parcelle sélectionnée"
            if len(parcelles) == 1
            else f"{len(parcelles)} parcelles adjacentes"
        )
        self.parcelles_status.setText(
            f"{selection}, surface cadastrale {surface} m²."
        )
        self.point_status.setText(
            f"Contour cadastral prêt avec {len(fusion.points)} sommets."
        )

    def retirer_parcelle(self) -> None:
        item = self.parcelles_list.currentItem()
        if not item:
            QMessageBox.information(
                self,
                "Parcelles cadastrales",
                "Sélectionnez une parcelle dans la liste avant de la retirer.",
            )
            return
        parcelle = self.parcelles_selectionnees.get(item.data(Qt.UserRole))
        if parcelle:
            self.ajouter_ou_retirer_parcelle(parcelle)

    def effacer_parcelles(self) -> None:
        self.parcelles_selectionnees.clear()
        self.actualiser_selection_cadastrale()

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
        self.parcelles_selectionnees.clear()
        self.parcelles_list.clear()
        self.parcelles_status.setText("Aucune parcelle cadastrale sélectionnée.")
        self.map_widget.reset()
        self.reset_batteries()
        self.map_widget.set_mode_selection(self.mode_zone.currentIndex() == 1)
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
        try:
            batteries = self.battery_states()
        except AutonomyError as exc:
            QMessageBox.warning(self, "Batteries", str(exc))
            return
        self.result_label.setText("Calcul de la trajectoire et création du KMZ...")
        self.start_worker(
            GenerationWorker(
                list(self.map_widget.points),
                self.parameters(),
                output_path,
                self.map_widget.home_point,
                batteries,
                self.reserve.value(),
            ),
            self.generation_completed,
        )

    @pyqtSlot(object)
    def generation_completed(self, payload: object) -> None:
        result, archive, assessment = payload
        estimate = result.flight_estimate
        self.map_widget.show_waypoints(result.waypoints)
        distance_line = f"Distance dans la zone : {estimate.route_distance_m / 1000:.2f} km"
        if estimate.transit_distance_m is not None:
            distance_line += f" • transit : {estimate.transit_distance_m / 1000:.2f} km"
        else:
            distance_line += " • transit non calculé"
        alert_names = {
            AlertLevel.OK: "AUTONOMIE OK",
            AlertLevel.WARNING: "AVERTISSEMENT AUTONOMIE",
            AlertLevel.CRITICAL: "CHARGE INSUFFISANTE",
            AlertLevel.PARTIAL: "ESTIMATION PARTIELLE",
        }
        details = [
            f"Mission prête : {result.waypoint_count} waypoints sur "
            f"{result.line_count} passe(s), {estimate.photo_count} photos prévues.",
            distance_line,
            f"Durée estimée : {human_duration(estimate.estimated_duration_s)}",
            f"{alert_names[assessment.level]} : {assessment.message}",
        ]
        if assessment.recommended_battery is not None:
            battery = assessment.recommended_battery
            details.append(
                f"Batterie recommandée : {battery.name}, {battery.charge_percent:.0f} % "
                f"({battery.profile.model})"
            )
        if assessment.margin_s is not None:
            margin_label = "Marge sûre" if assessment.margin_s >= 0 else "Déficit"
            details.append(f"{margin_label} : {human_duration(abs(assessment.margin_s))}")
        if estimate.short_photo_intervals:
            details.append(
                f"Cadence photo : {estimate.short_photo_intervals} segment(s) trop courts "
                "pour l'intervalle choisi."
            )
        if assessment.estimated_profile_used:
            details.append(
                "Une autonomie de batterie personnalisée a été extrapolée depuis les Wh."
            )
        details.extend(
            (
                f"FOV : {result.fov_width:.1f} × {result.fov_height:.1f} m",
                f"Fichier : {result.output_path}",
            )
        )
        self.result_label.setText("\n".join(details))
        self.mission_generated.emit(result, archive, assessment)
        cadence_note = ""
        if estimate.short_photo_intervals:
            cadence_note = (
                f"\n\nAttention : {estimate.short_photo_intervals} intervalle(s) photo "
                "sont trop courts. Le choix indique une cadence supposée et ne configure "
                "pas la résolution de la caméra dans le KMZ."
            )
        dialog_text = (
            f"Le KMZ a été généré et validé avec {archive.waypoint_count} waypoints.\n\n"
            f"{assessment.message}{cadence_note}\n\n"
            "Il est maintenant sélectionné dans l'onglet Radiocommande et transfert."
        )
        dialog = {
            AlertLevel.OK: QMessageBox.information,
            AlertLevel.WARNING: QMessageBox.warning,
            AlertLevel.CRITICAL: QMessageBox.critical,
            AlertLevel.PARTIAL: QMessageBox.warning,
        }[assessment.level]
        dialog(
            self,
            "Mission générée",
            dialog_text,
        )

    def set_busy(self, busy: bool) -> None:
        self.map_widget.set_interaction_active(not busy)
        self.locate_button.setEnabled(not busy)
        self.place_input.setEnabled(not busy)
        self.close_button.setEnabled(
            not busy and not self.map_widget.is_closed and len(self.map_widget.points) >= 3
        )
        self.reset_button.setEnabled(not busy)
        self.generate_button.setEnabled(not busy and self.map_widget.is_closed)
        self.install_cadastre_button.setEnabled(not busy)
        self.mode_zone.setEnabled(not busy)
        self.add_battery_button.setEnabled(not busy)
        self.reserve.setEnabled(not busy)
        self.photo_mode.setEnabled(not busy)
        self.place_home_button.setEnabled(not busy)
        self.clear_home_button.setEnabled(not busy and self.map_widget.home_point is not None)
        for row in self.battery_rows:
            row.setEnabled(not busy)
        self._refresh_battery_rows()
        cadastral_disponible = (
            not busy and self.mode_zone.currentIndex() == 1 and self.cadastre.est_installe
        )
        for widget in (
            self.commune_cadastrale,
            self.prefixe_cadastral,
            self.section_cadastrale,
            self.numero_parcelle,
            self.search_parcelle_button,
            self.parcelles_list,
            self.remove_parcelle_button,
            self.clear_parcelles_button,
        ):
            widget.setEnabled(cadastral_disponible)

    @pyqtSlot(str)
    def worker_failed(self, message: str) -> None:
        if isinstance(self.worker, InstallationCadastreWorker):
            self.cadastre_progress.setVisible(False)
            self.actualiser_etat_cadastre()
        elif isinstance(self.worker, GeocodeWorker):
            self.location_status.setText(message)
        else:
            self.result_label.setText(message)
        super().worker_failed(message)


class TransferTab(ThreadedPanel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.source_archive: MissionArchive | None = None
        self.source_assessment: BatteryAssessment | None = None
        self.source_cadence_warning: str | None = None
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

    @pyqtSlot(object, object, object)
    def set_generated_source(
        self, result: object, archive: object, assessment: object
    ) -> None:
        short_intervals = result.flight_estimate.short_photo_intervals
        cadence_warning = None
        if short_intervals:
            cadence_warning = (
                f"{short_intervals} intervalle(s) photo sont trop courts pour la "
                "cadence supposée."
            )
        self.set_source_archive(archive, assessment, cadence_warning)

    def set_source_archive(
        self,
        archive: MissionArchive,
        assessment: BatteryAssessment | None = None,
        cadence_warning: str | None = None,
    ) -> None:
        self.source_archive = archive
        self.source_assessment = assessment
        self.source_cadence_warning = cadence_warning
        self.source_label.setText(str(archive.path))
        information = (
            f"{archive.waypoint_count} waypoints   •   {human_size(archive.size)}   •   "
            f"SHA-256 {archive.sha256[:16]}…"
        )
        if assessment is None:
            information += "\nAutonomie non évaluée pour ce fichier sélectionné manuellement."
        else:
            information += f"\nAutonomie : {assessment.message}"
        if cadence_warning:
            information += f"\nCadence photo : {cadence_warning}"
        self.source_info.setText(information)
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
        if (
            self.source_assessment is not None
            and self.source_assessment.level is AlertLevel.CRITICAL
        ):
            unsafe_answer = QMessageBox.warning(
                self,
                "Autonomie critique",
                f"{self.source_assessment.message}\n\n"
                "Le transfert de cette mission est déconseillé. Confirmez-vous que vous "
                "souhaitez malgré tout poursuivre vers l'étape de remplacement ?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if unsafe_answer != QMessageBox.Yes:
                return
        autonomy_note = ""
        if self.source_assessment is None:
            autonomy_note = "\nAutonomie : non évaluée.\n"
        elif self.source_assessment.level is not AlertLevel.OK:
            autonomy_note = f"\nAutonomie : {self.source_assessment.message}\n"
        if self.source_cadence_warning:
            autonomy_note += f"\nCadence photo : {self.source_cadence_warning}\n"
        answer = QMessageBox.question(
            self,
            "Confirmer le remplacement",
            "La mission DJI Fly sélectionnée va être remplacée.\n\n"
            f"Source : {self.source_archive.path}\n"
            f"Waypoints : {self.source_archive.waypoint_count}\n"
            f"Cible : {mission.relative_target}\n\n"
            f"{autonomy_note}"
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
            QFrame#batteryCard { background: #f8faf9; border: 1px solid #d7dfde; border-radius: 8px; }
            QLineEdit, QDoubleSpinBox, QComboBox { background: white; border: 1px solid #bdc9c8; border-radius: 6px; padding: 6px; }
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
