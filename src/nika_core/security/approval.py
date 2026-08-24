from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Protocol

from nika_core.security.policy import ActionIntent, ApprovalEvidence, ApprovalVerifier
from nika_core.tools import ToolRisk

_DEFAULT_TTL = timedelta(minutes=5)
_MAX_TTL = timedelta(minutes=15)
_ACTION_SIGNATURE_SCHEMA = "nika-r4-approval-evidence-v1"
_REVIEW_SUBJECT_SCHEMA = "nika-trusted-review-subject-v1"
_REVIEW_SIGNATURE_SCHEMA = "nika-trusted-review-evidence-v1"
_PROCESS_KEY_SCHEMA = b"nika-m10-process-ephemeral-key-v1"


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
    return value


def _validate_ttl(ttl: timedelta) -> None:
    if ttl <= timedelta(0) or ttl > _MAX_TTL:
        raise ValueError("approval request ttl must be greater than zero and at most 15 minutes")


def _action_signature_payload(evidence: ApprovalEvidence) -> bytes:
    payload = json.dumps(
        (
            _ACTION_SIGNATURE_SCHEMA,
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
class ReviewSubject:
    """Exact immutable subject reviewed by the trusted host.

    ``bindings`` carries consumer-specific immutable identity such as full release,
    environment, WorkOrder, or compliance component identity without coupling M10
    to PF6/PF10/framework types.
    """

    subject_kind: str
    project_id: str
    purpose: str
    resource_id: str
    bindings: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for value, label in (
            (self.subject_kind, "review subject_kind"),
            (self.project_id, "review project_id"),
            (self.purpose, "review purpose"),
            (self.resource_id, "review resource_id"),
        ):
            _require_text(value, label)
        seen: set[str] = set()
        previous = ""
        for key, value in self.bindings:
            _require_text(key, "review binding key")
            _require_text(value, "review binding value")
            if key in seen:
                raise ValueError("review binding keys must be unique")
            if previous and key <= previous:
                raise ValueError("review bindings must be sorted by key")
            seen.add(key)
            previous = key

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            (
                _REVIEW_SUBJECT_SCHEMA,
                self.subject_kind,
                self.project_id,
                self.purpose,
                self.resource_id,
                self.bindings,
            ),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    evidence_ref: str
    request_id: str
    issuer_id: str
    subject_fingerprint: str
    approved_at: datetime
    expires_at: datetime
    signature: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.evidence_ref, "review evidence_ref"),
            (self.request_id, "review request_id"),
            (self.issuer_id, "review issuer_id"),
            (self.subject_fingerprint, "review subject fingerprint"),
            (self.signature, "review signature"),
        ):
            _require_text(value, label)
        if self.approved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("review timestamps must be timezone-aware")
        if self.expires_at <= self.approved_at:
            raise ValueError("review expiry must follow approval time")


def _review_signature_payload(evidence: ReviewEvidence) -> bytes:
    payload = json.dumps(
        (
            _REVIEW_SIGNATURE_SCHEMA,
            evidence.evidence_ref,
            evidence.request_id,
            evidence.issuer_id,
            evidence.subject_fingerprint,
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
    approval_required: bool
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
            "approval_required": self.approval_required,
            "requested_at": _timestamp(self.requested_at),
            "expires_at": _timestamp(self.expires_at),
        }


@dataclass(frozen=True, slots=True)
class ReviewRequestView:
    request_id: str
    subject_kind: str
    project_id: str
    purpose: str
    resource_id: str
    bindings: tuple[tuple[str, str], ...]
    reason: str
    requested_at: datetime
    expires_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "subject_kind": self.subject_kind,
            "project_id": self.project_id,
            "purpose": self.purpose,
            "resource_id": self.resource_id,
            "bindings": [list(item) for item in self.bindings],
            "reason": self.reason,
            "requested_at": _timestamp(self.requested_at),
            "expires_at": _timestamp(self.expires_at),
        }


@dataclass(frozen=True, slots=True)
class _PendingApproval:
    intent: ActionIntent
    view: ApprovalRequestView


@dataclass(frozen=True, slots=True)
class _PendingReview:
    subject: ReviewSubject
    view: ReviewRequestView


class ProjectPurposeReviewVerifier(Protocol):
    """Framework-neutral shape consumed structurally by PF10-style review ports."""

    def verify(self, *, project_id: str, evidence_ref: str, purpose: str) -> bool: ...


class ExactReviewVerifier(Protocol):
    """Framework-neutral verifier for PF6/Toolsmith exact immutable subjects."""

    def verify_subject(
        self,
        subject: ReviewSubject,
        evidence_ref: str,
        *,
        now: datetime | None = None,
    ) -> bool: ...


class _HmacApprovalVerifier:
    __slots__ = ("_issuer_id", "_secret", "_lock", "_used")

    def __init__(
        self,
        *,
        issuer_id: str,
        secret: bytes,
        lock: RLock,
        used: set[tuple[str, str]],
    ) -> None:
        self._issuer_id = issuer_id
        self._secret = secret
        self._lock = lock
        self._used = used

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
            raise PermissionError("approval evidence came from an untrusted issuer")
        if (approval.issuer_id, approval.approval_id) in self._used:
            raise PermissionError("approval evidence was already used by trusted host")
        if approval.action_fingerprint != intent.approval_fingerprint:
            raise PermissionError("approval does not match the exact action")
        expected = hmac.new(
            self._secret, _action_signature_payload(approval), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, approval.signature):
            raise PermissionError("approval evidence signature is invalid")
        if now < approval.approved_at or now >= approval.expires_at:
            raise PermissionError("approval is not currently valid")

    def commit_locked(self, approval: ApprovalEvidence) -> None:
        self._used.add((approval.issuer_id, approval.approval_id))

    def verify(
        self,
        intent: ActionIntent,
        approval: ApprovalEvidence,
        *,
        now: datetime,
    ) -> None:
        with self._lock:
            self.validate_locked(intent, approval, now=now)


class _ExactReviewVerifier:
    __slots__ = ("_authority",)

    def __init__(self, authority: ApprovalAuthority) -> None:
        self._authority = authority

    def verify_subject(
        self,
        subject: ReviewSubject,
        evidence_ref: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        return self._authority.verify_review(subject, evidence_ref, now=now)


class _ProjectPurposeReviewAdapter:
    __slots__ = ("_authority", "_subject_kind")

    def __init__(self, authority: ApprovalAuthority, *, subject_kind: str) -> None:
        self._authority = authority
        self._subject_kind = _require_text(subject_kind, "review adapter subject_kind")

    def verify(self, *, project_id: str, evidence_ref: str, purpose: str) -> bool:
        try:
            subject = project_purpose_review_subject(
                subject_kind=self._subject_kind,
                project_id=project_id,
                purpose=purpose,
            )
            return self._authority.verify_review(subject, evidence_ref)
        except (LookupError, PermissionError, TypeError, ValueError):
            return False


def project_purpose_review_subject(
    *, subject_kind: str, project_id: str, purpose: str
) -> ReviewSubject:
    """Canonical subject for a consumer port whose exact identity is project+purpose+ref."""
    return ReviewSubject(
        subject_kind=subject_kind,
        project_id=project_id,
        purpose=purpose,
        resource_id=purpose,
    )


class ApprovalAuthority:
    """Trusted host-owned issuer for R4 action approvals and exact human reviews.

    The effective key is always process-instance ephemeral, even when deterministic seed bytes
    are supplied by tests. Restart therefore invalidates all pending requests and issued evidence;
    the human must review again. Runtime agents/JS receive only safe request views and opaque
    evidence references, never this authority, its key, signatures, or verifier internals.
    """

    def __init__(self, *, issuer_id: str | None = None, secret: bytes | None = None) -> None:
        resolved_issuer = f"host-{secrets.token_urlsafe(18)}" if issuer_id is None else issuer_id
        _require_text(resolved_issuer, "approval issuer_id")
        seed = secrets.token_bytes(32) if secret is None else bytes(secret)
        if len(seed) < 32:
            raise ValueError("approval authority secret seed must contain at least 32 bytes")
        process_nonce = secrets.token_bytes(32)
        self._issuer_id = resolved_issuer
        self._secret = hmac.new(
            seed,
            _PROCESS_KEY_SCHEMA + process_nonce,
            hashlib.sha256,
        ).digest()
        self._pending: dict[str, _PendingApproval] = {}
        self._approved: dict[str, ApprovalEvidence] = {}
        self._denied: set[str] = set()
        self._used_action_approvals: set[tuple[str, str]] = set()
        self._pending_reviews: dict[str, _PendingReview] = {}
        self._reviews: dict[str, tuple[ReviewSubject, ReviewEvidence]] = {}
        self._denied_reviews: set[str] = set()
        self._lock = RLock()

    @property
    def issuer_id(self) -> str:
        return self._issuer_id

    def verifier(self) -> ApprovalVerifier:
        return _HmacApprovalVerifier(
            issuer_id=self._issuer_id,
            secret=self._secret,
            lock=self._lock,
            used=self._used_action_approvals,
        )

    def exact_review_verifier(self) -> ExactReviewVerifier:
        return _ExactReviewVerifier(self)

    def project_purpose_review_verifier(self, *, subject_kind: str) -> ProjectPurposeReviewVerifier:
        return _ProjectPurposeReviewAdapter(self, subject_kind=subject_kind)

    def request(
        self,
        intent: ActionIntent,
        *,
        reason: str,
        now: datetime | None = None,
        ttl: timedelta = _DEFAULT_TTL,
    ) -> ApprovalRequestView:
        _require_text(reason, "approval reason")
        if not (intent.approval_required or intent.risk is ToolRisk.HIGH_IMPACT):
            raise ValueError("approval requests are reserved for approval-gated actions")
        _validate_ttl(ttl)
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
            approval_required=intent.approval_required,
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
            evidence = ApprovalEvidence(
                approval_id=unsigned.approval_id,
                request_id=unsigned.request_id,
                issuer_id=unsigned.issuer_id,
                action_fingerprint=unsigned.action_fingerprint,
                approved_at=unsigned.approved_at,
                expires_at=unsigned.expires_at,
                signature=hmac.new(
                    self._secret, _action_signature_payload(unsigned), hashlib.sha256
                ).hexdigest(),
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
            try:
                evidence = self._approved[request_id]
            except KeyError as exc:
                raise KeyError("approval evidence is unavailable") from exc
            if current >= evidence.expires_at:
                self._approved.pop(request_id, None)
                raise PermissionError("approval evidence expired")
            return evidence

    def request_review(
        self,
        subject: ReviewSubject,
        *,
        reason: str,
        now: datetime | None = None,
        ttl: timedelta = _DEFAULT_TTL,
    ) -> ReviewRequestView:
        _require_text(reason, "review reason")
        _validate_ttl(ttl)
        current = _aware_now(now)
        request_id = f"review-{secrets.token_urlsafe(18)}"
        view = ReviewRequestView(
            request_id=request_id,
            subject_kind=subject.subject_kind,
            project_id=subject.project_id,
            purpose=subject.purpose,
            resource_id=subject.resource_id,
            bindings=subject.bindings,
            reason=reason.strip(),
            requested_at=current,
            expires_at=current + ttl,
        )
        with self._lock:
            self._prune_unlocked(current)
            self._pending_reviews[request_id] = _PendingReview(subject=subject, view=view)
        return view

    def pending_review_views(self, *, now: datetime | None = None) -> tuple[ReviewRequestView, ...]:
        current = _aware_now(now)
        with self._lock:
            self._prune_unlocked(current)
            return tuple(
                item.view
                for item in sorted(
                    self._pending_reviews.values(),
                    key=lambda item: (item.view.requested_at, item.view.request_id),
                )
            )

    def approve_review(self, request_id: str, *, now: datetime | None = None) -> ReviewEvidence:
        current = _aware_now(now)
        with self._lock:
            self._prune_unlocked(current)
            if request_id in self._denied_reviews:
                raise PermissionError("review request was denied")
            if any(evidence.request_id == request_id for _, evidence in self._reviews.values()):
                raise PermissionError("review request was already approved")
            try:
                pending = self._pending_reviews.pop(request_id)
            except KeyError as exc:
                raise KeyError("unknown or expired review request") from exc
            evidence_ref = f"review-evidence-{secrets.token_urlsafe(18)}"
            unsigned = ReviewEvidence(
                evidence_ref=evidence_ref,
                request_id=request_id,
                issuer_id=self._issuer_id,
                subject_fingerprint=pending.subject.fingerprint,
                approved_at=current,
                expires_at=pending.view.expires_at,
                signature="pending",
            )
            evidence = ReviewEvidence(
                evidence_ref=unsigned.evidence_ref,
                request_id=unsigned.request_id,
                issuer_id=unsigned.issuer_id,
                subject_fingerprint=unsigned.subject_fingerprint,
                approved_at=unsigned.approved_at,
                expires_at=unsigned.expires_at,
                signature=hmac.new(
                    self._secret, _review_signature_payload(unsigned), hashlib.sha256
                ).hexdigest(),
            )
            self._reviews[evidence_ref] = (pending.subject, evidence)
            return evidence

    def deny_review(self, request_id: str, *, now: datetime | None = None) -> None:
        current = _aware_now(now)
        with self._lock:
            self._prune_unlocked(current)
            if any(evidence.request_id == request_id for _, evidence in self._reviews.values()):
                raise PermissionError("approved review cannot be denied")
            if request_id in self._denied_reviews:
                raise PermissionError("review request was already denied")
            try:
                self._pending_reviews.pop(request_id)
            except KeyError as exc:
                raise KeyError("unknown or expired review request") from exc
            self._denied_reviews.add(request_id)

    def verify_review(
        self,
        subject: ReviewSubject,
        evidence_ref: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        current = _aware_now(now)
        with self._lock:
            self._prune_unlocked(current)
            try:
                stored_subject, evidence = self._reviews[evidence_ref]
            except KeyError:
                return False
            if stored_subject != subject or evidence.subject_fingerprint != subject.fingerprint:
                return False
            if evidence.issuer_id != self._issuer_id:
                return False
            if current < evidence.approved_at or current >= evidence.expires_at:
                return False
            expected = hmac.new(
                self._secret, _review_signature_payload(evidence), hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(expected, evidence.signature)

    def _prune_unlocked(self, now: datetime) -> None:
        for request_id in [
            key for key, item in self._pending.items() if now >= item.view.expires_at
        ]:
            self._pending.pop(request_id, None)
        for request_id in [
            key for key, evidence in self._approved.items() if now >= evidence.expires_at
        ]:
            self._approved.pop(request_id, None)
        for request_id in [
            key for key, item in self._pending_reviews.items() if now >= item.view.expires_at
        ]:
            self._pending_reviews.pop(request_id, None)
        for evidence_ref in [
            key for key, (_, evidence) in self._reviews.items() if now >= evidence.expires_at
        ]:
            self._reviews.pop(evidence_ref, None)
