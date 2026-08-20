import pytest

from nika_core.media.contracts import EngineDescriptor
from nika_core.media.evidence import EngineExecutionEvidence, MediaProofManifest


def _engine(engine_id: str) -> EngineDescriptor:
    return EngineDescriptor(
        engine_id=engine_id,
        name=engine_id,
        version="fixture",
        license_id="fixture-license",
        source_reference="https://example.invalid/source",
    )


def _execution(engine_id: str, kind: str = "probe") -> EngineExecutionEvidence:
    return EngineExecutionEvidence(
        engine_id=engine_id,
        evidence_kind=kind,
        fixture_sha256="1" * 64,
        result_sha256="2" * 64,
    )


def test_full_real_engine_flag_requires_execution_evidence_for_every_engine() -> None:
    engines = (_engine("ffprobe"), _engine("tesseract"))
    with pytest.raises(ValueError, match="every declared engine"):
        MediaProofManifest(
            engines=engines,
            executions=(_execution("ffprobe"),),
            real_engine_execution_proven=True,
        )

    manifest = MediaProofManifest(
        engines=engines,
        executions=(
            _execution("ffprobe"),
            _execution("tesseract", "ocr"),
        ),
        real_engine_execution_proven=True,
    )
    assert manifest.real_engine_execution_proven is True
    assert {item.engine_id for item in manifest.executions} == {"ffprobe", "tesseract"}


def test_execution_evidence_cannot_reference_unproven_engine() -> None:
    with pytest.raises(ValueError, match="execution evidence"):
        MediaProofManifest(
            engines=(_engine("ffprobe"),),
            executions=(_execution("tesseract", "ocr"),),
        )
