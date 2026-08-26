from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog

from .contracts import (
    CandidateState,
    CapabilityGap,
    CapabilityManifestV1,
    GapKind,
    ReuseCandidate,
)

_ALLOWED_TRANSITIONS: dict[CandidateState, frozenset[CandidateState]] = {
    CandidateState.PROPOSED: frozenset(
        {CandidateState.REUSE_SELECTED, CandidateState.BUILD_REQUIRED, CandidateState.BLOCKED}
    ),
    CandidateState.REUSE_SELECTED: frozenset({CandidateState.VERIFYING, CandidateState.REJECTED}),
    CandidateState.BUILD_REQUIRED: frozenset({CandidateState.BUILDING, CandidateState.BLOCKED}),
    CandidateState.BUILDING: frozenset(
        {CandidateState.BUILT, CandidateState.BLOCKED, CandidateState.QUARANTINED}
    ),
    CandidateState.BUILT: frozenset(
        {CandidateState.VERIFYING, CandidateState.REJECTED, CandidateState.QUARANTINED}
    ),
    CandidateState.VERIFYING: frozenset(
        {CandidateState.VERIFIED, CandidateState.REJECTED, CandidateState.QUARANTINED}
    ),
    CandidateState.VERIFIED: frozenset({CandidateState.REGISTERING, CandidateState.ROLLED_BACK}),
    CandidateState.REGISTERING: frozenset({CandidateState.REGISTERED, CandidateState.ROLLED_BACK}),
    CandidateState.REGISTERED: frozenset({CandidateState.ROLLED_BACK}),
    CandidateState.REJECTED: frozenset(),
    CandidateState.BLOCKED: frozenset(),
    CandidateState.QUARANTINED: frozenset(),
    CandidateState.ROLLED_BACK: frozenset(),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class StaleTransitionError(RuntimeError):
    pass


class InvalidTransitionError(RuntimeError):
    pass


class ToolsmithRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store
        self._audit = AuditLog(store)

    def create_escalation(self, gap: CapabilityGap) -> tuple[int, CandidateState]:
        with self._store.connection() as conn:
            existing = conn.execute(
                "SELECT row_version, state FROM capability_escalations "
                "WHERE task_id = ? AND requested_capability = ?",
                (gap.task_id, gap.requested_capability),
            ).fetchone()
            if existing is not None:
                return int(existing["row_version"]), CandidateState(existing["state"])
            conn.execute(
                "INSERT INTO capability_escalations("
                "task_id, requested_capability, gap_kind, reason, attempted_methods_json, "
                "permission_ceiling_json, state, row_version, pinned_version, pinned_digest, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?)",
                (
                    gap.task_id,
                    gap.requested_capability,
                    gap.kind.value,
                    gap.reason,
                    _json(gap.attempted_methods),
                    _json(sorted(gap.permission_ceiling)),
                    CandidateState.PROPOSED.value,
                    _now(),
                    _now(),
                ),
            )
            self._audit.append_with_connection(
                conn,
                event_type="capability_escalation.created",
                entity_type="task",
                entity_id=gap.task_id,
                payload={"capability_id": gap.requested_capability, "gap_kind": gap.kind.value},
            )
        return 0, CandidateState.PROPOSED

    def get_escalation(self, *, task_id: str, capability_id: str) -> dict[str, object] | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM capability_escalations "
                "WHERE task_id = ? AND requested_capability = ?",
                (task_id, capability_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def transition(
        self,
        *,
        task_id: str,
        capability_id: str,
        expected_version: int,
        target: CandidateState,
        evidence: dict[str, object] | None = None,
    ) -> int:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT state, row_version FROM capability_escalations "
                "WHERE task_id = ? AND requested_capability = ?",
                (task_id, capability_id),
            ).fetchone()
            if row is None:
                raise KeyError((task_id, capability_id))
            current = CandidateState(row["state"])
            version = int(row["row_version"])
            if version != expected_version:
                raise StaleTransitionError(
                    f"expected row version {expected_version}, found {version}"
                )
            if target not in _ALLOWED_TRANSITIONS[current]:
                raise InvalidTransitionError(
                    f"invalid candidate transition {current.value} -> {target.value}"
                )
            cursor = conn.execute(
                "UPDATE capability_escalations SET state = ?, row_version = row_version + 1, "
                "updated_at = ? WHERE task_id = ? AND requested_capability = ? AND row_version = ?",
                (target.value, _now(), task_id, capability_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise StaleTransitionError("candidate row changed during transition")
            self._audit.append_with_connection(
                conn,
                event_type="capability_escalation.transition",
                entity_type="task",
                entity_id=task_id,
                payload={
                    "capability_id": capability_id,
                    "from": current.value,
                    "to": target.value,
                    "evidence": evidence or {},
                },
            )
        return expected_version + 1

    def accept_verification(
        self,
        *,
        task_id: str,
        capability_id: str,
        expected_version: int,
        candidate_digest: str,
        verifier_evidence: dict[str, object],
    ) -> int:
        """Atomically persist independent verification and its exact artifact digest."""

        if not candidate_digest.strip():
            raise ValueError("verification requires exact candidate digest")
        if not verifier_evidence:
            raise ValueError("verification requires independent evidence")
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT state, row_version, pinned_digest FROM capability_escalations "
                "WHERE task_id = ? AND requested_capability = ?",
                (task_id, capability_id),
            ).fetchone()
            if row is None:
                raise KeyError((task_id, capability_id))
            current = CandidateState(str(row["state"]))
            version = int(row["row_version"])
            if version != expected_version:
                raise StaleTransitionError(
                    f"expected row version {expected_version}, found {version}"
                )
            if current is not CandidateState.VERIFYING:
                raise InvalidTransitionError("verification acceptance requires VERIFYING state")
            prior_digest = row["pinned_digest"]
            if prior_digest is not None and str(prior_digest) != candidate_digest:
                raise RuntimeError("verification digest conflicts with prior durable identity")
            cursor = conn.execute(
                "UPDATE capability_escalations SET state = ?, pinned_digest = ?, "
                "row_version = row_version + 1, updated_at = ? "
                "WHERE task_id = ? AND requested_capability = ? AND row_version = ?",
                (
                    CandidateState.VERIFIED.value,
                    candidate_digest,
                    _now(),
                    task_id,
                    capability_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleTransitionError("candidate row changed during verification acceptance")
            self._audit.append_with_connection(
                conn,
                event_type="capability_escalation.transition",
                entity_type="task",
                entity_id=task_id,
                payload={
                    "capability_id": capability_id,
                    "from": CandidateState.VERIFYING.value,
                    "to": CandidateState.VERIFIED.value,
                    "evidence": {
                        "digest": candidate_digest,
                        "verifier": verifier_evidence,
                    },
                },
            )
        return expected_version + 1

    def record_search_candidate(self, *, task_id: str, candidate: ReuseCandidate) -> None:
        row = self.get_escalation(task_id=task_id, capability_id=candidate.capability_id)
        if row is None:
            raise KeyError((task_id, candidate.capability_id))
        ceiling = frozenset(json.loads(str(row["permission_ceiling_json"])))
        if not candidate.permissions.issubset(ceiling):
            raise PermissionError("candidate permissions exceed original task ceiling")
        with self._store.connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO capability_search_candidates("
                "task_id, capability_id, version, source, digest, permissions_json, metadata_json, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    candidate.capability_id,
                    candidate.version,
                    candidate.source,
                    candidate.digest,
                    _json(sorted(candidate.permissions)),
                    _json(candidate.metadata),
                    _now(),
                ),
            )

    def register_exact(self, *, task_id: str, manifest: CapabilityManifestV1) -> None:
        # Keep the preflight read for fast diagnostics, but never use it as commit authority.
        # A rollback may win after this read, so publication must revalidate the escalation
        # under the same SQLite write transaction that creates the active registry row.
        row = self.get_escalation(task_id=task_id, capability_id=manifest.capability_id)
        if row is None:
            raise KeyError((task_id, manifest.capability_id))
        if CandidateState(str(row["state"])) is not CandidateState.REGISTERING:
            raise InvalidTransitionError("capability must be REGISTERING before exact registration")

        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            authoritative = conn.execute(
                "SELECT state, permission_ceiling_json, pinned_digest "
                "FROM capability_escalations "
                "WHERE task_id = ? AND requested_capability = ?",
                (task_id, manifest.capability_id),
            ).fetchone()
            if authoritative is None:
                raise KeyError((task_id, manifest.capability_id))
            if CandidateState(str(authoritative["state"])) is not CandidateState.REGISTERING:
                raise StaleTransitionError("candidate changed before exact registry publication")
            ceiling = frozenset(json.loads(str(authoritative["permission_ceiling_json"])))
            if not manifest.permissions.issubset(ceiling):
                raise PermissionError("registered capability permissions exceed original task ceiling")
            verified_digest = authoritative["pinned_digest"]
            if verified_digest is None or str(verified_digest) != manifest.digest:
                raise StaleTransitionError(
                    "verified candidate identity changed before exact registry publication"
                )
            conflicting = conn.execute(
                "SELECT digest FROM capability_registry WHERE capability_id = ? AND version = ?",
                (manifest.capability_id, manifest.version),
            ).fetchone()
            if conflicting is not None and str(conflicting["digest"]) != manifest.digest:
                raise RuntimeError("capability version collision with a different digest")
            conn.execute(
                "INSERT OR IGNORE INTO capability_registry("
                "capability_id, version, digest, manifest_json, registered_at, active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (
                    manifest.capability_id,
                    manifest.version,
                    manifest.digest,
                    _json({**asdict(manifest), "permissions": sorted(manifest.permissions)}),
                    _now(),
                ),
            )
            conn.execute(
                "UPDATE capability_escalations SET pinned_version = ?, pinned_digest = ?, "
                "updated_at = ? WHERE task_id = ? AND requested_capability = ?",
                (manifest.version, manifest.digest, _now(), task_id, manifest.capability_id),
            )
            self._audit.append_with_connection(
                conn,
                event_type="capability.registered.exact",
                entity_type="task",
                entity_id=task_id,
                payload={
                    "capability_id": manifest.capability_id,
                    "version": manifest.version,
                    "digest": manifest.digest,
                },
            )

    def mark_resume_ready(self, *, task_id: str, capability_id: str) -> None:
        row = self.get_escalation(task_id=task_id, capability_id=capability_id)
        if row is None:
            raise KeyError((task_id, capability_id))
        if CandidateState(str(row["state"])) is not CandidateState.REGISTERED:
            raise InvalidTransitionError("original task may resume only after REGISTERED")
        version = row["pinned_version"]
        digest = row["pinned_digest"]
        if not version or not digest:
            raise RuntimeError("registered escalation is missing exact pinned capability identity")
        with self._store.connection() as conn:
            conn.execute(
                "INSERT INTO capability_resume_bindings("
                "task_id, capability_id, version, digest, status, updated_at) "
                "VALUES (?, ?, ?, ?, 'ready', ?) "
                "ON CONFLICT(task_id, capability_id) DO UPDATE SET "
                "version=excluded.version, digest=excluded.digest, status='ready', "
                "updated_at=excluded.updated_at",
                (task_id, capability_id, version, digest, _now()),
            )
            self._audit.append_with_connection(
                conn,
                event_type="capability.resume.ready",
                entity_type="task",
                entity_id=task_id,
                payload={"capability_id": capability_id, "version": version, "digest": digest},
            )

    def rollback_registration(self, *, task_id: str, capability_id: str) -> None:
        row = self.get_escalation(task_id=task_id, capability_id=capability_id)
        if row is None:
            raise KeyError((task_id, capability_id))
        version = row["pinned_version"]
        digest = row["pinned_digest"]
        with self._store.connection() as conn:
            if version and digest:
                conn.execute(
                    "UPDATE capability_registry SET active = 0 "
                    "WHERE capability_id = ? AND version = ? AND digest = ?",
                    (capability_id, version, digest),
                )
            conn.execute(
                "DELETE FROM capability_resume_bindings WHERE task_id = ? AND capability_id = ?",
                (task_id, capability_id),
            )
            self._audit.append_with_connection(
                conn,
                event_type="capability.registration.rolled_back",
                entity_type="task",
                entity_id=task_id,
                payload={"capability_id": capability_id, "version": version, "digest": digest},
            )

    def list_incomplete(self) -> tuple[dict[str, object], ...]:
        terminal = (
            CandidateState.REGISTERED.value,
            CandidateState.REJECTED.value,
            CandidateState.BLOCKED.value,
            CandidateState.QUARANTINED.value,
            CandidateState.ROLLED_BACK.value,
        )
        placeholders = ",".join("?" for _ in terminal)
        with self._store.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM capability_escalations WHERE state NOT IN ({placeholders}) "
                "ORDER BY created_at",
                terminal,
            ).fetchall()
        return tuple(dict(row) for row in rows)

    @staticmethod
    def gap_from_row(row: dict[str, object]) -> CapabilityGap:
        return CapabilityGap(
            task_id=str(row["task_id"]),
            requested_capability=str(row["requested_capability"]),
            kind=GapKind(str(row["gap_kind"])),
            reason=str(row["reason"]),
            attempted_methods=tuple(json.loads(str(row["attempted_methods_json"]))),
            permission_ceiling=frozenset(json.loads(str(row["permission_ceiling_json"]))),
        )
