from __future__ import annotations

import argparse
from pathlib import Path

from nika_core.packaging.attestation import (
    build_release_attestation_evidence,
    write_release_attestation_evidence,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Bind verified GitHub artifact attestation to exact M12 distributable."
    )
    result.add_argument("--artifact", type=Path, required=True)
    result.add_argument("--artifact-reference", required=True)
    result.add_argument("--prehuman-evidence", type=Path, required=True)
    result.add_argument("--verification", type=Path, required=True)
    result.add_argument("--source-sha", required=True)
    result.add_argument("--repository", required=True)
    result.add_argument("--signer-workflow", required=True)
    result.add_argument("--source-ref", required=True)
    result.add_argument("--attestation-id", required=True)
    result.add_argument("--attestation-url", required=True)
    result.add_argument("--output", type=Path, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    evidence = build_release_attestation_evidence(
        args.artifact,
        args.prehuman_evidence,
        args.verification,
        source_sha=args.source_sha,
        artifact_reference=args.artifact_reference,
        repository=args.repository,
        signer_workflow=args.signer_workflow,
        source_ref=args.source_ref,
        attestation_id=args.attestation_id,
        attestation_url=args.attestation_url,
    )
    write_release_attestation_evidence(args.output, evidence)
    print(
        "M12 cryptographic distributable attestation evidence verified "
        f"for {evidence.artifact_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
