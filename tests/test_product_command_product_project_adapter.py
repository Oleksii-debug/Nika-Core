from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.contracts import ProductStatusKind
from nika_core.product_command.product_project_adapter import (
    ProductProjectCommandService,
    ProductProjectDecisionUnavailableError,
)
from nika_core.product_command.routing import route_command
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
    StaleProjectVersionError,
)


def _service(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return ProductProjectCommandService(ProductProjectRepository(store)), store


def _spec(goal: str = "Build accessible expense app") -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=goal,
        desired_outcome="A tested accessible Windows application",
        requirements=(
            ProductRequirement(
                "req-keyboard",
                "Keyboard operation",
                ("All primary actions are keyboard reachable",),
                ("research-expense-1",),
            ),
        ),
        repository_refs=("repo://expense/core",),
        team_refs=("role://accessibility",),
        credential_refs=("credential://github/project-writer",),
    )


def test_product_route_can_create_and_inspect_real_durable_pf1_project(tmp_path) -> None:
    service, _store = _service(tmp_path)
    routed = route_command("Створи застосунок для керування особистими витратами")

    created = service.create_project(
        project_id="expense-app",
        name="Accessible Expense",
        spec=_spec(routed.normalized_goal or ""),
        idempotency_key="command:create:expense-app",
    )
    inspected = service.inspect_project("expense-app")

    assert created == inspected
    assert created.summary.project_id == "expense-app"
    assert created.summary.version == 1
    assert created.summary.goal == routed.normalized_goal
    kinds = {item.kind for item in created.statuses}
    assert ProductStatusKind.REQUIREMENT in kinds
    assert ProductStatusKind.REPOSITORY in kinds
    assert ProductStatusKind.TEAM_ROLE in kinds


def test_product_command_adapter_survives_sqlite_restart(tmp_path) -> None:
    db = tmp_path / "nika.db"
    store = SQLiteStore(db)
    store.initialize()
    first = ProductProjectCommandService(ProductProjectRepository(store))
    first.create_project(
        project_id="p1",
        name="Expense",
        spec=_spec(),
        idempotency_key="create:p1",
    )

    restarted_store = SQLiteStore(db)
    restarted_store.initialize()
    restarted = ProductProjectCommandService(ProductProjectRepository(restarted_store))

    detail = restarted.inspect_project("p1")

    assert detail.summary.version == 1
    assert detail.summary.goal == "Build accessible expense app"
    assert detail.logs and "Durable ProductProject spec version 1" in detail.logs[0]


def test_update_uses_visible_spec_version_and_preserves_unspecified_fields(tmp_path) -> None:
    service, _store = _service(tmp_path)
    created = service.create_project(
        project_id="p1",
        name="Expense",
        spec=_spec(),
        idempotency_key="create:p1",
    )

    updated = service.update_project(
        "p1",
        expected_spec_version=created.summary.version,
        desired_outcome="A packaged accessible Windows application",
    )

    assert updated.summary.version == 2
    assert updated.summary.goal == created.summary.goal
    requirement = next(
        item for item in updated.statuses if item.kind is ProductStatusKind.REQUIREMENT
    )
    assert requirement.item_id == "req-keyboard"


def test_stale_visible_spec_version_fails_closed(tmp_path) -> None:
    service, _store = _service(tmp_path)
    service.create_project(
        project_id="p1",
        name="Expense",
        spec=_spec(),
        idempotency_key="create:p1",
    )
    service.update_project("p1", expected_spec_version=1, goal="v2")

    with pytest.raises(StaleProjectVersionError, match="stale ProductProject spec"):
        service.update_project("p1", expected_spec_version=1, goal="stale")


def test_full_spec_replacement_cannot_be_mixed_with_partial_update(tmp_path) -> None:
    service, _store = _service(tmp_path)
    service.create_project(
        project_id="p1",
        name="Expense",
        spec=_spec(),
        idempotency_key="create:p1",
    )

    with pytest.raises(ValueError, match="cannot be combined"):
        service.update_project(
            "p1",
            expected_spec_version=1,
            spec=_spec("replacement"),
            goal="partial",
        )


def test_presentation_never_exposes_credential_refs(tmp_path) -> None:
    service, _store = _service(tmp_path)
    detail = service.create_project(
        project_id="p1",
        name="Expense",
        spec=_spec(),
        idempotency_key="create:p1",
    )

    serialized = detail.model_dump_json()

    assert "credential://github/project-writer" not in serialized
    assert "research-expense-1" in serialized


def test_decision_write_fails_closed_without_integrated_pf1_decision_api(tmp_path) -> None:
    service, _store = _service(tmp_path)
    service.create_project(
        project_id="p1",
        name="Expense",
        spec=_spec(),
        idempotency_key="create:p1",
    )

    with pytest.raises(ProductProjectDecisionUnavailableError, match="durable decision-write API"):
        service.persist_decision("p1", "decision-1")
