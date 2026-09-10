from __future__ import annotations

from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "m12-prehuman-release-gate.yml"
)


def _between(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start + len(start_marker))
    return text[start:end]


def _final_artifact_window(workflow: str) -> str:
    return _between(
        workflow,
        "- name: Create distributable ZIP",
        "- name: Verify exact final distributable evidence binding",
    )


def _exact_final_runtime_step(workflow: str) -> str:
    return _between(
        workflow,
        "- name: Re-prove runtime from exact extracted final ZIP",
        "- name: Record automated pre-human evidence",
    )


def _prehuman_evidence_step(workflow: str) -> str:
    return _between(
        workflow,
        "- name: Record automated pre-human evidence",
        "- name: Verify exact final distributable evidence binding",
    )


def test_m12_reexecutes_packaged_uia_from_the_exact_extracted_final_zip() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    final_artifact_window = _final_artifact_window(workflow)

    assert "Expand-Archive" in final_artifact_window
    expand_at = final_artifact_window.index("Expand-Archive")
    post_extract = final_artifact_window[expand_at:]

    # v01_autostart_uia_proof is the existing packaged restart + UIA/keyboard
    # authority: Enable -> fresh-process Observe -> Disable. Reuse it rather than
    # inventing another packaged smoke/restart harness.
    assert "v01_autostart_uia_proof.ps1" in post_extract
    assert "-ExePath ./dist/NikaCore/NikaCore.exe" not in post_extract
    assert "exact_final_artifact_runtime_uia = $true" in post_extract
    assert post_extract.index("v01_autostart_uia_proof.ps1") < post_extract.index(
        "exact_final_artifact_runtime_uia = $true"
    )


def test_m12_expands_the_same_zip_that_is_bound_to_distributable_evidence() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    runtime_step = _exact_final_runtime_step(workflow)
    evidence_step = _prehuman_evidence_step(workflow)

    zip_binding = "$zipPath = '${{ steps.distributable.outputs.zip_path }}'"
    archive_binding = (
        "verify_release_archive(Path(r'$zipPath'), "
        "source_sha='${{ env.NIKA_CANDIDATE_SHA }}')"
    )
    expand_binding = "Expand-Archive -LiteralPath $zipPath -DestinationPath $extractRoot -Force"
    evidence_binding = (
        "distributable_zip_path = '${{ steps.distributable.outputs.zip_path }}'"
    )

    # Bind identity semantically instead of relying on a brittle character-distance
    # window: the runtime step must source the already-hashed distributable output,
    # canonically verify that exact variable, expand it, and record the same output
    # in final evidence.
    assert zip_binding in runtime_step
    assert archive_binding in runtime_step
    assert expand_binding in runtime_step
    assert runtime_step.index(zip_binding) < runtime_step.index(archive_binding)
    assert runtime_step.index(archive_binding) < runtime_step.index(expand_binding)
    assert evidence_binding in evidence_step
    assert "exact_final_artifact_runtime_uia = $true" in runtime_step


def test_m12_extracts_to_a_fresh_unicode_and_space_path() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    final_artifact_window = _final_artifact_window(workflow)
    expand_at = final_artifact_window.index("Expand-Archive")
    pre_expand = final_artifact_window[:expand_at]

    # The exact-user-artifact proof must not reuse ./dist/NikaCore. Require a
    # deliberately awkward fresh extraction root so path handling is exercised.
    assert "Nika Core" in pre_expand
    assert any(ord(character) > 127 for character in pre_expand)
    assert "Remove-Item -LiteralPath" in pre_expand
    assert "-Recurse" in pre_expand
    assert "-Force" in pre_expand
    assert "./dist/NikaCore" not in final_artifact_window[expand_at:]


def test_m12_reverifies_extracted_manifest_before_runtime_and_truth_credit() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    final_artifact_window = _final_artifact_window(workflow)

    expand_at = final_artifact_window.index("Expand-Archive")
    post_extract = final_artifact_window[expand_at:]
    manifest_verify_at = post_extract.index("verify_release_manifest")
    restart_uia_at = post_extract.index("v01_autostart_uia_proof.ps1")
    truth_at = post_extract.index("exact_final_artifact_runtime_uia = $true")

    assert "release-manifest.json" in post_extract[:restart_uia_at]
    assert "${{ env.NIKA_CANDIDATE_SHA }}" in post_extract[:restart_uia_at]
    assert manifest_verify_at < restart_uia_at < truth_at


def test_m12_records_final_artifact_truth_only_after_all_extracted_checks() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    final_artifact_window = _final_artifact_window(workflow)

    truth_at = final_artifact_window.index("exact_final_artifact_runtime_uia = $true")
    before_truth = final_artifact_window[:truth_at]

    assert "Expand-Archive" in before_truth
    assert "verify_release_manifest" in before_truth
    assert "v01_autostart_uia_proof.ps1" in before_truth

    # The normal pre-ZIP proof may remain as useful lineage evidence, but it must
    # never be the only source of exact-final-artifact runtime truth.
    assert before_truth.rindex("Expand-Archive") < before_truth.rindex(
        "verify_release_manifest"
    )
    assert before_truth.rindex("verify_release_manifest") < before_truth.rindex(
        "v01_autostart_uia_proof.ps1"
    )
