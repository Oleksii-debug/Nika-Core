from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path

from nika_core.packaging.notices import build_third_party_notices, verify_third_party_notices
from nika_core.packaging.release import (
    build_release_manifest,
    verify_release_manifest,
    write_release_manifest,
)
from nika_core.packaging.windows import default_windows_plan
from nika_core.product_factory_packaged_planning import TEAM_PLAN_REF_PREFIX

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PF11_EVIDENCE_NAME = "pf11-packaged-product-journey.json"


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
            f"requested release version {requested!r} does not match "
            f"pyproject version {canonical!r}"
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


def _require_exact_nonnegative_int(payload: dict[str, object], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"packaged PF11 proof returned invalid {field}")
    return value


def _require_nonempty_string(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"packaged PF11 proof returned invalid {field}")
    return value


def prove_packaged_product_journey(bundle_dir: Path, *, source_sha: str) -> Path:
    """Run the packaged executable twice and persist restart-bound PF11 evidence."""
    executable = bundle_dir / "NikaCore.exe"
    if not executable.is_file():
        raise RuntimeError(f"packaged PF11 proof executable is missing: {executable}")
    if not _FULL_SHA_RE.fullmatch(source_sha):
        raise ValueError("packaged PF11 proof requires exact source SHA")

    with tempfile.TemporaryDirectory(prefix="nika-pf11-proof-") as temporary:
        root = Path(temporary)
        database = root / "product-journey.db"
        outputs: list[dict[str, object]] = []
        environment = dict(os.environ)
        environment["NIKA_DB_PATH"] = str(database)
        for attempt in (1, 2):
            output = root / f"proof-{attempt}.json"
            completed = subprocess.run(
                [
                    str(executable),
                    "--pf11-proof",
                    "--pf11-proof-output",
                    str(output),
                ],
                check=False,
                env=environment,
                timeout=60,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"packaged PF11 ProductProject proof failed on attempt {attempt}: "
                    f"exit {completed.returncode}"
                )
            try:
                payload = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("packaged PF11 proof did not emit valid JSON evidence") from exc
            if not isinstance(payload, dict):
                raise TypeError("packaged PF11 proof evidence must be a JSON object")
            outputs.append(payload)

    first, second = outputs
    if first != second:
        raise RuntimeError("packaged PF11 ProductProject restart replay changed durable identity")
    project_id = first.get("project_id")
    if (
        first.get("route") != "product_project"
        or first.get("spec_version") != 2
        or not isinstance(project_id, str)
        or not project_id.strip()
        or first.get("command_center_state_proven") is not True
        or first.get("current_command_proven") is not True
        or first.get("current_command_focus_proven") is not True
        or first.get("bounded_projection_proven") is not True
        or first.get("bridge_state_project_id") != project_id
        or first.get("bridge_state_spec_version") != 2
        or first.get("team_plan_persisted_proven") is not True
        or first.get("team_plan_restart_recovery_proven") is not True
        or first.get("team_plan_worker_dispatch_started") is not False
    ):
        raise RuntimeError("packaged PF11 ProductProject proof returned invalid route evidence")
    status_count = _require_exact_nonnegative_int(first, "bridge_state_status_count")
    decision_count = _require_exact_nonnegative_int(first, "bridge_state_decision_count")
    role_count = _require_exact_nonnegative_int(first, "team_plan_role_count")
    review_count = _require_exact_nonnegative_int(
        first,
        "team_plan_independent_review_count",
    )
    team_plan_id = _require_nonempty_string(first, "team_plan_id")
    team_plan_binding_ref = _require_nonempty_string(first, "team_plan_binding_ref")
    if not team_plan_binding_ref.startswith(TEAM_PLAN_REF_PREFIX):
        raise RuntimeError("packaged PF11 proof returned an unrecognized team-plan binding")
    if role_count < 1 or review_count < 1 or review_count > role_count:
        raise RuntimeError("packaged PF11 proof returned invalid team-plan role counts")
    if first.get("team_plan_permission_ceiling") != ["read_project"]:
        raise RuntimeError("packaged PF11 proof exceeded the planning-only permission ceiling")
    for forbidden_true in (
        "human_tested",
        "nvda_verified",
        "production_release_ready",
    ):
        if first.get(forbidden_true) is not False:
            raise RuntimeError(f"packaged PF11 proof may not set {forbidden_true}=true")

    target = bundle_dir / _PF11_EVIDENCE_NAME
    evidence = {
        "schema_version": 3,
        "source_sha": source_sha,
        "route": first["route"],
        "product_project_id": project_id,
        "product_project_spec_version": first["spec_version"],
        "product_project_state": first.get("state"),
        "product_command_center_proven": True,
        "packaged_bridge_state_proven": True,
        "bounded_projection_proven": True,
        "bridge_state_status_count": status_count,
        "bridge_state_decision_count": decision_count,
        "team_plan_id": team_plan_id,
        "team_plan_binding_ref": team_plan_binding_ref,
        "team_plan_role_count": role_count,
        "team_plan_independent_review_count": review_count,
        "team_plan_permission_ceiling": ["read_project"],
        "team_plan_persisted_proven": True,
        "team_plan_restart_recovery_proven": True,
        "team_plan_worker_dispatch_started": False,
        "packaged_executable_proven": True,
        "restart_replay_proven": True,
        "human_tested": False,
        "nvda_verified": False,
        "production_release_ready": False,
    }
    target.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


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

    prove_packaged_product_journey(plan.bundle_dir, source_sha=exact_source_sha)
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
