from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import tomllib
from contextlib import closing
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
_DATA_ADOPTION_EVIDENCE_NAME = "packaged-data-adoption-proof.json"
_RECOVERY_DIALOG_TITLE = "Nika Core — відновлення даних"


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
        or first.get("spec_version") != 1
        or not isinstance(project_id, str)
        or not project_id.strip()
        or first.get("command_center_state_proven") is not True
        or first.get("bounded_projection_proven") is not True
        or first.get("bridge_state_project_id") != project_id
        or first.get("bridge_state_spec_version") != 1
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
    target.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def _hosted_windows_proof_enabled() -> bool:
    """Keep destructive-looking migration fixtures off developer machines.

    The proof intentionally starts a real frozen executable against temporary
    profile directories.  It belongs to the isolated GitHub-hosted Windows
    release gate, not to a normal local package build.
    """
    return (
        os.name == "nt"
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_legacy_database(path: Path) -> str:
    """Create a minimal real legacy store using the canonical SQLite schema."""
    from nika_core.data.sqlite import SQLiteStore
    from nika_core.kernel.task_queue import TaskQueue

    store = SQLiteStore(path)
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="packaged-upgrade",
        agent_id="migration-proof",
        payload={"kind": "packaged_data_adoption"},
    )
    return task.task_id


def _task_ids(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {str(row[0]) for row in connection.execute("SELECT task_id FROM tasks")}


def _run_packaged_pf11(
    executable: Path,
    *,
    output: Path,
    environment: dict[str, str],
    cwd: Path,
) -> dict[str, object]:
    completed = subprocess.run(
        [str(executable), "--pf11-proof", "--pf11-proof-output", str(output)],
        check=False,
        env=environment,
        cwd=cwd,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError("packaged data-adoption proof executable exited unsuccessfully")
    try:
        payload = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("packaged data-adoption proof did not emit JSON") from exc
    if not isinstance(payload, dict) or payload.get("route") != "product_project":
        raise RuntimeError("packaged data-adoption proof returned invalid PF11 evidence")
    return payload


def _close_packaged_recovery_dialog(
    process: subprocess.Popen,
    *,
    timeout_seconds: float = 10.0,
) -> None:
    """Close only the recovery dialog owned by the exact packaged child process."""
    if os.name != "nt":
        raise RuntimeError("packaged recovery dialog proof requires Windows")

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    enum_windows = user32.EnumWindows
    enum_windows.argtypes = [enum_proc, wintypes.LPARAM]
    enum_windows.restype = wintypes.BOOL
    get_pid = user32.GetWindowThreadProcessId
    get_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    get_pid.restype = wintypes.DWORD
    get_length = user32.GetWindowTextLengthW
    get_length.argtypes = [wintypes.HWND]
    get_length.restype = ctypes.c_int
    get_text = user32.GetWindowTextW
    get_text.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    get_text.restype = ctypes.c_int
    post_message = user32.PostMessageW
    post_message.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    post_message.restype = wintypes.BOOL

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("packaged conflict process exited before recovery dialog was observed")
        found: list[wintypes.HWND] = []

        @enum_proc
        def visit(
            hwnd: wintypes.HWND,
            _lparam: wintypes.LPARAM,
            found: list[wintypes.HWND] = found,
        ) -> bool:
            owner_pid = wintypes.DWORD()
            get_pid(hwnd, ctypes.byref(owner_pid))
            if owner_pid.value != process.pid:
                return True
            length = get_length(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            get_text(hwnd, buffer, length + 1)
            if buffer.value == _RECOVERY_DIALOG_TITLE:
                found.append(hwnd)
                return False
            return True

        enum_windows(visit, 0)
        if found:
            if not post_message(found[0], 0x0010, 0, 0):  # WM_CLOSE
                raise RuntimeError("packaged recovery dialog could not be closed")
            return
        time.sleep(0.05)
    raise RuntimeError("packaged recovery dialog was not observed before timeout")


def _run_packaged_conflict_refusal(
    executable: Path,
    *,
    output: Path,
    environment: dict[str, str],
    cwd: Path,
    legacy_database: Path,
    canonical_database: Path,
) -> None:
    """Require the frozen default-startup path to reject conflicting databases."""
    legacy_digest = _sha256(legacy_database)
    canonical_digest = _sha256(canonical_database)
    process = subprocess.Popen(
        [str(executable), "--pf11-proof", "--pf11-proof-output", str(output)],
        env=environment,
        cwd=cwd,
    )
    try:
        _close_packaged_recovery_dialog(process)
        returncode = process.wait(timeout=10)
    except Exception:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        raise
    if returncode != 1:
        raise RuntimeError("packaged existing-canonical conflict did not fail closed")
    if output.exists():
        raise RuntimeError("packaged conflict reached PF11 runtime after startup refusal")
    if _sha256(legacy_database) != legacy_digest or _sha256(canonical_database) != canonical_digest:
        raise RuntimeError("packaged conflict modified a legacy or canonical database")


def prove_packaged_data_adoption(bundle_dir: Path, *, source_sha: str) -> Path | None:
    """Exercise guarded legacy adoption through the real frozen Windows executable.

    This complements unit recovery tests with an isolated release-gate proof of
    the default-profile path.  It never scans real user directories and never
    runs outside a GitHub-hosted Windows runner.
    """
    if not _hosted_windows_proof_enabled():
        return None
    executable = bundle_dir / "NikaCore.exe"
    if not executable.is_file() or not _FULL_SHA_RE.fullmatch(source_sha):
        raise RuntimeError("packaged data-adoption proof requires its exact executable and SHA")

    from nika_core.data.sqlite import SQLiteStore
    from nika_core.kernel.task_queue import TaskQueue

    with tempfile.TemporaryDirectory(prefix="nika-packaged-data-upgrade-") as temporary:
        root = Path(temporary)
        profile = root / "profile"
        first_cwd = root / "legacy-launch"
        legacy = first_cwd / "data" / "nika_core.db"
        legacy_task = _create_legacy_database(legacy)
        source_digest = _sha256(legacy)
        environment = dict(os.environ)
        environment.pop("NIKA_DB_PATH", None)
        environment.pop("NIKA_DATABASE_PATH", None)
        environment["LOCALAPPDATA"] = str(profile)
        # platformdirs gives this documented process-local override precedence
        # over the runner's real user profile.  The packaged program still uses
        # its ordinary default-path code; only the hosted fixture is isolated.
        environment["WIN_PD_OVERRIDE_LOCAL_APPDATA"] = str(profile)
        canonical = profile / "NikaCore" / "nika_core.db"

        first = _run_packaged_pf11(
            executable,
            output=root / "first.json",
            environment=environment,
            cwd=first_cwd,
        )
        if not canonical.is_file() or _sha256(legacy) != source_digest:
            raise RuntimeError("packaged legacy adoption did not preserve source/default target")
        backups = canonical.parent / "legacy-adoption-backups"
        if len(list(backups.glob("*.sqlite3"))) < 2:
            raise RuntimeError("packaged legacy adoption did not retain verified backups")
        if legacy_task not in _task_ids(canonical):
            raise RuntimeError("packaged legacy adoption lost legacy task identity")
        new_task = TaskQueue(SQLiteStore(canonical)).create(
            workspace_id="packaged-upgrade",
            agent_id="post-adoption",
            payload={"kind": "new_work"},
        ).task_id
        different_cwd = root / "different-launch-directory"
        different_cwd.mkdir()
        second = _run_packaged_pf11(
            executable,
            output=root / "second.json",
            environment=environment,
            cwd=different_cwd,
        )
        if first != second or {legacy_task, new_task} - _task_ids(canonical):
            raise RuntimeError("packaged restart did not preserve adopted and newer work")

        override_root = root / "explicit-override"
        override_legacy = override_root / "legacy" / "data" / "nika_core.db"
        _create_legacy_database(override_legacy)
        override_digest = _sha256(override_legacy)
        override = override_root / "chosen" / "nika_core.db"
        override_environment = dict(environment)
        override_environment["LOCALAPPDATA"] = str(override_root / "profile")
        override_environment["NIKA_DB_PATH"] = str(override)
        _run_packaged_pf11(
            executable,
            output=override_root / "override.json",
            environment=override_environment,
            cwd=override_root / "legacy",
        )
        default_override_target = override_root / "profile" / "NikaCore" / "nika_core.db"
        if not override.is_file() or default_override_target.exists() or _sha256(override_legacy) != override_digest:
            raise RuntimeError("explicit database override did not bypass legacy adoption")

        conflict_cwd = root / "conflict-launch"
        conflict_legacy = conflict_cwd / "data" / "nika_core.db"
        _create_legacy_database(conflict_legacy)
        conflict_profile = root / "conflict-profile"
        conflict_target = conflict_profile / "NikaCore" / "nika_core.db"
        _create_legacy_database(conflict_target)
        conflict_environment = dict(environment)
        conflict_environment.pop("NIKA_DB_PATH", None)
        conflict_environment.pop("NIKA_DATABASE_PATH", None)
        conflict_environment["LOCALAPPDATA"] = str(conflict_profile)
        conflict_environment["WIN_PD_OVERRIDE_LOCAL_APPDATA"] = str(conflict_profile)
        _run_packaged_conflict_refusal(
            executable,
            output=root / "conflict.json",
            environment=conflict_environment,
            cwd=conflict_cwd,
            legacy_database=conflict_legacy,
            canonical_database=conflict_target,
        )

    target = bundle_dir / _DATA_ADOPTION_EVIDENCE_NAME
    target.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha": source_sha,
                "packaged_executable_proven": True,
                "legacy_cwd_adopted": True,
                "legacy_source_preserved": True,
                "verified_backups_retained": True,
                "different_cwd_restart_preserved_new_work": True,
                "explicit_database_override_bypassed_adoption": True,
                "existing_canonical_profile_refused": True,
                "human_tested": False,
                "nvda_verified": False,
                "production_release_ready": False,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
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
    prove_packaged_data_adoption(plan.bundle_dir, source_sha=exact_source_sha)
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
