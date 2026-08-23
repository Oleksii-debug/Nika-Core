from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from threading import RLock
from typing import Protocol

_MAX_CREDENTIAL_LEASE_TTL_SECONDS = 900
_AUDIT_EVENT_PREFIX = "credential-event-"
_AUTHORITY_SCHEMA = "nika-product-factory-credential-authority-v1"


class CredentialBrokerError(ValueError):
    """Raised when Product Factory credential/identity invariants are violated."""


class CredentialState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class SecretRef:
    secret_ref: str
    project_id: str
    provider: str
    purpose: str
    scopes: frozenset[str]
    allowed_audiences: frozenset[str]
    generation: int = 1
    state: CredentialState = CredentialState.ACTIVE

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.secret_ref, self.project_id, self.provider, self.purpose)
        ):
            raise CredentialBrokerError("secret reference identity fields must not be empty")
        _positive_int(self.generation, "secret generation")
        if not isinstance(self.state, CredentialState):
            raise CredentialBrokerError("secret state must be a CredentialState")
        if not self.scopes or not self.allowed_audiences:
            raise CredentialBrokerError("secret scopes and audiences must not be empty")
        if any(not value.strip() for value in self.scopes | self.allowed_audiences):
            raise CredentialBrokerError("secret scopes and audiences must not contain empty values")


@dataclass(frozen=True, slots=True)
class IdentityRef:
    identity_ref: str
    project_id: str
    provider: str
    subject_ref: str
    secret_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.identity_ref, self.project_id, self.provider, self.subject_ref)
        ):
            raise CredentialBrokerError("identity reference fields must not be empty")
        if not self.secret_refs or any(not value.strip() for value in self.secret_refs):
            raise CredentialBrokerError("identity must bind at least one secret reference")
        if len(self.secret_refs) != len(set(self.secret_refs)):
            raise CredentialBrokerError("identity contains duplicate secret references")


@dataclass(frozen=True, slots=True)
class CredentialLease:
    lease_id: str
    secret_ref: str
    project_id: str
    audience: str
    scopes: frozenset[str]
    generation: int
    handle_ref: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _aware(self.issued_at)
        _aware(self.expires_at)
        _positive_int(self.generation, "credential lease generation")
        if self.expires_at <= self.issued_at:
            raise CredentialBrokerError("credential lease must expire after issuance")
        if not all(
            value.strip()
            for value in (
                self.lease_id,
                self.secret_ref,
                self.project_id,
                self.audience,
                self.handle_ref,
            )
        ):
            raise CredentialBrokerError("credential lease identity fields must not be empty")
        if not self.scopes or any(not scope.strip() for scope in self.scopes):
            raise CredentialBrokerError("credential lease scopes must not be empty")


@dataclass(frozen=True, slots=True)
class CredentialUseEvidence:
    event_id: str
    lease_id: str
    secret_ref: str
    project_id: str
    audience: str
    scope: str
    generation: int
    used_at: datetime

    def __post_init__(self) -> None:
        _aware(self.used_at)
        _positive_int(self.generation, "credential use generation")
        if not all(
            value.strip()
            for value in (
                self.event_id,
                self.lease_id,
                self.secret_ref,
                self.project_id,
                self.audience,
                self.scope,
            )
        ):
            raise CredentialBrokerError("credential use evidence fields must not be empty")


@dataclass(frozen=True, slots=True)
class CredentialAuditEvent:
    event_id: str
    action: str
    project_id: str
    secret_ref: str
    at: datetime
    detail: str

    def __post_init__(self) -> None:
        _aware(self.at)
        if not all(
            value.strip()
            for value in (self.event_id, self.action, self.project_id, self.secret_ref, self.detail)
        ):
            raise CredentialBrokerError("credential audit fields must not be empty")


@dataclass(frozen=True, slots=True)
class CredentialBrokerSnapshot:
    secrets: tuple[SecretRef, ...]
    identities: tuple[IdentityRef, ...]
    audit_events: tuple[CredentialAuditEvent, ...]
    next_lease: int
    next_event: int

    def __post_init__(self) -> None:
        counters = (self.next_lease, self.next_event)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in counters
        ):
            raise CredentialBrokerError("credential broker counters must be positive integers")


@dataclass(frozen=True, slots=True)
class _PendingCredentialLease:
    lease_id: str
    secret_ref: str
    project_id: str
    audience: str
    scopes: frozenset[str]
    generation: int
    ttl_seconds: int
    issued_at: datetime
    expires_at: datetime

    def matches(
        self,
        *,
        secret_ref: str,
        project_id: str,
        audience: str,
        scopes: frozenset[str],
        generation: int,
        ttl_seconds: int,
    ) -> bool:
        return (
            self.secret_ref == secret_ref
            and self.project_id == project_id
            and self.audience == audience
            and self.scopes == scopes
            and self.generation == generation
            and self.ttl_seconds == ttl_seconds
        )


class ProtectedSecretStorePort(Protocol):
    """OS/protected-store boundary. Raw secret material never crosses this port outward."""

    def contains(self, secret_ref: str, generation: int) -> bool: ...

    def bind_authority(
        self,
        *,
        secret_ref: str,
        generation: int,
        authority_fingerprint: str,
    ) -> None: ...

    def authority_matches(
        self,
        *,
        secret_ref: str,
        generation: int,
        authority_fingerprint: str,
    ) -> bool: ...

    def issue_handle(
        self,
        *,
        operation_id: str,
        secret_ref: str,
        generation: int,
        project_id: str,
        audience: str,
        scopes: frozenset[str],
        expires_at: datetime,
    ) -> str: ...

    def reconcile_handle(
        self,
        *,
        operation_id: str,
        secret_ref: str,
        generation: int,
        project_id: str,
        audience: str,
        scopes: frozenset[str],
        expires_at: datetime,
    ) -> str | None: ...

    def revoke_handles(self, secret_ref: str, generation: int) -> None: ...


@dataclass(slots=True)
class CredentialBroker:
    store: ProtectedSecretStorePort = field(repr=False)
    _secrets: dict[str, SecretRef] = field(default_factory=dict, init=False, repr=False)
    _identities: dict[str, IdentityRef] = field(default_factory=dict, init=False, repr=False)
    _leases: dict[str, CredentialLease] = field(default_factory=dict, init=False, repr=False)
    _audit: list[CredentialAuditEvent] = field(default_factory=list, init=False, repr=False)
    _pending_lease: _PendingCredentialLease | None = field(default=None, init=False, repr=False)
    _next_lease: int = field(default=1, init=False, repr=False)
    _next_event: int = field(default=1, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def register_secret(self, secret: SecretRef, *, now: datetime | None = None) -> None:
        instant = _aware(now or datetime.now(UTC))
        with self._lock:
            existing = self._secrets.get(secret.secret_ref)
            if existing is not None:
                raise CredentialBrokerError("secret reference is already registered")
            if not self.store.contains(secret.secret_ref, secret.generation):
                raise CredentialBrokerError(
                    "protected store does not contain referenced secret generation"
                )
            self.store.bind_authority(
                secret_ref=secret.secret_ref,
                generation=secret.generation,
                authority_fingerprint=_secret_authority_fingerprint(secret),
            )
            self._secrets[secret.secret_ref] = secret
            self._record(
                "register", secret.project_id, secret.secret_ref, instant, "reference registered"
            )

    def register_identity(self, identity: IdentityRef) -> None:
        with self._lock:
            if identity.identity_ref in self._identities:
                raise CredentialBrokerError("identity reference is already registered")
            secrets = tuple(self._require_secret(secret_ref) for secret_ref in identity.secret_refs)
            if any(secret.project_id != identity.project_id for secret in secrets):
                raise CredentialBrokerError("identity cannot bind credentials from another project")
            if any(secret.provider != identity.provider for secret in secrets):
                raise CredentialBrokerError("identity and credential providers must match")
            self._identities[identity.identity_ref] = identity

    def issue_lease(
        self,
        *,
        project_id: str,
        secret_ref: str,
        audience: str,
        scopes: frozenset[str],
        now: datetime | None = None,
        ttl_seconds: int = 300,
    ) -> CredentialLease:
        _positive_int(ttl_seconds, "credential lease ttl")
        if ttl_seconds > _MAX_CREDENTIAL_LEASE_TTL_SECONDS:
            raise CredentialBrokerError("credential lease ttl exceeds maximum")
        with self._lock:
            secret = self._authorized_secret(project_id, secret_ref)
            if secret.state is CredentialState.REVOKED:
                raise CredentialBrokerError("credential is revoked")
            if not scopes or not scopes <= secret.scopes:
                raise CredentialBrokerError("requested credential scopes exceed registered scope")
            if audience not in secret.allowed_audiences:
                raise CredentialBrokerError("requested credential audience is not allowed")
            if not self.store.contains(secret.secret_ref, secret.generation):
                raise CredentialBrokerError("protected store secret generation is unavailable")
            self._require_authority(secret)

            lease_id = f"credential-lease-{self._next_lease:08d}"
            pending = self._pending_lease
            if pending is None:
                instant = _aware(now or datetime.now(UTC))
                pending = _PendingCredentialLease(
                    lease_id,
                    secret.secret_ref,
                    project_id,
                    audience,
                    scopes,
                    secret.generation,
                    ttl_seconds,
                    instant,
                    instant + timedelta(seconds=ttl_seconds),
                )
                self._pending_lease = pending
            elif pending.lease_id != lease_id or not pending.matches(
                secret_ref=secret.secret_ref,
                project_id=project_id,
                audience=audience,
                scopes=scopes,
                generation=secret.generation,
                ttl_seconds=ttl_seconds,
            ):
                raise CredentialBrokerError(
                    "pending credential lease must be reconciled by retrying the original request"
                )

            try:
                handle_ref = self.store.issue_handle(
                    operation_id=pending.lease_id,
                    secret_ref=pending.secret_ref,
                    generation=pending.generation,
                    project_id=pending.project_id,
                    audience=pending.audience,
                    scopes=pending.scopes,
                    expires_at=pending.expires_at,
                )
            except RuntimeError:
                try:
                    handle_ref = self.store.reconcile_handle(
                        operation_id=pending.lease_id,
                        secret_ref=pending.secret_ref,
                        generation=pending.generation,
                        project_id=pending.project_id,
                        audience=pending.audience,
                        scopes=pending.scopes,
                        expires_at=pending.expires_at,
                    )
                except RuntimeError:
                    raise CredentialBrokerError(
                        "protected store handle issuance failed and could not be reconciled"
                    ) from None
                if handle_ref is None:
                    raise CredentialBrokerError(
                        "protected store handle issuance failed and could not be reconciled"
                    ) from None

            lease = CredentialLease(
                pending.lease_id,
                pending.secret_ref,
                pending.project_id,
                pending.audience,
                pending.scopes,
                pending.generation,
                handle_ref,
                pending.issued_at,
                pending.expires_at,
            )
            self._next_lease += 1
            self._leases[lease.lease_id] = lease
            self._pending_lease = None
            self._record("lease", project_id, secret_ref, pending.issued_at, "opaque lease issued")
            return lease

    def authorize_use(
        self,
        *,
        lease_id: str,
        project_id: str,
        scope: str,
        now: datetime | None = None,
    ) -> CredentialUseEvidence:
        instant = _aware(now or datetime.now(UTC))
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise CredentialBrokerError("unknown or invalidated credential lease")
            if lease.project_id != project_id:
                raise CredentialBrokerError("credential lease belongs to another project")
            if lease.expires_at <= instant:
                del self._leases[lease_id]
                raise CredentialBrokerError("credential lease has expired")
            secret = self._authorized_secret(project_id, lease.secret_ref)
            if secret.state is CredentialState.REVOKED or secret.generation != lease.generation:
                self._leases.pop(lease_id, None)
                raise CredentialBrokerError("credential lease generation is revoked or superseded")
            self._require_authority(secret)
            if scope not in lease.scopes:
                raise CredentialBrokerError("credential lease does not authorize requested scope")
            evidence = CredentialUseEvidence(
                self._new_event_id(),
                lease.lease_id,
                secret.secret_ref,
                project_id,
                lease.audience,
                scope,
                lease.generation,
                instant,
            )
            self._audit.append(
                CredentialAuditEvent(
                    evidence.event_id,
                    "use",
                    project_id,
                    secret.secret_ref,
                    instant,
                    f"audience={lease.audience};scope={scope}",
                )
            )
            return evidence

    def revoke(self, *, project_id: str, secret_ref: str, now: datetime | None = None) -> None:
        instant = _aware(now or datetime.now(UTC))
        with self._lock:
            secret = self._authorized_secret(project_id, secret_ref)
            self._require_authority(secret)
            if secret.state is CredentialState.REVOKED:
                return
            revoked = SecretRef(
                secret.secret_ref,
                secret.project_id,
                secret.provider,
                secret.purpose,
                secret.scopes,
                secret.allowed_audiences,
                secret.generation,
                CredentialState.REVOKED,
            )
            self._secrets[secret_ref] = revoked
            self._invalidate_generation(secret)
            self._record("revoke", project_id, secret_ref, instant, "reference revoked")

    def rotate(
        self,
        *,
        project_id: str,
        secret_ref: str,
        now: datetime | None = None,
    ) -> SecretRef:
        instant = _aware(now or datetime.now(UTC))
        with self._lock:
            secret = self._authorized_secret(project_id, secret_ref)
            self._require_authority(secret)
            next_generation = secret.generation + 1
            if not self.store.contains(secret_ref, next_generation):
                raise CredentialBrokerError(
                    "protected store does not contain next secret generation"
                )
            rotated = SecretRef(
                secret.secret_ref,
                secret.project_id,
                secret.provider,
                secret.purpose,
                secret.scopes,
                secret.allowed_audiences,
                next_generation,
                CredentialState.ACTIVE,
            )
            self.store.bind_authority(
                secret_ref=rotated.secret_ref,
                generation=rotated.generation,
                authority_fingerprint=_secret_authority_fingerprint(rotated),
            )
            self._invalidate_generation(secret)
            self._secrets[secret_ref] = rotated
            self._record("rotate", project_id, secret_ref, instant, "reference generation advanced")
            return rotated

    def get_secret_ref(self, *, project_id: str, secret_ref: str) -> SecretRef:
        """Resolve one already-known opaque reference without exposing an enumeration surface."""

        if not project_id.strip() or not secret_ref.strip():
            raise CredentialBrokerError("project_id and secret_ref must not be empty")
        with self._lock:
            return self._authorized_secret(project_id, secret_ref)

    def get_identity(self, *, project_id: str, identity_ref: str) -> IdentityRef:
        with self._lock:
            identity = self._identities.get(identity_ref)
            if identity is None or identity.project_id != project_id:
                raise CredentialBrokerError("identity reference is unavailable for project")
            return identity

    def audit_events(self, project_id: str) -> tuple[CredentialAuditEvent, ...]:
        if not project_id.strip():
            raise CredentialBrokerError("project_id must not be empty")
        with self._lock:
            return tuple(event for event in self._audit if event.project_id == project_id)

    def snapshot(self) -> CredentialBrokerSnapshot:
        with self._lock:
            return CredentialBrokerSnapshot(
                tuple(self._secrets[key] for key in sorted(self._secrets)),
                tuple(self._identities[key] for key in sorted(self._identities)),
                tuple(self._audit),
                self._next_lease,
                self._next_event,
            )

    def restore(self, snapshot: CredentialBrokerSnapshot) -> None:
        with self._lock:
            secret_ids = [secret.secret_ref for secret in snapshot.secrets]
            identity_ids = [identity.identity_ref for identity in snapshot.identities]
            audit_ids = [event.event_id for event in snapshot.audit_events]
            duplicate_secret_ids = len(secret_ids) != len(set(secret_ids))
            duplicate_identity_ids = len(identity_ids) != len(set(identity_ids))
            if duplicate_secret_ids or duplicate_identity_ids:
                raise CredentialBrokerError(
                    "credential broker snapshot contains duplicate identities"
                )
            if len(audit_ids) != len(set(audit_ids)):
                raise CredentialBrokerError(
                    "credential broker snapshot contains duplicate audit events"
                )
            audit_sequences = [_audit_event_sequence(event_id) for event_id in audit_ids]
            if any(current <= previous for previous, current in pairwise(audit_sequences)):
                raise CredentialBrokerError(
                    "credential broker snapshot audit events are not monotonic"
                )
            if audit_sequences and snapshot.next_event <= audit_sequences[-1]:
                raise CredentialBrokerError(
                    "credential broker snapshot audit counter was rolled back"
                )
            lease_event_count = sum(event.action == "lease" for event in snapshot.audit_events)
            if snapshot.next_lease <= lease_event_count:
                raise CredentialBrokerError(
                    "credential broker snapshot lease counter was rolled back"
                )
            secrets = {secret.secret_ref: secret for secret in snapshot.secrets}
            for identity in snapshot.identities:
                bound = [secrets.get(secret_ref) for secret_ref in identity.secret_refs]
                if any(secret is None for secret in bound):
                    raise CredentialBrokerError("snapshot identity references unknown secret")
                if any(
                    secret.project_id != identity.project_id
                    for secret in bound
                    if secret is not None
                ):
                    raise CredentialBrokerError("snapshot identity crosses project boundary")
                if any(
                    secret.provider != identity.provider
                    for secret in bound
                    if secret is not None
                ):
                    raise CredentialBrokerError(
                        "snapshot identity provider does not match credential provider"
                    )
            for event in snapshot.audit_events:
                secret = secrets.get(event.secret_ref)
                if secret is None or secret.project_id != event.project_id:
                    raise CredentialBrokerError(
                        "snapshot audit event crosses credential project boundary"
                    )
            for secret in snapshot.secrets:
                self._require_authority(secret)
                if secret.state is CredentialState.ACTIVE and not self.store.contains(
                    secret.secret_ref, secret.generation
                ):
                    raise CredentialBrokerError(
                        "protected store active secret generation is unavailable during restore"
                    )
            self._secrets = secrets
            self._identities = {
                identity.identity_ref: identity for identity in snapshot.identities
            }
            self._leases = {}
            self._pending_lease = None
            self._audit = list(snapshot.audit_events)
            self._next_lease = snapshot.next_lease
            self._next_event = snapshot.next_event

    def _require_secret(self, secret_ref: str) -> SecretRef:
        secret = self._secrets.get(secret_ref)
        if secret is None:
            raise CredentialBrokerError("unknown secret reference")
        return secret

    def _authorized_secret(self, project_id: str, secret_ref: str) -> SecretRef:
        secret = self._secrets.get(secret_ref)
        if secret is None or secret.project_id != project_id:
            raise CredentialBrokerError("secret reference is unavailable for project")
        return secret

    def _require_authority(self, secret: SecretRef) -> None:
        if not self.store.authority_matches(
            secret_ref=secret.secret_ref,
            generation=secret.generation,
            authority_fingerprint=_secret_authority_fingerprint(secret),
        ):
            raise CredentialBrokerError(
                "protected store credential authority does not match reference metadata"
            )

    def _invalidate_generation(self, secret: SecretRef) -> None:
        self.store.revoke_handles(secret.secret_ref, secret.generation)
        if self._pending_lease is not None and (
            self._pending_lease.secret_ref == secret.secret_ref
            and self._pending_lease.generation == secret.generation
        ):
            self._pending_lease = None
        for lease_id in [
            lease_id
            for lease_id, lease in self._leases.items()
            if lease.secret_ref == secret.secret_ref and lease.generation == secret.generation
        ]:
            del self._leases[lease_id]

    def _record(
        self,
        action: str,
        project_id: str,
        secret_ref: str,
        at: datetime,
        detail: str,
    ) -> None:
        self._audit.append(
            CredentialAuditEvent(
                self._new_event_id(), action, project_id, secret_ref, at, detail
            )
        )

    def _new_event_id(self) -> str:
        event_id = f"credential-event-{self._next_event:08d}"
        self._next_event += 1
        return event_id


def _secret_authority_fingerprint(secret: SecretRef) -> str:
    payload = {
        "schema": _AUTHORITY_SCHEMA,
        "secret_ref": secret.secret_ref,
        "generation": secret.generation,
        "project_id": secret.project_id,
        "provider": secret.provider,
        "purpose": secret.purpose,
        "scopes": sorted(secret.scopes),
        "allowed_audiences": sorted(secret.allowed_audiences),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _audit_event_sequence(event_id: str) -> int:
    if not event_id.startswith(_AUDIT_EVENT_PREFIX):
        raise CredentialBrokerError(
            "credential broker snapshot contains invalid audit event identity"
        )
    suffix = event_id[len(_AUDIT_EVENT_PREFIX) :]
    if not suffix or not suffix.isascii() or not suffix.isdigit():
        raise CredentialBrokerError(
            "credential broker snapshot contains invalid audit event identity"
        )
    sequence = int(suffix)
    if sequence < 1 or event_id != f"{_AUDIT_EVENT_PREFIX}{sequence:08d}":
        raise CredentialBrokerError(
            "credential broker snapshot contains invalid audit event identity"
        )
    return sequence


def _positive_int(value: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CredentialBrokerError(f"{label} must be a positive integer")
    return value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CredentialBrokerError("credential timestamps must be timezone-aware")
    return value.astimezone(UTC)
