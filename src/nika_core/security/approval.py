from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock

from nika_core.security.policy import ActionIntent, ApprovalEvidence, ApprovalVerifier
from nika_core.tools import ToolRisk

_DEFAULT_TTL = timedelta(minutes=5)
_MAX_TTL = timedelta(minutes=15)
_SIGNATURE_SCHEMA = "nika-r4-approval-evidence-v1"


def _aware_now(now: datetime | None) -> datetime:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("approval time must be timezone-aware")
    return current


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _signature_payload(evidence: ApprovalEvidence) -> bytes:
    payload = json.dumps(
        (
            _SIGNATURE_SCHEMA,
            evidence.approval_id,
            evidence.request_id,
            evidence.issuer_id,
            evidence.action_fingerprint,
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
    action_id: str
    tool_id: str
    risk: str
    target: str
    reason: str
    write_path: str | None
    write_bytes: int
    network_host: str | None
    executable: str | None
    requested_at: datetime
    expires_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "action_id": self.action_id,
            "tool_id": self.tool_id,
            "risk": self.risk,
            "target": self.target,
            "reason": self.reason,
            "write_path": self.write_path,
            "write_bytes": self.write_bytes,
            "network_host": self.network_host,
            "executable": self.executable,
            "requested_at": _timestamp(self.requested_at),
            "expires_at": _timestamp(self.expires_at),
        }


@dataclass(frozen=True, slots=True)
class _PendingApproval:
    intent: ActionIntent
    view: ApprovalRequestView


class _HmacApprovalVerifier:
    __slots__ = ("_issuer_id", "_secret")

    def __init__(self, *, issuer_id: str, secret: bytes) -> None:
        self._issuer_id = issuer_id
        self._secret = secret

    def verify(
        self,
        intent: ActionIntent,
        approval: ApprovalEvidence,
        *,
        now: datetime,
    ) -> None:
        if now.tzinfo is None:
            raise ValueError("approval verification time must be timezone-aware")
        if approval.issuer_id != self._issuer_id:
            raise PermissionError("approval evidence came from an untrusted issuer")
        if approval.action_fingerprint != intent.approval_fingerprint:
            raise PermissionError("approval does not match the exact action")
        expected = hmac.new(
            self._secret,
            _signature_payload(approval),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, approval.signature):
            raise PermissionError("approval evidence signature is invalid")
        if now < approval.approved_at or now >= approval.expires_at:
            raise PermissionError("approval is not currently valid")


class ApprovalAuthority:
    """Trusted in-process issuer for explicit R4 human approval evidence.

    The secret is process-ephemeral by default and is never exposed through request views.
    Runtime agents should receive neither this authority nor its verifier; the packaged host owns
    both and injects only the verifier into downstream security policy.
    """

    def __init__(
        self,
        *,
        issuer_id: str | None = None,
        secret: bytes | None = None,
    ) -> None:
        resolved_issuer = issuer_id or f"desktop-{secrets.token_urlsafe(18)}"
        if not resolved_issuer.strip():
            raise ValueError("approval issuer_id must not be empty")
        resolved_secret = secret or secrets.token_bytes(32)
        if len(resolved_secret) < 32:
            raise ValueError("approval authority secret must contain at least 32 bytes")
        self._issuer_id = resolved_issuer
        self._secret = bytes(resolved_secret)
        self._pending: dict[str, _PendingApproval] = {}
        self._approved: dict[str, ApprovalEvidence] = {}
        self._denied: set[str] = set()
        self._lock = RLock()

    @property
    def issuer_id(self) -> str:
        return self._issuer_id

    def verifier(self) -> ApprovalVerifier:
        return _HmacApprovalVerifier(issuer_id=self._issuer_id, secret=self._secret)

    def request(
        self,
        intent: ActionIntent,
        *,
        reason: str,
        now: datetime | None = None,
        ttl: timedelta = _DEFAULT_TTL,
    ) -> ApprovalRequestView:
        if not reason.strip():
            raise ValueError("approval reason must not be empty")
        if not (intent.approval_required or intent.risk is ToolRisk.HIGH_IMPACT):
            raise ValueError("approval requests are reserved for approval-gated actions")
        if ttl <= timedelta(0) or ttl > _MAX_TTL:
            raise ValueError("approval request ttl must be greater than zero and at most 15 minutes")
        current = _aware_now(now)
        request_id = f"r4-{secrets.token_urlsafe(18)}"
        view = ApprovalRequestView(
            request_id=request_id,
            action_id=intent.action_id,
            tool_id=intent.tool_id,
            risk=intent.risk.value,
            target=intent.target,
            reason=reason.strip(),
            write_path=intent.write_path,
            write_bytes=intent.write_bytes,
            network_host=intent.network_host,
            executable=intent.executable,
            requested_at=current,
            expires_at=current + ttl,
        )
        with self._lock:
            self._prune_unlocked(current)
            self._pending[request_id] = _PendingApproval(intent=intent, view=view)
        return view

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

    def approve(self, request_id: str, *, now: datetime | None = None) -> ApprovalEvidence:
        current = _aware_now(now)
        with self._lock:
            self._prune_unlocked(current)
            if request_id in self._approved:
                raise PermissionError("approval request was already approved")
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
                action_fingerprint=pending.intent.approval_fingerprint,
                approved_at=current,
                expires_at=pending.view.expires_at,
                signature="pending",
            )
            signature = hmac.new(
                self._secret,
                _signature_payload(unsigned),
                hashlib.sha256,
            ).hexdigest()
            evidence = ApprovalEvidence(
                approval_id=unsigned.approval_id,
                request_id=unsigned.request_id,
                issuer_id=unsigned.issuer_id,
                action_fingerprint=unsigned.action_fingerprint,
                approved_at=unsigned.approved_at,
                expires_at=unsigned.expires_at,
                signature=signature,
            )
            self._approved[request_id] = evidence
            return evidence

    def deny(self, request_id: str, *, now: datetime | None = None) -> None:
        current = _aware_now(now)
        with self._lock:
            self._prune_unlocked(current)
            if request_id in self._approved:
                raise PermissionError("approved request cannot be denied")
            if request_id in self._denied:
                raise PermissionError("approval request was already denied")
            try:
                self._pending.pop(request_id)
            except KeyError as exc:
                raise KeyError("unknown or expired approval request") from exc
            self._denied.add(request_id)

    def evidence(self, request_id: str, *, now: datetime | None = None) -> ApprovalEvidence:
        current = _aware_now(now)
        with self._lock:
            self._prune_unlocked(current)
            try:
                evidence = self._approved[request_id]
            except KeyError as exc:
                raise KeyError("approval evidence is unavailable") from exc
            if current >= evidence.expires_at:
                self._approved.pop(request_id, None)
                raise PermissionError("approval evidence expired")
            return evidence

    def _prune_unlocked(self, now: datetime) -> None:
        expired_pending = [
            request_id
            for request_id, item in self._pending.items()
            if now >= item.view.expires_at
        ]
        for request_id in expired_pending:
            self._pending.pop(request_id, None)
        expired_evidence = [
            request_id
            for request_id, evidence in self._approved.items()
            if now >= evidence.expires_at
        ]
        for request_id in expired_evidence:
            self._approved.pop(request_id, None)
