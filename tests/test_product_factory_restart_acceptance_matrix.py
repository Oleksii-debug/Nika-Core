from __future__ import annotations

import json
from itertools import product
from pathlib import Path


MATRIX_PATH = (
    Path(__file__).resolve().parents[1]
    / "qa"
    / "product_factory_restart_acceptance_matrix.json"
)

EXPECTED_BOUNDARIES = {
    "product_project_spec_write",
    "research_handoff",
    "team_creation_replacement",
    "repository_lease",
    "checkpoint",
    "worker_dispatch_result",
    "build_dispatch",
    "credential_handle",
    "deployment",
    "health",
    "incident_repair",
    "business_factory_handoff",
    "delivery_state",
    "backup_update",
}
EXPECTED_FAULT_MODES = {
    "effect_before_state",
    "state_before_effect",
    "lost_acknowledgement",
    "duplicate_retry",
    "concurrent_writer",
    "stale_checkpoint",
    "corrupt_type_or_version",
    "two_process_like_recoverers",
}
EXPECTED_INVARIANTS = {
    "NO_DUPLICATE_EFFECT",
    "NO_LOST_STATE",
    "NO_STALE_AUTHORITY",
    "NO_CROSS_PROJECT_DATA",
    "ONE_CANONICAL_OWNER",
}
ALLOWED_BASELINE_DISPOSITIONS = {
    "BLOCKED_ON_OPEN_OWNER",
    "KNOWN_GAP",
    "EXACT_HEAD_PROVEN",
}


def _load_matrix() -> dict[str, object]:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def test_restart_matrix_is_qa_only_and_production_read_only() -> None:
    matrix = _load_matrix()

    assert matrix["qa_only"] is True
    assert matrix["production_source_edits_forbidden"] is True
    assert matrix["schema_version"] == 1


def test_restart_matrix_covers_exact_14_by_8_cartesian_product() -> None:
    matrix = _load_matrix()
    boundaries = matrix["boundaries"]
    fault_modes = matrix["fault_modes"]

    boundary_ids = {boundary["id"] for boundary in boundaries}
    fault_mode_ids = {fault_mode["id"] for fault_mode in fault_modes}
    cells = {
        f"{boundary_id}::{fault_mode_id}"
        for boundary_id, fault_mode_id in product(boundary_ids, fault_mode_ids)
    }

    assert boundary_ids == EXPECTED_BOUNDARIES
    assert fault_mode_ids == EXPECTED_FAULT_MODES
    assert len(boundaries) == 14
    assert len(fault_modes) == 8
    assert len(cells) == 112


def test_every_fault_mode_requires_all_global_invariants() -> None:
    matrix = _load_matrix()
    global_invariants = set(matrix["global_invariants"])

    assert global_invariants == EXPECTED_INVARIANTS
    for fault_mode in matrix["fault_modes"]:
        assert fault_mode["inject"].strip()
        assert len(fault_mode["must_assert"]) >= 3
        assert set(fault_mode["required_invariants"]) == global_invariants


def test_every_boundary_has_one_canonical_owner_and_exact_route() -> None:
    matrix = _load_matrix()

    for boundary in matrix["boundaries"]:
        assert boundary["canonical_owner"].strip()
        assert boundary["owner_account"] == "Oleksii-debug"
        assert isinstance(boundary["primary_pr"], int)
        assert boundary["primary_pr"] > 0
        assert boundary["owner_lane"].strip()
        assert boundary["evidence_refs"]
        assert boundary["baseline_disposition"] in ALLOWED_BASELINE_DISPOSITIONS
        assert boundary["blocker"].strip()


def test_no_exact_head_credit_is_claimed_without_exact_green_evidence() -> None:
    matrix = _load_matrix()
    baseline = matrix["baseline_assessment"]

    assert baseline["starting_main_sha"] == "23c7c1ce97b263b4aafa61bdcbace207b4476a3d"
    assert baseline["exact_current_main_green"] is False
    assert baseline["human_tested"] is False
    assert baseline["nvda_verified"] is False

    for boundary in matrix["boundaries"]:
        assert boundary["baseline_disposition"] != "EXACT_HEAD_PROVEN"


def test_known_process_crash_gaps_remain_explicit() -> None:
    matrix = _load_matrix()
    boundaries = {boundary["id"]: boundary for boundary in matrix["boundaries"]}

    assert (
        boundaries["team_creation_replacement"]["blocker_code"]
        == "canonical_snapshot_persistence_not_integrated"
    )
    assert (
        boundaries["credential_handle"]["blocker_code"]
        == "cross_process_atomicity_unproven"
    )
    assert (
        boundaries["deployment"]["blocker_code"]
        == "pre_dispatch_durable_uncertainty_marker_missing"
    )
    assert boundaries["health"]["blocker_code"] == "pf8_pre_effect_checkpoint_missing"


def test_backup_update_keeps_sqlite_recovery_manager_as_single_owner() -> None:
    matrix = _load_matrix()
    boundaries = {boundary["id"]: boundary for boundary in matrix["boundaries"]}
    backup = boundaries["backup_update"]

    assert backup["canonical_owner"] == "SQLiteRecoveryManager"
    assert backup["primary_pr"] == 311
    assert backup["adapter_prs"] == [218]
