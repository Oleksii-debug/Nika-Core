"""Plan 1 Section 2: runtime identities must be bounded at all ingress/egress ports."""

from __future__ import annotations

import pytest

from nika_core.runtime.contracts import (
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
    RuntimeResumeRequest,
    RuntimeResult,
)


def _request(value: str) -> object:
    return RuntimeRequest(task_id=value, thread_id="thread")


def _resume(value: str) -> object:
    return RuntimeResumeRequest(task_id="task", thread_id="thread", resume_token=value)


def _probe(value: str) -> object:
    return RuntimeResumeProbe(
        status=RuntimeResumeProbeStatus.READY,
        reason="trusted durable cursor",
        checkpoint_id=value,
    )


def _result(value: str) -> object:
    return RuntimeResult(outcome=RuntimeOutcome.PAUSED, resume_token=value)


def _event(value: str) -> object:
    return RuntimeEvent(sequence=0, event_type=value)


@pytest.mark.parametrize("factory", [_request, _resume, _probe, _result, _event])
@pytest.mark.parametrize("value", ["a" * 513, "🧩" * 129, "name\nforged", "name\u202espoof"])
def test_bounded_runtime_identity_rejects_oversized_or_unsafe_text(factory, value) -> None:
    with pytest.raises(ValueError):
        factory(value)


@pytest.mark.parametrize("factory", [_request, _resume, _probe, _result, _event])
@pytest.mark.parametrize("value", ["a" * 512, "🧩" * 128, "safe:id.v1"])
def test_bounded_runtime_identity_preserves_existing_canonical_names(factory, value) -> None:
    assert factory(value) is not None


class MisleadingFloat(float):
    """Float subclass must not supply runtime control parameters."""

    def __le__(self, other):
        raise AssertionError("Untrusted comparison was invoked")


class MisleadingInt(int):
    def __le__(self, other):
        raise AssertionError("Untrusted comparison was invoked")


@pytest.mark.parametrize("timeout", [MisleadingFloat(4.0), MisleadingInt(4)])
def test_nonbuiltin_numeric_timeout_is_rejected_before_external_behavior(timeout: object) -> None:
    with pytest.raises(TypeError, match="timeout_seconds"):
        RuntimeRequest("task", "thread", timeout_seconds=timeout)
    with pytest.raises(TypeError, match="timeout_seconds"):
        RuntimeResumeRequest("task", "thread", "token", timeout_seconds=timeout)


def test_runtime_event_type_is_canonical_and_does_not_inject_fake_events() -> None:
    with pytest.raises((TypeError, ValueError)):
        RuntimeEvent(sequence=0, event_type="runtime.started\nruntime.completed")
    with pytest.raises((TypeError, ValueError)):
        RuntimeEvent(sequence=0, event_type="runtime.\u200bhidden")
    assert RuntimeEvent(sequence=0, event_type="runtime.started").event_type == (
        "runtime.started"
    )
