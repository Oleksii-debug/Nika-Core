from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ROOT / ".github" / "workflows" / "ci.yml",
    ROOT / ".github" / "workflows" / "m11-windows-release.yml",
    ROOT / ".github" / "workflows" / "m12-prehuman-release-gate.yml",
)
RELEASE_WORKFLOWS = WORKFLOWS[1:]
CANDIDATE_ENV = "NIKA_CANDIDATE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}"
CHECKOUT_REF = "ref: ${{ env.NIKA_CANDIDATE_SHA }}"
IDENTITY_ASSERTION = "python scripts/qa_assert_checkout_identity.py"
MAIN_PUSH_TRIGGER = '  push:\n    branches:\n      - "main"'
M12_UPSTREAM_WORKFLOW_PATHS = (
    '      - ".github/workflows/ci.yml"',
    '      - ".github/workflows/m11-windows-release.yml"',
)


def test_every_ci_checkout_is_bound_to_exact_candidate_sha() -> None:
    for path in WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        assert CANDIDATE_ENV in text, path
        lines = text.splitlines()
        checkout_indexes = [
            index for index, line in enumerate(lines) if "uses: actions/checkout@v4" in line
        ]
        assert checkout_indexes, path
        for index in checkout_indexes:
            checkout_block = "\n".join(lines[index : index + 4])
            assert CHECKOUT_REF in checkout_block, f"implicit checkout in {path}"


def test_every_candidate_job_fails_closed_on_checkout_identity() -> None:
    for path in WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        checkout_count = text.count("uses: actions/checkout@v4")
        assertion_count = text.count(IDENTITY_ASSERTION)
        assert assertion_count == checkout_count, path


def test_release_artifact_and_evidence_use_the_verified_candidate_sha() -> None:
    m11 = (ROOT / ".github" / "workflows" / "m11-windows-release.yml").read_text(
        encoding="utf-8"
    )
    m12 = (ROOT / ".github" / "workflows" / "m12-prehuman-release-gate.yml").read_text(
        encoding="utf-8"
    )

    assert "--source-sha '${{ env.NIKA_CANDIDATE_SHA }}'" in m11
    assert "windows-x64-${{ env.NIKA_CANDIDATE_SHA }}" in m11
    assert "--source-sha '${{ env.NIKA_CANDIDATE_SHA }}'" in m12
    assert "commit_sha = '${{ env.NIKA_CANDIDATE_SHA }}'" in m12
    assert "exact_checkout_sha_verified = $true" in m12
    assert "m12-prehuman-${{ env.NIKA_CANDIDATE_SHA }}" in m12


def test_release_gates_run_automatically_on_main_push() -> None:
    for path in RELEASE_WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        assert MAIN_PUSH_TRIGGER in text, path


def test_m12_runs_when_upstream_release_workflows_change() -> None:
    m12 = (ROOT / ".github" / "workflows" / "m12-prehuman-release-gate.yml").read_text(
        encoding="utf-8"
    )

    for workflow_path in M12_UPSTREAM_WORKFLOW_PATHS:
        assert m12.count(workflow_path) == 2, workflow_path
