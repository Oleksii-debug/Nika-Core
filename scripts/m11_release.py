from __future__ import annotations

import argparse
from pathlib import Path

from nika_core.packaging.release import (
    build_release_manifest,
    verify_release_manifest,
    write_release_manifest,
)
from nika_core.packaging.windows import default_windows_plan


def build(project_root: Path, version: str) -> Path:
    import PyInstaller.__main__

    plan = default_windows_plan(project_root)
    PyInstaller.__main__.run(list(plan.pyinstaller_args()))
    manifest = build_release_manifest(plan.bundle_dir, product="NikaCore", version=version)
    write_release_manifest(plan.bundle_dir, manifest)
    findings = verify_release_manifest(plan.bundle_dir, manifest)
    if findings:
        raise RuntimeError(f"release integrity verification failed: {findings}")
    return plan.bundle_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    bundle = build(args.project_root, args.version)
    print(bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
