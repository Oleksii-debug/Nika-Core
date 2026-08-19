from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    ROOT / ".github" / "workflows" / "ci.yml",
    ROOT / ".github" / "workflows" / "m11-windows-release.yml",
    ROOT / ".github" / "workflows" / "m12-prehuman-release-gate.yml",
)
CANDIDATE_ENV = "NIKA_CANDIDATE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}"
CHECKOUT_REF = "ref: ${{ env.NIKA_CANDIDATE_SHA }}"
IDENTITY_ASSERTION = "python scripts/qa_assert_checkout_identity.py"


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
