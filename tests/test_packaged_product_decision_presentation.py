from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from _product_decision_test_support import ApprovedProductProjectCommandService
from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.command_center import ProductCommandCenter
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductJourneyError,
    PackagedProductSelectionStore,
    PackagedProductStateProvider,
    product_project_identity,
)
from nika_core.product_project import (
    EvidenceRef,
    ProductDecision,
    ProductDecisionState,
    ProductOption,
    ProductProjectRepository,
    ProductProjectSpec,
    ResearchEvidencePackage,
)
from nika_core.ui.bridge_models import UIResult
from scripts import nika_windows

_PROJECT_ID = "product-decision-presentation"


def _ordinary_handler(_payload: Mapping[str, Any]) -> UIResult:
    raise AssertionError("ProductDecision presentation must not create an ordinary task")


def _build(
    database: Path,
) -> tuple[
    ApprovedProductProjectCommandService,
    ProductProjectRepository,
    PackagedProductCommandRouter,
    PackagedProductStateProvider,
]:
    store = SQLiteStore(database)
    store.initialize()
    repository = ProductProjectRepository(store)
    service = ApprovedProductProjectCommandService(repository)
    try:
        repository.get(_PROJECT_ID)
    except KeyError:
        service.create_project(
            project_id=_PROJECT_ID,
            name="Accessible product decision",
            spec=ProductProjectSpec(
                goal="Build an accessible expense application",
                desired_outcome="A verified accessible Windows application",
                risk={"level": "R3"},
            ),
            idempotency_key="create:product-decision-presentation",
        )
    selection = PackagedProductSelectionStore(store)
    selection.select(_PROJECT_ID)
    router = PackagedProductCommandRouter(
        products=service,
        ordinary_handler=_ordinary_handler,
        selection_store=selection,
    )
    provider = PackagedProductStateProvider(
        base_state=lambda: {},
        router=router,
        command_center=ProductCommandCenter(service),
    )
    return service, repository, router, provider


def _add_pending(
    service: ApprovedProductProjectCommandService,
    repository: ProductProjectRepository,
    *,
    package_id: str,
    option_id: str,
    decision_id: str,
    expected_row_version: int,
) -> None:
    repository.record_research_handoff(
        _PROJECT_ID,
        ResearchEvidencePackage(
            package_id,
            (
                EvidenceRef(
                    f"evidence:{package_id}",
                    f"research://{package_id}/claim/1",
                    "Evidence-backed product choice",
                ),
            ),
        ),
        (
            ProductOption(
                option_id,
                option_id,
                "Evidence-backed option",
                (package_id,),
            ),
        ),
    )
    service.record_decision(
        _PROJECT_ID,
        ProductDecision(
            decision_id=decision_id,
            option_id=option_id,
            state=ProductDecisionState.PROPOSED,
            rationale="Needs owner confirmation",
            decided_by_ref="user://owner",
        ),
        expected_row_version=expected_row_version,
        idempotency_key=f"decision:proposed:{decision_id}",
    )


def test_pending_product_decision_is_bounded_and_visible_without_authority(
    tmp_path: Path,
) -> None:
    service, repository, _router, provider = _build(tmp_path / "pending decision.db")
    _add_pending(
        service,
        repository,
        package_id="research-1",
        option_id="option-1",
        decision_id="decision-1",
        expected_row_version=0,
    )

    project = provider()["product_project"]
    assert project is not None
    assert project["current_decision"] == {
        "decision_id": "decision-1",
        "title": "Product decision: option-1",
        "question": "Option option-1. Rationale: Needs owner confirmation",
        "risk_level": 3,
        "state": "pending",
    }
    serialized = json.dumps(project, ensure_ascii=False, sort_keys=True)
    for forbidden in (
        "research://",
        "authorization_ref",
        "approval",
        "credential",
        "decided_by_ref",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "command",
    [
        "Current product decision",
        "Show current product decision",
        "Поточне рішення ProductProject",
        "Покажи поточне рішення ProductProject",
    ],
)
def test_current_product_decision_command_is_read_only_and_restart_safe(
    tmp_path: Path,
    command: str,
) -> None:
    database = tmp_path / "decision restart.db"
    service, repository, _router, _provider = _build(database)
    _add_pending(
        service,
        repository,
        package_id="research-1",
        option_id="option-1",
        decision_id="decision-1",
        expected_row_version=0,
    )
    before = repository.get(_PROJECT_ID)

    _restarted_service, restarted_repository, restarted, _state = _build(database)
    result = restarted.create({"command": command})

    assert result.status == "completed"
    assert result.focus_id == "product-project-decision-heading"
    assert "decision-1" in result.message
    assert "ризик R3" in result.message
    assert "Needs owner confirmation" in result.message
    assert restarted_repository.get(_PROJECT_ID) == before


def test_multiple_pending_decisions_are_not_auto_selected_in_packaged_ui(
    tmp_path: Path,
) -> None:
    service, repository, router, provider = _build(tmp_path / "ambiguous decisions.db")
    _add_pending(
        service,
        repository,
        package_id="research-a",
        option_id="option-a",
        decision_id="decision-a",
        expected_row_version=0,
    )
    _add_pending(
        service,
        repository,
        package_id="research-b",
        option_id="option-b",
        decision_id="decision-b",
        expected_row_version=1,
    )

    project = provider()["product_project"]
    assert project is not None
    assert project["decision_count"] == 2
    assert project["decision_state_counts"] == {"pending": 2}
    assert project["current_decision"] is None

    with pytest.raises(PackagedProductJourneyError, match="Кілька рішень"):
        router.create({"command": "Show current product decision"})


def test_real_windows_bridge_restores_pending_decision_and_focus_after_restart(
    tmp_path: Path,
) -> None:
    database = (tmp_path / "Windows рішення користувача.db").resolve()
    config = AppConfig(database_path=database)
    command = "Створи застосунок для доступного обліку витрат"
    project_id = product_project_identity(command)

    bridge, products = nika_windows.build_windows_bridge(
        config,
        start_startup_recovery=False,
    )
    created = bridge.dispatch(
        {
            "request_id": "decision-product-create",
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )
    assert created["status"] == "completed"

    store = SQLiteStore(database)
    store.initialize()
    repository = ProductProjectRepository(store)
    repository.record_research_handoff(
        project_id,
        ResearchEvidencePackage(
            "research-windows-decision",
            (
                EvidenceRef(
                    "evidence:windows-decision",
                    "research://windows-decision/claim/1",
                    "Packaged Windows product choice",
                ),
            ),
        ),
        (
            ProductOption(
                "option-windows",
                "option-windows",
                "Use the accessible Windows path",
                ("research-windows-decision",),
            ),
        ),
    )
    products.record_decision(
        project_id,
        ProductDecision(
            decision_id="decision-windows",
            option_id="option-windows",
            state=ProductDecisionState.PROPOSED,
            rationale="Owner must choose before continuation",
            decided_by_ref="user://owner",
        ),
        expected_row_version=0,
        idempotency_key="decision:windows:pending",
    )

    first_state = bridge.get_state()["state"]["product_project"]
    assert first_state["current_decision"]["decision_id"] == "decision-windows"
    assert first_state["current_decision"]["risk_level"] == 0

    restarted, restarted_products = nika_windows.build_windows_bridge(
        config,
        start_startup_recovery=False,
    )
    recovered = restarted.get_state()["state"]["product_project"]
    assert recovered["project_id"] == project_id
    assert recovered["current_decision"] == first_state["current_decision"]
    assert restarted_products.inspect_project(project_id).summary.current_decision is not None

    result = restarted.dispatch(
        {
            "request_id": "decision-product-read",
            "action_id": "task.create",
            "payload": {"command": "Покажи поточне рішення ProductProject"},
        }
    )
    assert result["status"] == "completed"
    assert result["focus_id"] == "product-project-decision-heading"
    assert "decision-windows" in result["message"]
    assert "Owner must choose before continuation" in result["message"]
