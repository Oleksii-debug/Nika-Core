import pytest

from nika_core.agents.spec import AgentSpec, PermissionPolicy, RiskLevel, ToolGrant
from nika_core.learning.experiments import Candidate, Evaluation, Experiment, ExperimentStatus
from nika_core.ui.bridge_models import UICommand


def test_r4_can_never_be_pre_authorized() -> None:
    with pytest.raises(ValueError, match="R4"):
        PermissionPolicy(
            tool_grants=(ToolGrant(tool_id="payments.send", max_risk=RiskLevel.R4_EXPLICIT_HUMAN),)
        )


def test_permission_policy_is_fail_closed() -> None:
    policy = PermissionPolicy(
        tool_grants=(ToolGrant(tool_id="browser.read", max_risk=RiskLevel.R1_REVERSIBLE),)
    )
    assert policy.permits("browser.read", RiskLevel.R0_READ_ONLY)
    assert not policy.permits("browser.write", RiskLevel.R0_READ_ONLY)
    assert not policy.permits("browser.read", RiskLevel.R4_EXPLICIT_HUMAN)


def test_agent_spec_round_trip() -> None:
    spec = AgentSpec(
        agent_id="research.primary",
        name="Research",
        system_prompt="Research carefully.",
        permission_policy=PermissionPolicy(
            tool_grants=(ToolGrant(tool_id="web.search", max_risk=RiskLevel.R0_READ_ONLY),)
        ),
    )
    assert AgentSpec.import_json(spec.export_json()) == spec


def test_experiment_promotes_only_measured_challenger() -> None:
    champion = Candidate("current", "v1", "prompt://current/v1")
    challenger = Candidate("candidate", "v2", "prompt://candidate/v2")
    experiment = Experiment("exp-1", champion, (challenger,), "quality", minimum_improvement=0.05)
    experiment.start()
    experiment.record(Evaluation("current", {"quality": 0.80}, "replay-a"))
    experiment.record(Evaluation("candidate", {"quality": 0.90}, "replay-a"))
    assert experiment.complete() == "candidate"
    assert experiment.status is ExperimentStatus.PROMOTED
    assert experiment.rollback() == "current"
    assert experiment.status is ExperimentStatus.ROLLED_BACK


def test_bridge_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        UICommand.model_validate(
            {"request_id": "1", "action_id": "task.create", "payload": {}, "unsafe": True}
        )
