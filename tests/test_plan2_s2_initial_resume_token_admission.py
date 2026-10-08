"""Plan 2 Section 2: preserve inert durable resume-token authority."""

from __future__ import annotations

import pytest

from nika_core.multi_agent.supervisor import MultiAgentSupervisor
from nika_core.runtime.contracts import RuntimeCapability


class _HostileToken(str):
    def __bool__(self) -> bool:
        raise AssertionError("resume token must not be bool-coerced")

    def __str__(self) -> str:
        raise AssertionError("resume token must not be string-coerced")


class _Runtime:
    capabilities = frozenset({RuntimeCapability.DURABLE_RESUME})

    def __init__(self, token: object) -> None:
        self.token = token
        self.calls: list[tuple[str, str]] = []

    def initial_resume_token(self, *, task_id: str, thread_id: str) -> object:
        self.calls.append((task_id, thread_id))
        return self.token


def _prepare(token: object) -> tuple[MultiAgentSupervisor, _Runtime]:
    runtime = _Runtime(token)
    # This focused port-boundary regression needs no SQLite effects or scheduler.
    supervisor = MultiAgentSupervisor.__new__(MultiAgentSupervisor)
    supervisor._runtime = runtime
    return supervisor, runtime


@pytest.mark.parametrize("token", (None, 1, True, _HostileToken("resume-cursor")))
def test_non_plain_resume_tokens_are_rejected_before_durable_use(token: object) -> None:
    supervisor, runtime = _prepare(token)
    with pytest.raises(TypeError, match="plain resume token"):
        supervisor._initial_resume_token(
            team_id="team-one", member_id="child-one", thread_id="thread-one"
        )
    assert runtime.calls == [("team:team-one:child-one", "thread-one")]


@pytest.mark.parametrize("token", ("", "  "))
def test_blank_resume_token_is_rejected(token: str) -> None:
    supervisor, runtime = _prepare(token)
    with pytest.raises(RuntimeError, match="empty initial resume token"):
        supervisor._initial_resume_token(
            team_id="team-one", member_id="child-one", thread_id="thread-one"
        )
    assert len(runtime.calls) == 1


def test_plain_resume_token_remains_exact_stable_cursor() -> None:
    supervisor, runtime = _prepare("cursor-a:b")
    assert supervisor._initial_resume_token(
        team_id="team-one", member_id="child-one", thread_id="thread-one"
    ) == "cursor-a:b"
    assert runtime.calls == [("team:team-one:child-one", "thread-one")]
