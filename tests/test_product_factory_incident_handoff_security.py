from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from nika_core.product_factory_incident_contracts import (
    ProductIncidentError,
    RepairCandidateEvidence,
    RepairWorkOrder,
)

OLD_SHA = "1" * 40
NEW_SHA = "2" * 40
ARTIFACT = "a" * 64
DIFF = "b" * 64
TEST_DIGEST = "e" * 64
NOW = datetime(2026, 8, 21, 13, 30, tzinfo=UTC)


def _work_order() -> RepairWorkOrder:
    return RepairWorkOrder(
        work_order_id="repair:incident-security",
        incident_id="incident-security",
        project_id="project-a",
        service_id="api",
        repository_id="repo-a",
        component_id="component-api",
        base_release_sha=OLD_SHA,
        goal="Repair only the bounded component.",
        allowed_paths=("src/api.py", "tests/test_api.py"),
        permission_ceiling=frozenset({"repo.read", "repo.write", "process.test"}),
        acceptance_commands=(("python", "-m", "pytest", "tests/test_api.py"),),
        evidence_refs=("health://api/degraded",),
        created_at=NOW,
    )


def test_repair_work_order_accepts_normalized_non_shell_handoff() -> None:
    order = _work_order()

    assert order.allowed_paths == ("src/api.py", "tests/test_api.py")
    assert order.acceptance_commands[0][0] == "python"


@pytest.mark.parametrize(
    "path",
    (
        ".git/config",
        "src/.git/config",
        "../outside.py",
        "/absolute.py",
        "C:\\outside.py",
        " src/api.py",
        "src/api.py ",
    ),
)
def test_repair_work_order_rejects_repository_escape_and_git_metadata(path: str) -> None:
    with pytest.raises(ProductIncidentError, match="project-relative non-.git"):
        replace(_work_order(), allowed_paths=(path,))


@pytest.mark.parametrize(
    "entrypoint",
    ("cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "bash", "sh"),
)
def test_repair_work_order_rejects_generic_shell_entrypoints(entrypoint: str) -> None:
    with pytest.raises(ProductIncidentError, match="non-shell Toolsmith entrypoint"):
        replace(
            _work_order(),
            acceptance_commands=((entrypoint, "-c", "echo unsafe"),),
        )


def test_repair_work_order_rejects_whitespace_shell_bypass() -> None:
    with pytest.raises(ProductIncidentError, match="normalized and non-empty"):
        replace(
            _work_order(),
            acceptance_commands=((" bash ", "-c", "echo unsafe"),),
        )


def test_repair_work_order_rejects_ambiguous_permission_identity() -> None:
    with pytest.raises(ProductIncidentError, match="normalized non-empty permission ceiling"):
        replace(
            _work_order(),
            permission_ceiling=frozenset({"repo.read", " repo.write"}),
        )


def test_candidate_regression_refs_are_actual_sha256_digests() -> None:
    with pytest.raises(ProductIncidentError, match="regression evidence digest"):
        RepairCandidateEvidence(
            candidate_id="candidate:incident-security",
            incident_id="incident-security",
            work_order_id="repair:incident-security",
            base_release_sha=OLD_SHA,
            result_sha=NEW_SHA,
            artifact_digest=ARTIFACT,
            diff_digest=DIFF,
            regression_evidence_refs=("test://green-but-not-a-digest",),
            provenance_evidence_refs=("build://exact-sha",),
            review_ref="review://independent/security",
            review_accepted=True,
            recorded_at=NOW,
        )

    candidate = RepairCandidateEvidence(
        candidate_id="candidate:incident-security",
        incident_id="incident-security",
        work_order_id="repair:incident-security",
        base_release_sha=OLD_SHA,
        result_sha=NEW_SHA,
        artifact_digest=ARTIFACT,
        diff_digest=DIFF,
        regression_evidence_refs=(TEST_DIGEST,),
        provenance_evidence_refs=("build://exact-sha",),
        review_ref="review://independent/security",
        review_accepted=True,
        recorded_at=NOW,
    )
    assert candidate.regression_evidence_refs == (TEST_DIGEST,)
