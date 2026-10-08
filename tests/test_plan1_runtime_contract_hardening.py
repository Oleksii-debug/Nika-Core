"""Plan 1 / Section 2: adversarial checks of framework-neutral runtime DTOs."""

from __future__ import annotations

import asyncio
from math import inf, nan

import pytest

from nika_core.runtime.contracts import (
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResumeMode,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
    RuntimeResumeRequest,
    RuntimeResult,
)
from nika_core.runtime.reference import ReferenceRuntime


@pytest.mark.parametrize("max_steps", [False, True, 1.5, "2", None])
def test_new_run_rejects_noninteger_step_budgets(max_steps: object) -> None:
    with pytest.raises(TypeError, match="max_steps"):
        RuntimeRequest("task", "thread", max_steps=max_steps)


@pytest.mark.parametrize("timeout_seconds", [nan, inf, -inf, 0, -1])
def test_new_run_rejects_nonfinite_or_nonpositive_time_budget(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        RuntimeRequest("task", "thread", timeout_seconds=timeout_seconds)


@pytest.mark.parametrize("timeout_seconds", [True, False, "30", object()])
def test_new_run_rejects_non_numeric_time_budget(timeout_seconds: object) -> None:
    with pytest.raises(TypeError, match="timeout_seconds"):
        RuntimeRequest("task", "thread", timeout_seconds=timeout_seconds)


@pytest.mark.parametrize("field,value", [
    ("max_steps", True),
    ("max_steps", 2.5),
    ("timeout_seconds", nan),
    ("timeout_seconds", inf),
    ("timeout_seconds", False),
])
def test_resume_rejects_same_invalid_limits_as_initial_run(field: str, value: object) -> None:
    kwargs = {field: value}
    with pytest.raises((TypeError, ValueError)):
        RuntimeResumeRequest("task", "thread", "persisted-checkpoint", **kwargs)


def test_resume_mode_must_be_a_real_enum_not_a_string_from_untrusted_adapter() -> None:
    with pytest.raises(TypeError, match="mode"):
        RuntimeResumeRequest("task", "thread", "token", mode="continue")


def test_checkpoint_authority_cannot_be_forged_with_string_enum() -> None:
    with pytest.raises(TypeError, match="status"):
        RuntimeResumeProbe(
            status="ready",
            reason="untrusted provider says it recovered",
            checkpoint_id="unverified",
        )
    missing = RuntimeResumeProbe(
        status=RuntimeResumeProbeStatus.MISSING,
        reason="checkpoint absent",
    )
    assert missing.can_resume is False
    valid = RuntimeResumeProbe(
        status=RuntimeResumeProbeStatus.READY,
        reason="durable checkpoint present",
        checkpoint_id="verified",
    )
    assert valid.can_resume is True


@pytest.mark.parametrize("sequence", [True, False, 0.5, "0", None])
def test_runtime_event_sequence_requires_actual_integer(sequence: object) -> None:
    with pytest.raises(TypeError, match="sequence"):
        RuntimeEvent(sequence, "runtime.event")


def test_framework_neutral_runtime_still_accepts_valid_execution_and_resume_envelope() -> None:
    request = RuntimeRequest(
        task_id="task", thread_id="thread", payload={"intent": "verify"}, max_steps=3,
        timeout_seconds=3.5,
    )
    result = asyncio.run(ReferenceRuntime().run(request))
    assert result.outcome is RuntimeOutcome.COMPLETED
    assert result.output["max_steps"] == 3
    assert result.events[0].sequence == 0
    resume = RuntimeResumeRequest(
        task_id="task", thread_id="thread", resume_token="checkpoint",
        mode=RuntimeResumeMode.CONTINUE, max_steps=3, timeout_seconds=3.5,
    )
    assert resume.timeout_seconds == 3.5


def test_failed_runtime_result_keeps_explicit_error_authority() -> None:
    with pytest.raises(ValueError, match="failed outcome"):
        RuntimeResult(outcome=RuntimeOutcome.FAILED)

@pytest.mark.parametrize("bad_id", [None, False, 7, object(), " task", "task ", "a\\nb", "a\\u202eb", "e\\u0301", "a\\u2028b"])
def test_runtime_request_rejects_hostile_or_noncanonical_task_id(bad_id: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        RuntimeRequest(task_id=bad_id, thread_id="thread")


@pytest.mark.parametrize("bad_id", [None, True, "thread\\rforged", "thread\\u200bhidden", "thread "])
def test_resume_rejects_hostile_thread_id_before_effects(bad_id: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        RuntimeResumeRequest(task_id="task", thread_id=bad_id, resume_token="cursor")


@pytest.mark.parametrize("bad_id", [None, 3, "cursor\\u0000injected", "cursor\\u2029fake"])
def test_resume_rejects_hostile_resume_token(bad_id: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        RuntimeResumeRequest(task_id="task", thread_id="thread", resume_token=bad_id)


def test_resume_probe_rejects_hostile_checkpoint_identity_even_if_ready() -> None:
    for bad in (3, "checkpoint\\ntrusted", "e\\u0301"):
        with pytest.raises((TypeError, ValueError)):
            RuntimeResumeProbe(
                status=RuntimeResumeProbeStatus.READY,
                reason="candidate checkpoint",
                checkpoint_id=bad,
            )
    valid = RuntimeResumeProbe(
        status=RuntimeResumeProbeStatus.READY,
        reason="verified checkpoint",
        checkpoint_id="checkpoint:2026-10-08",
    )
    assert valid.can_resume is True

