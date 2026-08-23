from __future__ import annotations

import hashlib
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

PROJECT_ID = "pf3-proof-project"
AUDIENCE = "pf3-proof-audience"
SCOPE = "credential:proof"


def _expect_unknown_handle(store: WindowsCredentialStore, handle_ref: str, now: datetime) -> None:
    try:
        store.validate_handle(
            handle_ref=handle_ref,
            project_id=PROJECT_ID,
            audience=AUDIENCE,
            scope=SCOPE,
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
            store.delete_authority(secret_ref, generation)
        except ProtectedCredentialStoreError as exc:
            errors.append(f"generation-{generation}:{type(exc).__name__}")
    return errors


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("PF3 Windows credential proof requires Windows")

    secret_ref = "pf3-proof-" + uuid.uuid4().hex
    raw_secret = secrets.token_urlsafe(48)
    exact_windows_limit_material = "L" * 1280
    authority_1 = hashlib.sha256(b"pf3-proof-authority-generation-1").hexdigest()
    authority_2 = hashlib.sha256(b"pf3-proof-authority-generation-2").hexdigest()
    operation_id = "pf3-proof-operation-" + uuid.uuid4().hex
    store: WindowsCredentialStore | None = None
    restarted: WindowsCredentialStore | None = None
    proof_error: Exception | None = None
    cleanup_errors: list[str] = []

    try:
        store = create_windows_credential_store()
        store.provision_secret(secret_ref, 1, raw_secret)
        if not store.contains(secret_ref, 1):
            raise RuntimeError("new Windows credential is not readable by exact target")
        store.bind_authority(
            secret_ref=secret_ref,
            generation=1,
            authority_fingerprint=authority_1,
        )
        if not store.authority_matches(
            secret_ref=secret_ref,
            generation=1,
            authority_fingerprint=authority_1,
        ):
            raise RuntimeError("protected credential authority binding was not persisted")

        store.provision_secret(secret_ref, 1, raw_secret)
        expires_at = datetime.now(UTC) + timedelta(minutes=5)
        handle = store.issue_handle(
            operation_id=operation_id,
            secret_ref=secret_ref,
            generation=1,
            project_id=PROJECT_ID,
            audience=AUDIENCE,
            scopes=frozenset({SCOPE}),
            expires_at=expires_at,
        )
        retry_handle = store.issue_handle(
            operation_id=operation_id,
            secret_ref=secret_ref,
            generation=1,
            project_id=PROJECT_ID,
            audience=AUDIENCE,
            scopes=frozenset({SCOPE}),
            expires_at=expires_at,
        )
        if retry_handle != handle:
            raise RuntimeError("same credential operation produced a duplicate handle")
        reconciled = store.reconcile_handle(
            operation_id=operation_id,
            secret_ref=secret_ref,
            generation=1,
            project_id=PROJECT_ID,
            audience=AUDIENCE,
            scopes=frozenset({SCOPE}),
            expires_at=expires_at,
        )
        if reconciled != handle:
            raise RuntimeError("credential operation reconciliation returned wrong handle")
        receipt = store.validate_handle(
            handle_ref=handle,
            project_id=PROJECT_ID,
            audience=AUDIENCE,
            scope=SCOPE,
        )
        if receipt.secret_ref != secret_ref or receipt.generation != 1:
            raise RuntimeError("credential handle validation returned wrong identity")

        restarted = create_windows_credential_store()
        if not restarted.contains(secret_ref, 1):
            raise RuntimeError("Windows credential did not survive adapter restart")
        if not restarted.authority_matches(
            secret_ref=secret_ref,
            generation=1,
            authority_fingerprint=authority_1,
        ):
            raise RuntimeError("credential authority did not survive adapter restart")
        restarted.provision_secret(secret_ref, 1, raw_secret)
        _expect_generation_conflict(restarted, secret_ref)
        _expect_unknown_handle(restarted, handle, datetime.now(UTC))
        if restarted.reconcile_handle(
            operation_id=operation_id,
            secret_ref=secret_ref,
            generation=1,
            project_id=PROJECT_ID,
            audience=AUDIENCE,
            scopes=frozenset({SCOPE}),
            expires_at=expires_at,
        ) is not None:
            raise RuntimeError("process-ephemeral operation authority survived adapter restart")

        restarted.provision_secret(secret_ref, 2, exact_windows_limit_material)
        restarted.bind_authority(
            secret_ref=secret_ref,
            generation=2,
            authority_fingerprint=authority_2,
        )
        if not restarted.contains(secret_ref, 2):
            raise RuntimeError("maximum-size Windows credential was not readable")
        if not restarted.authority_matches(
            secret_ref=secret_ref,
            generation=2,
            authority_fingerprint=authority_2,
        ):
            raise RuntimeError("generation-two authority binding was not readable")
    except Exception as exc:  # noqa: BLE001
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
                "authority_binding_restart": "verified",
                "backend": "python-keyring WinVaultKeyring",
                "cleanup": "verified",
                "credential_blob_2560_bytes": "verified",
                "handle_operation_idempotency": "verified",
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
