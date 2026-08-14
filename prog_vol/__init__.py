"""API réutilisable de génération et de transfert de missions DJI."""

from .autonomy import (
    AlertLevel,
    AutonomyError,
    BatteryAssessment,
    BatteryProfile,
    BatteryState,
    DEFAULT_BATTERY_PROFILE,
    FlightEstimate,
    assess_batteries,
    custom_battery_profile,
    estimate_flight,
)
from .generator import (
    GenerationError,
    GenerationResult,
    MissionParameters,
    generate_mission,
)
from .missions import MissionArchive, MissionError, inspect_mission
from .mtp import StorageError, detect_storage
from .transfer import TransferError, build_transfer_plan, execute_transfer

__all__ = [
    "AlertLevel",
    "AutonomyError",
    "BatteryAssessment",
    "BatteryProfile",
    "BatteryState",
    "DEFAULT_BATTERY_PROFILE",
    "FlightEstimate",
    "GenerationError",
    "GenerationResult",
    "MissionArchive",
    "MissionError",
    "MissionParameters",
    "StorageError",
    "TransferError",
    "build_transfer_plan",
    "assess_batteries",
    "custom_battery_profile",
    "detect_storage",
    "execute_transfer",
    "estimate_flight",
    "generate_mission",
    "inspect_mission",
]
