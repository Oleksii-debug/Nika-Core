from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from nika_core.interaction.domain import (
    AmbiguousTargetError,
    ControlLocator,
    ControlNode,
    InteractionTarget,
    SemanticSnapshot,
)
from nika_core.interaction.resolver import resolve_strict
from nika_core.runtime.contracts import RuntimeErrorCode, RuntimeOutcome, RuntimeRequest
from nika_core.runtime.langgraph_runtime import LangGraphRuntime
from nika_core.runtime.retry import RetryPolicy


class _DuplicateAccessibleNameGraph:
    """Thin QA harness around the current canonical semantic resolver."""

    def __init__(self) -> None:
        self.effect_count = 0
        self.captured_error: BaseException | None = None
        self.snapshot = SemanticSnapshot(
            target=InteractionTarget(),
            generation=1,
            revision=1,
            controls=(
                ControlNode(
                    node_id="raw-dom-node-alpha",
                    role="button",
                    name="Run",
                    enabled=True,
                    visible=True,
                ),
                ControlNode(
                    node_id="raw-dom-node-beta",
                    role="button",
                    name="Run",
                    enabled=True,
                    visible=True,
                ),
            ),
        )

    async def ainvoke(self, graph_input: object, *, config: object) -> object:
        del config
        assert isinstance(graph_input, Mapping)
        locator = ControlLocator(
            role=str(graph_input["role"]),
            name=str(graph_input["name"]),
        )
        try:
            target = resolve_strict(self.snapshot, locator)
        except AmbiguousTargetError as exc:
            self.captured_error = exc
            raise

        # This represents the first point where an external interaction could occur.
        # Ambiguity must fail before this line, leaving the effect count at zero.
        self.effect_count += 1
        return {"invoked": target.node_id}


def test_duplicate_accessible_name_fails_closed_without_retry_or_effect() -> None:
    graph = _DuplicateAccessibleNameGraph()
    runtime = LangGraphRuntime(graph)

    result = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id="v01-duplicate-name",
                thread_id="v01-duplicate-name-thread",
                payload={"role": "button", "name": "Run"},
            )
        )
    )

    # The canonical resolver must reject the two enabled, visible semantic matches.
    assert isinstance(graph.captured_error, AmbiguousTargetError)
    assert graph.effect_count == 0

    # The runtime boundary keeps the failure terminal/non-transient: unknown semantic
    # failures normalize to INTERNAL, while retries require an explicit allowlisted code.
    assert result.outcome == RuntimeOutcome.FAILED
    assert result.error_code == RuntimeErrorCode.INTERNAL
    assert result.error_code != RuntimeErrorCode.TRANSIENT
    retry_policy = RetryPolicy(
        max_retries=3,
        retryable_error_codes=frozenset({RuntimeErrorCode.TRANSIENT}),
        allow_fresh_retry=True,
    )
    assert retry_policy.should_retry(result, retries_used=0) is False

    # Diagnostics are useful but bounded to semantic locator context. Raw node/DOM
    # identities from either candidate must not be exposed by the ambiguity error.
    error = result.error or ""
    assert "ambiguous" in error.lower()
    assert "2 controls" in error
    assert "button" in error
    assert "Run" in error
    assert "raw-dom-node-alpha" not in error
    assert "raw-dom-node-beta" not in error
    assert "<button" not in error.lower()
    assert "outerhtml" not in error.lower()
    assert "<html" not in error.lower()
