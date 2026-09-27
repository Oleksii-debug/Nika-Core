from __future__ import annotations

import pytest

from nika_core.runtime.contracts import (
    RuntimeEvent,
    RuntimeRequest,
    RuntimeResumeMode,
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


def test_runtime_resume_request_rejects_raw_resume_mode_string() -> None:
    with pytest.raises(TypeError, match="mode must be a RuntimeResumeMode"):
        RuntimeResumeRequest(
            task_id="task",
            thread_id="thread",
            resume_token="resume",
            mode=RuntimeResumeMode.CONTINUE.value,
        )


@pytest.mark.parametrize("max_steps", (True, 1.0))
def test_runtime_requests_reject_non_exact_step_count(max_steps: object) -> None:
    with pytest.raises(TypeError, match="max_steps must be an exact integer"):
        RuntimeRequest(task_id="task", thread_id="thread", max_steps=max_steps)
    with pytest.raises(TypeError, match="max_steps must be an exact integer"):
        RuntimeResumeRequest(
            task_id="task",
            thread_id="thread",
            resume_token="resume",
            max_steps=max_steps,
        )


@pytest.mark.parametrize("timeout_seconds", (True, "1"))
def test_runtime_requests_reject_non_numeric_timeout_carriers(
    timeout_seconds: object,
) -> None:
    with pytest.raises(TypeError, match="timeout_seconds must be numeric"):
        RuntimeRequest(
            task_id="task",
            thread_id="thread",
            timeout_seconds=timeout_seconds,
        )
    with pytest.raises(TypeError, match="timeout_seconds must be numeric"):
        RuntimeResumeRequest(
            task_id="task",
            thread_id="thread",
            resume_token="resume",
            timeout_seconds=timeout_seconds,
        )


@pytest.mark.parametrize(
    "timeout_seconds",
    (float("nan"), float("inf"), float("-inf"), 10**400),
)
def test_runtime_requests_reject_non_finite_timeouts(timeout_seconds: object) -> None:
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
    event = RuntimeEvent(sequence=0, event_type="runtime.completed")

    assert request.task_id == resume.task_id == "task"
    assert resume.mode is RuntimeResumeMode.CONTINUE
    assert event.event_type == "runtime.completed"
