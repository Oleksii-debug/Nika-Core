from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyRecord,
    IdempotencyStatus,
)

RECOVERY_RESUME_OPERATION_TYPE = "runtime.recovery_resume"
RECOVERY_CLAIM_SCHEMA = "nika-runtime-recovery-claim-v1"
_RECOVERY_CLAIM_ACTIVATION_LEASE = timedelta(seconds=10)


class RecoveryClaimPhase(StrEnum):
    CLAIMED = "claimed"
    EFFECT_STARTED = "effect_started"


@dataclass(frozen=True, slots=True)
class RecoveryClaimMetadata:
    claim_id: str
    owner_id: str
    checkpoint_id: str
    session_fingerprint: str
    claim_fingerprint: str
    resume_mode: str
    phase: RecoveryClaimPhase
    lease_expires_at: str
    effect_started_at: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("claim_id", self.claim_id),
            ("owner_id", self.owner_id),
            ("checkpoint_id", self.checkpoint_id),
            ("session_fingerprint", self.session_fingerprint),
            ("claim_fingerprint", self.claim_fingerprint),
            ("resume_mode", self.resume_mode),
            ("lease_expires_at", self.lease_expires_at),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"recovery claim {name} must not be empty")
        _parse_timestamp(self.lease_expires_at, "lease_expires_at")
        if self.effect_started_at is not None:
            _parse_timestamp(self.effect_started_at, "effect_started_at")
        if self.phase is RecoveryClaimPhase.CLAIMED and self.effect_started_at is not None:
            raise ValueError("claimed recovery metadata cannot have effect_started_at")
        if self.phase is RecoveryClaimPhase.EFFECT_STARTED and self.effect_started_at is None:
            raise ValueError("effect-started recovery metadata requires effect_started_at")

    def as_result(self) -> dict[str, object]:
        return {
            "schema": RECOVERY_CLAIM_SCHEMA,
            "claim_id": self.claim_id,
            "owner_id": self.owner_id,
            "checkpoint_id": self.checkpoint_id,
            "session_fingerprint": self.session_fingerprint,
            "claim_fingerprint": self.claim_fingerprint,
            "resume_mode": self.resume_mode,
            "phase": self.phase.value,
            "lease_expires_at": self.lease_expires_at,
            "effect_started_at": self.effect_started_at,
            "checkpoint_proven": True,
        }


def new_recovery_claim_metadata(
    *,
    claim_id: str,
    owner_id: str,
    checkpoint_id: str,
    session_fingerprint: str,
    claim_fingerprint: str,
    resume_mode: str,
) -> RecoveryClaimMetadata:
    now = datetime.now(UTC)
    return RecoveryClaimMetadata(
        claim_id=claim_id,
        owner_id=owner_id,
        checkpoint_id=checkpoint_id,
        session_fingerprint=session_fingerprint,
        claim_fingerprint=claim_fingerprint,
        resume_mode=resume_mode,
        phase=RecoveryClaimPhase.CLAIMED,
        lease_expires_at=(now + _RECOVERY_CLAIM_ACTIVATION_LEASE).isoformat(),
    )


def recovery_claim_metadata(record: IdempotencyRecord) -> RecoveryClaimMetadata | None:
    if (
        record.operation_type != RECOVERY_RESUME_OPERATION_TYPE
        or record.status is not IdempotencyStatus.PENDING
        or record.result is None
    ):
        return None
    result = record.result
    if result.get("schema") != RECOVERY_CLAIM_SCHEMA:
        return None
    try:
        effect_started_at = result.get("effect_started_at")
        return RecoveryClaimMetadata(
            claim_id=_result_text(result, "claim_id"),
            owner_id=_result_text(result, "owner_id"),
            checkpoint_id=_result_text(result, "checkpoint_id"),
            session_fingerprint=_result_text(result, "session_fingerprint"),
            claim_fingerprint=_result_text(result, "claim_fingerprint"),
            resume_mode=_result_text(result, "resume_mode"),
            phase=RecoveryClaimPhase(_result_text(result, "phase")),
            lease_expires_at=_result_text(result, "lease_expires_at"),
            effect_started_at=(
                _result_text(result, "effect_started_at")
                if effect_started_at is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def recovery_claim_is_reclaimable(record: IdempotencyRecord) -> bool:
    metadata = recovery_claim_metadata(record)
    if metadata is None or metadata.phase is not RecoveryClaimPhase.CLAIMED:
        return False
    return datetime.now(UTC) >= _parse_timestamp(metadata.lease_expires_at, "lease_expires_at")


def write_pending_recovery_claim(
    conn: sqlite3.Connection,
    *,
    operation_key: str,
    metadata: RecoveryClaimMetadata,
) -> None:
    row = conn.execute(
        "SELECT status FROM idempotency_records WHERE operation_key = ?",
        (operation_key,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Unknown idempotency operation: {operation_key}")
    if IdempotencyStatus(row["status"]) is not IdempotencyStatus.PENDING:
        raise IdempotencyConflictError("only pending recovery claims may change ownership phase")
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        UPDATE idempotency_records
        SET result_json = ?, updated_at = ?
        WHERE operation_key = ? AND status = ?
        """,
        (
            json.dumps(metadata.as_result(), ensure_ascii=False, sort_keys=True),
            now,
            operation_key,
            IdempotencyStatus.PENDING.value,
        ),
    )


def begin_recovery_effect(
    conn: sqlite3.Connection,
    *,
    operation_key: str,
    claim_id: str,
) -> RecoveryClaimMetadata:
    row = conn.execute(
        "SELECT * FROM idempotency_records WHERE operation_key = ?",
        (operation_key,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Unknown idempotency operation: {operation_key}")
    result = json.loads(row["result_json"]) if row["result_json"] else None
    record = IdempotencyRecord(
        operation_key=row["operation_key"],
        task_id=row["task_id"],
        operation_type=row["operation_type"],
        input_fingerprint=row["input_fingerprint"],
        status=IdempotencyStatus(row["status"]),
        result=result,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
    metadata = recovery_claim_metadata(record)
    if metadata is None:
        raise IdempotencyConflictError("recovery claim metadata is missing or invalid")
    if metadata.claim_id != claim_id:
        raise IdempotencyConflictError("recovery claim ownership changed before resume effect")
    if metadata.phase is not RecoveryClaimPhase.CLAIMED:
        raise IdempotencyConflictError("recovery resume effect was already started")
    started = replace(
        metadata,
        phase=RecoveryClaimPhase.EFFECT_STARTED,
        effect_started_at=datetime.now(UTC).isoformat(),
    )
    write_pending_recovery_claim(
        conn,
        operation_key=operation_key,
        metadata=started,
    )
    return started


def _result_text(result: Mapping[str, object], key: str) -> str:
    value = result[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid recovery claim field: {key}")
    return value


def _parse_timestamp(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid recovery claim {name}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"recovery claim {name} must be timezone-aware")
    return parsed.astimezone(UTC)
