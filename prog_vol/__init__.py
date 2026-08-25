"""API réutilisable de génération et de transfert de missions DJI."""

from .autonomy import (
    AlertLevel,
    AutonomyError,
    BatteryAssessment,
    BatteryProfile,
    BatteryState,
    DEFAULT_BATTERY_PROFILE,
    FlightEstimate,
    MissionPart,
    MissionSplitPlan,
    assess_batteries,
    custom_battery_profile,
    estimate_flight,
    plan_mission_split,
)
from .generator import (
    GeneratedRoute,
    GenerationError,
    GenerationResult,
    MissionGenerationSet,
    MissionParameters,
    PassRange,
    generate_mission_parts,
    generate_mission,
    generate_route_polygon,
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
    "GeneratedRoute",
    "GenerationError",
    "GenerationResult",
    "MissionGenerationSet",
    "MissionArchive",
    "MissionError",
    "MissionParameters",
    "MissionPart",
    "MissionSplitPlan",
    "PassRange",
    "StorageError",
    "TransferError",
    "build_transfer_plan",
    "assess_batteries",
    "custom_battery_profile",
    "detect_storage",
    "execute_transfer",
    "estimate_flight",
    "generate_mission",
    "generate_mission_parts",
    "generate_route_polygon",
    "inspect_mission",
    "plan_mission_split",
]
