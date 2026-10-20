"""Python API for the Bitrix24 Time-manager."""

from bitrix_time_manager.client import BitrixClient
from bitrix_time_manager.config import PortalSchema, Settings
from bitrix_time_manager.models import ContractRef, OperationResult, RemoteWorkCreate, TimeEntryCreate, WorkdayCreate
from bitrix_time_manager.service import TimeManager

__version__ = "0.1.0"

__all__ = [
    "BitrixClient",
    "ContractRef",
    "OperationResult",
    "PortalSchema",
    "RemoteWorkCreate",
    "Settings",
    "TimeEntryCreate",
    "TimeManager",
    "WorkdayCreate",
]
