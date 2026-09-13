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


class HostileTuple(tuple):
    def _trap(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("hostile tuple method must not run before exact-type rejection")

    __iter__ = _trap
    __len__ = _trap
    __getitem__ = _trap
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


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("paths", ["src"], "component paths must be a plain tuple"),
        (
            "dependencies",
            HostileTuple(("core",)),
            "component dependencies must be a plain tuple",
        ),
        (
            "build_commands",
            [["python", "-m", "build"]],
            "component build_commands must be a plain tuple",
        ),
        (
            "test_commands",
            HostileTuple((("python", "-m", "pytest"),)),
            "component test_commands must be a plain tuple",
        ),
    ),
)
def test_component_authority_collections_require_exact_tuple(
    field: str,
    value: object,
    message: str,
) -> None:
    kwargs: dict[str, object] = {
        "component_id": "app",
        "repository_id": "repo:app",
        "paths": ("src",),
        "dependencies": (),
        "build_commands": (),
        "test_commands": (),
    }
    kwargs[field] = value

    with pytest.raises(RepositoryGraphError, match=message):
        ProductComponent(**kwargs)  # type: ignore[arg-type]


def test_component_command_argv_requires_exact_tuple_before_iteration() -> None:
    with pytest.raises(RepositoryGraphError, match="command argv must be a plain tuple"):
        ProductComponent(
            "app",
            "repo:app",
            ("src",),
            build_commands=(HostileTuple(("python", "-m", "build")),),
        )


def test_lease_authority_collections_require_exact_tuple() -> None:
    with pytest.raises(RepositoryGraphError, match="lease component_ids must be a plain tuple"):
        OwnershipLease(
            "lease:candidate",
            "worker:a",
            ["app"],  # type: ignore[arg-type]
            ("src/api",),
        )

    with pytest.raises(RepositoryGraphError, match="lease allowed_paths must be a plain tuple"):
        OwnershipLease(
            "lease:candidate",
            "worker:a",
            ("app",),
            HostileTuple(("src/api",)),
        )


def test_integration_decision_authority_collections_require_exact_tuple() -> None:
    with pytest.raises(
        RepositoryGraphError,
        match="integration decision lease_ids must be a plain tuple",
    ):
        IntegrationDecision(
            "decision:hostile-container",
            IntegrationDecisionKind.RECONCILE,
            ["lease:candidate", "lease:active"],  # type: ignore[arg-type]
            "mutable lease authority must fail closed",
            ("evidence:test",),
        )

    with pytest.raises(
        RepositoryGraphError,
        match="integration decision evidence_refs must be a plain tuple",
    ):
        IntegrationDecision(
            "decision:hostile-container",
            IntegrationDecisionKind.RECONCILE,
            ("lease:candidate", "lease:active"),
            "hostile evidence authority must fail closed",
            HostileTuple(("evidence:test",)),
        )


def test_graph_authority_collections_require_exact_tuple_before_iteration() -> None:
    repository = RepositoryRef("repo:app", "github", "owner/app", "main")
    component = ProductComponent("app", "repo:app", ("src",))

    with pytest.raises(RepositoryGraphError, match="repositories must be a plain tuple"):
        ProductRepositoryGraph(
            project_id="project:primitive-authority",
            repositories=[repository],  # type: ignore[arg-type]
            components=(component,),
        )

    with pytest.raises(RepositoryGraphError, match="components must be a plain tuple"):
        ProductRepositoryGraph(
            project_id="project:primitive-authority",
            repositories=(repository,),
            components=HostileTuple((component,)),
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
