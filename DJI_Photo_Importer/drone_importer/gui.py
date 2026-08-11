"""Interface PyQt5 réactive pour analyser et importer les missions DJI."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PyQt5.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QCloseEvent, QFont
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from get_latest_drone_photo import (
    DESTINATION,
    DroneError,
    find_dji_devices,
    human_size,
    mounted_storages,
)

from .core import scan_drone_photos
from .history import DEFAULT_HISTORY_PATH, HistoryError, SyncHistory
from .missions import ImportPlan, Mission, build_import_plan
from .transfer import TransferError, TransferProgress, import_missions


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = PROJECT_ROOT / "logs" / "drone_importer.log"
LOGGER = logging.getLogger("drone_importer")


def configure_logging() -> None:
    """Active un journal tournant sans dupliquer les handlers."""
    if LOGGER.handlers:
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(threadName)s | %(message)s")
    )
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)


def friendly_error(exc: Exception) -> str:
    # Les erreurs prévues sont compréhensibles telles quelles. Les détails d'une
    # erreur inattendue restent dans le log plutôt que dans une fenêtre technique.
    if isinstance(exc, (DroneError, HistoryError, TransferError, ValueError)):
        return str(exc)
    return "Une erreur inattendue est survenue. Consultez le fichier de log."


class ConnectionWorker(QObject):
    """Vérifie rapidement la présence du drone au démarrage de la fenêtre."""

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    done = pyqtSignal()

    @pyqtSlot()
    def run(self) -> None:
        try:
            devices = find_dji_devices()
            storages = mounted_storages(devices) if devices else []
            self.succeeded.emit((devices, storages))
        except Exception as exc:
            LOGGER.exception("Echec de la detection DJI")
            self.failed.emit(friendly_error(exc))
        finally:
            self.done.emit()


class ScanWorker(QObject):
    """Exécute le scan et la préparation des missions hors du thread graphique."""

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    done = pyqtSignal()

    def __init__(
        self,
        gap_minutes: int,
        include_jpeg: bool,
        include_dng: bool,
        ignore_imported: bool,
    ) -> None:
        super().__init__()
        self.gap_minutes = gap_minutes
        self.include_jpeg = include_jpeg
        self.include_dng = include_dng
        self.ignore_imported = ignore_imported

    @pyqtSlot()
    def run(self) -> None:
        try:
            scan = scan_drone_photos(
                include_jpeg=self.include_jpeg,
                include_dng=self.include_dng,
            )
            history = SyncHistory(DEFAULT_HISTORY_PATH).load()
            plan = build_import_plan(
                scan,
                history,
                gap_minutes=self.gap_minutes,
                ignore_imported=self.ignore_imported,
            )
            self.succeeded.emit(plan)
        except Exception as exc:
            LOGGER.exception("Echec de l'analyse du drone")
            self.failed.emit(friendly_error(exc))
        finally:
            self.done.emit()


class TransferWorker(QObject):
    """Copie les missions hors du thread graphique et publie la progression."""

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(object)
    done = pyqtSignal()

    def __init__(self, missions: list[Mission]) -> None:
        super().__init__()
        self.missions = missions

    @pyqtSlot()
    def run(self) -> None:
        try:
            history = SyncHistory(DEFAULT_HISTORY_PATH).load()
            summary = import_missions(
                self.missions,
                history,
                destination_root=DESTINATION,
                progress_callback=self.progress.emit,
            )
            self.succeeded.emit(summary)
        except Exception as exc:
            LOGGER.exception("Echec de l'import des missions")
            self.failed.emit(friendly_error(exc))
        finally:
            self.done.emit()


class MainWindow(QMainWindow):
    """Fenêtre principale : elle affiche les données mais ne les calcule pas."""

    def __init__(self) -> None:
        super().__init__()
        self.plan: ImportPlan | None = None
        self.thread: QThread | None = None
        self.worker: QObject | None = None
        self.rescan_after_task = False
        self.setWindowTitle("DJI Photo Importer")
        self.setMinimumSize(760, 680)
        self.resize(840, 760)
        self._build_ui()
        self._apply_style()
        # La vérification démarre après l'affichage pour ne pas retarder la fenêtre.
        QTimer.singleShot(150, self.check_connection)

    def _build_ui(self) -> None:
        # L'interface est construite en code pour éviter un fichier .ui séparé
        # et garder cette petite application simple à distribuer.
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        title_row = QHBoxLayout()
        title = QLabel("DJI Photo Importer")
        title.setObjectName("title")
        title.setFont(QFont("Arial", 25, QFont.Bold))
        title_row.addWidget(title)
        title_row.addStretch()
        self.connection_label = QLabel("\u25cf Verification du drone...")
        self.connection_label.setObjectName("connectionPending")
        title_row.addWidget(self.connection_label)
        layout.addLayout(title_row)

        subtitle = QLabel(
            "Importez les nouvelles photos originales et classez-les automatiquement par mission."
        )
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        controls = QHBoxLayout()
        self.scan_button = QPushButton("Analyser le drone")
        self.scan_button.setObjectName("secondaryButton")
        self.scan_button.clicked.connect(self.start_scan)
        controls.addWidget(self.scan_button)
        controls.addStretch()
        layout.addLayout(controls)

        summary_frame = QFrame()
        summary_frame.setObjectName("summaryFrame")
        summary_layout = QVBoxLayout(summary_frame)
        self.summary_label = QLabel("Branchez le drone puis lancez une analyse.")
        self.summary_label.setObjectName("summary")
        self.summary_label.setWordWrap(True)
        summary_layout.addWidget(self.summary_label)
        layout.addWidget(summary_frame)

        missions_header = QHBoxLayout()
        missions_title = QLabel("Missions detectees")
        missions_title.setObjectName("sectionTitle")
        missions_header.addWidget(missions_title)
        missions_header.addStretch()
        self.select_all_button = QPushButton("Tout selectionner")
        self.select_none_button = QPushButton("Tout deselectionner")
        self.select_all_button.clicked.connect(lambda: self.set_all_checked(True))
        self.select_none_button.clicked.connect(lambda: self.set_all_checked(False))
        missions_header.addWidget(self.select_all_button)
        missions_header.addWidget(self.select_none_button)
        layout.addLayout(missions_header)

        self.mission_list = QListWidget()
        self.mission_list.setAlternatingRowColors(False)
        self.mission_list.setSpacing(5)
        self.mission_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.mission_list, 1)

        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("Parametres avances")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(Qt.RightArrow)
        self.advanced_toggle.toggled.connect(self.toggle_advanced)
        layout.addWidget(self.advanced_toggle)

        self.advanced_widget = QWidget()
        advanced_layout = QVBoxLayout(self.advanced_widget)
        advanced_layout.setContentsMargins(14, 0, 0, 0)
        gap_row = QHBoxLayout()
        gap_row.addWidget(QLabel("Separer deux missions apres :"))
        self.gap_spin = QSpinBox()
        self.gap_spin.setRange(1, 120)
        self.gap_spin.setValue(5)
        self.gap_spin.setSuffix(" minutes")
        gap_row.addWidget(self.gap_spin)
        gap_row.addStretch()
        advanced_layout.addLayout(gap_row)
        self.jpeg_checkbox = QCheckBox("Importer JPG/JPEG")
        self.jpeg_checkbox.setChecked(True)
        self.dng_checkbox = QCheckBox("Importer DNG")
        self.dng_checkbox.setChecked(True)
        self.ignore_checkbox = QCheckBox("Ignorer les photos deja importees")
        self.ignore_checkbox.setChecked(True)
        advanced_layout.addWidget(self.jpeg_checkbox)
        advanced_layout.addWidget(self.dng_checkbox)
        advanced_layout.addWidget(self.ignore_checkbox)
        self.gap_spin.valueChanged.connect(self.invalidate_plan)
        self.jpeg_checkbox.toggled.connect(self.invalidate_plan)
        self.dng_checkbox.toggled.connect(self.invalidate_plan)
        self.ignore_checkbox.toggled.connect(self.invalidate_plan)
        self.advanced_widget.hide()
        layout.addWidget(self.advanced_widget)

        self.progress_label = QLabel("")
        self.progress_label.setObjectName("progressText")
        self.progress_label.hide()
        layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        self.import_button = QPushButton("Importer les missions selectionnees")
        self.import_button.setObjectName("primaryButton")
        self.import_button.setMinimumHeight(48)
        self.import_button.clicked.connect(self.start_import)
        self.import_button.setEnabled(False)
        layout.addWidget(self.import_button)

        destination = QLabel(f"Destination : {DESTINATION}")
        destination.setObjectName("footnote")
        destination.setWordWrap(True)
        layout.addWidget(destination)

        self.setCentralWidget(root)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #f4f6f8; color: #17212b; }
            QLabel#title { color: #10212f; }
            QLabel#subtitle, QLabel#footnote { color: #687784; }
            QLabel#connectionPending { color: #7a8791; font-weight: 600; }
            QLabel#connectionOnline { color: #16835b; font-weight: 700; }
            QLabel#connectionOffline { color: #bd3d3d; font-weight: 700; }
            QFrame#summaryFrame { background: #e7edf2; border-radius: 9px; }
            QLabel#summary { padding: 8px; font-size: 14px; font-weight: 600; }
            QLabel#sectionTitle { font-size: 16px; font-weight: 700; }
            QListWidget { background: white; border: 1px solid #d8e0e6; border-radius: 9px; padding: 7px; }
            QListWidget::item { border-bottom: 1px solid #edf1f4; padding: 10px 8px; }
            QListWidget::item:selected { background: #dcecf8; color: #17212b; }
            QPushButton { border: 1px solid #c8d2da; border-radius: 7px; background: white; padding: 7px 12px; }
            QPushButton:hover { background: #edf4f8; }
            QPushButton:disabled { color: #98a4ad; background: #edf0f2; }
            QPushButton#primaryButton { background: #146b8c; color: white; border: none; font-size: 15px; font-weight: 700; }
            QPushButton#primaryButton:hover { background: #0f5c79; }
            QPushButton#secondaryButton { color: #0f5c79; border-color: #7fa8ba; font-weight: 600; }
            QProgressBar { border: none; border-radius: 6px; background: #dce3e8; height: 13px; text-align: center; }
            QProgressBar::chunk { border-radius: 6px; background: #16835b; }
            QSpinBox { background: white; border: 1px solid #c8d2da; border-radius: 5px; padding: 4px; }
            QToolButton { border: none; color: #405461; font-weight: 600; }
            """
        )

    def toggle_advanced(self, visible: bool) -> None:
        self.advanced_toggle.setArrowType(Qt.DownArrow if visible else Qt.RightArrow)
        self.advanced_widget.setVisible(visible)

    def invalidate_plan(self, *_args) -> None:
        if self.plan is None:
            return
        # Un plan calculé avec d'anciens paramètres ne doit jamais être importé.
        self.plan = None
        self.mission_list.clear()
        self.import_button.setEnabled(False)
        self.summary_label.setText("Parametres modifies. Relancez l'analyse du drone.")

    def _start_worker(self, worker: QObject, success_slot) -> None:
        if self.thread and self.thread.isRunning():
            return
        # Chaque opération longue possède son worker. Les signaux ramènent les
        # résultats dans le thread principal, seul autorisé à modifier les widgets.
        self.thread = QThread(self)
        self.worker = worker
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker.run)
        worker.succeeded.connect(success_slot)
        worker.failed.connect(self.task_failed)
        worker.done.connect(self.thread.quit)
        worker.done.connect(worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.task_finished)
        self._set_busy(True)
        self.thread.start()

    def _set_busy(self, busy: bool) -> None:
        self.scan_button.setEnabled(not busy)
        self.import_button.setEnabled(not busy and self.mission_list.count() > 0)
        self.select_all_button.setEnabled(not busy)
        self.select_none_button.setEnabled(not busy)
        self.advanced_toggle.setEnabled(not busy)
        self.advanced_widget.setEnabled(not busy)

    def check_connection(self) -> None:
        self.connection_label.setObjectName("connectionPending")
        self.connection_label.setText("\u25cf Verification du drone...")
        self.connection_label.style().unpolish(self.connection_label)
        self.connection_label.style().polish(self.connection_label)
        self._start_worker(ConnectionWorker(), self.connection_checked)

    @pyqtSlot(object)
    def connection_checked(self, result: object) -> None:
        devices, storages = result
        if devices and storages:
            self._set_connection(True, f"Drone : \u25cf Connecte ({devices[0].name})")
        elif devices:
            self._set_connection(False, "Drone : \u25cf Stockage inaccessible")
        else:
            self._set_connection(False, "Drone : \u25cf Non connecte")

    def _set_connection(self, connected: bool, text: str) -> None:
        self.connection_label.setObjectName(
            "connectionOnline" if connected else "connectionOffline"
        )
        self.connection_label.setText(text)
        self.connection_label.style().unpolish(self.connection_label)
        self.connection_label.style().polish(self.connection_label)

    def start_scan(self) -> None:
        if not self.jpeg_checkbox.isChecked() and not self.dng_checkbox.isChecked():
            QMessageBox.information(
                self,
                "Formats",
                "Selectionnez au moins JPG/JPEG ou DNG.",
            )
            return
        self.summary_label.setText("Analyse du stockage et lecture des metadonnees...")
        self.plan = None
        self.mission_list.clear()
        self.import_button.setEnabled(False)
        worker = ScanWorker(
            gap_minutes=self.gap_spin.value(),
            include_jpeg=self.jpeg_checkbox.isChecked(),
            include_dng=self.dng_checkbox.isChecked(),
            ignore_imported=self.ignore_checkbox.isChecked(),
        )
        self._start_worker(worker, self.scan_completed)

    @pyqtSlot(object)
    def scan_completed(self, result: object) -> None:
        self.plan = result
        device = result.scan.devices[0].name if result.scan.devices else "DJI"
        self._set_connection(True, f"Drone : \u25cf Connecte ({device})")
        self.summary_label.setText(
            f"{len(result.scan.photos)} photos trouvees  |  "
            f"{result.imported_count} deja importees  |  "
            f"{result.new_count} nouvelles  |  "
            f"{len(result.missions)} missions detectees"
        )
        # L'index stocké dans Qt.UserRole relie chaque ligne à l'objet Mission.
        for index, mission in enumerate(result.missions):
            item = QListWidgetItem(
                f"Mission {mission.number}    {mission.start_at:%d/%m/%Y}    "
                f"{mission.start_at:%H:%M:%S} \u2192 {mission.end_at:%H:%M:%S}\n"
                f"{len(mission.photos)} photo(s)    {human_size(mission.total_size)}"
            )
            item.setData(Qt.UserRole, index)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.mission_list.addItem(item)
        if not result.missions:
            self.summary_label.setText(
                self.summary_label.text() + "\nAucune nouvelle photo a importer."
            )
        if result.scan.warnings:
            LOGGER.warning("Avertissements de scan: %s", " | ".join(result.scan.warnings))
            self.summary_label.setText(
                self.summary_label.text()
                + f"\nAttention : {len(result.scan.warnings)} element(s) n'ont pas pu etre lus."
            )

    def set_all_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for index in range(self.mission_list.count()):
            self.mission_list.item(index).setCheckState(state)

    def selected_missions(self) -> list[Mission]:
        if not self.plan:
            return []
        selected: list[Mission] = []
        for row in range(self.mission_list.count()):
            item = self.mission_list.item(row)
            if item.checkState() == Qt.Checked:
                selected.append(self.plan.missions[item.data(Qt.UserRole)])
        return selected

    def start_import(self) -> None:
        missions = self.selected_missions()
        if not missions:
            QMessageBox.information(
                self,
                "Selection",
                "Selectionnez au moins une mission a importer.",
            )
            return
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.progress_label.setText("Preparation de l'import...")
        self.progress_label.show()
        # Le callback de progression devient un signal Qt sûr entre les threads.
        worker = TransferWorker(missions)
        worker.progress.connect(self.transfer_progress)
        self._start_worker(worker, self.import_completed)

    @pyqtSlot(object)
    def transfer_progress(self, progress: TransferProgress) -> None:
        self.progress_bar.setValue(progress.percent)
        self.progress_label.setText(
            f"Importation de Mission {progress.mission.number}  |  "
            f"{progress.current} / {progress.total}  |  {progress.photo.name}"
        )

    @pyqtSlot(object)
    def import_completed(self, summary: object) -> None:
        self.progress_bar.setValue(100)
        self.progress_label.setText(
            f"Import termine : {summary.imported} photo(s) copiee(s), "
            f"{summary.already_present} deja presente(s)."
        )
        self.rescan_after_task = True
        QMessageBox.information(
            self,
            "Import termine",
            f"{summary.imported} photo(s) importee(s) dans :\n{DESTINATION}",
        )

    @pyqtSlot(str)
    def task_failed(self, message: str) -> None:
        self.progress_bar.hide()
        self.progress_label.hide()
        if "drone" in message.casefold() or "stockage" in message.casefold():
            self._set_connection(False, "Drone : \u25cf Non connecte")
        QMessageBox.critical(self, "DJI Photo Importer", message)
        self.summary_label.setText(message)

    @pyqtSlot()
    def task_finished(self) -> None:
        self._set_busy(False)
        self.thread = None
        self.worker = None
        # Après un import, un nouveau scan actualise immédiatement l'historique.
        if self.rescan_after_task:
            self.rescan_after_task = False
            QTimer.singleShot(100, self.start_scan)

    def closeEvent(self, event: QCloseEvent) -> None:
        # Fermer pendant une copie serait déroutant même si la reprise est sûre.
        if self.thread and self.thread.isRunning():
            QMessageBox.warning(
                self,
                "Operation en cours",
                "Attendez la fin de l'operation avant de fermer l'application.",
            )
            event.ignore()
            return
        event.accept()


def run_gui() -> int:
    """Crée l'application Qt et démarre sa boucle d'événements."""
    configure_logging()
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("DJI Photo Importer")
    window = MainWindow()
    window.show()
    return app.exec_()
