from __future__ import annotations

import inspect

from nika_core.multi_agent.supervisor import MultiAgentSupervisor
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeResult


def test_new_child_runtime_request_has_explicit_finite_timeout() -> None:
    source = inspect.getsource(MultiAgentSupervisor._run_new_child)
    assert "timeout_seconds=" in source, (
        "V0.1 child execution has no explicit finite RuntimeRequest timeout and can hang indefinitely"
    )


def test_recovered_child_runtime_resume_has_explicit_finite_timeout() -> None:
    source = inspect.getsource(MultiAgentSupervisor._recover_child)
    assert "timeout_seconds=" in source, (
        "V0.1 resumed child execution has no explicit finite RuntimeResumeRequest timeout"
    )


def test_paused_runtime_outcome_is_not_persisted_as_running_member() -> None:
    state = MultiAgentSupervisor._state_for_result(RuntimeResult(outcome=RuntimeOutcome.PAUSED))
    assert state.value == "paused", (
        "a paused runtime result is persisted/projected as an actively running team member"
    )
