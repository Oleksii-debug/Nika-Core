from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from nika_core.product_project import (
    ProductProject,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    StaleProjectVersionError,
)

_SPEC_MUTATION_KIND = "product_project.spec_update.v2"
_SPEC_MUTATION_EVENT = "product_project.spec_versioned"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _durable_int(value: Any, *, label: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ProductProjectError(f"invalid durable {label}")
    return value


def _nonempty_str(value: Any, *, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ProductProjectError(f"invalid durable {label}")
    return value


def _aware_timestamp(value: Any, *, label: str) -> str:
    text = _nonempty_str(value, label=label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProductProjectError(f"invalid durable {label}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductProjectError(f"invalid durable {label}")
    return text


def _is_sha256(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _effective_spec(spec: ProductProjectSpec) -> ProductProjectSpec:
    # Caller-supplied lineage is not authoritative: the repository binds it to durable state.
    return replace(spec, supersedes_spec_version=None, revision_reason="")


def _input_fingerprint(
    project_id: str,
    spec: ProductProjectSpec,
    *,
    expected_row_version: int,
    change_reason: str,
) -> str:
    payload = {
        "project_id": project_id,
        "expected_row_version": expected_row_version,
        "spec": _effective_spec(spec).to_dict(),
        "change_reason": change_reason,
    }
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _audit_payload(
    *,
    operation_key_sha256: str,
    expected_row_version: int,
    previous_spec_version: int,
    spec_version: int,
    row_version: int,
    input_fingerprint: str,
    spec_sha256: str,
    change_reason: str,
) -> dict[str, Any]:
    # Keep the canonical historical audit type/fields and extend it with durable retry evidence.
    return {
        "spec_version": spec_version,
        "supersedes_spec_version": previous_spec_version,
        "change_reason": change_reason,
        "row_version": row_version,
        "expected_row_version": expected_row_version,
        "operation_kind": _SPEC_MUTATION_KIND,
        "operation_key_sha256": operation_key_sha256,
        "input_fingerprint": input_fingerprint,
        "spec_sha256": spec_sha256,
    }


def _status_at_row_version(conn: Any, project_id: str, row_version: int) -> str:
    """Reconstruct the lifecycle status visible at one exact ProductProject row version."""
    status = "active"
    previous_event_version = 0
    for event in conn.execute(
        "SELECT payload_json FROM audit_events "
        "WHERE event_type='product_project.status_changed' "
        "AND entity_type='product_project' AND entity_id=? ORDER BY event_id",
        (project_id,),
    ).fetchall():
        try:
            payload = json.loads(event["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProductProjectError("invalid ProductProject lifecycle audit evidence") from exc
        if type(payload) is not dict:
            raise ProductProjectError("invalid ProductProject lifecycle audit evidence")
        event_version = _durable_int(
            payload.get("row_version"),
            label="lifecycle audit row_version",
            minimum=1,
        )
        if event_version <= previous_event_version:
            raise ProductProjectError(
                "ProductProject lifecycle audit row_version must increase monotonically"
            )
        previous_event_version = event_version
        if event_version > row_version:
            break
        previous_state = _nonempty_str(
            payload.get("previous_state"),
            label="lifecycle audit previous_state",
        )
        new_state = _nonempty_str(
            payload.get("new_state"),
            label="lifecycle audit new_state",
        )
        if previous_state != status:
            raise ProductProjectError("ProductProject lifecycle audit state chain is inconsistent")
        status = new_state
    return status


def _materialize_replay_result(
    conn: Any,
    *,
    project_id: str,
    result_spec_version: int,
    result_row_version: int,
    stored_spec: ProductProjectSpec,
    result_updated_at: str,
) -> ProductProject:
    """Build the exact mutation result from receipt-bound durable identity, not current state."""
    project_row = conn.execute(
        "SELECT project_id,name,current_spec_version,row_version,created_at "
        "FROM product_projects WHERE project_id=?",
        (project_id,),
    ).fetchone()
    if project_row is None:
        raise ProductProjectError("spec idempotency record references missing project")
    durable_project_id = _nonempty_str(project_row["project_id"], label="project_id")
    if durable_project_id != project_id:
        raise ProductProjectError("spec idempotency project identity mismatch")
    name = _nonempty_str(project_row["name"], label="project name")
    created_at = _aware_timestamp(project_row["created_at"], label="project created_at")
    current_spec_version = _durable_int(
        project_row["current_spec_version"],
        label="current_spec_version",
        minimum=1,
    )
    current_row_version = _durable_int(
        project_row["row_version"],
        label="row_version",
        minimum=0,
    )
    if current_spec_version < result_spec_version or current_row_version < result_row_version:
        raise ProductProjectError("spec idempotency record is ahead of durable project state")
    return ProductProject(
        project_id=project_id,
        name=name,
        spec_version=result_spec_version,
        row_version=result_row_version,
        status=_status_at_row_version(conn, project_id, result_row_version),
        spec=stored_spec,
        created_at=created_at,
        updated_at=result_updated_at,
    )


def _validate_replay(
    conn: Any,
    replay: Any,
    *,
    project_id: str,
    expected_row_version: int,
    operation_key: str,
    fingerprint: str,
    change_reason: str,
) -> tuple[int, int, ProductProjectSpec, str]:
    if (
        replay["project_id"] != project_id
        or replay["operation_kind"] != _SPEC_MUTATION_KIND
        or replay["input_fingerprint"] != fingerprint
        or replay["change_reason"] != change_reason
    ):
        raise ProductProjectError(
            "idempotency key was already used with different specification mutation input"
        )
    stored_expected = _durable_int(
        replay["expected_row_version"],
        label="spec idempotency expected_row_version",
        minimum=0,
    )
    previous_spec_version = _durable_int(
        replay["previous_spec_version"],
        label="spec idempotency previous_spec_version",
        minimum=1,
    )
    result_spec_version = _durable_int(
        replay["result_spec_version"],
        label="spec idempotency result_spec_version",
        minimum=2,
    )
    result_row_version = _durable_int(
        replay["result_row_version"],
        label="spec idempotency result_row_version",
        minimum=1,
    )
    created_at = _aware_timestamp(replay["created_at"], label="spec idempotency created_at")
    if stored_expected != expected_row_version:
        raise ProductProjectError(
            "idempotency key was already used with a different expected row version"
        )
    if result_spec_version != previous_spec_version + 1:
        raise ProductProjectError("corrupt spec idempotency version lineage")
    if result_row_version != stored_expected + 1:
        raise ProductProjectError("corrupt spec idempotency row-version lineage")
    spec_sha256 = replay["spec_sha256"]
    if not _is_sha256(spec_sha256) or not _is_sha256(replay["input_fingerprint"]):
        raise ProductProjectError("corrupt spec idempotency digest")

    project_row = conn.execute(
        "SELECT current_spec_version,row_version FROM product_projects WHERE project_id=?",
        (project_id,),
    ).fetchone()
    if project_row is None:
        raise ProductProjectError("spec idempotency record references missing project")
    current_spec_version = _durable_int(
        project_row["current_spec_version"],
        label="current_spec_version",
        minimum=1,
    )
    current_row_version = _durable_int(
        project_row["row_version"],
        label="row_version",
        minimum=0,
    )
    if current_spec_version < result_spec_version or current_row_version < result_row_version:
        raise ProductProjectError("spec idempotency record is ahead of durable project state")

    spec_row = conn.execute(
        "SELECT spec_json,created_at FROM product_project_specs "
        "WHERE project_id=? AND spec_version=?",
        (project_id, result_spec_version),
    ).fetchone()
    if spec_row is None:
        raise ProductProjectError("spec idempotency record has no durable specification")
    raw_spec = spec_row["spec_json"]
    if type(raw_spec) is not str:
        raise ProductProjectError("invalid durable specification payload type")
    if hashlib.sha256(raw_spec.encode()).hexdigest() != spec_sha256:
        raise ProductProjectError(
            "spec idempotency digest does not match durable specification"
        )
    try:
        parsed = json.loads(raw_spec)
    except json.JSONDecodeError as exc:
        raise ProductProjectError("invalid durable specification JSON") from exc
    if type(parsed) is not dict:
        raise ProductProjectError("invalid durable specification: expected object")
    try:
        stored_spec = ProductProjectSpec.from_dict(parsed)
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductProjectError("invalid durable specification") from exc
    if stored_spec.supersedes_spec_version != previous_spec_version:
        raise ProductProjectError("durable specification disagrees with idempotency lineage")
    if stored_spec.revision_reason != change_reason:
        raise ProductProjectError("durable specification disagrees with mutation reason")
    if spec_row["created_at"] != created_at:
        raise ProductProjectError("spec idempotency timestamp disagrees with specification")

    operation_key_sha256 = hashlib.sha256(operation_key.encode()).hexdigest()
    expected_audit = _audit_payload(
        operation_key_sha256=operation_key_sha256,
        expected_row_version=stored_expected,
        previous_spec_version=previous_spec_version,
        spec_version=result_spec_version,
        row_version=result_row_version,
        input_fingerprint=fingerprint,
        spec_sha256=spec_sha256,
        change_reason=change_reason,
    )
    audit_matches = 0
    for event in conn.execute(
        "SELECT payload_json,created_at FROM audit_events "
        "WHERE event_type=? AND entity_type='product_project' AND entity_id=? ORDER BY event_id",
        (_SPEC_MUTATION_EVENT, project_id),
    ).fetchall():
        try:
            payload = json.loads(event["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProductProjectError(
                "invalid ProductProject specification audit evidence"
            ) from exc
        if type(payload) is not dict:
            raise ProductProjectError("invalid ProductProject specification audit evidence")
        if payload.get("operation_key_sha256") != operation_key_sha256:
            continue
        if payload != expected_audit or event["created_at"] != created_at:
            raise ProductProjectError(
                "spec idempotency disagrees with canonical durable audit evidence"
            )
        audit_matches += 1
    if audit_matches != 1:
        raise ProductProjectError(
            "spec idempotency record must have exactly one canonical durable audit event"
        )
    return result_spec_version, result_row_version, stored_spec, created_at


def update_product_project_spec(
    store: Any,
    project_id: str,
    spec: ProductProjectSpec,
    *,
    expected_row_version: int,
    change_reason: str = "specification revision",
    idempotency_key: str | None = None,
) -> ProductProject:
    """Single authoritative PF0/PF12 transaction primitive for specification mutation."""
    if type(project_id) is not str or not project_id.strip():
        raise ProductProjectError("project_id must not be empty")
    if not isinstance(spec, ProductProjectSpec):
        raise ProductProjectError("spec must be ProductProjectSpec")
    if type(expected_row_version) is not int or expected_row_version < 0:
        raise ProductProjectError("expected_row_version must be a non-negative integer")
    if type(change_reason) is not str or not change_reason.strip():
        raise ProductProjectError("change_reason must not be empty")
    if idempotency_key is not None and (
        type(idempotency_key) is not str or not idempotency_key.strip()
    ):
        raise ProductProjectError("idempotency_key must be a non-empty string when supplied")

    fingerprint = _input_fingerprint(
        project_id,
        spec,
        expected_row_version=expected_row_version,
        change_reason=change_reason,
    )
    operation_key = idempotency_key or f"auto:{fingerprint}"
    operation_key_sha256 = hashlib.sha256(operation_key.encode()).hexdigest()

    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        replay = conn.execute(
            "SELECT * FROM product_project_spec_idempotency WHERE operation_key=?",
            (operation_key,),
        ).fetchone()
        if replay is not None:
            (
                result_spec_version,
                result_row_version,
                stored_spec,
                result_updated_at,
            ) = _validate_replay(
                conn,
                replay,
                project_id=project_id,
                expected_row_version=expected_row_version,
                operation_key=operation_key,
                fingerprint=fingerprint,
                change_reason=change_reason,
            )
            result = _materialize_replay_result(
                conn,
                project_id=project_id,
                result_spec_version=result_spec_version,
                result_row_version=result_row_version,
                stored_spec=stored_spec,
                result_updated_at=result_updated_at,
            )
        else:
            row = conn.execute(
                "SELECT project_id,name,current_spec_version,row_version,status,created_at "
                "FROM product_projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise KeyError(project_id)
            durable_project_id = _nonempty_str(row["project_id"], label="project_id")
            if durable_project_id != project_id:
                raise ProductProjectError("durable ProductProject identity mismatch")
            name = _nonempty_str(row["name"], label="project name")
            status = _nonempty_str(row["status"], label="project status")
            project_created_at = _aware_timestamp(
                row["created_at"],
                label="project created_at",
            )
            current_spec_version = _durable_int(
                row["current_spec_version"],
                label="current_spec_version",
                minimum=1,
            )
            current_row_version = _durable_int(
                row["row_version"],
                label="row_version",
                minimum=0,
            )
            if current_row_version != expected_row_version:
                raise StaleProjectVersionError(
                    f"stale ProductProject write: expected {expected_row_version}, "
                    f"current {current_row_version}"
                )

            next_spec_version = current_spec_version + 1
            next_row_version = current_row_version + 1
            stored_spec = replace(
                _effective_spec(spec),
                supersedes_spec_version=current_spec_version,
                revision_reason=change_reason,
            )
            spec_json = _canonical(stored_spec.to_dict())
            spec_sha256 = hashlib.sha256(spec_json.encode()).hexdigest()
            now = datetime.now(UTC).isoformat()
            cursor = conn.execute(
                "UPDATE product_projects SET current_spec_version=?,row_version=row_version+1,"
                "updated_at=? WHERE project_id=? AND row_version=?",
                (next_spec_version, now, project_id, current_row_version),
            )
            if cursor.rowcount != 1:
                raise StaleProjectVersionError("concurrent ProductProject update")
            conn.execute(
                "INSERT INTO product_project_specs(project_id,spec_version,spec_json,created_at) "
                "VALUES (?,?,?,?)",
                (project_id, next_spec_version, spec_json, now),
            )
            conn.execute(
                "INSERT INTO product_project_spec_idempotency("
                "operation_key,project_id,operation_kind,expected_row_version,"
                "previous_spec_version,result_spec_version,result_row_version,"
                "input_fingerprint,spec_sha256,change_reason,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_key,
                    project_id,
                    _SPEC_MUTATION_KIND,
                    current_row_version,
                    current_spec_version,
                    next_spec_version,
                    next_row_version,
                    fingerprint,
                    spec_sha256,
                    change_reason,
                    now,
                ),
            )
            audit_payload = _audit_payload(
                operation_key_sha256=operation_key_sha256,
                expected_row_version=current_row_version,
                previous_spec_version=current_spec_version,
                spec_version=next_spec_version,
                row_version=next_row_version,
                input_fingerprint=fingerprint,
                spec_sha256=spec_sha256,
                change_reason=change_reason,
            )
            conn.execute(
                "INSERT INTO audit_events(event_type,entity_type,entity_id,payload_json,"
                "created_at) VALUES (?,?,?,?,?)",
                (
                    _SPEC_MUTATION_EVENT,
                    "product_project",
                    project_id,
                    _canonical(audit_payload),
                    now,
                ),
            )
            result = ProductProject(
                project_id=project_id,
                name=name,
                spec_version=next_spec_version,
                row_version=next_row_version,
                status=status,
                spec=stored_spec,
                created_at=project_created_at,
                updated_at=now,
            )

    # Non-authoritative current-state integrity probe. A concurrent later mutation may
    # legitimately advance this read; the public return value remains transaction-bound above.
    ProductProjectRepository(store).get(project_id)
    return result


@dataclass(frozen=True, slots=True)
class ProductProjectSpecMutationReceipt:
    project_id: str
    operation_key: str
    previous_spec_version: int
    spec_version: int
    previous_row_version: int
    row_version: int
    input_fingerprint: str
    spec_sha256: str
    change_reason: str
    created_at: str


class ProductProjectSpecDurabilityService:
    """Compatibility façade; all writes delegate to ProductProjectRepository.update_spec()."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.projects = ProductProjectRepository(store)

    def update_spec(
        self,
        project_id: str,
        spec: ProductProjectSpec,
        *,
        expected_row_version: int,
        idempotency_key: str,
        change_reason: str = "specification revision",
    ) -> ProductProjectSpecMutationReceipt:
        self.projects.update_spec(
            project_id,
            spec,
            expected_row_version=expected_row_version,
            change_reason=change_reason,
            idempotency_key=idempotency_key,
        )
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM product_project_spec_idempotency WHERE operation_key=?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            raise ProductProjectError(
                "canonical spec mutation did not persist an idempotency receipt"
            )
        return ProductProjectSpecMutationReceipt(
            project_id=_nonempty_str(row["project_id"], label="receipt project_id"),
            operation_key=idempotency_key,
            previous_spec_version=_durable_int(
                row["previous_spec_version"], label="receipt previous_spec_version", minimum=1
            ),
            spec_version=_durable_int(
                row["result_spec_version"], label="receipt result_spec_version", minimum=2
            ),
            previous_row_version=_durable_int(
                row["expected_row_version"], label="receipt expected_row_version", minimum=0
            ),
            row_version=_durable_int(
                row["result_row_version"], label="receipt result_row_version", minimum=1
            ),
            input_fingerprint=_nonempty_str(
                row["input_fingerprint"], label="receipt input_fingerprint"
            ),
            spec_sha256=_nonempty_str(row["spec_sha256"], label="receipt spec_sha256"),
            change_reason=_nonempty_str(row["change_reason"], label="receipt change_reason"),
            created_at=_aware_timestamp(row["created_at"], label="receipt created_at"),
        )
