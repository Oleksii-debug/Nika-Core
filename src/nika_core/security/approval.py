from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Protocol

from nika_core.security.policy import (
    V01_APPROVAL_AUTHORITY_VERSION,
    ActionIntent,
    ApprovalEvidence,
    ApprovalVerifier,
)

_DEFAULT_TTL = timedelta(minutes=5)
_MAX_TTL = timedelta(minutes=15)
_SIGNATURE_SCHEMA = "nika-v01-approval-evidence-v1"
_PROCESS_KEY_SCHEMA = b"nika-v01-approval-process-key-v1"


class ApprovalAuditSink(Protocol):
    def append(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object] | None = None,
    ) -> object: ...


def _aware_now(now: datetime | None) -> datetime:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("approval time must be timezone-aware")
    return current


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    if value != value.strip():
        raise ValueError(f"{label} must not contain surrounding whitespace")
    return value


def _validate_ttl(ttl: timedelta) -> None:
    if ttl <= timedelta(0) or ttl > _MAX_TTL:
        raise ValueError("approval request ttl must be greater than zero and at most 15 minutes")


def _signature_payload(evidence: ApprovalEvidence) -> bytes:
    payload = json.dumps(
        (
            _SIGNATURE_SCHEMA,
            evidence.approval_id,
            evidence.request_id,
            evidence.issuer_id,
            evidence.authority_version,
            evidence.action_fingerprint,
            evidence.effect_fingerprint,
            _timestamp(evidence.approved_at),
            _timestamp(evidence.expires_at),
        ),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return payload.encode("ascii")


@dataclass(frozen=True, slots=True)
class ApprovalRequestView:
    request_id: str
    task_id: str
    project_id: str
    action_id: str
    tool_id: str
    target: str
    site: str | None
    resource: str | None
    effect_id: str
    arguments_fingerprint: str
    effect_fingerprint: str
    approval_fingerprint: str
    risk: str
    authority_version: str
    scope: tuple[tuple[str, str], ...]
    requested_at: datetime
    expires_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "action_id": self.action_id,
            "tool_id": self.tool_id,
            "target": self.target,
            "site": self.site,
            "resource": self.resource,
            "effect_id": self.effect_id,
            "arguments_fingerprint": self.arguments_fingerprint,
            "effect_fingerprint": self.effect_fingerprint,
            "approval_fingerprint": self.approval_fingerprint,
            "risk": self.risk,
            "authority_version": self.authority_version,
            "scope": [list(item) for item in self.scope],
            "requested_at": _timestamp(self.requested_at),
            "expires_at": _timestamp(self.expires_at),
        }


@dataclass(frozen=True, slots=True)
class _PendingApproval:
    intent: ActionIntent
    view: ApprovalRequestView


class _HmacApprovalVerifier:
    __slots__ = (
        "_issuer_id",
        "_authority_version",
        "_secret",
        "_lock",
        "_used",
        "_issued",
        "_audit_sink",
    )

    def __init__(
        self,
        *,
        issuer_id: str,
        authority_version: str,
        secret: bytes,
        lock: RLock,
        used: set[tuple[str, str]],
        issued: dict[str, tuple[ActionIntent, ApprovalEvidence]],
        audit_sink: ApprovalAuditSink | None,
    ) -> None:
        self._issuer_id = issuer_id
        self._authority_version = authority_version
        self._secret = secret
        self._lock = lock
        self._used = used
        self._issued = issued
        self._audit_sink = audit_sink

    @property
    def authorization_lock(self) -> RLock:
        return self._lock

    def validate_locked(
        self,
        intent: ActionIntent,
        approval: ApprovalEvidence,
        *,
        now: datetime,
    ) -> None:
        if now.tzinfo is None:
            raise ValueError("approval verification time must be timezone-aware")
        if approval.issuer_id != self._issuer_id:
            self._reject(intent, approval.request_id, "untrusted_issuer")
        if approval.authority_version != self._authority_version:
            self._reject(intent, approval.request_id, "untrusted_authority_version")
        if intent.authority_version != self._authority_version:
            self._reject(intent, approval.request_id, "stale_authority_version")
        key = (approval.issuer_id, approval.approval_id)
        if key in self._used:
            self._reject(intent, approval.request_id, "already_used")
        issued = self._issued.get(approval.approval_id)
        if issued is None or issued[1] != approval:
            self._reject(intent, approval.request_id, "not_issued")
        if issued[0].approval_fingerprint != intent.approval_fingerprint:
            self._reject(intent, approval.request_id, "stale_exact_action")
        if approval.action_fingerprint != intent.approval_fingerprint:
            self._reject(intent, approval.request_id, "action_fingerprint_mismatch")
        if approval.effect_fingerprint != intent.effect_fingerprint:
            self._reject(intent, approval.request_id, "effect_fingerprint_mismatch")
        expected = hmac.new(self._secret, _signature_payload(approval), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, approval.signature):
            self._reject(intent, approval.request_id, "invalid_signature")
        if now < approval.approved_at or now >= approval.expires_at:
            self._reject(intent, approval.request_id, "expired_or_not_yet_valid")

    def commit_locked(self, approval: ApprovalEvidence) -> None:
        self._used.add((approval.issuer_id, approval.approval_id))
        issued = self._issued.get(approval.approval_id)
        if issued is not None:
            self._audit("security.approval_consumed", issued[0], approval.request_id)

    def _reject(self, intent: ActionIntent, request_id: str, reason: str) -> None:
        if self._audit_sink is not None:
            payload = _safe_audit_payload(intent)
            payload["reason"] = reason
            self._audit_sink.append(
                event_type="security.approval_rejected",
                entity_type="approval_request",
                entity_id=request_id,
                payload=payload,
            )
        messages = {
            "untrusted_issuer": "approval evidence came from an untrusted issuer",
            "untrusted_authority_version": "approval evidence uses an untrusted authority version",
            "stale_authority_version": "action requires a different approval authority version",
            "already_used": "approval evidence was already used by trusted host",
            "not_issued": "approval evidence was not issued by this trusted host",
            "stale_exact_action": "approval request no longer matches the exact action",
            "action_fingerprint_mismatch": "approval does not match the exact action",
            "effect_fingerprint_mismatch": "approval does not match the exact effect",
            "invalid_signature": "approval evidence signature is invalid",
            "expired_or_not_yet_valid": "approval is not currently valid",
        }
        raise PermissionError(messages[reason])

    def _audit(self, event_type: str, intent: ActionIntent, request_id: str) -> None:
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            event_type=event_type,
            entity_type="approval_request",
            entity_id=request_id,
            payload=_safe_audit_payload(intent),
        )


class ApprovalAuthority:
    """Trusted host issuer for exact, bounded, one-shot V0.1 action authority.

    Approval is resolved by opaque request identity against the host's stored canonical
    ``ActionIntent``. Human-facing request text is informational and is never authority.
    """

    def __init__(
        self,
        *,
        issuer_id: str | None = None,
        authority_version: str = V01_APPROVAL_AUTHORITY_VERSION,
        secret: bytes | None = None,
        audit_sink: ApprovalAuditSink | None = None,
    ) -> None:
        resolved_issuer = f"host-{secrets.token_urlsafe(18)}" if issuer_id is None else issuer_id
        self._issuer_id = _require_text(resolved_issuer, "approval issuer_id")
        self._authority_version = _require_text(authority_version, "authority_version")
        seed = secrets.token_bytes(32) if secret is None else bytes(secret)
        if len(seed) < 32:
            raise ValueError("approval authority secret seed must contain at least 32 bytes")
        process_nonce = secrets.token_bytes(32)
        self._secret = hmac.new(
            seed,
            _PROCESS_KEY_SCHEMA + process_nonce,
            hashlib.sha256,
        ).digest()
        self._audit_sink = audit_sink
        self._pending: dict[str, _PendingApproval] = {}
        self._issued: dict[str, tuple[ActionIntent, ApprovalEvidence]] = {}
        self._denied: set[str] = set()
        self._used: set[tuple[str, str]] = set()
        self._lock = RLock()

    @property
    def issuer_id(self) -> str:
        return self._issuer_id

    @property
    def authority_version(self) -> str:
        return self._authority_version

    def verifier(self) -> ApprovalVerifier:
        return _HmacApprovalVerifier(
            issuer_id=self._issuer_id,
            authority_version=self._authority_version,
            secret=self._secret,
            lock=self._lock,
            used=self._used,
            issued=self._issued,
            audit_sink=self._audit_sink,
        )

    def request(
        self,
        intent: ActionIntent,
        *,
        now: datetime | None = None,
        ttl: timedelta = _DEFAULT_TTL,
    ) -> ApprovalRequestView:
        if not intent.requires_approval:
            raise ValueError("approval requests are reserved for approval-gated actions")
        for value, label in (
            (intent.task_id, "task_id"),
            (intent.project_id, "project_id"),
            (intent.effect_id, "effect_id"),
        ):
            if value is None:
                raise ValueError(f"approval-gated action requires {label}")
        if intent.authority_version != self._authority_version:
            raise PermissionError("action requires a different approval authority version")
        _validate_ttl(ttl)
        current = _aware_now(now)
        request_id = f"approval-request-{secrets.token_urlsafe(18)}"
        assert intent.task_id is not None
        assert intent.project_id is not None
        assert intent.effect_id is not None
        view = ApprovalRequestView(
            request_id=request_id,
            task_id=intent.task_id,
            project_id=intent.project_id,
            action_id=intent.action_id,
            tool_id=intent.tool_id,
            target=intent.target,
            site=intent.site,
            resource=intent.resource,
            effect_id=intent.effect_id,
            arguments_fingerprint=intent.arguments_fingerprint,
            effect_fingerprint=intent.effect_fingerprint,
            approval_fingerprint=intent.approval_fingerprint,
            risk=intent.risk.value,
            authority_version=intent.authority_version,
            scope=intent.scope,
            requested_at=current,
            expires_at=current + ttl,
        )
        with self._lock:
            self._prune_unlocked(current)
            self._pending[request_id] = _PendingApproval(intent=intent, view=view)
            self._audit("security.approval_requested", intent, request_id)
        return view

    def approve(self, request_id: str, *, now: datetime | None = None) -> ApprovalEvidence:
        current = _aware_now(now)
        with self._lock:
            self._prune_unlocked(current)
            if request_id in self._denied:
                raise PermissionError("approval request was denied")
            try:
                pending = self._pending.pop(request_id)
            except KeyError as exc:
                raise KeyError("unknown or expired approval request") from exc
            if current >= pending.view.expires_at:
                raise PermissionError("approval request expired")
            unsigned = ApprovalEvidence(
                approval_id=f"approval-{secrets.token_urlsafe(18)}",
                request_id=request_id,
                issuer_id=self._issuer_id,
                authority_version=self._authority_version,
                action_fingerprint=pending.intent.approval_fingerprint,
                effect_fingerprint=pending.intent.effect_fingerprint,
                approved_at=current,
                expires_at=pending.view.expires_at,
                signature="pending",
            )
            evidence = ApprovalEvidence(
                approval_id=unsigned.approval_id,
                request_id=unsigned.request_id,
                issuer_id=unsigned.issuer_id,
                authority_version=unsigned.authority_version,
                action_fingerprint=unsigned.action_fingerprint,
                effect_fingerprint=unsigned.effect_fingerprint,
                approved_at=unsigned.approved_at,
                expires_at=unsigned.expires_at,
                signature=hmac.new(
                    self._secret,
                    _signature_payload(unsigned),
                    hashlib.sha256,
                ).hexdigest(),
            )
            self._issued[evidence.approval_id] = (pending.intent, evidence)
            self._audit("security.approval_granted", pending.intent, request_id)
            return evidence

    def deny(self, request_id: str, *, now: datetime | None = None) -> None:
        current = _aware_now(now)
        with self._lock:
            self._prune_unlocked(current)
            try:
                pending = self._pending.pop(request_id)
            except KeyError as exc:
                raise KeyError("unknown or expired approval request") from exc
            self._denied.add(request_id)
            self._audit("security.approval_denied", pending.intent, request_id)

    def pending_views(self, *, now: datetime | None = None) -> tuple[ApprovalRequestView, ...]:
        current = _aware_now(now)
        with self._lock:
            self._prune_unlocked(current)
            return tuple(
                item.view
                for item in sorted(
                    self._pending.values(),
                    key=lambda item: (item.view.requested_at, item.view.request_id),
                )
            )

    def _prune_unlocked(self, now: datetime) -> None:
        expired = [
            request_id
            for request_id, pending in self._pending.items()
            if now >= pending.view.expires_at
        ]
        for request_id in expired:
            self._pending.pop(request_id, None)

    def _audit(self, event_type: str, intent: ActionIntent, request_id: str) -> None:
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            event_type=event_type,
            entity_type="approval_request",
            entity_id=request_id,
            payload=_safe_audit_payload(intent),
        )


def _safe_audit_payload(intent: ActionIntent) -> dict[str, object]:
    return {
        "task_id": intent.task_id,
        "project_id": intent.project_id,
        "action_id": intent.action_id,
        "tool_id": intent.tool_id,
        "target": intent.target,
        "site": intent.site,
        "resource": intent.resource,
        "effect_id": intent.effect_id,
        "arguments_fingerprint": intent.arguments_fingerprint,
        "effect_fingerprint": intent.effect_fingerprint,
        "approval_fingerprint": intent.approval_fingerprint,
        "risk": intent.risk.value,
        "authority_version": intent.authority_version,
        "scope_fingerprint": hashlib.sha256(
            json.dumps(intent.scope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
