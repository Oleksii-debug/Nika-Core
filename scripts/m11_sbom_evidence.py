from __future__ import annotations

import argparse
from pathlib import Path

from nika_core.packaging.release import (
    build_release_manifest,
    verify_release_manifest,
    write_release_manifest,
)
from nika_core.packaging.sbom import (
    SupplyChainEvidenceError,
    verify_supply_chain_evidence,
    write_supply_chain_evidence,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and verify sanitized CycloneDX release supply-chain evidence."
    )
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    bundle = args.bundle.resolve(strict=True)
    project_root = args.project_root.resolve(strict=True)
    report = args.report.resolve(strict=True)

    try:
        supply_path, sbom_path = write_supply_chain_evidence(
            bundle,
            report,
            project_root=project_root,
            application_name="nika-core",
            application_version=args.version,
            source_sha=args.source_sha,
            extras=("gui",),
        )
        manifest = build_release_manifest(
            bundle,
            product="NikaCore",
            version=args.version,
            source_sha=args.source_sha,
        )
        write_release_manifest(bundle, manifest)
        manifest_findings = verify_release_manifest(bundle, manifest)
        supply_findings = verify_supply_chain_evidence(
            bundle,
            report,
            project_root=project_root,
            application_name="nika-core",
            application_version=args.version,
            source_sha=args.source_sha,
            extras=("gui",),
        )
    except (OSError, ValueError, SupplyChainEvidenceError) as exc:
        raise SystemExit(f"SBOM release evidence failed: {exc}") from exc

    findings = tuple(manifest_findings) + tuple(supply_findings)
    if findings:
        raise SystemExit(f"SBOM release evidence verification failed: {findings}")

    print(f"supply-chain={supply_path.name}")
    print(f"sbom={sbom_path.name}")
    print("release-manifest=verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
