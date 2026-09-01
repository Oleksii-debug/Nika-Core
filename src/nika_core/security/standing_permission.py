from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.security.policy import ActionIntent
from nika_core.tools import (
    ToolAuthorization,
    ToolCall,
    ToolRisk,
    ToolSpec,
    tool_arguments_fingerprint,
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_ACTION_RE = re.compile(r"^[a-z0-9_.:-]{1,160}$")
_BROAD = frozenset({"*", "all", "any", "everything"})
_STANDING_AUTHORITY_VERSION = "nika-v01-standing-permission-v1"
_RISK_ORDER = {
    ToolRisk.READ_ONLY: 0,
    ToolRisk.LOCAL_WRITE: 1,
    ToolRisk.EXTERNAL_SIDE_EFFECT: 2,
    ToolRisk.HIGH_IMPACT: 3,
}


class StandingPermissionConflictError(RuntimeError):
    """An existing permission id was presented with different authority."""


class StandingPermissionIntegrityError(RuntimeError):
    """Durable standing-permission state is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class PermissionContext:
    user_id: str
    project_id: str
    task_id: str

    def __post_init__(self) -> None:
        _identity(self.user_id, "user_id")
        _identity(self.project_id, "project_id")
        _identity(self.task_id, "task_id")


@dataclass(frozen=True, slots=True)
class StandingPermissionBinding:
    """Trusted host metadata that callers cannot substitute through ToolCall."""

    permission_id: str
    subject_id: str
    context: PermissionContext
    target: str
    resource_id: str
    network_host: str | None

    def __post_init__(self) -> None:
        _permission_id(self.permission_id)
        _identity(self.subject_id, "subject_id")
        _identity(self.target, "target")
        _identity(self.resource_id, "resource_id")
        if self.network_host is not None:
            object.__setattr__(self, "network_host", _site(self.network_host))


@dataclass(frozen=True, slots=True)
class StandingPermissionScope:
    """Finite authority for one exact action class and bounded canonical identities."""

    subject_id: str
    context: PermissionContext
    action_class: str
    targets: tuple[str, ...]
    sites: tuple[str, ...]
    resources: tuple[str, ...]
    risk_ceiling: ToolRisk
    granted_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _identity(self.subject_id, "subject_id")
        _action(self.action_class)
        if self.risk_ceiling is ToolRisk.HIGH_IMPACT:
            raise ValueError("high-impact actions require fresh explicit per-action approval")
        object.__setattr__(self, "targets", _ids(self.targets, "target", required=True))
        object.__setattr__(self, "sites", _sites(self.sites))
        object.__setattr__(self, "resources", _ids(self.resources, "resource", required=True))
        granted_at = _utc(self.granted_at, "granted_at")
        expires_at = _utc(self.expires_at, "expires_at")
        if expires_at <= granted_at:
            raise ValueError("standing permission must have a finite expiry after grant time")
        object.__setattr__(self, "granted_at", granted_at)
        object.__setattr__(self, "expires_at", expires_at)


@dataclass(frozen=True, slots=True)
class StandingPermissionUse:
    subject_id: str
    context: PermissionContext
    intent: ActionIntent
    resource_id: str

    def __post_init__(self) -> None:
        _identity(self.subject_id, "subject_id")
        _action(self.intent.tool_id)
        _identity(self.intent.target, "target")
        _identity(self.resource_id, "resource_id")
        if self.intent.network_host is not None:
            _site(self.intent.network_host)


@dataclass(frozen=True, slots=True)
class _ScopeRecord:
    subject_hash: str
    user_hash: str
    project_hash: str
    task_hash: str
    action_class: str
    risk_ceiling: ToolRisk
    target_hashes: tuple[str, ...]
    site_hashes: tuple[str, ...]
    resource_hashes: tuple[str, ...]
    granted_at: datetime
    expires_at: datetime

    @property
    def payload(self) -> dict[str, object]:
        return {
            "subject_hash": self.subject_hash,
            "user_hash": self.user_hash,
            "project_hash": self.project_hash,
            "task_hash": self.task_hash,
            "action_class": self.action_class,
            "risk_ceiling": self.risk_ceiling.value,
            "target_hashes": self.target_hashes,
            "site_hashes": self.site_hashes,
            "resource_hashes": self.resource_hashes,
            "granted_at": self.granted_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    @property
    def fingerprint(self) -> str:
        return _hash(_json(self.payload))


@dataclass(frozen=True, slots=True)
class StoredStandingPermission:
    permission_id: str
    parent_permission_id: str | None
    scope_fingerprint: str
    granted_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    _scope: _ScopeRecord


class StandingPermissionStore:
    """Durable bounded authority; not a replacement for mandatory per-action approval."""

    def __init__(self, store: SQLiteStore, *, audit_log: AuditLog | None = None) -> None:
        self._store = store
        self._audit_log = audit_log

    def initialize(self) -> None:
        with self._store.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS standing_permission_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT MAX(version) AS version FROM standing_permission_schema_migrations"
            ).fetchone()
            version = int(row["version"] or 0)
            if version > 1:
                raise RuntimeError(
                    f"standing permission schema {version} is newer than supported 1"
                )
            if version == 0:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS standing_permissions (
                        permission_id TEXT PRIMARY KEY,
                        parent_permission_id TEXT,
                        scope_json TEXT NOT NULL,
                        scope_fingerprint TEXT NOT NULL,
                        revoked_at TEXT,
                        FOREIGN KEY(parent_permission_id)
                            REFERENCES standing_permissions(permission_id)
                    )"""
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_standing_permissions_parent "
                    "ON standing_permissions(parent_permission_id)"
                )
                conn.execute(
                    "INSERT INTO standing_permission_schema_migrations(version, applied_at) "
                    "VALUES (1, ?)",
                    (datetime.now(UTC).isoformat(),),
                )

    def grant(
        self,
        *,
        permission_id: str,
        scope: StandingPermissionScope,
    ) -> StoredStandingPermission:
        """Persist a root scope already granted through canonical user authority."""
        _permission_id(permission_id)
        material = _material(scope)
        with self._store.connection() as conn:
            existing = self._get(conn, permission_id)
            if existing is not None:
                if (
                    existing.parent_permission_id is None
                    and existing.scope_fingerprint == material.fingerprint
                ):
                    return existing
                raise StandingPermissionConflictError(
                    "permission id belongs to different authority; "
                    "changed scope requires new authority"
                )
            record = self._insert(conn, permission_id, None, material)
            self._audit(conn, "standing_permission.granted", record)
            return record

    def delegate(
        self,
        *,
        parent_permission_id: str,
        permission_id: str,
        scope: StandingPermissionScope,
        delegated_by_subject_id: str,
    ) -> StoredStandingPermission:
        _permission_id(parent_permission_id)
        _permission_id(permission_id)
        _identity(delegated_by_subject_id, "delegated_by_subject_id")
        if parent_permission_id == permission_id:
            raise PermissionError("permission cannot delegate to itself")
        child = _material(scope)
        with self._store.connection() as conn:
            existing = self._get(conn, permission_id)
            if existing is not None:
                if (
                    existing.parent_permission_id == parent_permission_id
                    and existing.scope_fingerprint == child.fingerprint
                ):
                    return existing
                raise StandingPermissionConflictError(
                    "permission id belongs to different authority; "
                    "changed scope requires new authority"
                )
            parent = self._require(conn, parent_permission_id)
            self._active(parent, scope.granted_at)
            if _hash(delegated_by_subject_id) != parent._scope.subject_hash:
                raise PermissionError("delegator is not the parent permission subject")
            self._child_subset(parent._scope, child)
            record = self._insert(conn, permission_id, parent_permission_id, child)
            self._audit(conn, "standing_permission.delegated", record)
            return record

    def revoke(
        self,
        permission_id: str,
        *,
        revoked_at: datetime | None = None,
    ) -> StoredStandingPermission:
        _permission_id(permission_id)
        instant = _utc(revoked_at or datetime.now(UTC), "revoked_at")
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._require(conn, permission_id)
            if current.revoked_at is not None:
                return current
            if instant < current.granted_at:
                raise ValueError("revocation time cannot precede grant time")
            conn.execute(
                "UPDATE standing_permissions SET revoked_at = ? WHERE permission_id = ?",
                (instant.isoformat(), permission_id),
            )
            revoked = self._require(conn, permission_id)
            self._audit(conn, "standing_permission.revoked", revoked)
            return revoked

    def authorize(
        self,
        permission_id: str,
        use: StandingPermissionUse,
        *,
        now: datetime | None = None,
    ) -> StoredStandingPermission:
        _permission_id(permission_id)
        instant = _utc(now or datetime.now(UTC), "now")
        try:
            with self._store.connection() as conn:
                # Authorization and revocation share one SQLite write-serialization point.
                # This prevents revoke from committing while an active-state decision is
                # still being returned to the effect-dispatch policy.
                conn.execute("BEGIN IMMEDIATE")
                current = self._require(conn, permission_id)
                cursor = current
                visited: set[str] = set()
                while True:
                    if cursor.permission_id in visited:
                        raise StandingPermissionIntegrityError("standing permission parent cycle")
                    visited.add(cursor.permission_id)
                    self._active(cursor, instant)
                    self._within(
                        cursor._scope,
                        use,
                        check_subject=cursor.permission_id == current.permission_id,
                    )
                    if cursor.parent_permission_id is None:
                        break
                    cursor = self._require(conn, cursor.parent_permission_id)
                self._audit(conn, "standing_permission.used", current, use_risk=use.intent.risk)
                return current
        except (PermissionError, StandingPermissionIntegrityError):
            self._audit_denial(permission_id, use)
            raise

    def get(self, permission_id: str) -> StoredStandingPermission | None:
        _permission_id(permission_id)
        with self._store.connection() as conn:
            return self._get(conn, permission_id)

    @staticmethod
    def _within(scope: _ScopeRecord, use: StandingPermissionUse, *, check_subject: bool) -> None:
        if use.intent.risk is ToolRisk.HIGH_IMPACT:
            raise PermissionError("high-impact action requires fresh explicit per-action approval")
        if use.intent.tool_id != scope.action_class:
            raise PermissionError("action class is outside standing permission scope")
        if _RISK_ORDER[use.intent.risk] > _RISK_ORDER[scope.risk_ceiling]:
            raise PermissionError("risk exceeds standing permission ceiling")
        if check_subject and _hash(use.subject_id) != scope.subject_hash:
            raise PermissionError("subject is outside standing permission scope")
        if _hash(use.context.user_id) != scope.user_hash:
            raise PermissionError("user context is outside standing permission scope")
        if _hash(use.context.project_id) != scope.project_hash:
            raise PermissionError("project context is outside standing permission scope")
        if _hash(use.context.task_id) != scope.task_hash:
            raise PermissionError("task context is outside standing permission scope")
        if _hash(use.intent.target) not in scope.target_hashes:
            raise PermissionError("target is outside standing permission scope")
        if _hash(use.resource_id) not in scope.resource_hashes:
            raise PermissionError("resource is outside standing permission scope")
        if use.intent.network_host is None:
            if scope.site_hashes:
                raise PermissionError("site-bound standing permission requires an exact site")
        elif _hash(_site(use.intent.network_host)) not in scope.site_hashes:
            raise PermissionError("site is outside standing permission scope")

    @staticmethod
    def _child_subset(parent: _ScopeRecord, child: _ScopeRecord) -> None:
        if child.action_class != parent.action_class:
            raise PermissionError("child cannot widen or change action class")
        if _RISK_ORDER[child.risk_ceiling] > _RISK_ORDER[parent.risk_ceiling]:
            raise PermissionError("child cannot widen risk ceiling")
        if child.subject_hash == parent.subject_hash:
            raise PermissionError("delegation must bind a distinct child subject")
        if (child.user_hash, child.project_hash, child.task_hash) != (
            parent.user_hash,
            parent.project_hash,
            parent.task_hash,
        ):
            raise PermissionError("child cannot change user/project/task authority context")
        if not set(child.target_hashes).issubset(parent.target_hashes):
            raise PermissionError("child cannot widen target scope")
        if not set(child.site_hashes).issubset(parent.site_hashes):
            raise PermissionError("child cannot widen site scope")
        if not set(child.resource_hashes).issubset(parent.resource_hashes):
            raise PermissionError("child cannot widen resource scope")
        if child.granted_at < parent.granted_at:
            raise PermissionError("child authority cannot predate parent authority")
        if child.expires_at > parent.expires_at:
            raise PermissionError("child cannot extend parent expiry")

    @staticmethod
    def _active(permission: StoredStandingPermission, now: datetime) -> None:
        if permission.revoked_at is not None:
            raise PermissionError("standing permission is revoked")
        if now < permission.granted_at:
            raise PermissionError("standing permission is not active yet")
        if now >= permission.expires_at:
            raise PermissionError("standing permission is expired")

    def _insert(
        self,
        conn: sqlite3.Connection,
        permission_id: str,
        parent_permission_id: str | None,
        scope: _ScopeRecord,
    ) -> StoredStandingPermission:
        conn.execute(
            "INSERT INTO standing_permissions("
            "permission_id, parent_permission_id, scope_json, scope_fingerprint, revoked_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (permission_id, parent_permission_id, _json(scope.payload), scope.fingerprint),
        )
        return self._require(conn, permission_id)

    def _get(
        self,
        conn: sqlite3.Connection,
        permission_id: str,
    ) -> StoredStandingPermission | None:
        row = conn.execute(
            "SELECT permission_id, parent_permission_id, scope_json, scope_fingerprint, revoked_at "
            "FROM standing_permissions WHERE permission_id = ?",
            (permission_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            scope = _scope_from_json(row["scope_json"])
            fingerprint = str(row["scope_fingerprint"])
            revoked_at = (
                None
                if row["revoked_at"] is None
                else _utc(datetime.fromisoformat(row["revoked_at"]), "revoked_at")
            )
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise StandingPermissionIntegrityError("standing permission row is malformed") from exc
        if fingerprint != scope.fingerprint:
            raise StandingPermissionIntegrityError("standing permission scope fingerprint mismatch")
        return StoredStandingPermission(
            permission_id=row["permission_id"],
            parent_permission_id=row["parent_permission_id"],
            scope_fingerprint=fingerprint,
            granted_at=scope.granted_at,
            expires_at=scope.expires_at,
            revoked_at=revoked_at,
            _scope=scope,
        )

    def _require(self, conn: sqlite3.Connection, permission_id: str) -> StoredStandingPermission:
        record = self._get(conn, permission_id)
        if record is None:
            raise PermissionError("standing permission does not exist")
        return record

    def _audit(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        permission: StoredStandingPermission,
        *,
        use_risk: ToolRisk | None = None,
    ) -> None:
        if self._audit_log is None:
            return
        scope = permission._scope
        payload: dict[str, object] = {
            "action_class": scope.action_class,
            "context_fingerprint": _hash(
                f"{scope.user_hash}:{scope.project_hash}:{scope.task_hash}"
            ),
            "delegated": permission.parent_permission_id is not None,
            "expires_at": permission.expires_at.isoformat(),
            "resource_count": len(scope.resource_hashes),
            "risk_ceiling": scope.risk_ceiling.value,
            "scope_fingerprint": permission.scope_fingerprint,
            "site_count": len(scope.site_hashes),
            "subject_fingerprint": scope.subject_hash,
            "target_count": len(scope.target_hashes),
        }
        if use_risk is not None:
            payload["use_risk"] = use_risk.value
        if permission.revoked_at is not None:
            payload["revoked_at"] = permission.revoked_at.isoformat()
        self._audit_log.append_with_connection(
            conn,
            event_type=event_type,
            entity_type="standing_permission",
            entity_id=permission.permission_id,
            payload=payload,
        )

    def _audit_denial(self, permission_id: str, use: StandingPermissionUse) -> None:
        if self._audit_log is None:
            return
        self._audit_log.append(
            event_type="standing_permission.denied",
            entity_type="standing_permission",
            entity_id=permission_id,
            payload={
                "action_class": use.intent.tool_id,
                "reason": "scope_or_state_denied",
                "use_risk": use.intent.risk.value,
            },
        )


class StandingPermissionPolicy:
    """Adapt bounded standing authority to the canonical ToolExecutor policy boundary."""

    def __init__(
        self,
        permissions: StandingPermissionStore,
        binding: StandingPermissionBinding,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._permissions = permissions
        self._binding = binding
        self._clock = clock or (lambda: datetime.now(UTC))

    async def __call__(self, spec: ToolSpec, call: ToolCall) -> ToolAuthorization:
        binding = self._binding
        if spec.tool_id != call.tool_id:
            raise PermissionError("tool call does not match the registered action class")
        if spec.risk is ToolRisk.HIGH_IMPACT:
            raise PermissionError("high-impact action requires fresh explicit per-action approval")
        if spec.risk is not ToolRisk.EXTERNAL_SIDE_EFFECT:
            raise PermissionError("standing policy is only an external-effect approval adapter")
        if call.task_id != binding.context.task_id:
            raise PermissionError("tool call task is outside standing permission context")
        _identity(call.call_id, "call_id")

        intent = ActionIntent(
            action_id=call.call_id,
            tool_id=spec.tool_id,
            risk=spec.risk,
            target=binding.target,
            network_host=binding.network_host,
            task_id=call.task_id,
            project_id=binding.context.project_id,
            site=binding.network_host,
            resource=binding.resource_id,
            arguments=call.arguments,
            effect_id=call.call_id,
            authority_version=_STANDING_AUTHORITY_VERSION,
        )
        permission = self._permissions.authorize(
            binding.permission_id,
            StandingPermissionUse(
                subject_id=binding.subject_id,
                context=binding.context,
                intent=intent,
                resource_id=binding.resource_id,
            ),
            now=self._clock(),
        )
        authorized_intent = ActionIntent(
            action_id=intent.action_id,
            tool_id=intent.tool_id,
            risk=intent.risk,
            target=intent.target,
            network_host=intent.network_host,
            task_id=intent.task_id,
            project_id=intent.project_id,
            site=intent.site,
            resource=intent.resource,
            arguments=call.arguments,
            effect_id=intent.effect_id,
            authority_version=intent.authority_version,
            scope=(
                ("standing_permission_id", permission.permission_id),
                ("standing_scope_fingerprint", permission.scope_fingerprint),
            ),
        )
        arguments_fingerprint = tool_arguments_fingerprint(call.arguments)
        if arguments_fingerprint != authorized_intent.arguments_fingerprint:
            raise StandingPermissionIntegrityError(
                "standing authorization argument canonicalization mismatch"
            )
        return ToolAuthorization(
            tool_id=spec.tool_id,
            task_id=call.task_id,
            risk=spec.risk,
            arguments_fingerprint=arguments_fingerprint,
            effect_fingerprint=authorized_intent.effect_fingerprint,
            approval_fingerprint=authorized_intent.approval_fingerprint,
        )


def _material(scope: StandingPermissionScope) -> _ScopeRecord:
    return _ScopeRecord(
        subject_hash=_hash(scope.subject_id),
        user_hash=_hash(scope.context.user_id),
        project_hash=_hash(scope.context.project_id),
        task_hash=_hash(scope.context.task_id),
        action_class=scope.action_class,
        risk_ceiling=scope.risk_ceiling,
        target_hashes=tuple(sorted(_hash(value) for value in scope.targets)),
        site_hashes=tuple(sorted(_hash(value) for value in scope.sites)),
        resource_hashes=tuple(sorted(_hash(value) for value in scope.resources)),
        granted_at=scope.granted_at,
        expires_at=scope.expires_at,
    )


def _scope_from_json(payload: str) -> _ScopeRecord:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise StandingPermissionIntegrityError("standing permission scope is malformed")
    try:
        scope = _ScopeRecord(
            subject_hash=str(value["subject_hash"]),
            user_hash=str(value["user_hash"]),
            project_hash=str(value["project_hash"]),
            task_hash=str(value["task_hash"]),
            action_class=str(value["action_class"]),
            risk_ceiling=ToolRisk(value["risk_ceiling"]),
            target_hashes=tuple(value["target_hashes"]),
            site_hashes=tuple(value["site_hashes"]),
            resource_hashes=tuple(value["resource_hashes"]),
            granted_at=_utc(datetime.fromisoformat(value["granted_at"]), "granted_at"),
            expires_at=_utc(datetime.fromisoformat(value["expires_at"]), "expires_at"),
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise StandingPermissionIntegrityError("standing permission scope is malformed") from exc
    if not all(_is_hash(item) for item in _scope_hashes(scope)):
        raise StandingPermissionIntegrityError("standing permission scope hash is malformed")
    if any(
        items != tuple(sorted(set(items)))
        for items in (scope.target_hashes, scope.site_hashes, scope.resource_hashes)
    ):
        raise StandingPermissionIntegrityError("standing permission scope is non-canonical")
    if not scope.target_hashes or not scope.resource_hashes:
        raise StandingPermissionIntegrityError("standing permission scope is incomplete")
    _action(scope.action_class)
    if scope.risk_ceiling is ToolRisk.HIGH_IMPACT or scope.expires_at <= scope.granted_at:
        raise StandingPermissionIntegrityError("standing permission scope violates policy")
    return scope


def _scope_hashes(scope: _ScopeRecord) -> tuple[str, ...]:
    return (
        scope.subject_hash,
        scope.user_hash,
        scope.project_hash,
        scope.task_hash,
        *scope.target_hashes,
        *scope.site_hashes,
        *scope.resource_hashes,
    )


def _permission_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or _SAFE_ID_RE.fullmatch(value) is None
        or value.casefold() in _BROAD
    ):
        raise ValueError("permission_id must be one safe opaque identifier")


def _action(value: str) -> None:
    if not isinstance(value, str) or _ACTION_RE.fullmatch(value) is None:
        raise ValueError("action class must be one exact canonical class")
    if value.casefold() in _BROAD:
        raise ValueError("action class cannot contain broad or wildcard authority")


def _identity(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        raise ValueError(f"{label} must be a non-empty canonical identity")
    if value.casefold() in _BROAD or "*" in value or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} cannot contain broad or wildcard authority")


def _ids(values: tuple[str, ...], label: str, *, required: bool) -> tuple[str, ...]:
    if not isinstance(values, tuple) or (required and not values):
        raise ValueError(f"{label} scope must be an explicit non-empty tuple")
    for value in values:
        _identity(value, label)
    return tuple(sorted(set(values)))


def _site(value: str) -> str:
    _identity(value, "site")
    normalized = value.lower().rstrip(".")
    if not normalized or any(char in normalized for char in "/\\?#@") or "://" in normalized:
        raise ValueError("site scope must be one exact host identity")
    return normalized


def _sites(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError("site scope must be an explicit tuple")
    return tuple(sorted({_site(value) for value in values}))


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
