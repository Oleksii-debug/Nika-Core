from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from nika_core.media.contracts import ModelDescriptor
from nika_core.media.evidence import (
    EngineExecutionEvidence,
    MediaProofManifest,
    binary_evidence,
    model_evidence,
)
from nika_core.media.ffprobe import FFprobeAdapter
from nika_core.media.hashing import sha256_file, sha256_json
from nika_core.media.ocr import OCRPageRequest, TesseractOCRAdapter


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect explicit local FFprobe/Tesseract evidence without downloading binaries or models."
        )
    )
    parser.add_argument("--ffprobe", type=Path, required=True)
    parser.add_argument("--tesseract", type=Path, required=True)
    parser.add_argument("--tesseract-binary-source-reference", required=True)
    parser.add_argument("--tesseract-binary-license", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--media-fixture", type=Path)
    parser.add_argument("--ocr-fixture", type=Path)
    parser.add_argument("--ocr-language", default="eng")
    parser.add_argument("--tessdata", type=Path)
    parser.add_argument("--tessdata-model-id")
    parser.add_argument("--tessdata-version")
    parser.add_argument("--tessdata-license-reference")
    parser.add_argument("--tessdata-source-reference")
    return parser


def main() -> int:
    args = _parser().parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="nika-media-proof-") as temp_dir:
        cwd = Path(temp_dir)
        ffprobe = FFprobeAdapter(args.ffprobe)
        ffprobe_audit = ffprobe.audit(cwd=cwd)
        tesseract = TesseractOCRAdapter(executable=args.tesseract)
        tesseract_descriptor = tesseract.descriptor(cwd=cwd)

        executions: list[EngineExecutionEvidence] = []
        if args.media_fixture is not None:
            probe = ffprobe.probe(args.media_fixture, asset_id="proof-media", cwd=cwd)
            executions.append(
                EngineExecutionEvidence(
                    engine_id=ffprobe_audit.descriptor.engine_id,
                    evidence_kind="probe",
                    fixture_sha256=sha256_file(args.media_fixture.resolve(strict=True)),
                    result_sha256=sha256_json(probe.model_dump(mode="json")),
                )
            )
        if args.ocr_fixture is not None:
            page = tesseract.recognize_page(
                OCRPageRequest(
                    page_number=1,
                    image_path=args.ocr_fixture,
                    language=args.ocr_language,
                ),
                cwd=cwd,
                timeout_seconds=60,
            )
            if not page.text.strip():
                raise RuntimeError("OCR proof produced empty text")
            executions.append(
                EngineExecutionEvidence(
                    engine_id=tesseract_descriptor.engine_id,
                    evidence_kind="ocr",
                    fixture_sha256=sha256_file(args.ocr_fixture.resolve(strict=True)),
                    result_sha256=sha256_json(page.model_dump(mode="json")),
                )
            )

        models = ()
        model_args = (
            args.tessdata,
            args.tessdata_model_id,
            args.tessdata_version,
            args.tessdata_license_reference,
            args.tessdata_source_reference,
        )
        if any(item is not None for item in model_args):
            if not all(item is not None for item in model_args):
                raise ValueError("all tessdata evidence arguments must be supplied together")
            descriptor = ModelDescriptor(
                model_id=args.tessdata_model_id,
                engine_id="tesseract",
                version=args.tessdata_version,
                license_reference=args.tessdata_license_reference,
            )
            models = (
                model_evidence(
                    descriptor=descriptor,
                    path=args.tessdata,
                    source_reference=args.tessdata_source_reference,
                ),
            )

        manifest = MediaProofManifest(
            engines=(ffprobe_audit.descriptor, tesseract_descriptor),
            binaries=(
                binary_evidence(
                    component_id="ffprobe",
                    path=args.ffprobe,
                    source_reference="https://ffmpeg.org/",
                    license_classification=ffprobe_audit.license_classification,
                ),
                binary_evidence(
                    component_id="tesseract",
                    path=args.tesseract,
                    source_reference=args.tesseract_binary_source_reference,
                    license_classification=args.tesseract_binary_license,
                ),
            ),
            models=models,
            executions=tuple(executions),
            real_engine_execution_proven=len(executions) == 2,
            target_machine_measured=False,
        )
        args.output.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
