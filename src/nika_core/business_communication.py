from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from nika_core.business_authority import (
    BusinessAuthorizationAuthorityPort,
    BusinessAuthorizationIntent,
    BusinessAuthorizationUse,
    trusted_business_authorization,
)
from nika_core.business_factory import (
    BusinessFactorySnapshot,
    BusinessPolicy,
    CommunicationAuthority,
)

COMMUNICATION_SCHEMA = "nika.business_communication.v1"
COMMUNICATION_SCHEMA_VERSION = 1
COMMUNICATION_MIGRATIONS = {
    1: (
        (
            "CREATE TABLE IF NOT EXISTS business_communications ("
            "message_id TEXT PRIMARY KEY, "
            "objective_id TEXT NOT NULL, "
            "row_version INTEGER NOT NULL, "
            "payload_json TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_business_communications_objective "
            "ON business_communications(objective_id)"
        ),
    ),
}


class BusinessCommunicationError(ValueError):
    pass


class StaleCommunicationStateError(BusinessCommunicationError):
    pass


class CommunicationState(StrEnum):
    DRAFT = "draft"
    AUTHORIZED = "authorized"
    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CommunicationAuditEvent:
    sequence: int
    event_type: str
    evidence_ref: str
    recorded_at: str


@dataclass(frozen=True, slots=True)
class BusinessCommunication:
    schema: str
    message_id: str
    objective_id: str
    lead_id: str
    counterparty_ref: str
    thread_ref: str
    channel_id: str
    payload_ref: str
    policy_id: str
    state: CommunicationState = CommunicationState.DRAFT
    authorization_ref: str | None = None
    authorization_use: BusinessAuthorizationUse | None = None
    authorization_fingerprint: str | None = None
    provider_evidence_ref: str | None = None
    failure_ref: str | None = None
    audit: tuple[CommunicationAuditEvent, ...] = ()
    row_version: int = 0


class BusinessCommunicationCoordinator:
    """Records policy-governed communication state without performing external sends."""

    @staticmethod
    def draft(
        snapshot: BusinessFactorySnapshot,
        *,
        message_id: str,
        thread_ref: str,
        payload_ref: str,
    ) -> BusinessCommunication:
        lead = snapshot.lead
        if lead is None:
            raise BusinessCommunicationError("communication requires an existing business lead")
        if lead.channel_id not in snapshot.policy.allowed_channel_ids:
            raise BusinessCommunicationError("lead channel is outside current business policy")
        _text(message_id, "message_id")
        _text(thread_ref, "thread_ref")
        _text(payload_ref, "payload_ref")
        record = BusinessCommunication(
            schema=COMMUNICATION_SCHEMA,
            message_id=message_id,
            objective_id=snapshot.objective.objective_id,
            lead_id=lead.lead_id,
            counterparty_ref=lead.counterparty_ref,
            thread_ref=thread_ref,
            channel_id=lead.channel_id,
            payload_ref=payload_ref,
            policy_id=snapshot.policy.policy_id,
        )
        return _record_event(record, "communication.drafted", payload_ref)

    @staticmethod
    def authorize(
        record: BusinessCommunication,
        snapshot: BusinessFactorySnapshot,
        *,
        approval_ref: str | None = None,
        approval_authority: BusinessAuthorizationAuthorityPort | None = None,
    ) -> BusinessCommunication:
        _validate_record(record)
        _validate_business_binding(record, snapshot)
        if record.state is not CommunicationState.DRAFT:
            raise BusinessCommunicationError("only a draft communication can be authorized")
        policy = snapshot.policy
        if policy.communication_authority is CommunicationAuthority.DRAFT_ONLY:
            raise BusinessCommunicationError("draft-only policy does not authorize external sending")
        if policy.communication_authority is CommunicationAuthority.APPROVAL_REQUIRED:
            _text(approval_ref, "communication approval_ref")
            authorization_ref = approval_ref
            authorization_use = BusinessAuthorizationUse.ONE_TIME
        elif policy.communication_authority is CommunicationAuthority.STANDING_POLICY:
            authorization_ref = policy.standing_policy_ref
            _text(authorization_ref, "standing communication policy ref")
            authorization_use = BusinessAuthorizationUse.STANDING_POLICY
        else:
            raise BusinessCommunicationError("unsupported communication authority")
        intent = _communication_authorization_intent(record, authorization_use)
        if not trusted_business_authorization(
            approval_authority,
            intent=intent,
            evidence_ref=authorization_ref,
        ):
            raise BusinessCommunicationError(
                "trusted communication approval authority rejected or could not verify the action"
            )
        authorized = replace(
            record,
            state=CommunicationState.AUTHORIZED,
            authorization_ref=authorization_ref,
            authorization_use=authorization_use,
            authorization_fingerprint=intent.fingerprint,
        )
        return _record_event(
            authorized,
            "communication.authorized",
            authorization_ref,
        )

    @staticmethod
    def record_provider_result(
        record: BusinessCommunication,
        snapshot: BusinessFactorySnapshot,
        *,
        provider_evidence_ref: str | None = None,
        failure_ref: str | None = None,
    ) -> BusinessCommunication:
        _validate_record(record)
        _validate_business_binding(record, snapshot)
        if record.state is not CommunicationState.AUTHORIZED:
            raise BusinessCommunicationError(
                "provider result requires an authorized communication"
            )
        success = provider_evidence_ref is not None
        failure = failure_ref is not None
        if success == failure:
            raise BusinessCommunicationError(
                "provider result requires exactly one success or failure evidence ref"
            )
        if provider_evidence_ref is not None:
            _text(provider_evidence_ref, "provider_evidence_ref")
            updated = replace(
                record,
                state=CommunicationState.SENT,
                provider_evidence_ref=provider_evidence_ref,
            )
            return _record_event(
                updated,
                "communication.sent-recorded",
                provider_evidence_ref,
            )
        _text(failure_ref, "failure_ref")
        updated = replace(
            record,
            state=CommunicationState.FAILED,
            failure_ref=failure_ref,
        )
        return _record_event(updated, "communication.failed-recorded", failure_ref)


class BusinessCommunicationRepository:
    """Durable PF9 communication state with optimistic concurrency control."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def initialize(self) -> None:
        with self.store.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS business_communication_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT MAX(version) AS version "
                "FROM business_communication_schema_migrations"
            ).fetchone()
            current = int(row["version"] or 0)
            if current > COMMUNICATION_SCHEMA_VERSION:
                raise RuntimeError(
                    "business communication database schema "
                    f"{current} is newer than supported schema {COMMUNICATION_SCHEMA_VERSION}"
                )
            for version in range(current + 1, COMMUNICATION_SCHEMA_VERSION + 1):
                statements = COMMUNICATION_MIGRATIONS.get(version)
                if statements is None:
                    raise RuntimeError(f"missing business communication migration {version}")
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO business_communication_schema_migrations(version, applied_at) "
                    "VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )

    def save(
        self,
        record: BusinessCommunication,
        *,
        expected_row_version: int,
    ) -> BusinessCommunication:
        _validate_record(record)
        if expected_row_version < 0:
            raise StaleCommunicationStateError("expected_row_version cannot be negative")
        if record.row_version <= expected_row_version:
            raise StaleCommunicationStateError(
                "communication state must advance beyond expected_row_version"
            )
        payload = dump_business_communication(record)
        now = datetime.now(UTC).isoformat()
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT row_version FROM business_communications WHERE message_id = ?",
                (record.message_id,),
            ).fetchone()
            if row is None:
                if expected_row_version != 0:
                    raise StaleCommunicationStateError(
                        "communication does not exist at expected row version"
                    )
                conn.execute(
                    "INSERT INTO business_communications("
                    "message_id, objective_id, row_version, payload_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        record.message_id,
                        record.objective_id,
                        record.row_version,
                        payload,
                        now,
                    ),
                )
            else:
                current = int(row["row_version"])
                if current != expected_row_version:
                    raise StaleCommunicationStateError(
                        "communication row version changed: "
                        f"{current} != {expected_row_version}"
                    )
                updated = conn.execute(
                    "UPDATE business_communications SET objective_id = ?, row_version = ?, "
                    "payload_json = ?, updated_at = ? "
                    "WHERE message_id = ? AND row_version = ?",
                    (
                        record.objective_id,
                        record.row_version,
                        payload,
                        now,
                        record.message_id,
                        expected_row_version,
                    ),
                )
                if updated.rowcount != 1:
                    raise StaleCommunicationStateError(
                        "communication changed during save"
                    )
        return record

    def load(self, message_id: str) -> BusinessCommunication | None:
        _text(message_id, "message_id")
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT objective_id, row_version, payload_json "
                "FROM business_communications WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        if row is None:
            return None
        record = load_business_communication(str(row["payload_json"]))
        if record.message_id != message_id:
            raise RuntimeError("communication identity does not match storage key")
        if record.objective_id != str(row["objective_id"]):
            raise RuntimeError("communication objective does not match storage metadata")
        if record.row_version != int(row["row_version"]):
            raise RuntimeError("communication row version does not match storage metadata")
        return record


def dump_business_communication(record: BusinessCommunication) -> str:
    _validate_record(record)
    return json.dumps(asdict(record), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_business_communication(payload: str) -> BusinessCommunication:
    if not isinstance(payload, str) or not payload.strip():
        raise BusinessCommunicationError("communication payload must be non-empty JSON text")
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BusinessCommunicationError("communication payload is invalid JSON") from exc
    if not isinstance(raw, dict):
        raise BusinessCommunicationError("communication payload root must be an object")
    expected = {
        "schema",
        "message_id",
        "objective_id",
        "lead_id",
        "counterparty_ref",
        "thread_ref",
        "channel_id",
        "payload_ref",
        "policy_id",
        "state",
        "authorization_ref",
        "authorization_use",
        "authorization_fingerprint",
        "provider_evidence_ref",
        "failure_ref",
        "audit",
        "row_version",
    }
    if set(raw) != expected:
        raise BusinessCommunicationError("communication payload fields do not match schema")
    audit_raw = raw["audit"]
    if not isinstance(audit_raw, list):
        raise BusinessCommunicationError("communication audit must be a list")
    audit: list[CommunicationAuditEvent] = []
    for item in audit_raw:
        if not isinstance(item, dict):
            raise BusinessCommunicationError("communication audit event must be an object")
        if set(item) != {"sequence", "event_type", "evidence_ref", "recorded_at"}:
            raise BusinessCommunicationError("communication audit fields do not match schema")
        audit.append(
            CommunicationAuditEvent(
                sequence=_integer(item.get("sequence"), "audit sequence"),
                event_type=_required(item, "event_type", "audit event"),
                evidence_ref=_required(item, "evidence_ref", "audit event"),
                recorded_at=_required(item, "recorded_at", "audit event"),
            )
        )
    state_raw = raw.get("state")
    if not isinstance(state_raw, str):
        raise BusinessCommunicationError("communication state must be text")
    try:
        state = CommunicationState(state_raw)
    except ValueError as exc:
        raise BusinessCommunicationError("communication state is invalid") from exc
    use_raw = raw.get("authorization_use")
    if use_raw is None:
        authorization_use = None
    elif isinstance(use_raw, str):
        try:
            authorization_use = BusinessAuthorizationUse(use_raw)
        except ValueError as exc:
            raise BusinessCommunicationError("communication authorization use is invalid") from exc
    else:
        raise BusinessCommunicationError("communication authorization use must be text or null")
    record = BusinessCommunication(
        schema=_required(raw, "schema", "communication"),
        message_id=_required(raw, "message_id", "communication"),
        objective_id=_required(raw, "objective_id", "communication"),
        lead_id=_required(raw, "lead_id", "communication"),
        counterparty_ref=_required(raw, "counterparty_ref", "communication"),
        thread_ref=_required(raw, "thread_ref", "communication"),
        channel_id=_required(raw, "channel_id", "communication"),
        payload_ref=_required(raw, "payload_ref", "communication"),
        policy_id=_required(raw, "policy_id", "communication"),
        state=state,
        authorization_ref=_optional(raw, "authorization_ref", "communication"),
        authorization_use=authorization_use,
        authorization_fingerprint=_optional(
            raw,
            "authorization_fingerprint",
            "communication",
        ),
        provider_evidence_ref=_optional(
            raw,
            "provider_evidence_ref",
            "communication",
        ),
        failure_ref=_optional(raw, "failure_ref", "communication"),
        audit=tuple(audit),
        row_version=_integer(raw.get("row_version"), "row_version"),
    )
    _validate_record(record)
    return record


def _record_event(
    record: BusinessCommunication,
    event_type: str,
    evidence_ref: str,
) -> BusinessCommunication:
    _text(event_type, "communication event_type")
    _text(evidence_ref, "communication evidence_ref")
    event = CommunicationAuditEvent(
        sequence=len(record.audit) + 1,
        event_type=event_type,
        evidence_ref=evidence_ref,
        recorded_at=datetime.now(UTC).isoformat(),
    )
    updated = replace(
        record,
        audit=(*record.audit, event),
        row_version=record.row_version + 1,
    )
    _validate_record(updated)
    return updated


def _validate_business_binding(
    record: BusinessCommunication,
    snapshot: BusinessFactorySnapshot,
) -> None:
    lead = snapshot.lead
    if record.objective_id != snapshot.objective.objective_id:
        raise BusinessCommunicationError("communication objective changed")
    if record.policy_id != snapshot.policy.policy_id:
        raise BusinessCommunicationError("communication policy changed; redraft is required")
    if lead is None or record.lead_id != lead.lead_id:
        raise BusinessCommunicationError("communication lead changed")
    if record.counterparty_ref != lead.counterparty_ref:
        raise BusinessCommunicationError("communication counterparty changed")
    if record.channel_id != lead.channel_id:
        raise BusinessCommunicationError("communication channel changed")
    if record.channel_id not in snapshot.policy.allowed_channel_ids:
        raise BusinessCommunicationError("communication channel is no longer allowed")


def _validate_record(record: BusinessCommunication) -> None:
    if record.schema != COMMUNICATION_SCHEMA:
        raise BusinessCommunicationError("unsupported communication schema")
    for value, label in (
        (record.message_id, "message_id"),
        (record.objective_id, "objective_id"),
        (record.lead_id, "lead_id"),
        (record.counterparty_ref, "counterparty_ref"),
        (record.thread_ref, "thread_ref"),
        (record.channel_id, "channel_id"),
        (record.payload_ref, "payload_ref"),
        (record.policy_id, "policy_id"),
    ):
        _text(value, label)
    if record.row_version < 0 or record.row_version != len(record.audit):
        raise BusinessCommunicationError("communication row_version/audit mismatch")
    for index, event in enumerate(record.audit, start=1):
        if event.sequence != index:
            raise BusinessCommunicationError("communication audit sequence is not contiguous")
        _text(event.event_type, "communication audit event_type")
        _text(event.evidence_ref, "communication audit evidence_ref")
        _text(event.recorded_at, "communication audit recorded_at")
    if record.state is CommunicationState.DRAFT:
        if any(
            value is not None
            for value in (
                record.authorization_ref,
                record.authorization_use,
                record.authorization_fingerprint,
                record.provider_evidence_ref,
                record.failure_ref,
            )
        ):
            raise BusinessCommunicationError("draft communication contains terminal evidence")
        return
    _text(record.authorization_ref, "communication authorization_ref")
    if not isinstance(record.authorization_use, BusinessAuthorizationUse):
        raise BusinessCommunicationError("communication authorization use is missing")
    _text(record.authorization_fingerprint, "communication authorization fingerprint")
    expected_fingerprint = _communication_authorization_intent(
        record,
        record.authorization_use,
    ).fingerprint
    if record.authorization_fingerprint != expected_fingerprint:
        raise BusinessCommunicationError(
            "communication authorization fingerprint does not match exact message scope"
        )
    if record.state is CommunicationState.AUTHORIZED:
        if record.provider_evidence_ref is not None or record.failure_ref is not None:
            raise BusinessCommunicationError("authorized communication contains provider result")
        return
    if record.state is CommunicationState.SENT:
        _text(record.provider_evidence_ref, "provider_evidence_ref")
        if record.failure_ref is not None:
            raise BusinessCommunicationError("sent communication contains failure_ref")
        return
    if record.state is CommunicationState.FAILED:
        _text(record.failure_ref, "failure_ref")
        if record.provider_evidence_ref is not None:
            raise BusinessCommunicationError("failed communication contains provider evidence")
        return
    raise BusinessCommunicationError("communication state is invalid")


def _communication_authorization_intent(
    record: BusinessCommunication,
    use: BusinessAuthorizationUse,
) -> BusinessAuthorizationIntent:
    return BusinessAuthorizationIntent(
        objective_id=record.objective_id,
        purpose="communication.send",
        subject_id=record.message_id,
        bindings=(
            ("channel_id", record.channel_id),
            ("counterparty_ref", record.counterparty_ref),
            ("lead_id", record.lead_id),
            ("payload_ref", record.payload_ref),
            ("policy_id", record.policy_id),
            ("thread_ref", record.thread_ref),
        ),
        use=use,
    )


def _required(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    _text(value, f"{label} {key}")
    return value


def _optional(raw: dict[str, Any], key: str, label: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    _text(value, f"{label} {key}")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise BusinessCommunicationError(f"{label} must be an integer")
    return value


def _text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BusinessCommunicationError(f"{label} must be non-empty text")


def communication_policy_ref(policy: BusinessPolicy) -> str | None:
    """Expose only the authorization reference, never credentials or provider account material."""
    if policy.communication_authority is CommunicationAuthority.STANDING_POLICY:
        return policy.standing_policy_ref
    return None
