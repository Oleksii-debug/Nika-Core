from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.product_factory_ansible_staging import (
    AuthorizedStagingTarget,
    StagingAdapterError,
)
from nika_core.product_factory_build_deployment_handoff import (
    BuildDeploymentAuthority,
    BuildDeploymentHandoffError,
)
from nika_core.product_factory_deployment import EnvironmentIdentity, EnvironmentTier

_SCHEMA_VERSION = 1
_SCHEMA_NAME = "nika.product-factory.packaged-staging-authority.v1"
_MAX_IDENTITY_BYTES = 1024
_MAX_RELEASE_VERSION_BYTES = 256
_MAX_MIGRATION_REFS = 256
_MAX_TARGET_FIELD_BYTES = 4096


class PackagedStagingAuthorityError(ValueError):
    """Raised when packaged PF5 -> PF6 staging authority is not exact/current."""


@dataclass(frozen=True, slots=True)
class PackagedStagingAuthoritySnapshot:
    authority: BuildDeploymentAuthority
    authority_digest: str
    target_digest: str
    revoked: bool

    def __post_init__(self) -> None:
        authority = _snapshot_authority(self.authority)
        authority_digest = _digest(self.authority_digest, "authority_digest")
        target_digest = _digest(self.target_digest, "target_digest")
        if type(self.revoked) is not bool:
            raise PackagedStagingAuthorityError("revoked must be an exact boolean")
        expected = _authority_digest(
            authority=authority,
            target_digest=target_digest,
        )
        if expected != authority_digest:
            raise PackagedStagingAuthorityError(
                "staging authority snapshot digest does not match its payload"
            )
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "authority_digest", authority_digest)
        object.__setattr__(self, "target_digest", target_digest)


class PackagedStagingAuthorityStore:
    """Durable host-owned release/staging authority for exact PF5 work.

    The provider target remains owned by the packaged staging-provider composition.
    SQLite stores only its digest alongside one immutable per-work release authorization,
    so restart requires the same exact provider target without duplicating inventory or
    authorization-reference material into this authority table.
    """

    def __init__(
        self,
        store: SQLiteStore,
        *,
        target: AuthorizedStagingTarget,
    ) -> None:
        if type(store) is not SQLiteStore:
            raise TypeError("store must be exact SQLiteStore")
        self._store = store
        self._target = _snapshot_target(target)
        self._target_digest = _target_digest(self._target)
        self._audit = AuditLog(store)
        self._initialize()

    @property
    def target(self) -> AuthorizedStagingTarget:
        """Return a detached canonical target for provider composition."""

        return _snapshot_target(self._target)

    def authorize(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
        release_version: str,
        migration_refs: tuple[str, ...] = (),
    ) -> PackagedStagingAuthoritySnapshot:
        authority = self._make_authority(
            project_id=project_id,
            repository_id=repository_id,
            work_id=work_id,
            release_version=release_version,
            migration_refs=migration_refs,
        )
        payload_digest = _authority_digest(
            authority=authority,
            target_digest=self._target_digest,
        )
        migration_json = _encode_migration_refs(authority.migration_refs)
        now = datetime.now(UTC).isoformat()

        try:
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM product_factory_staging_work_authority "
                    "WHERE project_id = ? AND repository_id = ? AND work_id = ?",
                    (
                        authority.project_id,
                        authority.repository_id,
                        authority.work_id,
                    ),
                ).fetchone()
                if row is not None:
                    existing = self._snapshot_row(row)
                    if existing.revoked:
                        raise PackagedStagingAuthorityError(
                            "revoked PF5 work cannot be reauthorized"
                        )
                    if (
                        existing.authority != authority
                        or existing.authority_digest != payload_digest
                        or existing.target_digest != self._target_digest
                    ):
                        raise PackagedStagingAuthorityError(
                            "PF5 work is already bound to different staging authority"
                        )
                    return existing

                conn.execute(
                    "INSERT INTO product_factory_staging_work_authority "
                    "(project_id, repository_id, work_id, release_version, "
                    "migration_refs_json, target_digest, authority_digest, "
                    "authorized_at, revoked_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (
                        authority.project_id,
                        authority.repository_id,
                        authority.work_id,
                        authority.release_version,
                        migration_json,
                        self._target_digest,
                        payload_digest,
                        now,
                    ),
                )
                self._audit.append_with_connection(
                    conn,
                    event_type="product_factory.staging_authority.authorized",
                    entity_type="product_factory_build_work",
                    entity_id=authority.work_id,
                    payload={
                        "project_id": authority.project_id,
                        "repository_id": authority.repository_id,
                        "authority_digest": payload_digest,
                        "target_digest": self._target_digest,
                    },
                )
        except sqlite3.Error as exc:
            raise PackagedStagingAuthorityError(
                "packaged staging authority could not be persisted"
            ) from exc

        return PackagedStagingAuthoritySnapshot(
            authority,
            payload_digest,
            self._target_digest,
            False,
        )

    def revoke(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
        expected_authority_digest: str,
    ) -> PackagedStagingAuthoritySnapshot:
        project_id = _text(project_id, "project_id")
        repository_id = _text(repository_id, "repository_id")
        work_id = _text(work_id, "work_id")
        expected = _digest(expected_authority_digest, "expected_authority_digest")

        try:
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM product_factory_staging_work_authority "
                    "WHERE project_id = ? AND repository_id = ? AND work_id = ?",
                    (project_id, repository_id, work_id),
                ).fetchone()
                if row is None:
                    raise PackagedStagingAuthorityError(
                        "PF5 work has no packaged staging authority to revoke"
                    )
                current = self._snapshot_row(row)
                if current.authority_digest != expected:
                    raise PackagedStagingAuthorityError(
                        "staging authority changed; reload before revocation"
                    )
                if current.revoked:
                    return current

                revoked_at = datetime.now(UTC).isoformat()
                updated = conn.execute(
                    "UPDATE product_factory_staging_work_authority "
                    "SET revoked_at = ? "
                    "WHERE project_id = ? AND repository_id = ? AND work_id = ? "
                    "AND authority_digest = ? AND revoked_at IS NULL",
                    (
                        revoked_at,
                        project_id,
                        repository_id,
                        work_id,
                        expected,
                    ),
                )
                if updated.rowcount != 1:
                    raise PackagedStagingAuthorityError(
                        "staging authority changed during revocation"
                    )
                self._audit.append_with_connection(
                    conn,
                    event_type="product_factory.staging_authority.revoked",
                    entity_type="product_factory_build_work",
                    entity_id=work_id,
                    payload={
                        "project_id": project_id,
                        "repository_id": repository_id,
                        "authority_digest": expected,
                    },
                )
        except sqlite3.Error as exc:
            raise PackagedStagingAuthorityError(
                "packaged staging authority revocation could not be persisted"
            ) from exc

        return PackagedStagingAuthoritySnapshot(
            current.authority,
            current.authority_digest,
            current.target_digest,
            True,
        )

    def snapshot(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
    ) -> PackagedStagingAuthoritySnapshot:
        project_id = _text(project_id, "project_id")
        repository_id = _text(repository_id, "repository_id")
        work_id = _text(work_id, "work_id")
        try:
            with self._store.connection() as conn:
                row = conn.execute(
                    "SELECT * FROM product_factory_staging_work_authority "
                    "WHERE project_id = ? AND repository_id = ? AND work_id = ?",
                    (project_id, repository_id, work_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise PackagedStagingAuthorityError(
                "packaged staging authority could not be read"
            ) from exc
        if row is None:
            raise PackagedStagingAuthorityError(
                "PF5 work has no packaged staging authority"
            )
        return self._snapshot_row(row)

    def resolve(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
    ) -> BuildDeploymentAuthority:
        snapshot = self.snapshot(
            project_id=project_id,
            repository_id=repository_id,
            work_id=work_id,
        )
        if snapshot.revoked:
            raise PackagedStagingAuthorityError(
                "packaged staging authority has been revoked"
            )
        if snapshot.target_digest != self._target_digest:
            raise PackagedStagingAuthorityError(
                "packaged staging provider target changed after work authorization"
            )
        return _snapshot_authority(snapshot.authority)

    def _make_authority(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
        release_version: str,
        migration_refs: tuple[str, ...],
    ) -> BuildDeploymentAuthority:
        project_id = _text(project_id, "project_id")
        if project_id != self._target.project_id:
            raise PackagedStagingAuthorityError(
                "staging target project does not match build project"
            )
        repository_id = _text(repository_id, "repository_id")
        work_id = _text(work_id, "work_id")
        release_version = _text(
            release_version,
            "release_version",
            max_bytes=_MAX_RELEASE_VERSION_BYTES,
        )
        migration_refs = _text_tuple(
            migration_refs,
            "migration_refs",
            max_items=_MAX_MIGRATION_REFS,
        )
        try:
            return BuildDeploymentAuthority(
                project_id=project_id,
                repository_id=repository_id,
                work_id=work_id,
                release_version=release_version,
                staging_environment=EnvironmentIdentity(
                    environment_id=self._target.environment_id,
                    project_id=project_id,
                    tier=EnvironmentTier.STAGING,
                    provider_ref=self._target.provider_ref,
                ),
                migration_refs=migration_refs,
            )
        except (BuildDeploymentHandoffError, TypeError, ValueError) as exc:
            raise PackagedStagingAuthorityError(
                "packaged staging authorization is invalid"
            ) from exc

    def _snapshot_row(self, row: sqlite3.Row) -> PackagedStagingAuthoritySnapshot:
        project_id = _text(row["project_id"], "stored project_id")
        repository_id = _text(row["repository_id"], "stored repository_id")
        work_id = _text(row["work_id"], "stored work_id")
        release_version = _text(
            row["release_version"],
            "stored release_version",
            max_bytes=_MAX_RELEASE_VERSION_BYTES,
        )
        migration_refs = _decode_migration_refs(row["migration_refs_json"])
        target_digest = _digest(row["target_digest"], "stored target_digest")
        authority_digest = _digest(
            row["authority_digest"],
            "stored authority_digest",
        )
        revoked_at = row["revoked_at"]
        if revoked_at is not None:
            _text(revoked_at, "stored revoked_at")

        target_for_row = self._target
        if target_for_row.project_id != project_id:
            raise PackagedStagingAuthorityError(
                "stored staging authority project does not match current target"
            )
        authority = self._make_authority(
            project_id=project_id,
            repository_id=repository_id,
            work_id=work_id,
            release_version=release_version,
            migration_refs=migration_refs,
        )
        snapshot = PackagedStagingAuthoritySnapshot(
            authority,
            authority_digest,
            target_digest,
            revoked_at is not None,
        )
        if snapshot.target_digest != self._target_digest:
            raise PackagedStagingAuthorityError(
                "packaged staging provider target changed after work authorization"
            )
        return snapshot

    def _initialize(self) -> None:
        try:
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS "
                    "product_factory_staging_authority_schema ("
                    "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                row = conn.execute(
                    "SELECT MAX(version) AS version "
                    "FROM product_factory_staging_authority_schema"
                ).fetchone()
                current = 0 if row is None or row["version"] is None else row["version"]
                if type(current) is not int or current < 0:
                    raise PackagedStagingAuthorityError(
                        "packaged staging authority schema version is invalid"
                    )
                if current > _SCHEMA_VERSION:
                    raise PackagedStagingAuthorityError(
                        "packaged staging authority schema is newer than this program"
                    )
                if current < 1:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS "
                        "product_factory_staging_work_authority ("
                        "project_id TEXT NOT NULL, "
                        "repository_id TEXT NOT NULL, "
                        "work_id TEXT NOT NULL, "
                        "release_version TEXT NOT NULL, "
                        "migration_refs_json TEXT NOT NULL, "
                        "target_digest TEXT NOT NULL, "
                        "authority_digest TEXT NOT NULL, "
                        "authorized_at TEXT NOT NULL, "
                        "revoked_at TEXT, "
                        "PRIMARY KEY(project_id, repository_id, work_id))"
                    )
                    conn.execute(
                        "INSERT INTO product_factory_staging_authority_schema "
                        "(version, applied_at) VALUES (?, ?)",
                        (1, datetime.now(UTC).isoformat()),
                    )
        except sqlite3.Error as exc:
            raise PackagedStagingAuthorityError(
                "packaged staging authority schema could not be initialized"
            ) from exc


def _snapshot_authority(value: object) -> BuildDeploymentAuthority:
    if type(value) is not BuildDeploymentAuthority:
        raise PackagedStagingAuthorityError(
            "staging authority carrier is invalid"
        )
    try:
        return BuildDeploymentAuthority(
            project_id=_text(value.project_id, "authority project_id"),
            repository_id=_text(value.repository_id, "authority repository_id"),
            work_id=_text(value.work_id, "authority work_id"),
            release_version=_text(
                value.release_version,
                "authority release_version",
                max_bytes=_MAX_RELEASE_VERSION_BYTES,
            ),
            staging_environment=EnvironmentIdentity(
                environment_id=_text(
                    value.staging_environment.environment_id,
                    "authority environment_id",
                ),
                project_id=_text(
                    value.staging_environment.project_id,
                    "authority staging project_id",
                ),
                tier=value.staging_environment.tier,
                provider_ref=_text(
                    value.staging_environment.provider_ref,
                    "authority provider_ref",
                ),
            ),
            migration_refs=_text_tuple(
                value.migration_refs,
                "authority migration_refs",
                max_items=_MAX_MIGRATION_REFS,
            ),
        )
    except (AttributeError, BuildDeploymentHandoffError, TypeError, ValueError) as exc:
        raise PackagedStagingAuthorityError(
            "staging authority failed canonical readmission"
        ) from exc


def _snapshot_target(value: object) -> AuthorizedStagingTarget:
    if type(value) is not AuthorizedStagingTarget:
        raise PackagedStagingAuthorityError(
            "staging provider target must be exact AuthorizedStagingTarget"
        )
    try:
        return AuthorizedStagingTarget(
            project_id=_text(value.project_id, "target project_id"),
            environment_id=_text(value.environment_id, "target environment_id"),
            provider_ref=_text(value.provider_ref, "target provider_ref"),
            inventory=_text(
                value.inventory,
                "target inventory",
                max_bytes=_MAX_TARGET_FIELD_BYTES,
            ),
            authorization_ref=_text(
                value.authorization_ref,
                "target authorization_ref",
                max_bytes=_MAX_TARGET_FIELD_BYTES,
            ),
        )
    except (AttributeError, StagingAdapterError, TypeError, ValueError) as exc:
        raise PackagedStagingAuthorityError(
            "staging provider target failed canonical readmission"
        ) from exc


def _target_digest(target: AuthorizedStagingTarget) -> str:
    canonical = json.dumps(
        {
            "schema": _SCHEMA_NAME,
            "project_id": target.project_id,
            "environment_id": target.environment_id,
            "provider_ref": target.provider_ref,
            "inventory": target.inventory,
            "authorization_ref": target.authorization_ref,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _authority_digest(
    *,
    authority: BuildDeploymentAuthority,
    target_digest: str,
) -> str:
    canonical = json.dumps(
        {
            "schema": _SCHEMA_NAME,
            "project_id": authority.project_id,
            "repository_id": authority.repository_id,
            "work_id": authority.work_id,
            "release_version": authority.release_version,
            "migration_refs": list(authority.migration_refs),
            "target_digest": _digest(target_digest, "target_digest"),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _encode_migration_refs(value: tuple[str, ...]) -> str:
    refs = _text_tuple(
        value,
        "migration_refs",
        max_items=_MAX_MIGRATION_REFS,
    )
    return json.dumps(
        list(refs),
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _decode_migration_refs(value: object) -> tuple[str, ...]:
    if type(value) is not str:
        raise PackagedStagingAuthorityError(
            "stored migration_refs_json must be exact text"
        )
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PackagedStagingAuthorityError(
            "stored migration refs JSON is invalid"
        ) from exc
    if type(decoded) is not list:
        raise PackagedStagingAuthorityError(
            "stored migration refs must be a JSON array"
        )
    return _text_tuple(
        tuple(decoded),
        "stored migration_refs",
        max_items=_MAX_MIGRATION_REFS,
    )


def _text_tuple(
    value: object,
    label: str,
    *,
    max_items: int,
) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > max_items:
        raise PackagedStagingAuthorityError(
            f"{label} must be an exact tuple with at most {max_items} items"
        )
    result = tuple(_text(item, f"{label} item") for item in value)
    if len(result) != len(set(result)):
        raise PackagedStagingAuthorityError(
            f"{label} must not contain duplicates"
        )
    return result


def _text(
    value: object,
    label: str,
    *,
    max_bytes: int = _MAX_IDENTITY_BYTES,
) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PackagedStagingAuthorityError(
            f"{label} must be exact normalized non-empty text"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PackagedStagingAuthorityError(
            f"{label} must be valid UTF-8 text"
        ) from exc
    if len(encoded) > max_bytes:
        raise PackagedStagingAuthorityError(
            f"{label} exceeds the byte limit"
        )
    if any(
        ord(character) < 32
        or ord(character) == 127
        or character in "\u0085\u2028\u2029"
        for character in value
    ):
        raise PackagedStagingAuthorityError(
            f"{label} must be single-line text"
        )
    return value


def _digest(value: object, label: str) -> str:
    text = _text(value, label, max_bytes=64)
    if len(text) != 64 or any(
        character not in "0123456789abcdef"
        for character in text
    ):
        raise PackagedStagingAuthorityError(
            f"{label} must be lowercase SHA-256 hex"
        )
    return text
