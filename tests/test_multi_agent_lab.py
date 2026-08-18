import pytest

from nika_core.agents.spec import PermissionPolicy, RiskLevel, ToolGrant
from nika_core.multi_agent.lab import AgentMessage, DelegationQuota, MessageKind, MultiAgentLab


def policy(*, risk: RiskLevel, network: bool = False, scopes: tuple[str, ...] = ()) -> PermissionPolicy:
    return PermissionPolicy(
        tool_grants=(ToolGrant(tool_id="browser.read", max_risk=risk, scopes=scopes),),
        allow_network=network,
    )


def test_child_privileges_are_attenuated() -> None:
    lab = MultiAgentLab(
        "root",
        policy(risk=RiskLevel.R1_REVERSIBLE, network=False, scopes=("public",)),
    )
    child = lab.delegate(
        "root",
        "researcher",
        policy(
            risk=RiskLevel.R3_SENSITIVE,
            network=True,
            scopes=("public", "private"),
        ),
    )

    grant = child.policy.grant_for("browser.read")
    assert grant is not None
    assert grant.max_risk is RiskLevel.R1_REVERSIBLE
    assert grant.scopes == ("public",)
    assert child.policy.allow_network is False


def test_ungranted_child_tool_is_dropped_fail_closed() -> None:
    root = PermissionPolicy(tool_grants=(ToolGrant(tool_id="browser.read"),))
    requested = PermissionPolicy(tool_grants=(ToolGrant(tool_id="shell.run"),))
    lab = MultiAgentLab("root", root)

    child = lab.delegate("root", "child", requested)

    assert child.policy.tool_grants == ()


def test_delegation_quotas_bound_tree_growth() -> None:
    quota = DelegationQuota(max_depth=1, max_children_per_parent=1, max_total_agents=2)
    lab = MultiAgentLab("root", PermissionPolicy(), quota)
    lab.delegate("root", "child", PermissionPolicy())

    with pytest.raises(RuntimeError, match="children-per-parent|total-agent"):
        lab.delegate("root", "second", PermissionPolicy())
    with pytest.raises(RuntimeError, match="depth"):
        lab.delegate("child", "grandchild", PermissionPolicy())


def test_typed_messages_require_known_agents_and_preserve_evidence() -> None:
    lab = MultiAgentLab("root", PermissionPolicy())
    lab.delegate("root", "child", PermissionPolicy())
    message = AgentMessage("root", "child", MessageKind.TASK, "Inspect workspace")

    lab.record_message(message)

    assert lab.messages == (message,)
    assert lab.ancestry("child") == ("root",)
    with pytest.raises(KeyError, match="unknown recipient"):
        lab.record_message(AgentMessage("root", "missing", MessageKind.STATUS, "ping"))
