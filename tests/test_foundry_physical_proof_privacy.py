from __future__ import annotations

from scripts.prove_foundry_local import _model_evidence_for_report
from nika_core.model_gateway.foundry_local import FoundryModelEvidence


def test_physical_proof_model_metadata_does_not_export_absolute_cache_path() -> None:
    local_path = r"C:\Users\private-profile\.foundry\models\test-model"
    evidence = FoundryModelEvidence(
        model_id="test-model-cpu:7",
        model_version="7",
        alias="test-model",
        cached=True,
        loaded=False,
        path=local_path,
        context_length=4096,
        input_modalities="text",
        output_modalities="text",
        capability_tags="chat",
        supports_tool_calling=False,
    )

    report = _model_evidence_for_report(evidence)

    assert "path" not in report
    assert local_path not in repr(report)
    assert report["cache_path_available"] is True
    assert report["model_id"] == "test-model-cpu:7"


def test_physical_proof_reports_missing_cache_path_without_inventing_one() -> None:
    evidence = FoundryModelEvidence(
        model_id="test-model-cpu:7",
        model_version="7",
        alias="test-model",
        cached=False,
        loaded=False,
        path=None,
        context_length=4096,
        input_modalities="text",
        output_modalities="text",
        capability_tags="chat",
        supports_tool_calling=False,
    )

    report = _model_evidence_for_report(evidence)

    assert "path" not in report
    assert report["cache_path_available"] is False
