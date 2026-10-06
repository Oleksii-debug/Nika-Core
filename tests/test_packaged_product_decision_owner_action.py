from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_decisions import ProductDecisionRepository
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductJourneyError,
    PackagedProductSelectionStore,
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
from nika_core.security import ApprovalAuthority
from nika_core.ui.bridge_models import UIResult
from scripts import nika_windows

_PROJECT_ID = "product-decision-owner-action"
_REQUEST_RE = re.compile(r"approval-request-[A-Za-z0-9_-]+")


def _ordinary_handler(_payload: Mapping[str, Any]) -> UIResult:
    raise AssertionError("owner decision command must not create an ordinary task")


def _build(
    database: Path,
) -> tuple[
    SQLiteStore,
    ProductProjectRepository,
    ProductProjectCommandService,
    PackagedProductCommandRouter,
]:
    store = SQLiteStore(database)
    store.initialize()
    repository = ProductProjectRepository(store)
    authority = ApprovalAuthority()
    service = ProductProjectCommandService(
        repository,
        approval_verifier=authority.verifier(),
    )
    service.create_project(
        project_id=_PROJECT_ID,
        name="Packaged owner decision",
        spec=ProductProjectSpec(
            goal="Resolve an owner decision without caller-forged authority",
            desired_outcome="One exact owner action is committed durably",
        ),
        idempotency_key="create:packaged-owner-decision",
    )
    repository.record_research_handoff(
        _PROJECT_ID,
        ResearchEvidencePackage(
            "research-owner-decision",
            (
                EvidenceRef(
                    "evidence-owner-decision",
                    "research://owner-decision/claim/1",
                    "Evidence-backed option for packaged owner action",
                ),
            ),
        ),
        (
            ProductOption(
                "option-owner",
                "Owner option",
                "Canonical evidence-backed option",
                ("research-owner-decision",),
            ),
        ),
    )
    service.record_decision(
        _PROJECT_ID,
        ProductDecision(
            decision_id="decision-owner",
            option_id="option-owner",
            state=ProductDecisionState.PROPOSED,
            rationale="Owner must explicitly choose",
            decided_by_ref="user://owner",
        ),
        expected_row_version=0,
        idempotency_key="decision:owner:proposed",
    )
    selection = PackagedProductSelectionStore(store)
    selection.select(_PROJECT_ID)
    router = PackagedProductCommandRouter(
        products=service,
        ordinary_handler=_ordinary_handler,
        selection_store=selection,
        decision_approval_authority=authority,
    )
    return store, repository, service, router


def _approval_request_id(message: str) -> str:
    match = _REQUEST_RE.search(message)
    assert match is not None
    return match.group(0)


@pytest.mark.parametrize(
    "decision_id",
    [
        " decision-leading-space",
        "decision-control\nline",
        "decision-zero-width\u200bformat",
        "d" * 161,
    ],
)
def test_pf1_rejects_unpresentable_decision_identity_before_mutation(
    tmp_path: Path,
    decision_id: str,
) -> None:
    store, repository, _service, _router = _build(tmp_path / "unsafe-id.db")
    decisions = ProductDecisionRepository(store)

    with pytest.raises(ValueError, match="safe presentation identity"):
        decisions.record(
            _PROJECT_ID,
            ProductDecision(
                decision_id=decision_id,
                option_id="option-owner",
                state=ProductDecisionState.PROPOSED,
                rationale="Must fail before a durable write",
                decided_by_ref="user://owner",
            ),
            expected_row_version=repository.get(_PROJECT_ID).row_version,
            idempotency_key="decision:unsafe-id",
        )

    assert repository.get(_PROJECT_ID).row_version == 1
    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM product_decisions WHERE project_id=?",
            (_PROJECT_ID,),
        ).fetchone()["count"]
    assert count == 1


def test_packaged_reject_write_does_not_materialize_decision_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _repository, _service, router = _build(tmp_path / "bounded reject.db")

    def fail_unbounded_list(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("packaged decision write must not materialize decision list()")

    monkeypatch.setattr(ProductDecisionRepository, "list", fail_unbounded_list)

    result = router.create({"command": "reject product decision decision-owner"})

    assert result.status == "completed"
    stored = ProductDecisionRepository(store).get(_PROJECT_ID, "decision-owner")
    assert stored.decision.state is ProductDecisionState.REJECTED


def test_pf1_corrupt_unpresentable_persisted_decision_id_fails_closed(
    tmp_path: Path,
) -> None:
    store, repository, service, _router = _build(tmp_path / "corrupt-id.db")
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_decisions SET decision_id=? "
            "WHERE project_id=? AND decision_id=?",
            ("decision-corrupt\ncontrol", _PROJECT_ID, "decision-owner"),
        )

    with pytest.raises(ValueError, match="safe presentation identity"):
        service.inspect_project(_PROJECT_ID)

    assert repository.get(_PROJECT_ID).row_version == 1


def test_missing_decision_equal_to_project_id_does_not_clear_valid_selection(
    tmp_path: Path,
) -> None:
    _store, repository, _service, router = _build(tmp_path / "id-collision.db")

    with pytest.raises(PackagedProductJourneyError, match="не знайдено"):
        router.create({"command": f"show product decision {_PROJECT_ID}"})

    assert router.active_project_id == _PROJECT_ID
    assert repository.get(_PROJECT_ID).project_id == _PROJECT_ID


def test_multiple_pending_decisions_are_discoverable_by_bounded_pages_and_exact_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, repository, service, router = _build(tmp_path / "pending-pages.db")
    for index in range(9):
        package_id = f"research-page-{index:02d}"
        option_id = f"option-page-{index:02d}"
        decision_id = f"decision-{index:02d}"
        repository.record_research_handoff(
            _PROJECT_ID,
            ResearchEvidencePackage(
                package_id,
                (
                    EvidenceRef(
                        f"evidence-page-{index:02d}",
                        f"research://pending-page/{index:02d}",
                        f"Evidence for pending decision {index:02d}",
                    ),
                ),
            ),
            (
                ProductOption(
                    option_id,
                    f"Option {index:02d}",
                    f"Candidate option {index:02d}",
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
                rationale=f"Question for owner {index:02d}",
                decided_by_ref="user://owner",
            ),
            expected_row_version=repository.get(_PROJECT_ID).row_version,
            idempotency_key=f"decision:page:{index:02d}",
        )

    def fail_unbounded_list(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("packaged decision reads must not materialize list()")

    monkeypatch.setattr(ProductDecisionRepository, "list", fail_unbounded_list)

    before = repository.get(_PROJECT_ID)
    first = router.create({"command": "list pending product decisions"})
    second = router.create(
        {"command": "list pending product decisions page 2"}
    )
    exact = router.create(
        {"command": "show product decision decision-00"}
    )

    assert "1-8 із 10" in first.message
    for index in range(8):
        assert f"decision-{index:02d}" in first.message
    assert "decision-08" not in first.message
    assert "decision-owner" not in first.message
    assert "page 2" in first.message

    assert "9-10 із 10" in second.message
    assert "decision-08" in second.message
    assert "decision-owner" in second.message
    assert "decision-00" not in second.message

    assert exact.status == "completed"
    assert exact.focus_id == "product-project-heading"
    assert "decision-00" in exact.message
    assert "Question for owner 00" in exact.message
    assert "стан pending" in exact.message
    assert repository.get(_PROJECT_ID) == before


def test_pending_page_rejects_corrupt_current_decision_beyond_visible_window(
    tmp_path: Path,
) -> None:
    store, repository, _service, router = _build(tmp_path / "hidden-corrupt.db")
    with store.connection() as conn:
        for index in range(8):
            conn.execute(
                "INSERT INTO product_decisions("
                "project_id,decision_id,decision_version,option_id,state,rationale,"
                "decided_by_ref,evidence_package_ids_json,created_at) "
                "SELECT project_id,?,1,option_id,state,rationale,decided_by_ref,"
                "evidence_package_ids_json,created_at FROM product_decisions "
                "WHERE project_id=? AND decision_id=? AND decision_version=1",
                (f"decision-{index:02d}", _PROJECT_ID, "decision-owner"),
            )
        conn.execute(
            "UPDATE product_decisions SET state=? "
            "WHERE project_id=? AND decision_id=?",
            ("corrupt-hidden-state", _PROJECT_ID, "decision-owner"),
        )

    with pytest.raises(ValueError, match="persisted product decision state is invalid"):
        router.create({"command": "list pending product decisions"})

    assert repository.get(_PROJECT_ID).row_version == 1


def test_approval_is_two_step_and_replay_does_not_mint_second_effect(
    tmp_path: Path,
) -> None:
    store, repository, service, router = _build(tmp_path / "approve.db")
    assert repository.get(_PROJECT_ID).row_version == 1

    requested = router.create(
        {"command": "approve product decision decision-owner"}
    )
    request_id = _approval_request_id(requested.message)

    assert requested.status == "completed"
    assert requested.focus_id == "command-input"
    assert service.decision_history(_PROJECT_ID, "decision-owner")[-1].state == "pending"
    assert repository.get(_PROJECT_ID).row_version == 1

    confirmed = router.create(
        {"command": f"confirm product decision approval {request_id}"}
    )

    assert confirmed.status == "completed"
    assert confirmed.focus_id == "product-project-heading"
    assert service.decision_history(_PROJECT_ID, "decision-owner")[-1].state == "approved"
    assert repository.get(_PROJECT_ID).row_version == 2
    stored = ProductDecisionRepository(store).get(_PROJECT_ID, "decision-owner")
    assert stored.decision.decided_by_ref.startswith("approval://")

    replay = router.create(
        {"command": "approve product decision decision-owner"}
    )
    assert "уже схвалено" in replay.message
    assert "approval-request-" not in replay.message
    assert repository.get(_PROJECT_ID).row_version == 2
    assert len(service.decision_history(_PROJECT_ID, "decision-owner")) == 2


def test_repeated_approval_request_reuses_same_live_host_request(
    tmp_path: Path,
) -> None:
    _store, repository, service, router = _build(tmp_path / "repeat-request.db")

    first = router.create(
        {"command": "approve product decision decision-owner"}
    )
    second = router.create(
        {"command": "схвали рішення ProductProject decision-owner"}
    )

    assert _approval_request_id(first.message) == _approval_request_id(second.message)
    assert repository.get(_PROJECT_ID).row_version == 1
    assert service.decision_history(_PROJECT_ID, "decision-owner")[-1].state == "pending"


def test_changing_active_product_project_cancels_old_approval_request(
    tmp_path: Path,
) -> None:
    _store, repository, service, router = _build(tmp_path / "selection-change.db")
    requested = router.create(
        {"command": "approve product decision decision-owner"}
    )
    request_id = _approval_request_id(requested.message)

    switched = router.create(
        {"command": "Create product application for a different owner decision"}
    )
    assert switched.status == "completed"
    assert router.active_project_id != _PROJECT_ID

    with pytest.raises(PackagedProductJourneyError, match="змінився"):
        router.create(
            {"command": f"confirm product decision approval {request_id}"}
        )
    with pytest.raises(PackagedProductJourneyError, match="невідомий|прострочений"):
        router.create(
            {"command": f"confirm product decision approval {request_id}"}
        )

    assert repository.get(_PROJECT_ID).row_version == 1
    assert service.decision_history(_PROJECT_ID, "decision-owner")[-1].state == "pending"


def test_changed_research_evidence_between_request_and_confirm_fails_closed(
    tmp_path: Path,
) -> None:
    store, repository, service, router = _build(tmp_path / "evidence-change.db")
    requested = router.create(
        {"command": "approve product decision decision-owner"}
    )
    request_id = _approval_request_id(requested.message)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM product_research_handoffs "
            "WHERE project_id=? AND package_id=?",
            (_PROJECT_ID, "research-owner-decision"),
        ).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE product_research_handoffs SET payload_json=? "
            "WHERE project_id=? AND package_id=?",
            (
                row["payload_json"] + " ",
                _PROJECT_ID,
                "research-owner-decision",
            ),
        )

    with pytest.raises(PackagedProductJourneyError, match="змінилися після запиту"):
        router.create(
            {"command": f"confirm product decision approval {request_id}"}
        )

    assert repository.get(_PROJECT_ID).row_version == 1
    assert service.decision_history(_PROJECT_ID, "decision-owner")[-1].state == "pending"


def test_reject_is_exact_id_durable_and_idempotent(tmp_path: Path) -> None:
    _store, repository, service, router = _build(tmp_path / "reject.db")

    rejected = router.create(
        {"command": "reject product decision decision-owner"}
    )
    replay = router.create(
        {"command": "відхили рішення ProductProject decision-owner"}
    )

    assert rejected.status == "completed"
    assert replay.status == "completed"
    assert service.decision_history(_PROJECT_ID, "decision-owner")[-1].state == "rejected"
    assert len(service.decision_history(_PROJECT_ID, "decision-owner")) == 2
    assert repository.get(_PROJECT_ID).row_version == 2


def test_unknown_or_forged_confirmation_cannot_create_authority(
    tmp_path: Path,
) -> None:
    _store, repository, service, router = _build(tmp_path / "forged.db")

    with pytest.raises(PackagedProductJourneyError, match="невідомий|прострочений"):
        router.create(
            {
                "command": (
                    "confirm product decision approval "
                    "approval-request-caller-forged"
                )
            }
        )

    assert repository.get(_PROJECT_ID).row_version == 1
    assert service.decision_history(_PROJECT_ID, "decision-owner")[-1].state == "pending"


def test_restart_invalidates_ephemeral_request_and_fresh_request_recovers(
    tmp_path: Path,
) -> None:
    store, repository, _service, router = _build(tmp_path / "restart-request.db")
    first = router.create(
        {"command": "approve product decision decision-owner"}
    )
    stale_request = _approval_request_id(first.message)

    restarted_authority = ApprovalAuthority()
    restarted_service = ProductProjectCommandService(
        repository,
        approval_verifier=restarted_authority.verifier(),
    )
    restarted = PackagedProductCommandRouter(
        products=restarted_service,
        ordinary_handler=_ordinary_handler,
        selection_store=PackagedProductSelectionStore(store),
        decision_approval_authority=restarted_authority,
    )

    with pytest.raises(PackagedProductJourneyError, match="іншому запуску|невідомий"):
        restarted.create(
            {"command": f"confirm product decision approval {stale_request}"}
        )

    fresh = restarted.create(
        {"command": "approve product decision decision-owner"}
    )
    fresh_request = _approval_request_id(fresh.message)
    assert fresh_request != stale_request
    restarted.create(
        {"command": f"confirm product decision approval {fresh_request}"}
    )

    assert restarted_service.decision_history(
        _PROJECT_ID, "decision-owner"
    )[-1].state == "approved"
    assert repository.get(_PROJECT_ID).row_version == 2


def test_stale_project_between_request_and_confirmation_fails_closed_then_recovers(
    tmp_path: Path,
) -> None:
    _store, repository, service, router = _build(tmp_path / "stale.db")

    requested = router.create(
        {"command": "approve product decision decision-owner"}
    )
    stale_request = _approval_request_id(requested.message)

    service.update_project(
        _PROJECT_ID,
        expected_spec_version=1,
        goal="Changed after approval request",
    )
    assert repository.get(_PROJECT_ID).row_version == 2

    with pytest.raises(PackagedProductJourneyError, match="змінилися після запиту"):
        router.create(
            {"command": f"confirm product decision approval {stale_request}"}
        )

    assert service.decision_history(_PROJECT_ID, "decision-owner")[-1].state == "pending"
    fresh = router.create(
        {"command": "схвали рішення ProductProject decision-owner"}
    )
    fresh_request = _approval_request_id(fresh.message)
    assert fresh_request != stale_request

    router.create(
        {
            "command": (
                "підтвердь схвалення рішення ProductProject "
                f"{fresh_request}"
            )
        }
    )
    assert service.decision_history(_PROJECT_ID, "decision-owner")[-1].state == "approved"
    assert repository.get(_PROJECT_ID).row_version == 3


@pytest.mark.parametrize(
    "command, expected",
    [
        ("approve product decision", "decision_id"),
        ("reject product decision", "decision_id"),
        ("show product decision", "decision_id"),
        ("confirm product decision approval", "approval request_id"),
        (
            "confirm product decision approval forged",
            "approval request_id",
        ),
        (
            "list pending product decisions page 0",
            "додатним цілим",
        ),
        (
            "list pending product decisions page 9999999999999999999",
            "SQLite offset",
        ),
        (
            "list pending product decisions page many",
            "додатним цілим",
        ),
    ],
)
def test_owner_decision_commands_fail_closed_on_missing_or_malformed_identity(
    tmp_path: Path,
    command: str,
    expected: str,
) -> None:
    _store, repository, service, router = _build(
        tmp_path / f"malformed-{abs(hash(command))}.db"
    )

    with pytest.raises(PackagedProductJourneyError, match=expected):
        router.create({"command": command})

    assert repository.get(_PROJECT_ID).row_version == 1
    assert service.decision_history(_PROJECT_ID, "decision-owner")[-1].state == "pending"


def test_packaged_help_exposes_keyboard_two_step_owner_flow() -> None:
    html = Path("src/nika_core/ui/web/index.html").read_text(encoding="utf-8")

    assert "list pending product decisions" in html
    assert "list pending product decisions page &lt;номер&gt;" in html
    assert "show product decision &lt;decision_id&gt;" in html
    assert "approve product decision &lt;decision_id&gt;" in html
    assert "reject product decision &lt;decision_id&gt;" in html
    assert (
        "confirm product decision approval &lt;approval-request-id&gt;"
        in html
    )
    assert "схвали рішення ProductProject &lt;decision_id&gt;" in html
    assert "відхили рішення ProductProject &lt;decision_id&gt;" in html


def test_real_windows_bridge_commits_owner_approval_without_creating_task(
    tmp_path: Path,
) -> None:
    database = (tmp_path / "Windows owner approval.db").resolve()
    config = AppConfig(database_path=database)
    command = "Створи застосунок для перевірки рішення власника"
    project_id = product_project_identity(command)

    bridge, products = nika_windows.build_windows_bridge(
        config,
        start_startup_recovery=False,
    )
    created = bridge.dispatch(
        {
            "request_id": "owner-product-create",
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
            "research-owner-windows",
            (
                EvidenceRef(
                    "evidence-owner-windows",
                    "research://owner-windows/claim/1",
                    "Windows packaged owner decision evidence",
                ),
            ),
        ),
        (
            ProductOption(
                "option-owner-windows",
                "Windows owner option",
                "Owner-approved Windows option",
                ("research-owner-windows",),
            ),
        ),
    )
    products.record_decision(
        project_id,
        ProductDecision(
            decision_id="decision-owner-windows",
            option_id="option-owner-windows",
            state=ProductDecisionState.PROPOSED,
            rationale="Windows owner confirmation required",
            decided_by_ref="user://owner",
        ),
        expected_row_version=0,
        idempotency_key="decision:owner-windows:proposed",
    )

    requested = bridge.dispatch(
        {
            "request_id": "owner-approval-request",
            "action_id": "task.create",
            "payload": {
                "command": "approve product decision decision-owner-windows"
            },
        }
    )
    approval_request = _approval_request_id(requested["message"])
    assert products.decision_history(
        project_id, "decision-owner-windows"
    )[-1].state == "pending"

    confirmed = bridge.dispatch(
        {
            "request_id": "owner-approval-confirm",
            "action_id": "task.create",
            "payload": {
                "command": (
                    "confirm product decision approval "
                    f"{approval_request}"
                )
            },
        }
    )

    assert confirmed["status"] == "completed"
    assert products.decision_history(
        project_id, "decision-owner-windows"
    )[-1].state == "approved"
    state = bridge.get_state()["state"]
    assert state["product_project"]["current_decision"] is None
    assert state["product_project"]["decision_state_counts"] == {"approved": 1}
    assert state["tasks"] == []
