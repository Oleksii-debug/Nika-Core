from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_deployment import (
    DeploymentFabricError,
    DeploymentIntent,
    DeploymentState,
)
from nika_core.runtime.idempotency import (
    IdempotencyLedger,
    IdempotencyStatus,
)

_OPERATION_TYPE = "product_factory.deployment"
_OPERATION_PREFIX = "pf6.deploy.v1"
_HOST_KIND = "product_factory"


@dataclass(slots=True)
class SQLiteDeploymentEffectJournal:
    """Durable PF6 side-effect reservation over Nika's canonical SQLite ledger.

    The journal owns no deployment state database. It reuses ``idempotency_records`` and
    binds every reservation to an existing Product Factory host task. A reservation is
    committed before the provider is called, so an OS/process loss cannot make the same
    exact intent look safe to dispatch again.
    """

    store: SQLiteStore
    host_task_id: str
    _ledger: IdempotencyLedger = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.host_task_id.strip():
            raise DeploymentFabricError(
                "deployment effect journal host task id must not be empty"
            )
        self._ledger = IdempotencyLedger(self.store)

    def before_effect(self, intent: DeploymentIntent) -> bool:
        """Reserve the exact deployment effect and report whether dispatch is new."""
        operation_key = _operation_key(intent)
        environment_prefix = _environment_prefix(intent)
        input_fingerprint = _intent_fingerprint(intent)
        with self.store.connection() as conn:
            # Serialize environment conflict detection with reservation. A second
            # process cannot reserve a different deployment for the same environment
            # between these two checks.
            conn.execute("BEGIN IMMEDIATE")
            _require_host_task(
                conn,
                host_task_id=self.host_task_id,
                project_id=intent.project_id,
            )
            rows = conn.execute(
                """
                SELECT operation_key
                FROM idempotency_records
                WHERE task_id = ?
                  AND operation_type = ?
                  AND operation_key LIKE ?
                  AND status IN (?, ?)
                """,
                (
                    self.host_task_id,
                    _OPERATION_TYPE,
                    f"{environment_prefix}%",
                    IdempotencyStatus.PENDING.value,
                    IdempotencyStatus.UNCERTAIN.value,
                ),
            ).fetchall()
            conflicting = {
                str(row["operation_key"])
                for row in rows
                if str(row["operation_key"]) != operation_key
            }
            if conflicting:
                raise DeploymentFabricError(
                    "environment has a durable unresolved deployment effect"
                )
            _record, created = self._ledger.reserve_with_connection(
                conn,
                operation_key=operation_key,
                task_id=self.host_task_id,
                operation_type=_OPERATION_TYPE,
                input_fingerprint=input_fingerprint,
            )
            return created

    def mark_uncertain(self, intent: DeploymentIntent) -> None:
        operation_key = _operation_key(intent)
        record = self._ledger.require(operation_key)
        self._validate_record_owner(record, intent)
        if record.status is IdempotencyStatus.PENDING:
            self._ledger.mark_uncertain(operation_key)
        elif record.status is IdempotencyStatus.UNCERTAIN:
            return
        else:
            raise DeploymentFabricError(
                "completed deployment effect cannot be reopened as uncertain"
            )

    def complete(self, intent: DeploymentIntent, state: DeploymentState) -> None:
        if state not in {
            DeploymentState.HEALTHY,
            DeploymentState.REJECTED,
            DeploymentState.ROLLED_BACK,
        }:
            raise DeploymentFabricError(
                "deployment effect journal can complete only a terminal deployment state"
            )
        operation_key = _operation_key(intent)
        record = self._ledger.require(operation_key)
        self._validate_record_owner(record, intent)
        summary = {
            "state": state.value,
            "project_id": intent.project_id,
            "environment_id": intent.environment.environment_id,
            "release_version": intent.release.version,
            "release_sha": intent.release.source_sha,
            "artifact_digest": intent.release.artifact_digest,
        }
        if record.status is IdempotencyStatus.PENDING:
            self._ledger.complete(operation_key, summary)
            return
        if record.status is IdempotencyStatus.UNCERTAIN:
            self._ledger.reconcile_completed(operation_key, summary)
            return
        if dict(record.result or {}) != summary:
            raise DeploymentFabricError(
                "completed deployment effect journal result conflicts with terminal state"
            )

    def _validate_record_owner(self, record: object, intent: DeploymentIntent) -> None:
        task_id = getattr(record, "task_id", None)
        operation_type = getattr(record, "operation_type", None)
        input_fingerprint = getattr(record, "input_fingerprint", None)
        if task_id != self.host_task_id or operation_type != _OPERATION_TYPE:
            raise DeploymentFabricError(
                "deployment effect journal record belongs to another host authority"
            )
        if input_fingerprint != _intent_fingerprint(intent):
            raise DeploymentFabricError(
                "deployment effect journal record does not match exact deployment intent"
            )
        with self.store.connection() as conn:
            _require_host_task(
                conn,
                host_task_id=self.host_task_id,
                project_id=intent.project_id,
            )


def _operation_key(intent: DeploymentIntent) -> str:
    return f"{_environment_prefix(intent)}{_intent_fingerprint(intent)}"


def _environment_prefix(intent: DeploymentIntent) -> str:
    environment = hashlib.sha256(
        (
            intent.project_id
            + "\0"
            + intent.environment.environment_id
            + "\0"
            + intent.environment.provider_ref
        ).encode("utf-8")
    ).hexdigest()
    return f"{_OPERATION_PREFIX}:{environment}:"


def _intent_fingerprint(intent: DeploymentIntent) -> str:
    payload = {
        "intent_id": intent.intent_id,
        "project_id": intent.project_id,
        "environment": {
            "environment_id": intent.environment.environment_id,
            "project_id": intent.environment.project_id,
            "tier": intent.environment.tier.value,
            "provider_ref": intent.environment.provider_ref,
        },
        "release": {
            "project_id": intent.release.project_id,
            "version": intent.release.version,
            "source_sha": intent.release.source_sha,
            "artifact_digest": intent.release.artifact_digest,
        },
        "migration_refs": list(intent.migration_refs),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_host_task(
    conn: sqlite3.Connection,
    *,
    host_task_id: str,
    project_id: str,
) -> None:
    row = conn.execute(
        "SELECT payload_json FROM tasks WHERE task_id = ?",
        (host_task_id,),
    ).fetchone()
    if row is None:
        raise DeploymentFabricError("Product Factory deployment host task does not exist")
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError) as exc:
        raise DeploymentFabricError(
            "Product Factory deployment host task payload is invalid"
        ) from exc
    if not isinstance(payload, dict) or payload.get("kind") != _HOST_KIND:
        raise DeploymentFabricError(
            "deployment host task is not explicitly typed as Product Factory orchestration"
        )
    if payload.get("product_project_id") != project_id:
        raise DeploymentFabricError(
            "deployment host task ProductProject identity does not match deployment intent"
        )
