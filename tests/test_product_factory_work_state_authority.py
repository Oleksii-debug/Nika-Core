from dataclasses import replace

import pytest

from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    CoordinatorError,
    ProductFactoryCoordinator,
    WorkRecord,
)
from tests.test_product_factory_coordinator import PERMISSIONS, SHA_A
from tests.test_product_factory_work_lifecycle import _coordinator, _core_record, _graph


@pytest.mark.parametrize("state", ("planned", "ready", "accepted", "done", "cancelled"))
def test_work_record_state_requires_exact_enum(state: str) -> None:
    record = _core_record(_coordinator())

    with pytest.raises(CoordinatorError, match="work state must be an exact WorkState"):
        replace(record, state=state)


def _forge_work_record_state(record: WorkRecord, state: object) -> WorkRecord:
    forged = object.__new__(WorkRecord)
    for field_name in ("request", "state", "result", "review", "blocker"):
        object.__setattr__(
            forged,
            field_name,
            state if field_name == "state" else getattr(record, field_name),
        )
    return forged


@pytest.mark.parametrize("state", ("ready", "accepted", "done"))
def test_restore_rejects_forged_strenum_equal_work_state(state: str) -> None:
    coordinator = _coordinator()
    snapshot = coordinator.snapshot()
    records = tuple(
        _forge_work_record_state(record, state)
        if record.request.component_id == "core"
        else record
        for record in snapshot.records
    )
    tampered = replace(snapshot, records=records)

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(
        CoordinatorError,
        match="snapshot work state must be an exact WorkState",
    ):
        restored.restore(
            tampered,
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )


def _plan(graph, *, permission_ceiling=PERMISSIONS) -> None:
    ProductFactoryCoordinator(graph).plan(
        base_shas={"repo-1": SHA_A},
        goals={"core": "build core", "ui": "build ui"},
        permission_ceiling=permission_ceiling,
    )


def test_plan_rejects_mutable_permission_ceiling_alias() -> None:
    with pytest.raises(
        CoordinatorError,
        match="permission ceiling must be a non-empty exact frozenset",
    ):
        _plan(_graph(), permission_ceiling=set(PERMISSIONS))


def test_plan_rejects_mutable_allowed_paths_alias() -> None:
    graph = _graph()
    components = tuple(
        replace(component, paths=list(component.paths))
        if component.component_id == "core"
        else component
        for component in graph.components
    )
    graph = replace(graph, components=components)

    with pytest.raises(
        CoordinatorError,
        match="allowed paths must be a non-empty exact tuple",
    ):
        _plan(graph)


def test_plan_rejects_mutable_acceptance_command_alias() -> None:
    graph = _graph()
    components = tuple(
        replace(component, test_commands=(["pytest", "tests/core"],))
        if component.component_id == "core"
        else component
        for component in graph.components
    )
    graph = replace(graph, components=components)

    with pytest.raises(
        CoordinatorError,
        match="acceptance commands must be an exact tuple of non-empty argv tuples",
    ):
        _plan(graph)


def _forge_request_authority(
    request: ComponentWorkRequest,
    field_name: str,
    value: object,
) -> ComponentWorkRequest:
    forged = object.__new__(ComponentWorkRequest)
    for request_field in (
        "work_id",
        "project_id",
        "component_id",
        "repository_id",
        "goal",
        "base_sha",
        "allowed_paths",
        "permission_ceiling",
        "acceptance_commands",
        "attempt",
    ):
        object.__setattr__(
            forged,
            request_field,
            value if request_field == field_name else getattr(request, request_field),
        )
    return forged


def _mutable_authority_value(request: ComponentWorkRequest, field_name: str) -> object:
    if field_name == "allowed_paths":
        return list(request.allowed_paths)
    if field_name == "permission_ceiling":
        return set(request.permission_ceiling)
    if field_name == "acceptance_commands":
        return (["pytest", "tests/core"],)
    raise AssertionError(f"unknown request authority field: {field_name}")


@pytest.mark.parametrize(
    ("field_name", "error_pattern"),
    (
        ("allowed_paths", "allowed paths must be a non-empty exact tuple"),
        ("permission_ceiling", "permission ceiling must be a non-empty exact frozenset"),
        (
            "acceptance_commands",
            "acceptance commands must be an exact tuple of non-empty argv tuples",
        ),
    ),
)
def test_restore_rejects_mutable_authority_in_trusted_plan(
    field_name: str,
    error_pattern: str,
) -> None:
    coordinator = _coordinator()
    snapshot = coordinator.snapshot()
    assert snapshot.trusted_plan is not None
    tampered_plan = tuple(
        _forge_request_authority(
            request,
            field_name,
            _mutable_authority_value(request, field_name),
        )
        if request.component_id == "core"
        else request
        for request in snapshot.trusted_plan
    )
    tampered = replace(snapshot, trusted_plan=tampered_plan)

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(CoordinatorError, match=error_pattern):
        restored.restore(
            tampered,
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )


@pytest.mark.parametrize(
    ("field_name", "error_pattern"),
    (
        ("allowed_paths", "allowed paths must be a non-empty exact tuple"),
        ("permission_ceiling", "permission ceiling must be a non-empty exact frozenset"),
        (
            "acceptance_commands",
            "acceptance commands must be an exact tuple of non-empty argv tuples",
        ),
    ),
)
def test_restore_rejects_mutable_authority_in_work_record(
    field_name: str,
    error_pattern: str,
) -> None:
    coordinator = _coordinator()
    snapshot = coordinator.snapshot()
    records = tuple(
        replace(
            record,
            request=_forge_request_authority(
                record.request,
                field_name,
                _mutable_authority_value(record.request, field_name),
            ),
        )
        if record.request.component_id == "core"
        else record
        for record in snapshot.records
    )
    tampered = replace(snapshot, records=records)

    restored = ProductFactoryCoordinator(_graph())
    with pytest.raises(CoordinatorError, match=error_pattern):
        restored.restore(
            tampered,
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )
