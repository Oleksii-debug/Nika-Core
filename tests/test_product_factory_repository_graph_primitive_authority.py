from __future__ import annotations

import pytest

from nika_core.product_factory_orchestration import (
    IntegrationDecision,
    IntegrationDecisionKind,
    OwnershipLease,
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryGraphError,
    RepositoryRef,
)


class HostileStr(str):
    def _trap(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("hostile primitive method must not run before exact-type rejection")

    strip = _trap
    casefold = _trap
    replace = _trap
    startswith = _trap
    endswith = _trap
    split = _trap
    __hash__ = _trap
    __eq__ = _trap


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project:primitive-authority",
        repositories=(RepositoryRef("repo:app", "github", "owner/app", "main"),),
        components=(ProductComponent("app", "repo:app", ("src",)),),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("repository_id", HostileStr("repo:app")),
        ("locator", HostileStr("owner/app")),
    ),
)
def test_repository_identity_rejects_hostile_string_subclass_before_normalization(
    field: str,
    value: str,
) -> None:
    kwargs = {
        "repository_id": "repo:app",
        "provider": "github",
        "locator": "owner/app",
        "default_branch": "main",
    }
    kwargs[field] = value

    with pytest.raises(RepositoryGraphError, match="plain string"):
        RepositoryRef(**kwargs)


def test_graph_project_id_rejects_hostile_string_subclass_before_hashing() -> None:
    with pytest.raises(RepositoryGraphError, match="project_id must be a plain string"):
        ProductRepositoryGraph(
            project_id=HostileStr("project:primitive-authority"),
            repositories=(RepositoryRef("repo:app", "github", "owner/app", "main"),),
            components=(ProductComponent("app", "repo:app", ("src",)),),
        )


def test_component_identity_and_paths_reject_hostile_string_subclasses() -> None:
    with pytest.raises(RepositoryGraphError, match="component_id must be a plain string"):
        ProductComponent(HostileStr("app"), "repo:app", ("src",))

    with pytest.raises(RepositoryGraphError, match="component path must be a plain string"):
        ProductComponent("app", "repo:app", (HostileStr("src"),))


def test_lease_identity_rejects_hostile_string_subclass_before_membership() -> None:
    with pytest.raises(RepositoryGraphError, match="lease_id must be a plain string"):
        OwnershipLease(
            HostileStr("lease:candidate"),
            "worker:a",
            ("app",),
            ("src/api",),
        )

    with pytest.raises(RepositoryGraphError, match="lease allowed path must be a plain string"):
        OwnershipLease(
            "lease:candidate",
            "worker:a",
            ("app",),
            (HostileStr("src/api"),),
        )


def test_integration_decision_lease_authority_rejects_hostile_string_before_set() -> None:
    with pytest.raises(
        RepositoryGraphError,
        match="integration decision lease id must be a plain string",
    ):
        IntegrationDecision(
            "decision:hostile",
            IntegrationDecisionKind.RECONCILE,
            ("lease:candidate", HostileStr("lease:active")),
            "hostile authority must fail closed",
            ("evidence:test",),
        )


def test_valid_plain_primitives_still_support_conflict_assessment() -> None:
    graph = _graph()
    active = OwnershipLease("lease:active", "worker:a", ("app",), ("src/api",))
    candidate = OwnershipLease("lease:candidate", "worker:b", ("app",), ("src/api/routes",))
    decision = IntegrationDecision(
        "decision:valid",
        IntegrationDecisionKind.RECONCILE,
        ("lease:candidate", "lease:active"),
        "valid exact primitives retain deterministic behavior",
        ("evidence:test",),
    )

    assessment = graph.assess_lease(candidate, (active,), decision=decision)

    assert assessment.requires_integration
    assert {conflict.active_lease_id for conflict in assessment.conflicts} == {"lease:active"}
