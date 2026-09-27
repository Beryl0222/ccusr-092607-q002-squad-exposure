"""梯队实战机会配置簿：契约、事件存储与选拔应用服务。"""

from .clock import SimulationClock
from .contracts import ContractIssue, validate_event
from .service import ServiceError, SquadExposureService
from .store import (
    AggregateVersionConflict,
    AppendOutcome,
    EventConflict,
    EventStore,
    StoreError,
)

__all__ = [
    "ContractIssue",
    "validate_event",
    "SimulationClock",
    "SquadExposureService",
    "ServiceError",
    "EventStore",
    "StoreError",
    "EventConflict",
    "AggregateVersionConflict",
    "AppendOutcome",
]
