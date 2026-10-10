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

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PF11_EVIDENCE_NAME = "pf11-packaged-product-journey.json"


def project_version(project_root: Path) -> str:
    pyproject = project_root / "pyproject.toml"
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    try:
        version = data["project"]["version"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("pyproject.toml is missing [project].version") from exc
    if not isinstance(version, str) or not version or version != version.strip():
        raise RuntimeError("pyproject.toml [project].version must be a nonempty, unpadded string")
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
    if requested is not None:
        candidate = requested
    elif "NIKA_SOURCE_SHA" in os.environ:
        candidate = os.environ["NIKA_SOURCE_SHA"]
    else:
        candidate = os.environ.get("GITHUB_SHA")
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


def _unique_proof_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate packaged PF11 proof field: {key}")
        result[key] = value
    return result


def _reject_nonfinite_proof_number(value: str) -> object:
    raise ValueError(f"non-finite packaged PF11 proof number: {value}")


def _proof_identity(payload: dict[str, object]) -> str:
    # JSON preserves bool/int distinctions that Python dict equality does not.
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


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
                # Bound the actual open/read, not a separate stat susceptible to file replacement.
                with output.open("rb") as handle:
                    encoded = handle.read(1024 * 1024 + 1)
                if len(encoded) > 1024 * 1024:
                    raise ValueError("packaged PF11 proof is too large")
                payload = json.loads(
                    encoded.decode("utf-8"),
                    object_pairs_hook=_unique_proof_fields,
                    parse_constant=_reject_nonfinite_proof_number,
                )
            except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                raise RuntimeError("packaged PF11 proof did not emit valid JSON evidence") from exc
            if not isinstance(payload, dict):
                raise TypeError("packaged PF11 proof evidence must be a JSON object")
            outputs.append(payload)

    first, second = outputs
    if _proof_identity(first) != _proof_identity(second):
        raise RuntimeError("packaged PF11 ProductProject restart replay changed durable identity")
    project_id = first.get("project_id")
    if (
        first.get("route") != "product_project"
        or type(first.get("spec_version")) is not int
        or first["spec_version"] != 1
        or not isinstance(project_id, str)
        or not project_id.strip()
        or first.get("command_center_state_proven") is not True
        or first.get("current_command_proven") is not True
        or first.get("current_command_focus_proven") is not True
        or first.get("restart_selection_integrity_proven") is not True
        or first.get("bounded_projection_proven") is not True
        or not isinstance(first.get("state"), str)
        or not first["state"].strip()
        or first.get("bridge_state_project_id") != project_id
        or type(first.get("bridge_state_spec_version")) is not int
        or first["bridge_state_spec_version"] != 1
    ):
        raise RuntimeError("packaged PF11 ProductProject proof returned invalid route evidence")
    status_count = _require_exact_nonnegative_int(first, "bridge_state_status_count")
    decision_count = _require_exact_nonnegative_int(first, "bridge_state_decision_count")
    for forbidden_true in (
        "human_tested",
        "nvda_verified",
        "production_release_ready",
    ):
        if first.get(forbidden_true) is not False:
            raise RuntimeError(f"packaged PF11 proof may not set {forbidden_true}=true")

    target = bundle_dir / _PF11_EVIDENCE_NAME
    evidence = {
        "schema_version": 2,
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
        "packaged_executable_proven": True,
        "restart_replay_proven": True,
        "human_tested": False,
        "nvda_verified": False,
        "production_release_ready": False,
    }
    encoded = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=bundle_dir,
            prefix=".pf11-proof-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
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
