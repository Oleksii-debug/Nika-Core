from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nika_core.resources.contracts import (
    ResourceBudget,
    ResourceObserverPort,
    ResourceRequestIdentity,
    ResourceSnapshot,
)
from nika_core.resources.manager import ResourceDecision, ResourceManager

if TYPE_CHECKING:
    from nika_core.resources.psutil_adapter import PsutilResourceObserver

__all__ = [
    "PsutilResourceObserver",
    "ResourceBudget",
    "ResourceDecision",
    "ResourceManager",
    "ResourceObserverPort",
    "ResourceRequestIdentity",
    "ResourceSnapshot",
]


def __getattr__(name: str) -> Any:
    if name == "PsutilResourceObserver":
        from nika_core.resources.psutil_adapter import PsutilResourceObserver

        return PsutilResourceObserver
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
