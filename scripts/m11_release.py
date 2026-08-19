from __future__ import annotations

import argparse
import os
import re
import tomllib
from pathlib import Path

from nika_core.packaging.notices import build_third_party_notices, verify_third_party_notices
from nika_core.packaging.release import (
    build_release_manifest,
    verify_release_manifest,
    write_release_manifest,
)
from nika_core.packaging.windows import default_windows_plan

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def project_version(project_root: Path) -> str:
    pyproject = project_root / "pyproject.toml"
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    try:
        version = str(data["project"]["version"]).strip()
    except (KeyError, TypeError) as exc:
        raise RuntimeError("pyproject.toml is missing [project].version") from exc
    if not version:
        raise RuntimeError("pyproject.toml [project].version is empty")
    return version


def resolve_release_version(project_root: Path, requested: str | None) -> str:
    canonical = project_version(project_root)
    if requested is not None and requested != canonical:
        raise ValueError(
            f"requested release version {requested!r} does not match pyproject version {canonical!r}"
        )
    return canonical


def resolve_source_sha(requested: str | None) -> str:
    candidate = requested or os.environ.get("NIKA_SOURCE_SHA") or os.environ.get("GITHUB_SHA")
    candidate = (candidate or "").strip().lower()
    if not _FULL_SHA_RE.fullmatch(candidate):
        raise ValueError(
            "exact 40-character source SHA is required via --source-sha, "
            "NIKA_SOURCE_SHA or GITHUB_SHA"
        )
    return candidate


def build(
    project_root: Path,
    version: str | None,
    source_sha: str | None,
) -> Path:
    import PyInstaller.__main__

    release_version = resolve_release_version(project_root, version)
    exact_source_sha = resolve_source_sha(source_sha)
    plan = default_windows_plan(project_root)
    PyInstaller.__main__.run(list(plan.pyinstaller_args()))

    build_third_party_notices(plan.bundle_dir)
    notice_findings = verify_third_party_notices(plan.bundle_dir)
    if notice_findings:
        raise RuntimeError(f"third-party notice verification failed: {notice_findings}")

    manifest = build_release_manifest(
        plan.bundle_dir,
        product="NikaCore",
        version=release_version,
        source_sha=exact_source_sha,
    )
    write_release_manifest(plan.bundle_dir, manifest)
    findings = verify_release_manifest(plan.bundle_dir, manifest)
    if findings:
        raise RuntimeError(f"release integrity verification failed: {findings}")
    return plan.bundle_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--version",
        help="Optional assertion; must equal [project].version in pyproject.toml",
    )
    parser.add_argument(
        "--source-sha",
        help="Exact source commit SHA; falls back to NIKA_SOURCE_SHA/GITHUB_SHA",
    )
    args = parser.parse_args()
    bundle = build(args.project_root, args.version, args.source_sha)
    print(bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
