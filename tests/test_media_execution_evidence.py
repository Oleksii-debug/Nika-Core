import pytest

from nika_core.media.contracts import EngineDescriptor
from nika_core.media.evidence import (
    BinaryEvidence,
    EngineExecutionEvidence,
    MediaProofManifest,
)


def _engine(engine_id: str, *, executable_sha256: str = "a" * 64) -> EngineDescriptor:
    return EngineDescriptor(
        engine_id=engine_id,
        name=engine_id,
        version="fixture",
        license_id="fixture-license",
        source_reference="https://example.invalid/source",
        executable_sha256=executable_sha256,
    )


def _binary(engine_id: str, *, sha256: str = "a" * 64) -> BinaryEvidence:
    return BinaryEvidence(
        component_id=engine_id,
        engine_id=engine_id,
        path_name=f"{engine_id}.exe",
        sha256=sha256,
        size_bytes=1,
        source_reference="https://example.invalid/binary",
        license_classification="fixture-binary-license",
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
    binaries = (_binary("ffprobe"), _binary("tesseract"))
    with pytest.raises(ValueError, match="every declared engine"):
        MediaProofManifest(
            engines=engines,
            binaries=binaries,
            executions=(_execution("ffprobe"),),
            real_engine_execution_proven=True,
        )

    manifest = MediaProofManifest(
        engines=engines,
        binaries=binaries,
        executions=(
            _execution("ffprobe"),
            _execution("tesseract", "ocr"),
        ),
        real_engine_execution_proven=True,
    )
    assert manifest.real_engine_execution_proven is True
    assert {item.engine_id for item in manifest.executions} == {"ffprobe", "tesseract"}


def test_full_real_engine_flag_requires_audited_binary_for_every_engine() -> None:
    engines = (_engine("ffprobe"), _engine("tesseract"))
    with pytest.raises(ValueError, match="audited binary evidence"):
        MediaProofManifest(
            engines=engines,
            binaries=(_binary("ffprobe"),),
            executions=(
                _execution("ffprobe"),
                _execution("tesseract", "ocr"),
            ),
            real_engine_execution_proven=True,
        )


def test_binary_evidence_must_match_engine_descriptor_checksum() -> None:
    with pytest.raises(ValueError, match="checksum must match"):
        MediaProofManifest(
            engines=(_engine("ffprobe", executable_sha256="a" * 64),),
            binaries=(_binary("ffprobe", sha256="b" * 64),),
        )


def test_binary_evidence_requires_descriptor_executable_checksum() -> None:
    with pytest.raises(ValueError, match="descriptor executable checksum"):
        MediaProofManifest(
            engines=(_engine("ffprobe", executable_sha256=None),),
            binaries=(_binary("ffprobe"),),
        )


def test_binary_evidence_cannot_reference_unproven_engine() -> None:
    with pytest.raises(ValueError, match="binary evidence"):
        MediaProofManifest(
            engines=(_engine("ffprobe"),),
            binaries=(_binary("tesseract"),),
        )


def test_execution_evidence_cannot_reference_unproven_engine() -> None:
    with pytest.raises(ValueError, match="execution evidence"):
        MediaProofManifest(
            engines=(_engine("ffprobe"),),
            executions=(_execution("tesseract", "ocr"),),
        )