from __future__ import annotations

import json
from pathlib import Path


MATRIX_PATH = (
    Path(__file__).resolve().parents[1] / "state" / "W092_PF0_PF12_ACCEPTANCE_MATRIX.json"
)
EXPECTED_MAIN = "109829579ab4693e038e218769c23c2547defd64"
EXPECTED_IDS = tuple(f"PF{index}" for index in range(13))
ALLOWED_STATUSES = {"PROVEN_EXACT", "PARTIAL", "BLOCKED", "NOT_INTEGRATED"}


def _matrix() -> dict[str, object]:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def test_w092_matrix_is_exact_main_bound_and_complete() -> None:
    matrix = _matrix()
    assert matrix["starting_main_sha"] == EXPECTED_MAIN
    assert matrix["evaluated_main_sha"] == EXPECTED_MAIN

    pf_matrix = matrix["pf_matrix"]
    assert isinstance(pf_matrix, dict)
    assert tuple(pf_matrix) == EXPECTED_IDS
    assert set(pf_matrix.values()) <= ALLOWED_STATUSES

    gates = matrix["gates"]
    assert isinstance(gates, list)
    assert tuple(gate["id"] for gate in gates) == EXPECTED_IDS
    assert len({gate["id"] for gate in gates}) == 13
    assert all(gate["status"] == pf_matrix[gate["id"]] for gate in gates)


def test_w092_summary_matches_gate_classification() -> None:
    matrix = _matrix()
    pf_matrix = matrix["pf_matrix"]
    summary = matrix["summary"]

    assert isinstance(pf_matrix, dict)
    assert isinstance(summary, dict)
    for status in ALLOWED_STATUSES:
        assert summary[status] == sum(value == status for value in pf_matrix.values())
    assert summary["total_gates"] == 13
    assert sum(summary[status] for status in ALLOWED_STATUSES) == 13


def test_w092_does_not_transfer_candidate_or_backend_only_credit() -> None:
    matrix = _matrix()
    policy = matrix["policy"]
    wave_i = matrix["wave_i"]
    product_journey = matrix["product_journey"]

    assert policy["stale_pr_credit"] is False
    assert policy["candidate_pr_credit"] is False
    assert policy["backend_only_completion_credit_when_product_journey_missing"] is False
    assert product_journey["full_pf0_pf12_representative_product_journey_integrated"] is False
    assert wave_i["required"] is True
    assert wave_i["sealed"] is False
    assert matrix["summary"]["PROVEN_EXACT"] == 0
    assert matrix["finality"]["matrix_status"] == "BLOCKED"


def test_w092_human_and_nvda_truth_remains_fail_safe() -> None:
    matrix = _matrix()
    policy = matrix["policy"]

    assert policy["human_tested"] is False
    assert policy["nvda_verified"] is False
    assert policy["production_release_ready"] is False
    assert matrix["exact_main_evidence"]["main_branch_protected"] is False
