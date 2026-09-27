from __future__ import annotations

import pytest

from nika_core.runtime.contracts import (
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeMode,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
    RuntimeResumeRequest,
)


class _BehavioralText(str):
    def strip(self, chars: str | None = None) -> str:
        del chars
        return "trusted"


@pytest.mark.parametrize("field_name", ("task_id", "thread_id"))
def test_runtime_request_rejects_behavioral_identity_text(field_name: str) -> None:
    values = {"task_id": "task", "thread_id": "thread"}
    values[field_name] = _BehavioralText("")

    with pytest.raises(TypeError, match=f"{field_name} must be an exact string"):
        RuntimeRequest(**values)


@pytest.mark.parametrize("field_name", ("task_id", "thread_id", "resume_token"))
def test_runtime_resume_request_rejects_behavioral_identity_text(field_name: str) -> None:
    values = {
        "task_id": "task",
        "thread_id": "thread",
        "resume_token": "resume",
    }
    values[field_name] = _BehavioralText("")

    with pytest.raises(TypeError, match=f"{field_name} must be an exact string"):
        RuntimeResumeRequest(**values)


def test_runtime_resume_request_rejects_raw_resume_mode_string() -> None:
    with pytest.raises(TypeError, match="mode must be a RuntimeResumeMode"):
        RuntimeResumeRequest(
            task_id="task",
            thread_id="thread",
            resume_token="resume",
            mode=RuntimeResumeMode.CONTINUE.value,
        )


def test_runtime_resume_probe_rejects_raw_status_string() -> None:
    with pytest.raises(TypeError, match="status must be a RuntimeResumeProbeStatus"):
        RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY.value,
            reason="checkpoint available",
            checkpoint_id="checkpoint",
        )


def test_runtime_resume_probe_rejects_behavioral_checkpoint_id() -> None:
    with pytest.raises(TypeError, match="checkpoint_id must be an exact string"):
        RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="checkpoint available",
            checkpoint_id=_BehavioralText("checkpoint"),
        )




@pytest.mark.parametrize("max_steps", (True, 1.0))
def test_runtime_request_rejects_non_exact_step_count(max_steps: object) -> None:
    with pytest.raises(TypeError, match="max_steps must be an exact integer"):
        RuntimeRequest(task_id="task", thread_id="thread", max_steps=max_steps)


@pytest.mark.parametrize("timeout_seconds", (True, "1"))
def test_runtime_request_rejects_non_numeric_timeout_carriers(
    timeout_seconds: object,
) -> None:
    with pytest.raises(TypeError, match="timeout_seconds must be numeric"):
        RuntimeRequest(
            task_id="task",
            thread_id="thread",
            timeout_seconds=timeout_seconds,
        )


@pytest.mark.parametrize("timeout_seconds", (float("nan"), float("inf"), float("-inf")))
def test_runtime_requests_reject_non_finite_timeouts(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be finite and positive"):
        RuntimeRequest(
            task_id="task",
            thread_id="thread",
            timeout_seconds=timeout_seconds,
        )
    with pytest.raises(ValueError, match="timeout_seconds must be finite and positive"):
        RuntimeResumeRequest(
            task_id="task",
            thread_id="thread",
            resume_token="resume",
            timeout_seconds=timeout_seconds,
        )


def test_runtime_event_rejects_boolean_sequence() -> None:
    with pytest.raises(TypeError, match="sequence must be an exact integer"):
        RuntimeEvent(sequence=True, event_type="runtime.completed")


def test_runtime_event_rejects_behavioral_event_type() -> None:
    with pytest.raises(TypeError, match="event_type must be an exact string"):
        RuntimeEvent(sequence=0, event_type=_BehavioralText("runtime.completed"))


def test_runtime_result_rejects_behavioral_resume_token() -> None:
    with pytest.raises(TypeError, match="resume_token must be an exact string"):
        RuntimeResult(
            outcome=RuntimeOutcome.WAITING_APPROVAL,
            resume_token=_BehavioralText("resume"),
        )


def test_exact_runtime_identity_carriers_remain_accepted() -> None:
    request = RuntimeRequest(
        task_id="task",
        thread_id="thread",
        max_steps=1,
        timeout_seconds=1.5,
    )
    resume = RuntimeResumeRequest(
        task_id="task",
        thread_id="thread",
        resume_token="resume",
        mode=RuntimeResumeMode.CONTINUE,
    )
    probe = RuntimeResumeProbe(
        status=RuntimeResumeProbeStatus.READY,
        reason="checkpoint available",
        checkpoint_id="checkpoint",
    )
    event = RuntimeEvent(sequence=0, event_type="runtime.completed")
    result = RuntimeResult(
        outcome=RuntimeOutcome.WAITING_APPROVAL,
        resume_token="resume",
    )

    assert request.task_id == resume.task_id == "task"
    assert resume.mode is RuntimeResumeMode.CONTINUE
    assert probe.can_resume is True
    assert event.event_type == "runtime.completed"
    assert result.resume_token == "resume"
