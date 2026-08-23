from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ROOT / ".github" / "workflows" / "ci.yml",
    ROOT / ".github" / "workflows" / "m11-windows-release.yml",
    ROOT / ".github" / "workflows" / "m12-prehuman-release-gate.yml",
)
RELEASE_WORKFLOWS = WORKFLOWS[1:]
M12_WORKFLOW = WORKFLOWS[2]
CANDIDATE_ENV = "NIKA_CANDIDATE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}"
CHECKOUT_ACTION = "uses: actions/checkout@"
CHECKOUT_REF = "ref: ${{ env.NIKA_CANDIDATE_SHA }}"
TRUSTED_MAIN_REF = "ref: ${{ github.sha }}"
IDENTITY_ASSERTION = "scripts/qa_assert_checkout_identity.py"
MAIN_PUSH_TRIGGER = '  push:\n    branches:\n      - "main"'
ATTEST_MAIN_JOB = "\n  attest-main-distributable:\n"
M12_UPSTREAM_WORKFLOW_PATHS = (
    '      - ".github/workflows/ci.yml"',
    '      - ".github/workflows/m11-windows-release.yml"',
)


def _checkout_indexes(text: str) -> list[int]:
    return [
        index for index, line in enumerate(text.splitlines()) if CHECKOUT_ACTION in line
    ]


def _candidate_job_text(path: Path, text: str) -> str:
    if path != M12_WORKFLOW:
        return text
    candidate_text, separator, _ = text.partition(ATTEST_MAIN_JOB)
    assert separator, "M12 trusted-main attestation job is missing"
    return candidate_text


def _trusted_main_job_text(text: str) -> str:
    _, separator, trusted_main_text = text.partition(ATTEST_MAIN_JOB)
    assert separator, "M12 trusted-main attestation job is missing"
    return trusted_main_text


def test_every_ci_checkout_is_bound_to_exact_candidate_sha() -> None:
    for path in WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        assert CANDIDATE_ENV in text, path
        candidate_text = _candidate_job_text(path, text)
        lines = candidate_text.splitlines()
        checkout_indexes = _checkout_indexes(candidate_text)
        assert checkout_indexes, path
        for index in checkout_indexes:
            checkout_block = "\n".join(lines[index : index + 4])
            assert CHECKOUT_REF in checkout_block, f"implicit candidate checkout in {path}"


def test_every_candidate_job_fails_closed_on_checkout_identity() -> None:
    for path in WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        candidate_text = _candidate_job_text(path, text)
        checkout_count = len(_checkout_indexes(candidate_text))
        assertion_count = candidate_text.count(IDENTITY_ASSERTION)
        assert assertion_count == checkout_count, path


def test_m12_trusted_main_attestation_checkout_is_exact_and_separate() -> None:
    m12 = M12_WORKFLOW.read_text(encoding="utf-8")
    trusted_main = _trusted_main_job_text(m12)
    checkout_indexes = _checkout_indexes(trusted_main)
    assert len(checkout_indexes) == 1
    lines = trusted_main.splitlines()
    checkout_block = "\n".join(lines[checkout_indexes[0] : checkout_indexes[0] + 5])
    assert TRUSTED_MAIN_REF in checkout_block
    assert "persist-credentials: false" in checkout_block
    assert trusted_main.count(IDENTITY_ASSERTION) == 1
    assert "if: github.event_name == 'push' && github.ref == 'refs/heads/main'" in trusted_main


def test_release_artifact_and_evidence_use_the_verified_candidate_sha() -> None:
    m11 = (ROOT / ".github" / "workflows" / "m11-windows-release.yml").read_text(
        encoding="utf-8"
    )
    m12 = M12_WORKFLOW.read_text(encoding="utf-8")

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
    m12 = M12_WORKFLOW.read_text(encoding="utf-8")

    for workflow_path in M12_UPSTREAM_WORKFLOW_PATHS:
        assert m12.count(workflow_path) == 2, workflow_path
