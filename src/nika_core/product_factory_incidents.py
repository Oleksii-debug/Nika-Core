from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .product_factory_incident_contracts import (
    INCIDENT_LIFECYCLE_SCHEMA,
    IncidentKind,
    IncidentLifecycleSnapshot,
    IncidentRecord,
    IncidentState,
    IncidentTrigger,
    OperationsSnapshotView,
    ProductIncidentError,
    ReleaseDisposition,
    ReleaseEvidence,
    RepairCandidateEvidence,
    RepairWorkOrder,
    ServiceRecordView,
    validate_digest,
)


@dataclass(slots=True)
class IncidentRepairReleaseCoordinator:
    project_id: str
    _incidents: dict[str, IncidentRecord] = field(default_factory=dict, init=False, repr=False)
    _fingerprints: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.project_id.strip():
            raise ProductIncidentError("project_id must not be empty")

    def open_incident(
        self,
        incident_id: str,
        trigger: IncidentTrigger,
        operations: OperationsSnapshotView,
    ) -> IncidentRecord:
        if not incident_id.strip():
            raise ProductIncidentError("incident_id must not be empty")
        if trigger.project_id != self.project_id or operations.project_id != self.project_id:
            raise ProductIncidentError("incident/operations belongs to another project")
        service = _service_from_operations(operations, trigger.service_id)
        if service.service.project_id != self.project_id:
            raise ProductIncidentError("operations service belongs to another project")
        if service.service.environment_id != trigger.environment_id:
            raise ProductIncidentError("incident environment does not match operations service")
        if service.service.release_sha != trigger.release_sha:
            raise ProductIncidentError(
                "incident release is stale or does not match operations service"
            )

        if trigger.kind in {IncidentKind.HEALTH, IncidentKind.ERROR, IncidentKind.OPERATOR}:
            known = _service_evidence(operations, trigger.service_id)
            if not set(trigger.evidence_refs) <= known:
                raise ProductIncidentError(
                    "incident evidence is not present in approved operations evidence"
                )

        existing = self._incidents.get(incident_id)
        if existing is not None:
            if existing.trigger.fingerprint != trigger.fingerprint:
                raise ProductIncidentError("incident id conflicts with prior trigger")
            return existing

        duplicate_id = self._fingerprints.get(trigger.fingerprint)
        if duplicate_id is not None:
            return self._incidents[duplicate_id]

        record = IncidentRecord(incident_id, trigger, IncidentState.OPEN)
        self._incidents[incident_id] = record
        self._fingerprints[trigger.fingerprint] = incident_id
        return record

    def create_repair_work_order(self, work_order: RepairWorkOrder) -> IncidentRecord:
        record = self._require(work_order.incident_id)
        if work_order.project_id != self.project_id:
            raise ProductIncidentError("repair work order belongs to another project")
        if work_order.service_id != record.trigger.service_id:
            raise ProductIncidentError("repair work order service does not match incident")
        if work_order.base_release_sha != record.trigger.release_sha:
            raise ProductIncidentError("repair work order base must equal incident release")
        if not set(record.trigger.evidence_refs) <= set(work_order.evidence_refs):
            raise ProductIncidentError("repair work order must preserve incident evidence")
        advisory = record.trigger.advisory
        if advisory is None:
            if work_order.advisory_id is not None or work_order.target_fixed_version is not None:
                raise ProductIncidentError(
                    "non-supply-chain repair cannot claim advisory remediation"
                )
        else:
            if work_order.advisory_id != advisory.advisory_id:
                raise ProductIncidentError("repair work order advisory does not match incident")
            if advisory.fixed_version is not None:
                if work_order.target_fixed_version != advisory.fixed_version:
                    raise ProductIncidentError(
                        "repair work order fixed version does not match advisory"
                    )
            elif work_order.target_fixed_version is not None:
                raise ProductIncidentError(
                    "repair work order cannot invent an unavailable advisory fixed version"
                )
        for other in self._incidents.values():
            if (
                other.incident_id != record.incident_id
                and other.work_order is not None
                and other.work_order.work_order_id == work_order.work_order_id
            ):
                raise ProductIncidentError("repair work order id belongs to another incident")
        if record.work_order is not None:
            if record.work_order != work_order:
                raise ProductIncidentError("incident already has a different repair work order")
            return record
        if record.state not in {IncidentState.OPEN, IncidentState.PLANNED}:
            raise ProductIncidentError("incident state does not allow repair planning")

        updated = IncidentRecord(
            record.incident_id,
            record.trigger,
            IncidentState.PLANNED,
            work_order,
            record.candidates,
            record.release_events,
        )
        self._incidents[record.incident_id] = updated
        return updated

    def record_candidate(self, candidate: RepairCandidateEvidence) -> IncidentRecord:
        record = self._require(candidate.incident_id)
        prior = {item.candidate_id: item for item in record.candidates}
        if candidate.candidate_id in prior:
            if prior[candidate.candidate_id] != candidate:
                raise ProductIncidentError("candidate id conflicts with prior evidence")
            return record
        if record.state not in {IncidentState.PLANNED, IncidentState.REVIEW_REQUIRED}:
            raise ProductIncidentError("incident state does not allow repair candidate evidence")
        if record.work_order is None:
            raise ProductIncidentError("repair candidate requires a repair work order")
        if candidate.work_order_id != record.work_order.work_order_id:
            raise ProductIncidentError("repair candidate work order does not match incident")
        if candidate.base_release_sha != record.trigger.release_sha:
            raise ProductIncidentError("repair candidate base release does not match incident")
        if any(item.result_sha == candidate.result_sha for item in record.candidates):
            raise ProductIncidentError("candidate release SHA is already recorded for incident")
        for other in self._incidents.values():
            if other.incident_id == record.incident_id:
                continue
            if any(item.candidate_id == candidate.candidate_id for item in other.candidates):
                raise ProductIncidentError("repair candidate id belongs to another incident")
        advisory = record.trigger.advisory
        if (
            advisory is not None
            and advisory.provenance_ref not in candidate.provenance_evidence_refs
        ):
            raise ProductIncidentError(
                "supply-chain repair candidate must preserve advisory provenance"
            )

        state = (
            IncidentState.RELEASE_READY
            if candidate.review_accepted
            else IncidentState.REVIEW_REQUIRED
        )
        updated = IncidentRecord(
            record.incident_id,
            record.trigger,
            state,
            record.work_order,
            record.candidates + (candidate,),
            record.release_events,
        )
        self._incidents[record.incident_id] = updated
        return updated

    def record_release(self, evidence: ReleaseEvidence) -> IncidentRecord:
        record = self._require(evidence.incident_id)
        candidate = self._candidate(record, evidence.candidate_id)
        if not candidate.review_accepted:
            raise ProductIncidentError("release requires independently accepted repair candidate")
        if candidate.result_sha != evidence.candidate_release_sha:
            raise ProductIncidentError("release SHA does not match accepted repair candidate")
        if candidate.artifact_digest != evidence.artifact_digest:
            raise ProductIncidentError("release artifact does not match accepted repair candidate")
        if evidence.previous_release_sha != record.trigger.release_sha:
            raise ProductIncidentError("release previous SHA does not match incident release")

        prior = {item.release_event_id: item for item in record.release_events}
        if evidence.release_event_id in prior:
            if prior[evidence.release_event_id] != evidence:
                raise ProductIncidentError("release event id conflicts with prior evidence")
            return record
        for other in self._incidents.values():
            if other.incident_id == record.incident_id:
                continue
            if any(
                item.release_event_id == evidence.release_event_id
                for item in other.release_events
            ):
                raise ProductIncidentError("release event id belongs to another incident")
        if record.release_events:
            latest = record.release_events[-1]
            if latest.disposition is not ReleaseDisposition.UNCERTAIN:
                raise ProductIncidentError("incident already has terminal release evidence")
            raise ProductIncidentError(
                "uncertain release must be reconciled, not followed by a new release event"
            )

        state = {
            ReleaseDisposition.HEALTHY: IncidentState.RESOLVED,
            ReleaseDisposition.ROLLED_BACK: IncidentState.ROLLED_BACK,
            ReleaseDisposition.UNCERTAIN: IncidentState.RECONCILE_REQUIRED,
        }[evidence.disposition]
        updated = IncidentRecord(
            record.incident_id,
            record.trigger,
            state,
            record.work_order,
            record.candidates,
            record.release_events + (evidence,),
        )
        self._incidents[record.incident_id] = updated
        return updated

    def reconcile_release(
        self,
        incident_id: str,
        *,
        reconciliation_ref: str,
        disposition: ReleaseDisposition,
        health_evidence_refs: tuple[str, ...],
        restored_release_sha: str | None,
        observed_at: datetime,
    ) -> IncidentRecord:
        record = self._require(incident_id)
        if record.state is not IncidentState.RECONCILE_REQUIRED or not record.release_events:
            raise ProductIncidentError("incident has no uncertain release to reconcile")
        if disposition is ReleaseDisposition.UNCERTAIN:
            raise ProductIncidentError("reconciliation must reach a terminal release disposition")
        if not reconciliation_ref.strip():
            raise ProductIncidentError("reconciliation_ref must not be empty")
        uncertain = record.release_events[-1]
        if uncertain.disposition is not ReleaseDisposition.UNCERTAIN:
            raise ProductIncidentError("latest release evidence is not uncertain")
        reconciled = ReleaseEvidence(
            release_event_id=uncertain.release_event_id,
            incident_id=uncertain.incident_id,
            candidate_id=uncertain.candidate_id,
            previous_release_sha=uncertain.previous_release_sha,
            candidate_release_sha=uncertain.candidate_release_sha,
            artifact_digest=uncertain.artifact_digest,
            disposition=disposition,
            deployment_evidence_refs=uncertain.deployment_evidence_refs,
            health_evidence_refs=health_evidence_refs,
            restored_release_sha=restored_release_sha,
            reconciliation_ref=reconciliation_ref,
            observed_at=observed_at,
        )
        state = (
            IncidentState.RESOLVED
            if disposition is ReleaseDisposition.HEALTHY
            else IncidentState.ROLLED_BACK
        )
        updated = IncidentRecord(
            record.incident_id,
            record.trigger,
            state,
            record.work_order,
            record.candidates,
            record.release_events[:-1] + (reconciled,),
        )
        self._incidents[incident_id] = updated
        return updated

    def get(self, incident_id: str) -> IncidentRecord:
        return self._require(incident_id)

    def list_incidents(self) -> tuple[IncidentRecord, ...]:
        return tuple(self._incidents[key] for key in sorted(self._incidents))

    def snapshot(self) -> IncidentLifecycleSnapshot:
        return IncidentLifecycleSnapshot(
            INCIDENT_LIFECYCLE_SCHEMA,
            self.project_id,
            self.list_incidents(),
            tuple(sorted(self._fingerprints.items())),
        )

    def restore(self, snapshot: IncidentLifecycleSnapshot) -> None:
        if snapshot.project_id != self.project_id:
            raise ProductIncidentError("incident snapshot belongs to another project")
        incident_ids = [record.incident_id for record in snapshot.incidents]
        if len(incident_ids) != len(set(incident_ids)):
            raise ProductIncidentError("incident snapshot contains duplicate incident ids")
        fingerprint_pairs = list(snapshot.fingerprint_index)
        fingerprints = [item[0] for item in fingerprint_pairs]
        mapped_ids = [item[1] for item in fingerprint_pairs]
        if len(fingerprints) != len(set(fingerprints)):
            raise ProductIncidentError("incident snapshot contains duplicate fingerprints")
        if len(mapped_ids) != len(set(mapped_ids)):
            raise ProductIncidentError("incident snapshot fingerprint aliases incident ids")
        for fingerprint in fingerprints:
            validate_digest(fingerprint, "incident fingerprint")
        if set(mapped_ids) != set(incident_ids):
            raise ProductIncidentError("incident snapshot fingerprint index is incomplete")

        incidents = {record.incident_id: record for record in snapshot.incidents}
        work_ids = [
            record.work_order.work_order_id
            for record in snapshot.incidents
            if record.work_order is not None
        ]
        candidate_ids = [
            candidate.candidate_id
            for record in snapshot.incidents
            for candidate in record.candidates
        ]
        release_ids = [
            release.release_event_id
            for record in snapshot.incidents
            for release in record.release_events
        ]
        if len(work_ids) != len(set(work_ids)):
            raise ProductIncidentError("incident snapshot contains duplicate work-order ids")
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ProductIncidentError("incident snapshot contains duplicate candidate ids")
        if len(release_ids) != len(set(release_ids)):
            raise ProductIncidentError("incident snapshot contains duplicate release-event ids")
        for record in snapshot.incidents:
            self._validate_record(record)
            if record.trigger.project_id != self.project_id:
                raise ProductIncidentError("incident snapshot crosses project boundary")
            expected = record.trigger.fingerprint
            if dict(fingerprint_pairs).get(expected) != record.incident_id:
                raise ProductIncidentError("incident snapshot fingerprint mapping is corrupt")

        self._incidents = incidents
        self._fingerprints = dict(fingerprint_pairs)

    def _validate_record(self, record: IncidentRecord) -> None:
        if record.work_order is not None:
            if record.work_order.incident_id != record.incident_id:
                raise ProductIncidentError("snapshot work order incident binding is invalid")
            if record.work_order.project_id != self.project_id:
                raise ProductIncidentError("snapshot work order crosses project boundary")
            if record.work_order.service_id != record.trigger.service_id:
                raise ProductIncidentError("snapshot work order service binding is invalid")
            if record.work_order.base_release_sha != record.trigger.release_sha:
                raise ProductIncidentError("snapshot work order base release is invalid")
            advisory = record.trigger.advisory
            if advisory is None:
                if (
                    record.work_order.advisory_id is not None
                    or record.work_order.target_fixed_version is not None
                ):
                    raise ProductIncidentError(
                        "snapshot non-supply-chain work order claims advisory remediation"
                    )
            else:
                if record.work_order.advisory_id != advisory.advisory_id:
                    raise ProductIncidentError("snapshot work order advisory binding is invalid")
                if record.work_order.target_fixed_version != advisory.fixed_version:
                    raise ProductIncidentError(
                        "snapshot work order fixed-version binding is invalid"
                    )
        elif record.candidates or record.release_events:
            raise ProductIncidentError("snapshot candidate/release requires work order")

        candidate_ids: set[str] = set()
        result_shas: set[str] = set()
        for candidate in record.candidates:
            if candidate.incident_id != record.incident_id:
                raise ProductIncidentError("snapshot candidate incident binding is invalid")
            if (
                record.work_order is None
                or candidate.work_order_id != record.work_order.work_order_id
            ):
                raise ProductIncidentError("snapshot candidate work-order binding is invalid")
            if candidate.base_release_sha != record.trigger.release_sha:
                raise ProductIncidentError("snapshot candidate base release is invalid")
            if candidate.candidate_id in candidate_ids or candidate.result_sha in result_shas:
                raise ProductIncidentError("snapshot contains duplicate repair candidate")
            candidate_ids.add(candidate.candidate_id)
            result_shas.add(candidate.result_sha)
            if (
                record.trigger.advisory is not None
                and record.trigger.advisory.provenance_ref
                not in candidate.provenance_evidence_refs
            ):
                raise ProductIncidentError(
                    "snapshot supply-chain candidate lost advisory provenance"
                )

        if len(record.release_events) > 1:
            raise ProductIncidentError(
                "snapshot incident has multiple release side-effect identities"
            )
        for release in record.release_events:
            if release.incident_id != record.incident_id:
                raise ProductIncidentError("snapshot release incident binding is invalid")
            candidate = next(
                (item for item in record.candidates if item.candidate_id == release.candidate_id),
                None,
            )
            if candidate is None or not candidate.review_accepted:
                raise ProductIncidentError("snapshot release lacks accepted candidate")
            if release.candidate_release_sha != candidate.result_sha:
                raise ProductIncidentError("snapshot release SHA differs from candidate")
            if release.artifact_digest != candidate.artifact_digest:
                raise ProductIncidentError("snapshot release artifact differs from candidate")
            if release.previous_release_sha != record.trigger.release_sha:
                raise ProductIncidentError("snapshot previous release differs from incident")

        expected_state = self._derived_state(record)
        if record.state is not expected_state:
            raise ProductIncidentError("snapshot incident state is not derivable from evidence")

    @staticmethod
    def _derived_state(record: IncidentRecord) -> IncidentState:
        if record.release_events:
            return {
                ReleaseDisposition.HEALTHY: IncidentState.RESOLVED,
                ReleaseDisposition.ROLLED_BACK: IncidentState.ROLLED_BACK,
                ReleaseDisposition.UNCERTAIN: IncidentState.RECONCILE_REQUIRED,
            }[record.release_events[-1].disposition]
        if record.candidates:
            latest = record.candidates[-1]
            return (
                IncidentState.RELEASE_READY
                if latest.review_accepted
                else IncidentState.REVIEW_REQUIRED
            )
        if record.work_order is not None:
            return IncidentState.PLANNED
        return IncidentState.OPEN

    def _require(self, incident_id: str) -> IncidentRecord:
        try:
            return self._incidents[incident_id]
        except KeyError as exc:
            raise ProductIncidentError("unknown product incident") from exc

    @staticmethod
    def _candidate(record: IncidentRecord, candidate_id: str) -> RepairCandidateEvidence:
        for candidate in record.candidates:
            if candidate.candidate_id == candidate_id:
                return candidate
        raise ProductIncidentError("unknown repair candidate")


def _service_from_operations(
    operations: OperationsSnapshotView,
    service_id: str,
) -> ServiceRecordView:
    matches = [record for record in operations.services if record.service.service_id == service_id]
    if len(matches) != 1:
        raise ProductIncidentError("operations snapshot must contain exactly one incident service")
    return matches[0]


def _service_evidence(
    operations: OperationsSnapshotView,
    service_id: str,
) -> set[str]:
    record = _service_from_operations(operations, service_id)
    refs: set[str] = set()
    if record.observation is not None:
        refs.update(record.observation.evidence_refs)
    if record.rollback is not None:
        refs.update(record.rollback.evidence_refs)
    for maintenance in operations.maintenance_records:
        if maintenance.request.service_id == service_id:
            refs.update(maintenance.result.evidence_refs)
    return refs
