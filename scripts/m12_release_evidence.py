from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from nika_core.packaging.release import (
    verify_distributable_evidence,
    verify_release_archive,
)

_INSTALLER_NAME = "install_nika_core.ps1"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Verify that M12 evidence binds the exact final distributable ZIP."
    )
    result.add_argument("--artifact", type=Path, required=True)
    result.add_argument("--evidence", type=Path, required=True)
    result.add_argument("--source-sha", required=True)
    result.add_argument("--artifact-reference", required=True)
    return result


def _powershell() -> str:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        raise RuntimeError("PowerShell is required for packaged installer lifecycle proof")
    return shell


def _run_checked(command: list[str], *, env: dict[str, str], label: str) -> None:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        env=env,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"{label} failed with exit {completed.returncode}: {detail}")


def _run_installer(
    shell: str,
    installer: Path,
    *,
    mode: str,
    destination: Path,
    env: dict[str, str],
    bundle: Path | None = None,
) -> None:
    command = [
        shell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(installer),
        "-Mode",
        mode,
        "-Destination",
        str(destination),
    ]
    if bundle is not None:
        command.extend(["-BundlePath", str(bundle)])
    _run_checked(command, env=env, label=f"packaged installer {mode}")


def _run_installed_pf11(executable: Path, output: Path, *, env: dict[str, str]) -> dict[str, object]:
    _run_checked(
        [
            str(executable),
            "--pf11-proof",
            "--pf11-proof-output",
            str(output),
        ],
        env=env,
        label="installed NikaCore.exe PF11 proof",
    )
    try:
        payload = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("installed NikaCore.exe did not emit valid PF11 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("installed NikaCore.exe PF11 evidence must be an object")
    project_id = payload.get("project_id")
    if (
        payload.get("route") != "product_project"
        or payload.get("spec_version") != 1
        or not isinstance(project_id, str)
        or not project_id.strip()
    ):
        raise RuntimeError("installed NikaCore.exe returned invalid PF11 route evidence")
    return payload


def prove_packaged_installer_lifecycle(artifact: Path) -> None:
    """Prove Install -> Update -> Rollback using only the exact final ZIP contents."""
    shell = _powershell()
    with tempfile.TemporaryDirectory(prefix="nika-m12-installer-") as temporary:
        root = Path(temporary)
        bundle = root / "exact-final-zip"
        bundle.mkdir()
        with zipfile.ZipFile(artifact) as archive:
            archive.extractall(bundle)

        installer = bundle / _INSTALLER_NAME
        executable = bundle / "NikaCore.exe"
        if not installer.is_file():
            raise RuntimeError("exact final ZIP does not contain the canonical installer")
        if not executable.is_file():
            raise RuntimeError("exact final ZIP does not contain NikaCore.exe")

        destination = root / "installed" / "Nika Core"
        rollback = destination.parent / f".{destination.name}.rollback"
        data_path = root / "durable-data" / "nika_core.db"
        environment = dict(os.environ)
        environment["NIKA_DB_PATH"] = str(data_path.resolve())

        _run_installer(
            shell,
            installer,
            mode="Install",
            destination=destination,
            bundle=bundle,
            env=environment,
        )
        installed_executable = destination / "NikaCore.exe"
        if not installed_executable.is_file():
            raise RuntimeError("Install succeeded without producing installed NikaCore.exe")
        install_proof = _run_installed_pf11(
            installed_executable,
            root / "pf11-install.json",
            env=environment,
        )

        _run_installer(
            shell,
            installer,
            mode="Update",
            destination=destination,
            bundle=bundle,
            env=environment,
        )
        if not rollback.is_dir():
            raise RuntimeError("Update succeeded without creating a rollback image")
        update_proof = _run_installed_pf11(
            destination / "NikaCore.exe",
            root / "pf11-update.json",
            env=environment,
        )

        installed_installer = destination / _INSTALLER_NAME
        if not installed_installer.is_file():
            raise RuntimeError("installed release does not retain the packaged installer")
        _run_installer(
            shell,
            installed_installer,
            mode="Rollback",
            destination=destination,
            env=environment,
        )
        if not rollback.is_dir():
            raise RuntimeError("Rollback did not preserve the replaced image for recovery")
        rollback_proof = _run_installed_pf11(
            destination / "NikaCore.exe",
            root / "pf11-rollback.json",
            env=environment,
        )

        stable_identity = {
            "route": install_proof.get("route"),
            "project_id": install_proof.get("project_id"),
            "spec_version": install_proof.get("spec_version"),
        }
        for label, proof in (("update", update_proof), ("rollback", rollback_proof)):
            candidate = {
                "route": proof.get("route"),
                "project_id": proof.get("project_id"),
                "spec_version": proof.get("spec_version"),
            }
            if candidate != stable_identity:
                raise RuntimeError(
                    f"packaged installer {label} changed durable ProductProject identity"
                )

        if not data_path.is_file():
            raise RuntimeError("packaged installer lifecycle did not preserve external durable data")


def main() -> int:
    args = parser().parse_args()
    findings = verify_distributable_evidence(
        args.artifact,
        args.evidence,
        source_sha=args.source_sha,
        artifact_reference=args.artifact_reference,
    )
    if not findings:
        findings = verify_release_archive(args.artifact, source_sha=args.source_sha)
    if findings:
        raise SystemExit("M12 distributable evidence verification failed: " + ", ".join(findings))
    if os.name == "nt":
        prove_packaged_installer_lifecycle(args.artifact)
        print("M12 packaged Install -> Update -> Rollback lifecycle verified")
    print("M12 distributable evidence verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
