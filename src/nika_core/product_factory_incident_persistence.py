from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum

from .product_factory_coordinator import CoordinatorSnapshot
from .product_factory_deployment import DeploymentFabricSnapshot
from .product_factory_incident_contracts import (
    IncidentKind,
    IncidentLifecycleSnapshot,
    IncidentRecord,
    IncidentSeverity,
    IncidentState,
    IncidentTrigger,
    ProductIncidentError,
    ReleaseDisposition,
    ReleaseEvidence,
    RepairCandidateEvidence,
    RepairWorkOrder,
    SupplyChainAdvisory,
)
from .product_factory_incidents import IncidentRepairReleaseCoordinator


def dump_incident_snapshot(snapshot: IncidentLifecycleSnapshot) -> str:
    """Serialize durable PF8 state as canonical JSON without provider/session authority."""

    payload = {
        "schema": snapshot.schema,
        "project_id": snapshot.project_id,
        "incidents": [_incident_payload(record) for record in snapshot.incidents],
        "fingerprint_index": [list(item) for item in snapshot.fingerprint_index],
    }
    return _canonical(payload).decode("utf-8")


def load_incident_snapshot(
    payload: str,
    *,
    deployments: DeploymentFabricSnapshot | None = None,
    review_authorities: tuple[CoordinatorSnapshot, ...] = (),
) -> IncidentLifecycleSnapshot:
    """Parse canonical PF8 JSON and revalidate external authority before trust."""

    if not isinstance(payload, str) or not payload.strip():
        raise ProductIncidentError("incident snapshot payload must be non-empty JSON text")
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProductIncidentError("incident snapshot payload is invalid JSON") from exc
    root = _mapping(
        raw,
        {"schema", "project_id", "incidents", "fingerprint_index"},
        "incident snapshot",
    )
    incidents_raw = _list(root["incidents"], "incident snapshot incidents")
    index_raw = _list(root["fingerprint_index"], "incident fingerprint index")
    fingerprint_index: list[tuple[str, str]] = []
    for item in index_raw:
        pair = _list(item, "incident fingerprint mapping")
        if len(pair) != 2:
            raise ProductIncidentError("incident fingerprint mapping must contain two values")
        fingerprint_index.append(
            (
                _text(pair[0], "incident fingerprint"),
                _text(pair[1], "incident fingerprint incident id"),
            )
        )
    snapshot = IncidentLifecycleSnapshot(
        _text(root["schema"], "incident snapshot schema"),
        _text(root["project_id"], "incident snapshot project"),
        tuple(_incident_from_payload(item) for item in incidents_raw),
        tuple(fingerprint_index),
    )
    IncidentRepairReleaseCoordinator(snapshot.project_id).restore(
        snapshot,
        deployments,
        review_authorities,
    )
    return snapshot


def _incident_payload(record: IncidentRecord) -> dict[str, object]:
    return {
        "incident_id": record.incident_id,
        "trigger": _trigger_payload(record.trigger),
        "state": record.state.value,
        "work_order": None if record.work_order is None else _work_order_payload(record.work_order),
        "candidates": [_candidate_payload(item) for item in record.candidates],
        "release_events": [_release_payload(item) for item in record.release_events],
    }


def _trigger_payload(trigger: IncidentTrigger) -> dict[str, object]:
    advisory = None
    if trigger.advisory is not None:
        advisory = {
            "advisory_id": trigger.advisory.advisory_id,
            "ecosystem": trigger.advisory.ecosystem,
            "package_name": trigger.advisory.package_name,
            "affected_version": trigger.advisory.affected_version,
            "fixed_version": trigger.advisory.fixed_version,
            "provenance_ref": trigger.advisory.provenance_ref,
        }
    return {
        "project_id": trigger.project_id,
        "service_id": trigger.service_id,
        "environment_id": trigger.environment_id,
        "release_sha": trigger.release_sha,
        "kind": trigger.kind.value,
        "severity": trigger.severity.value,
        "evidence_refs": list(trigger.evidence_refs),
        "approval_ref": trigger.approval_ref,
        "observed_at": _aware_json(trigger.observed_at).isoformat(),
        "advisory": advisory,
    }


def _work_order_payload(order: RepairWorkOrder) -> dict[str, object]:
    return {
        "work_order_id": order.work_order_id,
        "incident_id": order.incident_id,
        "project_id": order.project_id,
        "service_id": order.service_id,
        "repository_id": order.repository_id,
        "component_id": order.component_id,
        "base_release_sha": order.base_release_sha,
        "goal": order.goal,
        "allowed_paths": list(order.allowed_paths),
        "permission_ceiling": sorted(order.permission_ceiling),
        "acceptance_commands": [list(command) for command in order.acceptance_commands],
        "evidence_refs": list(order.evidence_refs),
        "created_at": _aware_json(order.created_at).isoformat(),
        "advisory_id": order.advisory_id,
        "target_fixed_version": order.target_fixed_version,
    }


def _candidate_payload(candidate: RepairCandidateEvidence) -> dict[str, object]:
    return {
        "candidate_id": candidate.candidate_id,
        "incident_id": candidate.incident_id,
        "work_order_id": candidate.work_order_id,
        "base_release_sha": candidate.base_release_sha,
        "result_sha": candidate.result_sha,
        "artifact_digest": candidate.artifact_digest,
        "diff_digest": candidate.diff_digest,
        "regression_evidence_refs": list(candidate.regression_evidence_refs),
        "provenance_evidence_refs": list(candidate.provenance_evidence_refs),
        "review_ref": candidate.review_ref,
        "review_accepted": candidate.review_accepted,
        "recorded_at": _aware_json(candidate.recorded_at).isoformat(),
    }


def _release_payload(release: ReleaseEvidence) -> dict[str, object]:
    return {
        "release_event_id": release.release_event_id,
        "incident_id": release.incident_id,
        "candidate_id": release.candidate_id,
        "previous_release_sha": release.previous_release_sha,
        "candidate_release_sha": release.candidate_release_sha,
        "artifact_digest": release.artifact_digest,
        "staging_intent_id": release.staging_intent_id,
        "production_intent_id": release.production_intent_id,
        "disposition": release.disposition.value,
        "deployment_evidence_refs": list(release.deployment_evidence_refs),
        "health_evidence_refs": list(release.health_evidence_refs),
        "restored_release_sha": release.restored_release_sha,
        "reconciliation_ref": release.reconciliation_ref,
        "observed_at": _aware_json(release.observed_at).isoformat(),
    }


def _incident_from_payload(raw: object) -> IncidentRecord:
    item = _mapping(
        raw,
        {"incident_id", "trigger", "state", "work_order", "candidates", "release_events"},
        "incident record",
    )
    work = item["work_order"]
    candidates = _list(item["candidates"], "incident candidates")
    releases = _list(item["release_events"], "incident release events")
    return IncidentRecord(
        _text(item["incident_id"], "incident id"),
        _trigger_from_payload(item["trigger"]),
        _enum(IncidentState, item["state"], "incident state"),
        None if work is None else _work_order_from_payload(work),
        tuple(_candidate_from_payload(value) for value in candidates),
        tuple(_release_from_payload(value) for value in releases),
    )


def _trigger_from_payload(raw: object) -> IncidentTrigger:
    item = _mapping(
        raw,
        {
            "project_id",
            "service_id",
            "environment_id",
            "release_sha",
            "kind",
            "severity",
            "evidence_refs",
            "approval_ref",
            "observed_at",
            "advisory",
        },
        "incident trigger",
    )
    advisory_raw = item["advisory"]
    advisory_value = None
    if advisory_raw is not None:
        advisory_item = _mapping(
            advisory_raw,
            {
                "advisory_id",
                "ecosystem",
                "package_name",
                "affected_version",
                "fixed_version",
                "provenance_ref",
            },
            "supply-chain advisory",
        )
        fixed_raw = advisory_item["fixed_version"]
        advisory_value = SupplyChainAdvisory(
            _text(advisory_item["advisory_id"], "advisory id"),
            _text(advisory_item["ecosystem"], "advisory ecosystem"),
            _text(advisory_item["package_name"], "advisory package"),
            _text(advisory_item["affected_version"], "advisory affected version"),
            None if fixed_raw is None else _text(fixed_raw, "advisory fixed version"),
            _text(advisory_item["provenance_ref"], "advisory provenance"),
        )
    return IncidentTrigger(
        _text(item["project_id"], "trigger project"),
        _text(item["service_id"], "trigger service"),
        _text(item["environment_id"], "trigger environment"),
        _text(item["release_sha"], "trigger release"),
        _enum(IncidentKind, item["kind"], "incident kind"),
        _enum(IncidentSeverity, item["severity"], "incident severity"),
        _string_tuple(item["evidence_refs"], "incident evidence"),
        _text(item["approval_ref"], "incident approval"),
        _datetime(item["observed_at"], "incident observed_at"),
        advisory_value,
    )


def _work_order_from_payload(raw: object) -> RepairWorkOrder:
    item = _mapping(
        raw,
        {
            "work_order_id",
            "incident_id",
            "project_id",
            "service_id",
            "repository_id",
            "component_id",
            "base_release_sha",
            "goal",
            "allowed_paths",
            "permission_ceiling",
            "acceptance_commands",
            "evidence_refs",
            "created_at",
            "advisory_id",
            "target_fixed_version",
        },
        "repair work order",
    )
    commands_raw = _list(item["acceptance_commands"], "repair acceptance commands")
    advisory_id = item["advisory_id"]
    fixed_version = item["target_fixed_version"]
    return RepairWorkOrder(
        _text(item["work_order_id"], "work order id"),
        _text(item["incident_id"], "work order incident"),
        _text(item["project_id"], "work order project"),
        _text(item["service_id"], "work order service"),
        _text(item["repository_id"], "work order repository"),
        _text(item["component_id"], "work order component"),
        _text(item["base_release_sha"], "work order base release"),
        _text(item["goal"], "work order goal"),
        _string_tuple(item["allowed_paths"], "work order allowed paths"),
        frozenset(_string_tuple(item["permission_ceiling"], "work order permission ceiling")),
        tuple(_string_tuple(command, "repair acceptance command") for command in commands_raw),
        _string_tuple(item["evidence_refs"], "work order evidence"),
        _datetime(item["created_at"], "work order created_at"),
        None if advisory_id is None else _text(advisory_id, "work order advisory id"),
        None if fixed_version is None else _text(fixed_version, "work order fixed version"),
    )


def _candidate_from_payload(raw: object) -> RepairCandidateEvidence:
    item = _mapping(
        raw,
        {
            "candidate_id",
            "incident_id",
            "work_order_id",
            "base_release_sha",
            "result_sha",
            "artifact_digest",
            "diff_digest",
            "regression_evidence_refs",
            "provenance_evidence_refs",
            "review_ref",
            "review_accepted",
            "recorded_at",
        },
        "repair candidate",
    )
    review_accepted = item["review_accepted"]
    if not isinstance(review_accepted, bool):
        raise ProductIncidentError("repair candidate review_accepted must be boolean")
    return RepairCandidateEvidence(
        _text(item["candidate_id"], "candidate id"),
        _text(item["incident_id"], "candidate incident"),
        _text(item["work_order_id"], "candidate work order"),
        _text(item["base_release_sha"], "candidate base release"),
        _text(item["result_sha"], "candidate result SHA"),
        _text(item["artifact_digest"], "candidate artifact digest"),
        _text(item["diff_digest"], "candidate diff digest"),
        _string_tuple(item["regression_evidence_refs"], "candidate regression evidence"),
        _string_tuple(item["provenance_evidence_refs"], "candidate provenance evidence"),
        _text(item["review_ref"], "candidate review ref"),
        review_accepted,
        _datetime(item["recorded_at"], "candidate recorded_at"),
    )


def _release_from_payload(raw: object) -> ReleaseEvidence:
    item = _mapping(
        raw,
        {
            "release_event_id",
            "incident_id",
            "candidate_id",
            "previous_release_sha",
            "candidate_release_sha",
            "artifact_digest",
            "staging_intent_id",
            "production_intent_id",
            "disposition",
            "deployment_evidence_refs",
            "health_evidence_refs",
            "restored_release_sha",
            "reconciliation_ref",
            "observed_at",
        },
        "release evidence",
    )
    restored = item["restored_release_sha"]
    reconciliation = item["reconciliation_ref"]
    return ReleaseEvidence(
        _text(item["release_event_id"], "release event id"),
        _text(item["incident_id"], "release incident id"),
        _text(item["candidate_id"], "release candidate id"),
        _text(item["previous_release_sha"], "previous release SHA"),
        _text(item["candidate_release_sha"], "candidate release SHA"),
        _text(item["artifact_digest"], "release artifact digest"),
        _text(item["staging_intent_id"], "staging deployment intent id"),
        _text(item["production_intent_id"], "production deployment intent id"),
        _enum(ReleaseDisposition, item["disposition"], "release disposition"),
        _string_tuple(item["deployment_evidence_refs"], "deployment evidence"),
        _string_tuple(item["health_evidence_refs"], "health evidence", allow_empty=True),
        None if restored is None else _text(restored, "restored release SHA"),
        None if reconciliation is None else _text(reconciliation, "reconciliation ref"),
        _datetime(item["observed_at"], "release observed_at"),
    )


def _mapping(raw: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ProductIncidentError(f"{label} must be an object")
    if set(raw) != keys:
        raise ProductIncidentError(f"{label} fields do not match schema")
    return raw


def _list(raw: object, label: str) -> list[object]:
    if not isinstance(raw, list):
        raise ProductIncidentError(f"{label} must be a list")
    return raw


def _text(raw: object, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ProductIncidentError(f"{label} must be non-empty text")
    return raw


def _string_tuple(
    raw: object,
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    values = _list(raw, label)
    if not values and not allow_empty:
        raise ProductIncidentError(f"{label} must not be empty")
    return tuple(_text(value, label) for value in values)


def _datetime(raw: object, label: str) -> datetime:
    text = _text(raw, label)
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProductIncidentError(f"{label} must be ISO-8601 datetime text") from exc
    return _aware_json(value)


def _enum(enum_type: type[StrEnum], raw: object, label: str) -> StrEnum:
    value = _text(raw, label)
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ProductIncidentError(f"{label} is not a supported enum value") from exc


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _aware_json(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProductIncidentError("datetime must be timezone-aware")
    return value.astimezone(UTC)
