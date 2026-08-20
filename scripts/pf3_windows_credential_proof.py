from __future__ import annotations

import json
import secrets
import sys
import uuid
from datetime import UTC, datetime, timedelta

from nika_core.product_factory_windows_credentials import (
    ProtectedCredentialStoreError,
    WindowsCredentialStore,
    create_windows_credential_store,
)


def _expect_unknown_handle(store: WindowsCredentialStore, handle_ref: str, now: datetime) -> None:
    try:
        store.validate_handle(
            handle_ref=handle_ref,
            project_id="pf3-proof-project",
            audience="pf3-proof-audience",
            scope="credential:proof",
            now=now,
        )
    except ProtectedCredentialStoreError as exc:
        if "unknown or invalidated" not in str(exc):
            raise RuntimeError("restart rejected handle for an unexpected reason") from exc
        return
    raise RuntimeError("process-ephemeral credential handle survived store restart")


def _expect_generation_conflict(store: WindowsCredentialStore, secret_ref: str) -> None:
    try:
        store.provision_secret(secret_ref, 1, "known-different-proof-material")
    except ProtectedCredentialStoreError as exc:
        if "rotate generation" not in str(exc):
            raise RuntimeError(
                "credential overwrite was rejected for an unexpected reason"
            ) from exc
        return
    raise RuntimeError("same-generation credential overwrite unexpectedly succeeded")


def _cleanup(store: WindowsCredentialStore | None, secret_ref: str) -> list[str]:
    if store is None:
        return []
    errors: list[str] = []
    for generation in (1, 2):
        try:
            store.delete_secret(secret_ref, generation)
        except ProtectedCredentialStoreError as exc:
            errors.append(f"generation-{generation}:{type(exc).__name__}")
    return errors


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("PF3 Windows credential proof requires Windows")

    secret_ref = "pf3-proof-" + uuid.uuid4().hex
    raw_secret = secrets.token_urlsafe(48)
    exact_windows_limit_material = "L" * 1280
    store: WindowsCredentialStore | None = None
    restarted: WindowsCredentialStore | None = None
    proof_error: BaseException | None = None
    cleanup_errors: list[str] = []

    try:
        store = create_windows_credential_store()
        store.provision_secret(secret_ref, 1, raw_secret)
        if not store.contains(secret_ref, 1):
            raise RuntimeError("new Windows credential is not readable by exact target")

        store.provision_secret(secret_ref, 1, raw_secret)
        handle = store.issue_handle(
            secret_ref=secret_ref,
            generation=1,
            project_id="pf3-proof-project",
            audience="pf3-proof-audience",
            scopes=frozenset({"credential:proof"}),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        receipt = store.validate_handle(
            handle_ref=handle,
            project_id="pf3-proof-project",
            audience="pf3-proof-audience",
            scope="credential:proof",
        )
        if receipt.secret_ref != secret_ref or receipt.generation != 1:
            raise RuntimeError("credential handle validation returned wrong identity")

        restarted = create_windows_credential_store()
        if not restarted.contains(secret_ref, 1):
            raise RuntimeError("Windows credential did not survive adapter restart")
        restarted.provision_secret(secret_ref, 1, raw_secret)
        _expect_generation_conflict(restarted, secret_ref)
        _expect_unknown_handle(restarted, handle, datetime.now(UTC))

        restarted.provision_secret(secret_ref, 2, exact_windows_limit_material)
        if not restarted.contains(secret_ref, 2):
            raise RuntimeError("maximum-size Windows credential was not readable")
    except BaseException as exc:
        proof_error = exc
    finally:
        cleanup_errors.extend(_cleanup(restarted or store, secret_ref))
        if restarted is not None and store is not None and restarted is not store:
            cleanup_errors.extend(_cleanup(store, secret_ref))

    raw_secret = ""
    exact_windows_limit_material = ""
    if proof_error is not None:
        raise proof_error
    if cleanup_errors:
        raise RuntimeError(
            "PF3 Windows credential proof cleanup failed: " + ",".join(cleanup_errors)
        )

    print(
        json.dumps(
            {
                "backend": "python-keyring WinVaultKeyring",
                "cleanup": "verified",
                "credential_blob_2560_bytes": "verified",
                "handle_restart_invalidation": "verified",
                "persistence": "local_machine",
                "raw_secret_output": False,
                "status": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
