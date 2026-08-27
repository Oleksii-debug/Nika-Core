from __future__ import annotations

import asyncio
import json

import pytest

from nika_core.multi_agent.checker import (
    CheckerSource,
    CheckerStatus,
    V01CheckerAgent,
)
from nika_core.multi_agent.contracts import AgentHandoff, HandoffKind
from nika_core.runtime.contracts import (
    AgentRuntimePort,
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeRequest,
    RuntimeUnsupportedError,
)


def _source_bindings() -> tuple[CheckerSource, CheckerSource]:
    return (
        CheckerSource(source_id="source-a", worker_id="worker-a"),
        CheckerSource(source_id="source-b", worker_id="worker-b"),
    )


def _handoff(
    worker_id: str,
    *,
    source_id: str,
    facts: dict[str, object] | None = None,
    result: object = True,
    kind: HandoffKind = HandoffKind.RESULT,
    include_result: bool = True,
    error: str | None = None,
    team_id: str = "team-v01",
) -> AgentHandoff:
    payload: dict[str, object] = {"source_id": source_id}
    if facts is not None:
        payload["facts"] = facts
    if include_result:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    return AgentHandoff(
        team_id=team_id,
        sender_id=worker_id,
        recipient_id="supervisor",
        kind=kind,
        payload=payload,
        handoff_id=f"handoff-{worker_id}",
        correlation_id=f"corr-{worker_id}",
    )


def test_checker_reports_agreement_and_preserves_source_provenance() -> None:
    checker = V01CheckerAgent()
    summary = checker.compare(
        team_id="team-v01",
        checker_id="checker",
        sources=_source_bindings(),
        handoffs=(
            _handoff(
                "worker-a",
                source_id="source-a",
                facts={"condition": True, "title": "Україна"},
                result={"matched": True},
            ),
            _handoff(
                "worker-b",
                source_id="source-b",
                facts={"condition": True, "title": "Україна"},
                result={"matched": True},
            ),
        ),
    )

    assert summary.status is CheckerStatus.AGREEMENT
    assert summary.agreements == ("facts.condition", "facts.title", "result")
    assert summary.differences == ()
    assert summary.missing_sources == ()

    payload = summary.to_payload()
    assert payload["schema"] == "nika.v01.checker_summary.v1"
    assert [item["source_id"] for item in payload["sources"]] == ["source-a", "source-b"]
    assert [item["worker_id"] for item in payload["sources"]] == ["worker-a", "worker-b"]
    assert [item["handoff_id"] for item in payload["sources"]] == [
        "handoff-worker-a",
        "handoff-worker-b",
    ]
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def test_checker_reports_differences_without_cross_source_mix() -> None:
    summary = V01CheckerAgent().compare(
        team_id="team-v01",
        checker_id="checker",
        sources=_source_bindings(),
        handoffs=(
            _handoff(
                "worker-a",
                source_id="source-a",
                facts={"condition": True, "only_a": 1},
                result={"value": 10},
            ),
            _handoff(
                "worker-b",
                source_id="source-b",
                facts={"condition": False},
                result={"value": 11},
            ),
        ),
    )

    assert summary.status is CheckerStatus.DIFFERENCE
    assert summary.agreements == ()
    payload = summary.to_payload()
    by_field = {item["field"]: item for item in payload["differences"]}

    condition = by_field["facts.condition"]["values"]
    assert condition == [
        {"source_id": "source-a", "present": True, "value": True},
        {"source_id": "source-b", "present": True, "value": False},
    ]
    only_a = by_field["facts.only_a"]["values"]
    assert only_a == [
        {"source_id": "source-a", "present": True, "value": 1},
        {"source_id": "source-b", "present": False},
    ]
    result = by_field["result"]["values"]
    assert result[0]["source_id"] == "source-a"
    assert result[0]["value"] == {"value": 10}
    assert result[1]["source_id"] == "source-b"
    assert result[1]["value"] == {"value": 11}


def test_checker_marks_absent_worker_result_without_fabricating_payload() -> None:
    summary = V01CheckerAgent().compare(
        team_id="team-v01",
        checker_id="checker",
        sources=_source_bindings(),
        handoffs=(
            _handoff(
                "worker-a",
                source_id="source-a",
                facts={"condition": True},
                result={"matched": True},
            ),
        ),
    )

    assert summary.status is CheckerStatus.MISSING_RESULT
    assert summary.missing_sources == ("source-b",)
    assert summary.agreements == ()
    assert summary.differences == ()

    payload = summary.to_payload()
    missing = payload["sources"][1]
    assert missing == {
        "source_id": "source-b",
        "worker_id": "worker-b",
        "state": "missing_result",
    }
    assert "facts" not in missing
    assert "result" not in missing
    assert "error" not in missing


def test_checker_treats_worker_error_as_missing_result_without_inventing_result() -> None:
    summary = V01CheckerAgent().compare(
        team_id="team-v01",
        checker_id="checker",
        sources=_source_bindings(),
        handoffs=(
            _handoff("worker-a", source_id="source-a", result="ok"),
            _handoff(
                "worker-b",
                source_id="source-b",
                kind=HandoffKind.ERROR,
                include_result=False,
                error="RuntimeError",
            ),
        ),
    )

    assert summary.status is CheckerStatus.MISSING_RESULT
    assert summary.missing_sources == ("source-b",)
    failed = summary.to_payload()["sources"][1]
    assert failed["state"] == "error"
    assert failed["error"] == "RuntimeError"
    assert "result" not in failed


def test_checker_treats_result_handoff_without_result_field_as_missing() -> None:
    summary = V01CheckerAgent().compare(
        team_id="team-v01",
        checker_id="checker",
        sources=_source_bindings(),
        handoffs=(
            _handoff("worker-a", source_id="source-a", result="ok"),
            _handoff(
                "worker-b",
                source_id="source-b",
                facts={"observed": True},
                include_result=False,
            ),
        ),
    )

    assert summary.status is CheckerStatus.MISSING_RESULT
    missing = summary.to_payload()["sources"][1]
    assert missing["facts"] == {"observed": True}
    assert "result" not in missing


def test_checker_rejects_source_identity_mismatch_and_undeclared_worker() -> None:
    checker = V01CheckerAgent()
    with pytest.raises(ValueError, match="source mismatch"):
        checker.compare(
            team_id="team-v01",
            checker_id="checker",
            sources=_source_bindings(),
            handoffs=(
                _handoff("worker-a", source_id="source-b", result="wrong-source"),
            ),
        )

    with pytest.raises(ValueError, match="undeclared worker"):
        checker.compare(
            team_id="team-v01",
            checker_id="checker",
            sources=_source_bindings(),
            handoffs=(
                _handoff("worker-c", source_id="source-c", result="unexpected"),
            ),
        )


def test_checker_rejects_duplicate_or_cross_team_output() -> None:
    checker = V01CheckerAgent()
    duplicate = _handoff("worker-a", source_id="source-a", result="same")
    with pytest.raises(ValueError, match="duplicate worker output"):
        checker.compare(
            team_id="team-v01",
            checker_id="checker",
            sources=_source_bindings(),
            handoffs=(duplicate, duplicate),
        )

    with pytest.raises(ValueError, match="different team"):
        checker.compare(
            team_id="team-v01",
            checker_id="checker",
            sources=_source_bindings(),
            handoffs=(
                _handoff(
                    "worker-a",
                    source_id="source-a",
                    result="wrong-team",
                    team_id="team-other",
                ),
            ),
        )


def test_checker_requires_exactly_two_distinct_source_bindings() -> None:
    checker = V01CheckerAgent()
    with pytest.raises(ValueError, match="exactly two"):
        checker.compare(
            team_id="team-v01",
            checker_id="checker",
            sources=(CheckerSource(source_id="source-a", worker_id="worker-a"),),
            handoffs=(),
        )

    with pytest.raises(ValueError, match="source_id values must be unique"):
        checker.compare(
            team_id="team-v01",
            checker_id="checker",
            sources=(
                CheckerSource(source_id="source-a", worker_id="worker-a"),
                CheckerSource(source_id="source-a", worker_id="worker-b"),
            ),
            handoffs=(),
        )


def test_checker_fails_closed_on_non_json_worker_evidence() -> None:
    with pytest.raises(TypeError, match="JSON-compatible"):
        V01CheckerAgent().compare(
            team_id="team-v01",
            checker_id="checker",
            sources=_source_bindings(),
            handoffs=(
                _handoff("worker-a", source_id="source-a", result={"bad": {1, 2}}),
                _handoff("worker-b", source_id="source-b", result="ok"),
            ),
        )


class _CheckerNoModelRuntime(AgentRuntimePort):
    """Test-only proof that checker semantics fit the existing no-LLM runtime port."""

    runtime_id = "checker-no-model-test"
    capabilities = frozenset({RuntimeCapability.DETERMINISTIC_NO_LLM})

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        checker = V01CheckerAgent()
        summary = checker.compare(
            team_id=str(request.payload["team_id"]),
            checker_id=str(request.payload["checker_id"]),
            sources=_source_bindings(),
            handoffs=(
                _handoff("worker-a", source_id="source-a", result={"matched": True}),
                _handoff("worker-b", source_id="source-b", result={"matched": True}),
            ),
        )
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output=summary.to_payload())

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        del request
        raise RuntimeUnsupportedError("test checker runtime is not resumable")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return False


def test_checker_structural_path_runs_through_existing_no_llm_runtime_contract() -> None:
    runtime = _CheckerNoModelRuntime()
    assert RuntimeCapability.DETERMINISTIC_NO_LLM in runtime.capabilities

    result = asyncio.run(
        runtime.run(
            RuntimeRequest(
                task_id="team:team-v01:checker",
                thread_id="thread-checker",
                payload={"team_id": "team-v01", "checker_id": "checker"},
            )
        )
    )

    assert result.outcome is RuntimeOutcome.COMPLETED
    assert result.output["schema"] == "nika.v01.checker_summary.v1"
    assert result.output["status"] == "agreement"
