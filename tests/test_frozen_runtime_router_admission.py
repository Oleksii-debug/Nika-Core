from __future__ import annotations

from collections.abc import Sequence

import pytest

from nika_core.runtime.contracts import RuntimeRequest, RuntimeResumeRequest
from nika_core.runtime.frozen_router import FrozenRuntimeRouter
from nika_core.runtime.registry import RuntimeRegistry


class _Resolver:
    def runtime_id_for_run(self, request: RuntimeRequest) -> str:
        del request
        return "runtime-a"

    def runtime_id_for_resume(self, request: RuntimeResumeRequest) -> str:
        del request
        return "runtime-a"

    def runtime_id_for_existing(self, *, task_id: str, thread_id: str) -> str:
        del task_id, thread_id
        return "runtime-a"


class _UnderstatedSequence(Sequence[str]):
    """Claims one route while yielding far more during materialization."""

    def __len__(self) -> int:
        return 1

    def __getitem__(self, index: int) -> str:
        if index < 0 or index >= 300:
            raise IndexError(index)
        return f"runtime-{index}"


def test_plain_text_is_not_accepted_as_runtime_id_sequence() -> None:
    with pytest.raises(TypeError, match="sequence of runtime IDs"):
        FrozenRuntimeRouter(
            registry=RuntimeRegistry(),
            resolver=_Resolver(),
            allowed_runtime_ids="runtime-a",  # type: ignore[arg-type]
        )


def test_understated_sequence_is_bounded_before_registry_lookup() -> None:
    with pytest.raises(ValueError, match="changed during admission"):
        FrozenRuntimeRouter(
            registry=RuntimeRegistry(),
            resolver=_Resolver(),
            allowed_runtime_ids=_UnderstatedSequence(),
        )
