from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.packaging.release import (
    build_release_manifest,
    verify_distributable_evidence,
    verify_release_archive,
    write_release_manifest,
)
from nika_core.product_project import ProductProjectRepository

_INSTALLER_NAME = "install_nika_core.ps1"
_UPGRADE_PROBE_NAME = "m12-byte-distinct-upgrade-proof.txt"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Verify that M12 evidence binds the exact final distributable ZIP."
    )
    result.add_argument("--artifact", type=Path, required=True)
    result.add_argument("--evidence", type=Path, required=True)
    result.add_argument("--source-sha", required=True)
    result.add_argument("--artifact-reference", required=True)
    result.add_argument("--product-version", required=True)
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


def _installer_command(
    shell: str,
    installer: Path,
    *,
    mode: str,
    destination: Path,
    bundle: Path | None = None,
    rollback_operation_id: str | None = None,
) -> list[str]:
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
    if rollback_operation_id is not None:
        if mode != "Rollback":
            raise ValueError("RollbackOperationId is only valid for Rollback mode")
        if (
            type(rollback_operation_id) is not str
            or len(rollback_operation_id) != 32
            or any(character not in "0123456789abcdef" for character in rollback_operation_id)
        ):
            raise ValueError(
                "RollbackOperationId must be exactly 32 lowercase hexadecimal characters"
            )
        command.extend(["-RollbackOperationId", rollback_operation_id])
    return command


def _run_installer(
    shell: str,
    installer: Path,
    *,
    mode: str,
    destination: Path,
    env: dict[str, str],
    bundle: Path | None = None,
    rollback_operation_id: str | None = None,
) -> None:
    _run_checked(
        _installer_command(
            shell,
            installer,
            mode=mode,
            destination=destination,
            bundle=bundle,
            rollback_operation_id=rollback_operation_id,
        ),
        env=env,
        label=f"packaged installer {mode}",
    )


def _run_installer_expect_process_failure(
    shell: str,
    installer: Path,
    *,
    destination: Path,
    env: dict[str, str],
    rollback_operation_id: str,
) -> None:
    completed = subprocess.run(
        _installer_command(
            shell,
            installer,
            mode="Rollback",
            destination=destination,
            rollback_operation_id=rollback_operation_id,
        ),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        env=env,
        timeout=120,
    )
    if completed.returncode == 0:
        raise RuntimeError("fault-injected packaged rollback unexpectedly completed successfully")


def _rollback_operation_id(
    source_manifest_sha: str,
    target_manifest_sha: str,
    *,
    label: str,
) -> str:
    material = (
        f"nika-m12-packaged-rollback-v1\0{source_manifest_sha}\0{target_manifest_sha}\0{label}"
    ).encode("ascii")
    return hashlib.sha256(material).hexdigest()[:32]


def _build_fault_injected_rollback_installer(source: Path, target: Path) -> Path:
    try:
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            script = handle.read()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("packaged rollback installer is unreadable") from exc

    newline = "\r\n" if "\r\n" in script else "\n"
    final_swap = (
        '        [System.IO.Directory]::Move($swapPath, $rollbackPath)'
        + newline
        + '        $rollbackPhase = "complete"'
    )
    if script.count(final_swap) != 1:
        raise RuntimeError("packaged installer rollback final-swap anchor is not unique")
    crash_line = (
        '        [System.Environment]::FailFast('
        '"M12 injected crash after rollback final swap")'
    )
    injected = (
        '        [System.IO.Directory]::Move($swapPath, $rollbackPath)'
        + newline
        + crash_line
        + newline
        + '        $rollbackPhase = "complete"'
    )
    try:
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(script.replace(final_swap, injected))
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(
            "fault-injected packaged rollback installer could not be written"
        ) from exc
    return target


def _require_rollback_operation_marker(
    marker_path: Path,
    *,
    operation_id: str,
    source_digest: str,
    target_digest: str,
) -> None:
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("packaged rollback did not preserve a valid operation marker") from exc
    expected = {
        "marker_version": 1,
        "operation_id": operation_id,
        "source_digest": source_digest,
        "target_digest": target_digest,
    }
    if type(payload) is not dict or payload != expected:
        raise RuntimeError("packaged rollback operation marker does not match exact image authority")


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
        raise TypeError("installed NikaCore.exe PF11 evidence must be an object")
    project_id = payload.get("project_id")
    if (
        payload.get("route") != "product_project"
        or payload.get("spec_version") != 1
        or not isinstance(project_id, str)
        or not project_id.strip()
    ):
        raise RuntimeError("installed NikaCore.exe returned invalid PF11 route evidence")
    return payload


def _read_durable_project_witness(
    data_path: Path,
    proof: dict[str, object],
) -> tuple[str, int, str, str]:
    """Bind PF11 evidence to the already-durable canonical ProductProject row."""
    if not data_path.is_file():
        raise RuntimeError("packaged installer lifecycle durable database is missing")
    project_id = proof.get("project_id")
    spec_version = proof.get("spec_version")
    if (
        not isinstance(project_id, str)
        or not project_id.strip()
        or not isinstance(spec_version, int)
        or isinstance(spec_version, bool)
        or spec_version < 1
    ):
        raise RuntimeError("PF11 evidence has invalid durable ProductProject identity")

    store = SQLiteStore(data_path)
    store.initialize()
    try:
        project = ProductProjectRepository(store).get(project_id)
    except KeyError as exc:
        raise RuntimeError("PF11 ProductProject is missing from the durable database") from exc
    if project.project_id != project_id or project.spec_version != spec_version:
        raise RuntimeError("PF11 evidence does not match the durable ProductProject row")
    if not project.spec.goal.strip() or not project.created_at.strip():
        raise RuntimeError("durable ProductProject continuity witness is incomplete")
    return (
        project.project_id,
        project.spec_version,
        project.spec.goal,
        project.created_at,
    )


def _require_durable_project_continuity(
    expected: tuple[str, int, str, str],
    data_path: Path,
    proof: dict[str, object],
    *,
    phase: str,
) -> tuple[str, int, str, str]:
    current = _read_durable_project_witness(data_path, proof)
    if current != expected:
        raise RuntimeError(
            f"packaged installer {phase} did not preserve the durable ProductProject row"
        )
    return current


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_acceptance_upgrade_bundle(source: Path, target: Path) -> Path:
    """Create one controlled byte-distinct package for installer transition evidence."""
    shutil.copytree(source, target)
    manifest_path = target / "release-manifest.json"
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("exact final ZIP contains an unreadable release manifest") from exc
    if not isinstance(raw_manifest, dict):
        raise TypeError("exact final ZIP release manifest must be an object")

    product = raw_manifest.get("product")
    version = raw_manifest.get("version")
    source_sha = raw_manifest.get("source_sha")
    if not all(isinstance(value, str) and value.strip() for value in (product, version, source_sha)):
        raise RuntimeError("exact final ZIP release manifest has invalid identity fields")

    probe = target / _UPGRADE_PROBE_NAME
    probe.write_text("M12 byte-distinct installer transition proof\n", encoding="utf-8")
    manifest = build_release_manifest(
        target,
        product=product,
        version=f"{version}-m12-transition-proof",
        source_sha=source_sha,
    )
    write_release_manifest(target, manifest)
    return probe


def prove_packaged_installer_lifecycle(artifact: Path) -> None:
    """Prove byte-distinct Install -> Update -> Rollback from the exact final ZIP."""
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

        upgrade_bundle = root / "byte-distinct-upgrade"
        upgrade_probe = _build_acceptance_upgrade_bundle(bundle, upgrade_bundle)
        exact_manifest_sha = _sha256(bundle / "release-manifest.json")
        upgrade_manifest_sha = _sha256(upgrade_bundle / "release-manifest.json")
        if exact_manifest_sha == upgrade_manifest_sha:
            raise RuntimeError("acceptance upgrade package is not byte-distinct")

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
        install_witness = _read_durable_project_witness(data_path, install_proof)
        if _sha256(destination / "release-manifest.json") != exact_manifest_sha:
            raise RuntimeError("Install did not preserve exact final package identity")
        if (destination / _UPGRADE_PROBE_NAME).exists():
            raise RuntimeError("Install unexpectedly contains the upgrade proof marker")

        _run_installer(
            shell,
            installer,
            mode="Update",
            destination=destination,
            bundle=upgrade_bundle,
            env=environment,
        )
        if not rollback.is_dir():
            raise RuntimeError("Update succeeded without creating a rollback image")
        if _sha256(destination / "release-manifest.json") != upgrade_manifest_sha:
            raise RuntimeError("Update did not activate the byte-distinct package identity")
        if _sha256(rollback / "release-manifest.json") != exact_manifest_sha:
            raise RuntimeError("Update rollback image did not preserve exact original identity")
        if not (destination / upgrade_probe.name).is_file():
            raise RuntimeError("Update did not activate the byte-distinct proof marker")
        if (rollback / upgrade_probe.name).exists():
            raise RuntimeError("original rollback image contains the upgrade proof marker")
        update_proof = _run_installed_pf11(
            destination / "NikaCore.exe",
            root / "pf11-update.json",
            env=environment,
        )
        _require_durable_project_continuity(
            install_witness,
            data_path,
            update_proof,
            phase="Update",
        )

        installed_installer = destination / _INSTALLER_NAME
        if not installed_installer.is_file():
            raise RuntimeError("installed release does not retain the packaged installer")

        rollback_marker = destination.parent / f".{destination.name}.rollback-operation.json"
        rollback_swap = destination.parent / f".{destination.name}.rollback-swap"
        rollback_operation_id = _rollback_operation_id(
            upgrade_manifest_sha,
            exact_manifest_sha,
            label="crash-retry",
        )
        fault_installer = _build_fault_injected_rollback_installer(
            installed_installer,
            root / "install_nika_core.rollback-crash.ps1",
        )
        _run_installer_expect_process_failure(
            shell,
            fault_installer,
            destination=destination,
            env=environment,
            rollback_operation_id=rollback_operation_id,
        )

        if rollback_swap.exists():
            raise RuntimeError("fault-injected rollback left an unresolved swap image")
        if not rollback.is_dir():
            raise RuntimeError("fault-injected rollback did not preserve the replaced image")
        if _sha256(destination / "release-manifest.json") != exact_manifest_sha:
            raise RuntimeError("fault-injected rollback did not reach the committed target image")
        if _sha256(rollback / "release-manifest.json") != upgrade_manifest_sha:
            raise RuntimeError("fault-injected rollback did not retain the committed source image")
        _require_rollback_operation_marker(
            rollback_marker,
            operation_id=rollback_operation_id,
            source_digest=upgrade_manifest_sha,
            target_digest=exact_manifest_sha,
        )

        _run_installer(
            shell,
            destination / _INSTALLER_NAME,
            mode="Rollback",
            destination=destination,
            env=environment,
            rollback_operation_id=rollback_operation_id,
        )
        if _sha256(destination / "release-manifest.json") != exact_manifest_sha:
            raise RuntimeError("same-ID rollback retry performed a second image swap")
        if _sha256(rollback / "release-manifest.json") != upgrade_manifest_sha:
            raise RuntimeError("same-ID rollback retry changed the retained source image")
        if (destination / upgrade_probe.name).exists():
            raise RuntimeError("Rollback left the upgrade proof marker in the active image")
        if not (rollback / upgrade_probe.name).is_file():
            raise RuntimeError("Rollback image did not retain the replaced upgrade proof marker")
        _require_rollback_operation_marker(
            rollback_marker,
            operation_id=rollback_operation_id,
            source_digest=upgrade_manifest_sha,
            target_digest=exact_manifest_sha,
        )

        rollback_proof = _run_installed_pf11(
            destination / "NikaCore.exe",
            root / "pf11-rollback.json",
            env=environment,
        )
        _require_durable_project_continuity(
            install_witness,
            data_path,
            rollback_proof,
            phase="Rollback",
        )

        reverse_operation_id = _rollback_operation_id(
            exact_manifest_sha,
            upgrade_manifest_sha,
            label="distinct-reverse",
        )
        if reverse_operation_id == rollback_operation_id:
            raise RuntimeError("distinct rollback proof operation IDs collided")
        _run_installer(
            shell,
            destination / _INSTALLER_NAME,
            mode="Rollback",
            destination=destination,
            env=environment,
            rollback_operation_id=reverse_operation_id,
        )
        if _sha256(destination / "release-manifest.json") != upgrade_manifest_sha:
            raise RuntimeError("distinct rollback operation did not perform exactly one reverse")
        if _sha256(rollback / "release-manifest.json") != exact_manifest_sha:
            raise RuntimeError("distinct rollback operation did not retain the prior active image")
        if not (destination / upgrade_probe.name).is_file():
            raise RuntimeError("distinct rollback operation did not reactivate the upgrade marker")
        if (rollback / upgrade_probe.name).exists():
            raise RuntimeError("distinct rollback operation left the upgrade marker in both images")
        _require_rollback_operation_marker(
            rollback_marker,
            operation_id=reverse_operation_id,
            source_digest=exact_manifest_sha,
            target_digest=upgrade_manifest_sha,
        )
        reverse_proof = _run_installed_pf11(
            destination / "NikaCore.exe",
            root / "pf11-distinct-reverse.json",
            env=environment,
        )
        _require_durable_project_continuity(
            install_witness,
            data_path,
            reverse_proof,
            phase="Distinct rollback reverse",
        )

        stable_identity = {
            "route": install_proof.get("route"),
            "project_id": install_proof.get("project_id"),
            "spec_version": install_proof.get("spec_version"),
        }
        for label, proof in (
            ("update", update_proof),
            ("rollback", rollback_proof),
            ("distinct rollback reverse", reverse_proof),
        ):
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
        expected_product_version=args.product_version,
    )
    if not findings:
        findings = verify_release_archive(
            args.artifact,
            source_sha=args.source_sha,
            expected_product_version=args.product_version,
        )
    if findings:
        raise SystemExit("M12 distributable evidence verification failed: " + ", ".join(findings))
    if os.name == "nt":
        prove_packaged_installer_lifecycle(args.artifact)
        print("M12 packaged Install -> Update -> Rollback lifecycle verified")
    print("M12 distributable evidence verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
