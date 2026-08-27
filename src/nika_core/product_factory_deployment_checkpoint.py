from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, NoReturn

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentFabricSnapshot,
    DeploymentIntent,
    DeploymentProviderPort,
    DeploymentRecord,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    HealthEvidence,
    ReleaseRef,
    RollbackEvidence,
)

_STAGE = "product_factory.deployment.v1"
_SCHEMA = "nika-product-factory-deployment-checkpoint-v1"
_HOST_KIND = "product_factory"


class ProductFactoryDeploymentCheckpointError(ValueError):
    """Raised when durable PF6 deployment checkpoint invariants are violated."""


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _reject_non_finite(_value: str) -> NoReturn:
    raise ValueError("deployment checkpoint payload contains a non-finite number")


def _decode_checkpoint_payload(payload_json: object, checksum_sha256: object) -> dict[str, object]:
    if not isinstance(payload_json, str) or not isinstance(checksum_sha256, str):
        raise ProductFactoryDeploymentCheckpointError(
            "deployment checkpoint durable fields must be text"
        )
    checksum = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    if checksum != checksum_sha256:
        raise ProductFactoryDeploymentCheckpointError(
            "deployment checkpoint checksum mismatch"
        )
    try:
        payload = json.loads(payload_json, parse_constant=_reject_non_finite)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProductFactoryDeploymentCheckpointError(
            "deployment checkpoint payload is not valid finite JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ProductFactoryDeploymentCheckpointError(
            "deployment checkpoint payload must be a JSON object"
        )
    if _canonical_json(payload) != payload_json:
        raise ProductFactoryDeploymentCheckpointError(
            "deployment checkpoint payload is not canonical JSON"
        )
    return payload


class ProductFactoryDeploymentCheckpointHost:
    """Thin PF6 adapter over Nika's canonical task/checkpoint SQLite authority."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store
        self._checkpoints = CheckpointService(store)

    def save(
        self,
        *,
        host_task_id: str,
        project_id: str,
        snapshot: DeploymentFabricSnapshot,
    ) -> str:
        self._require_host_task(host_task_id=host_task_id, project_id=project_id)
        _validate_snapshot_project(snapshot, project_id)
        checkpoint = self._checkpoints.save(
            task_id=host_task_id,
            stage=_STAGE,
            payload={
                "schema": _SCHEMA,
                "project_id": project_id,
                "snapshot": _encode_snapshot(snapshot),
            },
        )
        return checkpoint.checkpoint_id

    def latest_snapshot(
        self,
        *,
        host_task_id: str,
        project_id: str,
    ) -> DeploymentFabricSnapshot | None:
        self._require_host_task(host_task_id=host_task_id, project_id=project_id)
        with self._store.connection() as conn:
            row = conn.execute(
                """
                SELECT payload_json, checksum_sha256
                FROM checkpoints
                WHERE task_id = ? AND stage = ?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (host_task_id, _STAGE),
            ).fetchone()
        if row is None:
            return None
        payload = _decode_checkpoint_payload(
            row["payload_json"],
            row["checksum_sha256"],
        )
        if payload.get("schema") != _SCHEMA:
            raise ProductFactoryDeploymentCheckpointError(
                "unsupported Product Factory deployment checkpoint schema"
            )
        if payload.get("project_id") != project_id:
            raise ProductFactoryDeploymentCheckpointError(
                "deployment checkpoint project does not match host task project"
            )
        try:
            snapshot = _decode_snapshot(payload["snapshot"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductFactoryDeploymentCheckpointError(
                "deployment checkpoint snapshot is invalid"
            ) from exc
        _validate_snapshot_project(snapshot, project_id)
        return snapshot

    def restore_latest(
        self,
        *,
        host_task_id: str,
        project_id: str,
        fabric: DeploymentFabric,
    ) -> bool:
        snapshot = self.latest_snapshot(
            host_task_id=host_task_id,
            project_id=project_id,
        )
        if snapshot is None:
            return False
        fabric.restore(snapshot)
        return True

    def _require_host_task(self, *, host_task_id: str, project_id: str) -> None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT payload_json FROM tasks WHERE task_id = ?",
                (host_task_id,),
            ).fetchone()
        if row is None:
            raise ProductFactoryDeploymentCheckpointError(
                "Product Factory host task does not exist"
            )
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError) as exc:
            raise ProductFactoryDeploymentCheckpointError(
                "Product Factory host task payload is invalid"
            ) from exc
        if not isinstance(payload, dict) or payload.get("kind") != _HOST_KIND:
            raise ProductFactoryDeploymentCheckpointError(
                "host task is not explicitly typed as Product Factory orchestration"
            )
        if payload.get("product_project_id") != project_id:
            raise ProductFactoryDeploymentCheckpointError(
                "host task ProductProject identity does not match deployment project"
            )


class DurableDeploymentFabric(DeploymentFabric):
    """DeploymentFabric whose state transition saves are task-anchored and durable."""

    def __init__(
        self,
        provider: DeploymentProviderPort,
        *,
        checkpoint_host: ProductFactoryDeploymentCheckpointHost,
        host_task_id: str,
        project_id: str,
    ) -> None:
        super().__init__(provider)
        if not host_task_id.strip() or not project_id.strip():
            raise ProductFactoryDeploymentCheckpointError(
                "durable deployment host/task identity must not be empty"
            )
        self._deployment_checkpoint_host = checkpoint_host
        self._deployment_host_task_id = host_task_id
        self._deployment_project_id = project_id

    def _save(self, record: DeploymentRecord) -> DeploymentRecord:
        if record.intent.project_id != self._deployment_project_id:
            raise ProductFactoryDeploymentCheckpointError(
                "deployment record project does not match durable host project"
            )
        saved = super()._save(record)
        self._deployment_checkpoint_host.save(
            host_task_id=self._deployment_host_task_id,
            project_id=self._deployment_project_id,
            snapshot=self.snapshot(),
        )
        return saved

    @classmethod
    def restore_latest(
        cls,
        provider: DeploymentProviderPort,
        *,
        checkpoint_host: ProductFactoryDeploymentCheckpointHost,
        host_task_id: str,
        project_id: str,
    ) -> DurableDeploymentFabric:
        fabric = cls(
            provider,
            checkpoint_host=checkpoint_host,
            host_task_id=host_task_id,
            project_id=project_id,
        )
        checkpoint_host.restore_latest(
            host_task_id=host_task_id,
            project_id=project_id,
            fabric=fabric,
        )
        return fabric


def _validate_snapshot_project(
    snapshot: DeploymentFabricSnapshot,
    project_id: str,
) -> None:
    for record in snapshot.records:
        if record.intent.project_id != project_id:
            raise ProductFactoryDeploymentCheckpointError(
                "deployment checkpoint contains another project"
            )
    for entry in snapshot.healthy_staging:
        if not entry or entry[0] != project_id:
            raise ProductFactoryDeploymentCheckpointError(
                "deployment staging checkpoint contains another project"
            )
    for entry in snapshot.current_releases:
        if not entry or entry[0] != project_id:
            raise ProductFactoryDeploymentCheckpointError(
                "deployment current-release checkpoint contains another project"
            )
    for entry in snapshot.exact_healthy_staging:
        if not entry or entry[0] != project_id:
            raise ProductFactoryDeploymentCheckpointError(
                "deployment exact staging checkpoint contains another project"
            )
    for entry in snapshot.exact_current_releases:
        if not entry or entry[0] != project_id:
            raise ProductFactoryDeploymentCheckpointError(
                "deployment exact current-release checkpoint contains another project"
            )


def _encode_snapshot(snapshot: DeploymentFabricSnapshot) -> dict[str, object]:
    return {
        "records": [_encode_record(record) for record in snapshot.records],
        "healthy_staging": [list(entry) for entry in snapshot.healthy_staging],
        "current_releases": [list(entry) for entry in snapshot.current_releases],
        "exact_healthy_staging": [
            list(entry) for entry in snapshot.exact_healthy_staging
        ],
        "exact_current_releases": [
            list(entry) for entry in snapshot.exact_current_releases
        ],
    }


def _decode_snapshot(value: Any) -> DeploymentFabricSnapshot:
    data = _mapping(value, "snapshot")
    records = data.get("records")
    staging = data.get("healthy_staging")
    current = data.get("current_releases")
    exact_staging = data.get("exact_healthy_staging", [])
    exact_current = data.get("exact_current_releases", [])
    if (
        not isinstance(records, list)
        or not isinstance(staging, list)
        or not isinstance(current, list)
        or not isinstance(exact_staging, list)
        or not isinstance(exact_current, list)
    ):
        raise TypeError("deployment snapshot collections must be lists")
    return DeploymentFabricSnapshot(
        tuple(_decode_record(item) for item in records),
        tuple(_text_tuple(item, "healthy staging entry") for item in staging),
        tuple(_text_tuple(item, "current release entry") for item in current),
        tuple(
            _text_tuple(item, "exact healthy staging entry")
            for item in exact_staging
        ),
        tuple(
            _text_tuple(item, "exact current release entry")
            for item in exact_current
        ),
    )


def _encode_record(record: DeploymentRecord) -> dict[str, object]:
    return {
        "intent": _encode_intent(record.intent),
        "state": record.state.value,
        "provider_evidence_refs": list(record.provider_evidence_refs),
        "health": None if record.health is None else _encode_health(record.health),
        "rollback": None if record.rollback is None else _encode_rollback(record.rollback),
        "previous_release_sha": record.previous_release_sha,
        "previous_release": _encode_release(record.previous_release),
    }


def _decode_record(value: Any) -> DeploymentRecord:
    data = _mapping(value, "deployment record")
    return DeploymentRecord(
        intent=_decode_intent(data["intent"]),
        state=DeploymentState(_text(data, "state")),
        provider_evidence_refs=_text_tuple(
            data.get("provider_evidence_refs", []),
            "evidence refs",
        ),
        health=None if data.get("health") is None else _decode_health(data["health"]),
        rollback=(
            None if data.get("rollback") is None else _decode_rollback(data["rollback"])
        ),
        previous_release_sha=_optional_text(data.get("previous_release_sha")),
        previous_release=_decode_optional_release(data.get("previous_release")),
    )


def _encode_intent(intent: DeploymentIntent) -> dict[str, object]:
    return {
        "intent_id": intent.intent_id,
        "project_id": intent.project_id,
        "environment": {
            "environment_id": intent.environment.environment_id,
            "project_id": intent.environment.project_id,
            "tier": intent.environment.tier.value,
            "provider_ref": intent.environment.provider_ref,
        },
        "release": _encode_release(intent.release),
        "migration_refs": list(intent.migration_refs),
    }


def _decode_intent(value: Any) -> DeploymentIntent:
    data = _mapping(value, "deployment intent")
    environment = _mapping(data["environment"], "environment")
    release = _decode_release(data["release"])
    return DeploymentIntent(
        intent_id=_text(data, "intent_id"),
        project_id=_text(data, "project_id"),
        environment=EnvironmentIdentity(
            _text(environment, "environment_id"),
            _text(environment, "project_id"),
            EnvironmentTier(_text(environment, "tier")),
            _text(environment, "provider_ref"),
        ),
        release=release,
        migration_refs=_text_tuple(
            data.get("migration_refs", []),
            "migration refs",
        ),
    )


def _encode_release(release: ReleaseRef | None) -> dict[str, object] | None:
    if release is None:
        return None
    return {
        "project_id": release.project_id,
        "version": release.version,
        "source_sha": release.source_sha,
        "artifact_digest": release.artifact_digest,
    }


def _decode_optional_release(value: Any) -> ReleaseRef | None:
    return None if value is None else _decode_release(value)


def _decode_release(value: Any) -> ReleaseRef:
    data = _mapping(value, "release")
    return ReleaseRef(
        _text(data, "project_id"),
        _text(data, "version"),
        _text(data, "source_sha"),
        _text(data, "artifact_digest"),
    )


def _encode_health(health: HealthEvidence) -> dict[str, object]:
    return {
        "environment_id": health.environment_id,
        "release_sha": health.release_sha,
        "healthy": health.healthy,
        "evidence_refs": list(health.evidence_refs),
        "checked_at": health.checked_at.isoformat(),
        "release": _encode_release(health.release),
    }


def _decode_health(value: Any) -> HealthEvidence:
    data = _mapping(value, "health")
    healthy = data.get("healthy")
    if not isinstance(healthy, bool):
        raise TypeError("health healthy must be boolean")
    return HealthEvidence(
        _text(data, "environment_id"),
        _text(data, "release_sha"),
        healthy,
        _text_tuple(data.get("evidence_refs", []), "health evidence refs"),
        datetime.fromisoformat(_text(data, "checked_at")),
        release=_decode_optional_release(data.get("release")),
    )


def _encode_rollback(rollback: RollbackEvidence) -> dict[str, object]:
    return {
        "environment_id": rollback.environment_id,
        "failed_release_sha": rollback.failed_release_sha,
        "restored_release_sha": rollback.restored_release_sha,
        "succeeded": rollback.succeeded,
        "evidence_refs": list(rollback.evidence_refs),
        "failed_release": _encode_release(rollback.failed_release),
        "restored_release": _encode_release(rollback.restored_release),
    }


def _decode_rollback(value: Any) -> RollbackEvidence:
    data = _mapping(value, "rollback")
    succeeded = data.get("succeeded")
    if not isinstance(succeeded, bool):
        raise TypeError("rollback succeeded must be boolean")
    return RollbackEvidence(
        _text(data, "environment_id"),
        _text(data, "failed_release_sha"),
        _optional_text(data.get("restored_release_sha")),
        succeeded,
        _text_tuple(data.get("evidence_refs", []), "rollback evidence refs"),
        failed_release=_decode_optional_release(data.get("failed_release")),
        restored_release=_decode_optional_release(data.get("restored_release")),
    )


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    return value


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{key} must be non-empty text")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TypeError("optional text must be non-empty when present")
    return value


def _text_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) for item in value
    ):
        raise TypeError(f"{label} must contain text values")
    return tuple(value)
