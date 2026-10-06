from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import pairwise
from typing import Any

from nika_core.product_project import (
    ProductDecisionState,
    ProductProjectError,
    ProductProjectSpec,
    StaleProjectVersionError,
)
from nika_core.product_project_integrity import (
    ProductProjectIntegrityReport,
    ProductProjectIntegrityService,
)
from nika_core.product_project_lifecycle import _ALLOWED_TRANSITIONS, ProductProjectState


@dataclass(frozen=True, slots=True)
class ProductProjectHistoricalIntegrityReport:
    """PF12 history/causality evidence layered on the current-snapshot PF1 report."""

    current: ProductProjectIntegrityReport
    historical_spec_reference_count: int
    historical_decision_reference_count: int
    lifecycle_transition_count: int
    causal_mutation_count: int
    mutation_idempotency_count: int


@dataclass(frozen=True, slots=True)
class _DecisionVersion:
    decision_id: str
    decision_version: int
    state: ProductDecisionState
    evidence_package_ids: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _LifecycleEvent:
    row_version: int
    previous_state: ProductProjectState
    new_state: ProductProjectState
    reason: str
    changed_by_ref: str


class ProductProjectHistoricalIntegrityService:
    """Fail closed on impossible PF1 history that a valid current snapshot could hide."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def validate(
        self,
        project_id: str,
        *,
        expected_spec_version: int | None = None,
        expected_row_version: int | None = None,
    ) -> ProductProjectHistoricalIntegrityReport:
        if type(project_id) is not str or not project_id.strip():
            raise ProductProjectError("project_id must be non-empty text")

        with self.store.connection() as conn:
            conn.execute("BEGIN")
            project = conn.execute(
                "SELECT current_spec_version,row_version,status FROM product_projects "
                "WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise KeyError(project_id)
            spec_version = self._required_json_int(
                project["current_spec_version"],
                minimum=1,
                label="ProductProject current_spec_version",
            )
            row_version = self._required_json_int(
                project["row_version"],
                minimum=0,
                label="ProductProject row_version",
            )
            self._validate_expected_versions(
                spec_version,
                row_version,
                expected_spec_version=expected_spec_version,
                expected_row_version=expected_row_version,
            )

            spec_rows = conn.execute(
                "SELECT spec_version,spec_json,created_at FROM product_project_specs "
                "WHERE project_id=? ORDER BY spec_version",
                (project_id,),
            ).fetchall()
            research_rows = conn.execute(
                "SELECT package_id,payload_json,created_at FROM product_research_handoffs "
                "WHERE project_id=? ORDER BY package_id",
                (project_id,),
            ).fetchall()
            decision_rows = conn.execute(
                "SELECT decision_id,decision_version,option_id,state,rationale,decided_by_ref,"
                "evidence_package_ids_json,created_at FROM product_decisions "
                "WHERE project_id=? ORDER BY decision_id,decision_version",
                (project_id,),
            ).fetchall()

            package_times = self._package_times(research_rows)
            decisions = self._decision_history(decision_rows)
            historical_refs, historical_decision_refs = self._validate_historical_specs(
                spec_rows,
                package_times=package_times,
                decisions=decisions,
            )
            lifecycle_events, idempotency_count = self._validate_causal_history(
                conn,
                project_id,
                current_status=self._required_text(
                    project["status"],
                    label="ProductProject status",
                ),
                current_row_version=row_version,
                spec_rows=spec_rows,
                research_rows=research_rows,
                decision_rows=decision_rows,
            )

        # Re-run the integrated current-snapshot validator with exact captured versions.
        # If another writer changed the project between snapshots, this fails stale rather
        # than returning evidence assembled from two different durable versions.
        current = ProductProjectIntegrityService(self.store).validate(
            project_id,
            expected_spec_version=spec_version,
            expected_row_version=row_version,
        )
        return ProductProjectHistoricalIntegrityReport(
            current=current,
            historical_spec_reference_count=historical_refs,
            historical_decision_reference_count=historical_decision_refs,
            lifecycle_transition_count=len(lifecycle_events),
            causal_mutation_count=row_version,
            mutation_idempotency_count=idempotency_count,
        )

    @staticmethod
    def _validate_expected_versions(
        spec_version: int,
        row_version: int,
        *,
        expected_spec_version: int | None,
        expected_row_version: int | None,
    ) -> None:
        if expected_spec_version is not None:
            ProductProjectHistoricalIntegrityService._required_json_int(
                expected_spec_version,
                minimum=1,
                label="expected_spec_version",
            )
            if spec_version != expected_spec_version:
                raise StaleProjectVersionError(
                    f"stale ProductProject spec: expected {expected_spec_version}, "
                    f"current {spec_version}"
                )
        if expected_row_version is not None:
            ProductProjectHistoricalIntegrityService._required_json_int(
                expected_row_version,
                minimum=0,
                label="expected_row_version",
            )
            if row_version != expected_row_version:
                raise StaleProjectVersionError(
                    f"stale ProductProject row: expected {expected_row_version}, "
                    f"current {row_version}"
                )

    @staticmethod
    def _required_text(value: Any, *, label: str) -> str:
        if type(value) is not str or not value.strip():
            raise ProductProjectError(f"invalid text identity for {label}")
        return value

    @staticmethod
    def _time(value: Any, *, label: str) -> datetime:
        if type(value) is not str:
            raise ProductProjectError(f"invalid timestamp for {label}")
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ProductProjectError(f"invalid timestamp for {label}") from exc
        if parsed.utcoffset() is None:
            raise ProductProjectError(f"naive timestamp for {label}")
        return parsed

    @staticmethod
    def _json_object(value: Any, *, label: str) -> dict[str, Any]:
        if type(value) is not str:
            raise ProductProjectError(f"invalid JSON for {label}")
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProductProjectError(f"invalid JSON for {label}") from exc
        if not isinstance(parsed, dict):
            raise ProductProjectError(f"invalid JSON object for {label}")
        return parsed

    @staticmethod
    def _required_json_int(value: Any, *, minimum: int, label: str) -> int:
        if type(value) is not int or value < minimum:
            raise ProductProjectError(f"invalid integer identity for {label}")
        return value

    def _package_times(self, rows: list[Any]) -> dict[str, datetime]:
        result: dict[str, datetime] = {}
        for row in rows:
            package_id = self._required_text(
                row["package_id"],
                label="research package identity",
            )
            if package_id in result:
                raise ProductProjectError("invalid or duplicate research package identity")
            payload = self._json_object(
                row["payload_json"],
                label=f"research package {package_id}",
            )
            if payload.get("package_id") != package_id:
                raise ProductProjectError(
                    f"research handoff package identity mismatch: {package_id}"
                )
            result[package_id] = self._time(
                row["created_at"],
                label=f"research package {package_id}",
            )
        return result

    def _decision_history(
        self,
        rows: list[Any],
    ) -> dict[str, tuple[_DecisionVersion, ...]]:
        grouped: dict[str, list[_DecisionVersion]] = defaultdict(list)
        for row in rows:
            decision_id = self._required_text(
                row["decision_id"],
                label="product decision identity",
            )
            raw_evidence = row["evidence_package_ids_json"]
            if type(raw_evidence) is not str:
                raise ProductProjectError(
                    f"invalid historical product decision: {decision_id}"
                )
            try:
                state = ProductDecisionState(row["state"])
                parsed_evidence = json.loads(raw_evidence)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ProductProjectError(
                    f"invalid historical product decision: {decision_id}"
                ) from exc
            if not isinstance(parsed_evidence, list) or any(
                type(ref) is not str or not ref.strip()
                for ref in parsed_evidence
            ):
                raise ProductProjectError(
                    f"invalid historical product decision: {decision_id}"
                )
            grouped[decision_id].append(
                _DecisionVersion(
                    decision_id=decision_id,
                    decision_version=self._required_json_int(
                        row["decision_version"],
                        minimum=1,
                        label=f"product decision version {decision_id}",
                    ),
                    state=state,
                    evidence_package_ids=tuple(parsed_evidence),
                    created_at=self._time(
                        row["created_at"],
                        label=f"product decision {decision_id}",
                    ),
                )
            )

        result: dict[str, tuple[_DecisionVersion, ...]] = {}
        for decision_id, history in grouped.items():
            versions = tuple(item.decision_version for item in history)
            if versions != tuple(range(1, len(history) + 1)):
                raise ProductProjectError(
                    f"product decision history is not contiguous: {decision_id}"
                )
            times = tuple(item.created_at for item in history)
            if any(later < earlier for earlier, later in pairwise(times)):
                raise ProductProjectError(
                    f"product decision timestamps move backwards: {decision_id}"
                )
            result[decision_id] = tuple(history)
        return result

    def _validate_historical_specs(
        self,
        rows: list[Any],
        *,
        package_times: dict[str, datetime],
        decisions: dict[str, tuple[_DecisionVersion, ...]],
    ) -> tuple[int, int]:
        previous_time: datetime | None = None
        reference_count = 0
        decision_reference_count = 0
        for row in rows:
            version = self._required_json_int(
                row["spec_version"],
                minimum=1,
                label="ProductProject spec_version",
            )
            created_at = self._time(
                row["created_at"],
                label=f"ProductProject spec version {version}",
            )
            if previous_time is not None and created_at < previous_time:
                raise ProductProjectError(
                    f"ProductProject spec timestamps move backwards at version {version}"
                )
            previous_time = created_at
            raw = self._json_object(
                row["spec_json"],
                label=f"ProductProject spec version {version}",
            )
            parent = raw.get("supersedes_spec_version")
            if version == 1:
                if parent is not None:
                    raise ProductProjectError(
                        "initial ProductProject spec must not have a supersession parent"
                    )
            else:
                parent_version = self._required_json_int(
                    parent,
                    minimum=1,
                    label=f"ProductProject spec {version} supersedes_spec_version",
                )
                if parent_version != version - 1:
                    raise ProductProjectError(
                        f"ProductProject spec {version} has incoherent supersession parent"
                    )
            try:
                spec = ProductProjectSpec.from_dict(raw)
            except (KeyError, TypeError, ValueError) as exc:
                raise ProductProjectError(
                    f"invalid ProductProject spec version {version}: {type(exc).__name__}"
                ) from exc

            for requirement in spec.requirements:
                for package_id in requirement.evidence_package_ids:
                    reference_count += 1
                    self._require_package_available(
                        package_id,
                        package_times=package_times,
                        at=created_at,
                        label=(
                            f"spec version {version} requirement "
                            f"{requirement.requirement_id}"
                        ),
                    )
                for decision_id in requirement.decision_ids:
                    reference_count += 1
                    decision_reference_count += 1
                    self._require_decision_approved_at(
                        decision_id,
                        decisions=decisions,
                        at=created_at,
                        label=(
                            f"spec version {version} requirement "
                            f"{requirement.requirement_id}"
                        ),
                    )
            for architecture in spec.architecture_decisions:
                for package_id in architecture.evidence_package_ids:
                    reference_count += 1
                    self._require_package_available(
                        package_id,
                        package_times=package_times,
                        at=created_at,
                        label=(
                            f"spec version {version} architecture decision "
                            f"{architecture.architecture_decision_id}"
                        ),
                    )
        return reference_count, decision_reference_count

    @staticmethod
    def _require_package_available(
        package_id: str,
        *,
        package_times: dict[str, datetime],
        at: datetime,
        label: str,
    ) -> None:
        created_at = package_times.get(package_id)
        if created_at is None:
            raise ProductProjectError(f"{label} references missing research package: {package_id}")
        if created_at > at:
            raise ProductProjectError(
                f"{label} references future research package: {package_id}"
            )

    @staticmethod
    def _require_decision_approved_at(
        decision_id: str,
        *,
        decisions: dict[str, tuple[_DecisionVersion, ...]],
        at: datetime,
        label: str,
    ) -> None:
        history = decisions.get(decision_id)
        if history is None:
            raise ProductProjectError(f"{label} references unknown product decision: {decision_id}")
        available = tuple(item for item in history if item.created_at <= at)
        if not available:
            raise ProductProjectError(f"{label} references future product decision: {decision_id}")
        if available[-1].state is not ProductDecisionState.APPROVED:
            raise ProductProjectError(
                f"{label} references product decision before approval: {decision_id}"
            )

    def _validate_causal_history(
        self,
        conn: Any,
        project_id: str,
        *,
        current_status: str,
        current_row_version: int,
        spec_rows: list[Any],
        research_rows: list[Any],
        decision_rows: list[Any],
    ) -> tuple[tuple[_LifecycleEvent, ...], int]:
        audit_rows = conn.execute(
            "SELECT event_id,event_type,payload_json,created_at FROM audit_events "
            "WHERE entity_type='product_project' AND entity_id=? ORDER BY event_id",
            (project_id,),
        ).fetchall()
        by_type: dict[str, list[Any]] = defaultdict(list)
        for row in audit_rows:
            event_type = self._required_text(
                row["event_type"],
                label="ProductProject audit event type",
            )
            by_type[event_type].append(row)

        self._validate_creation_audit(by_type.get("product_project.created", []))
        self._validate_research_audits(
            by_type.get("product_project.research_handoff", []),
            research_rows,
        )
        self._validate_spec_audits(
            by_type.get("product_project.spec_versioned", []),
            spec_rows,
        )
        self._validate_decision_audits(
            by_type.get("product_project.decision_recorded", []),
            decision_rows,
        )
        lifecycle = self._validate_lifecycle_audits(
            by_type.get("product_project.status_changed", []),
            current_status=current_status,
            current_row_version=current_row_version,
        )

        causal_count = max(len(spec_rows) - 1, 0) + len(decision_rows) + len(lifecycle)
        if causal_count != current_row_version:
            raise ProductProjectError(
                "ProductProject row_version has no exact PF1 mutation history: "
                f"row={current_row_version}, mutations={causal_count}"
            )
        idempotency_count = self._validate_idempotency(
            conn,
            project_id,
            spec_rows=spec_rows,
            decision_rows=decision_rows,
            lifecycle=lifecycle,
        )
        return lifecycle, idempotency_count

    def _validate_creation_audit(self, rows: list[Any]) -> None:
        if len(rows) != 1:
            raise ProductProjectError("ProductProject requires exactly one creation audit event")
        payload = self._json_object(rows[0]["payload_json"], label="ProductProject creation audit")
        spec_version = self._required_json_int(
            payload.get("spec_version"),
            minimum=1,
            label="ProductProject creation audit spec_version",
        )
        if spec_version != 1:
            raise ProductProjectError("invalid ProductProject creation audit spec version")
        self._time(rows[0]["created_at"], label="ProductProject creation audit")

    def _validate_research_audits(self, audit_rows: list[Any], research_rows: list[Any]) -> None:
        package_ids = {
            self._required_text(
                row["package_id"],
                label="research package identity",
            )
            for row in research_rows
        }
        audited: set[str] = set()
        for row in audit_rows:
            payload = self._json_object(row["payload_json"], label="research handoff audit")
            try:
                package_id = self._required_text(
                    payload.get("package_id"),
                    label="research handoff audit package_id",
                )
            except ProductProjectError as exc:
                raise ProductProjectError("invalid or duplicate research handoff audit") from exc
            if package_id in audited:
                raise ProductProjectError("invalid or duplicate research handoff audit")
            audited.add(package_id)
            self._time(row["created_at"], label=f"research handoff audit {package_id}")
        if audited != package_ids:
            raise ProductProjectError(
                "research handoff audit history does not match durable packages"
            )

    def _validate_spec_audits(self, audit_rows: list[Any], spec_rows: list[Any]) -> None:
        expected_versions = set(range(2, len(spec_rows) + 1))
        audited: set[int] = set()
        for row in audit_rows:
            payload = self._json_object(row["payload_json"], label="spec revision audit")
            try:
                reason = self._required_text(
                    payload["change_reason"],
                    label="ProductProject spec revision audit change_reason",
                )
            except (KeyError, ProductProjectError) as exc:
                raise ProductProjectError("invalid ProductProject spec revision audit") from exc
            version = self._required_json_int(
                payload.get("spec_version"),
                minimum=2,
                label="ProductProject spec revision audit spec_version",
            )
            parent = self._required_json_int(
                payload.get("supersedes_spec_version"),
                minimum=1,
                label="ProductProject spec revision audit supersedes_spec_version",
            )
            if version in audited or parent != version - 1 or not reason.strip():
                raise ProductProjectError("invalid ProductProject spec revision audit")
            audited.add(version)
            self._time(row["created_at"], label=f"spec revision audit {version}")
        if audited != expected_versions:
            raise ProductProjectError("spec revision audit history does not match durable specs")

    def _validate_decision_audits(self, audit_rows: list[Any], decision_rows: list[Any]) -> None:
        durable = {
            (
                self._required_text(
                    row["decision_id"],
                    label="product decision identity",
                ),
                self._required_json_int(
                    row["decision_version"],
                    minimum=1,
                    label="product decision version",
                ),
            ): row
            for row in decision_rows
        }
        audited: set[tuple[str, int]] = set()
        for row in audit_rows:
            payload = self._json_object(row["payload_json"], label="product decision audit")
            try:
                decision_id = self._required_text(
                    payload["decision_id"],
                    label="product decision audit decision_id",
                )
                state = ProductDecisionState(payload["state"])
                raw_evidence = payload["evidence_package_ids"]
                if not isinstance(raw_evidence, list) or any(
                    type(ref) is not str or not ref.strip()
                    for ref in raw_evidence
                ):
                    raise ValueError("invalid evidence_package_ids")
                evidence = tuple(raw_evidence)
            except (KeyError, TypeError, ValueError, ProductProjectError) as exc:
                raise ProductProjectError("invalid product decision audit") from exc
            decision_version = self._required_json_int(
                payload.get("decision_version"),
                minimum=1,
                label="product decision audit decision_version",
            )
            key = (decision_id, decision_version)
            durable_row = durable.get(key)
            if durable_row is None or key in audited:
                raise ProductProjectError("product decision audit has no unique durable decision")
            durable_state = self._required_text(
                durable_row["state"],
                label="durable product decision state",
            )
            if state.value != durable_state:
                raise ProductProjectError("product decision audit state drift")
            raw_durable_evidence = durable_row["evidence_package_ids_json"]
            if type(raw_durable_evidence) is not str:
                raise ProductProjectError("invalid durable product decision evidence")
            try:
                parsed_durable_evidence = json.loads(raw_durable_evidence)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ProductProjectError("invalid durable product decision evidence") from exc
            if not isinstance(parsed_durable_evidence, list) or any(
                type(ref) is not str or not ref.strip()
                for ref in parsed_durable_evidence
            ):
                raise ProductProjectError("invalid durable product decision evidence")
            durable_evidence = tuple(parsed_durable_evidence)
            if evidence != durable_evidence:
                raise ProductProjectError("product decision audit evidence drift")
            audited.add(key)
            self._time(row["created_at"], label=f"product decision audit {key[0]}")
        if audited != set(durable):
            raise ProductProjectError(
                "product decision audit history does not match durable decisions"
            )

    def _validate_lifecycle_audits(
        self,
        rows: list[Any],
        *,
        current_status: str,
        current_row_version: int,
    ) -> tuple[_LifecycleEvent, ...]:
        try:
            durable_state = ProductProjectState(current_status)
        except ValueError as exc:
            raise ProductProjectError(
                f"unsupported durable ProductProject status: {current_status}"
            ) from exc
        previous = ProductProjectState.ACTIVE
        previous_row_version = 0
        events: list[_LifecycleEvent] = []
        for row in rows:
            payload = self._json_object(row["payload_json"], label="ProductProject lifecycle audit")
            try:
                old_state = ProductProjectState(payload["previous_state"])
                new_state = ProductProjectState(payload["new_state"])
                reason = self._required_text(
                    payload["reason"],
                    label="ProductProject lifecycle reason",
                )
                actor = self._required_text(
                    payload["changed_by_ref"],
                    label="ProductProject lifecycle changed_by_ref",
                )
            except (KeyError, TypeError, ValueError, ProductProjectError) as exc:
                raise ProductProjectError("invalid ProductProject lifecycle audit") from exc
            row_version = self._required_json_int(
                payload.get("row_version"),
                minimum=1,
                label="ProductProject lifecycle audit row_version",
            )
            if (
                row_version <= previous_row_version
                or row_version > current_row_version
                or old_state is not previous
                or new_state not in _ALLOWED_TRANSITIONS[old_state]
                or not reason.strip()
                or not actor.strip()
            ):
                raise ProductProjectError("incoherent ProductProject lifecycle audit chain")
            events.append(
                _LifecycleEvent(
                    row_version=row_version,
                    previous_state=old_state,
                    new_state=new_state,
                    reason=reason,
                    changed_by_ref=actor,
                )
            )
            previous = new_state
            previous_row_version = row_version
            self._time(row["created_at"], label=f"ProductProject lifecycle row {row_version}")
        if durable_state is not previous:
            raise ProductProjectError(
                "durable ProductProject status does not match lifecycle audit tail"
            )
        return tuple(events)

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _fingerprint(cls, value: Any) -> str:
        return hashlib.sha256(cls._canonical(value).encode()).hexdigest()

    def _validate_spec_idempotency(
        self,
        conn: Any,
        project_id: str,
        *,
        spec_rows: list[Any],
    ) -> int:
        receipts = conn.execute(
            "SELECT operation_key,operation_kind,expected_row_version,"
            "previous_spec_version,result_spec_version,result_row_version,"
            "input_fingerprint,spec_sha256,change_reason,created_at "
            "FROM product_project_spec_idempotency WHERE project_id=? "
            "ORDER BY result_spec_version",
            (project_id,),
        ).fetchall()
        spec_by_version = {
            self._required_json_int(
                row["spec_version"],
                minimum=1,
                label="ProductProject spec_version",
            ): row
            for row in spec_rows
        }
        receipt_by_version: dict[int, Any] = {}
        for row in receipts:
            operation_key = self._required_text(
                row["operation_key"],
                label="ProductProject spec idempotency operation_key",
            )
            operation_kind = self._required_text(
                row["operation_kind"],
                label="ProductProject spec idempotency operation_kind",
            )
            if operation_kind != "product_project.spec_update.v2":
                raise ProductProjectError("invalid ProductProject spec idempotency operation kind")
            expected_row_version = self._required_json_int(
                row["expected_row_version"],
                minimum=0,
                label="ProductProject spec idempotency expected_row_version",
            )
            previous_spec_version = self._required_json_int(
                row["previous_spec_version"],
                minimum=1,
                label="ProductProject spec idempotency previous_spec_version",
            )
            result_spec_version = self._required_json_int(
                row["result_spec_version"],
                minimum=2,
                label="ProductProject spec idempotency result_spec_version",
            )
            result_row_version = self._required_json_int(
                row["result_row_version"],
                minimum=1,
                label="ProductProject spec idempotency result_row_version",
            )
            if result_spec_version in receipt_by_version:
                raise ProductProjectError("duplicate ProductProject spec idempotency result")
            if result_spec_version != previous_spec_version + 1:
                raise ProductProjectError("invalid ProductProject spec idempotency lineage")
            if result_row_version != expected_row_version + 1:
                raise ProductProjectError("invalid ProductProject spec idempotency row lineage")
            change_reason = self._required_text(
                row["change_reason"],
                label="ProductProject spec idempotency change_reason",
            )
            fingerprint = row["input_fingerprint"]
            spec_sha256 = row["spec_sha256"]
            if not self._valid_sha256(fingerprint) or not self._valid_sha256(spec_sha256):
                raise ProductProjectError("invalid ProductProject spec idempotency digest")
            spec_row = spec_by_version.get(result_spec_version)
            if spec_row is None:
                raise ProductProjectError(
                    "ProductProject spec idempotency record has no durable specification"
                )
            raw_spec = spec_row["spec_json"]
            if type(raw_spec) is not str:
                raise ProductProjectError("invalid ProductProject specification JSON")
            actual_spec_sha256 = hashlib.sha256(raw_spec.encode()).hexdigest()
            if spec_sha256 != actual_spec_sha256:
                raise ProductProjectError("ProductProject spec idempotency digest drift")
            raw = self._json_object(
                raw_spec,
                label=f"ProductProject spec version {result_spec_version}",
            )
            try:
                durable_spec = ProductProjectSpec.from_dict(raw)
            except (KeyError, TypeError, ValueError) as exc:
                raise ProductProjectError(
                    f"invalid ProductProject spec version {result_spec_version}"
                ) from exc
            effective_spec = replace(
                durable_spec,
                supersedes_spec_version=None,
                revision_reason="",
            )
            expected_fingerprint = self._fingerprint(
                {
                    "project_id": project_id,
                    "expected_row_version": expected_row_version,
                    "spec": effective_spec.to_dict(),
                    "change_reason": change_reason,
                }
            )
            if fingerprint != expected_fingerprint:
                raise ProductProjectError("ProductProject spec idempotency fingerprint drift")
            receipt_time = self._time(
                row["created_at"],
                label=f"ProductProject spec idempotency {result_spec_version}",
            )
            spec_time = self._time(
                spec_row["created_at"],
                label=f"ProductProject spec version {result_spec_version}",
            )
            if receipt_time != spec_time:
                raise ProductProjectError("ProductProject spec idempotency timestamp drift")
            receipt_by_version[result_spec_version] = row

        modern_audits: dict[int, tuple[dict[str, Any], datetime]] = {}
        for audit in conn.execute(
            "SELECT payload_json,created_at FROM audit_events "
            "WHERE event_type='product_project.spec_versioned' "
            "AND entity_type='product_project' AND entity_id=? ORDER BY event_id",
            (project_id,),
        ).fetchall():
            payload = self._json_object(
                audit["payload_json"],
                label="ProductProject spec revision audit",
            )
            if payload.get("operation_kind") != "product_project.spec_update.v2":
                continue
            version = self._required_json_int(
                payload.get("spec_version"),
                minimum=2,
                label="ProductProject spec revision audit spec_version",
            )
            if version in modern_audits:
                raise ProductProjectError("duplicate modern ProductProject spec revision audit")
            modern_audits[version] = (
                payload,
                self._time(
                    audit["created_at"],
                    label=f"ProductProject spec revision audit {version}",
                ),
            )

        if set(modern_audits) != set(receipt_by_version):
            raise ProductProjectError(
                "modern ProductProject spec revisions lack exact idempotency receipts"
            )
        for version, row in receipt_by_version.items():
            payload, audit_time = modern_audits[version]
            operation_key = self._required_text(
                row["operation_key"],
                label="ProductProject spec idempotency operation_key",
            )
            expected_payload = {
                "spec_version": version,
                "supersedes_spec_version": self._required_json_int(
                    row["previous_spec_version"],
                    minimum=1,
                    label="ProductProject spec idempotency previous_spec_version",
                ),
                "change_reason": self._required_text(
                    row["change_reason"],
                    label="ProductProject spec idempotency change_reason",
                ),
                "row_version": self._required_json_int(
                    row["result_row_version"],
                    minimum=1,
                    label="ProductProject spec idempotency result_row_version",
                ),
                "expected_row_version": self._required_json_int(
                    row["expected_row_version"],
                    minimum=0,
                    label="ProductProject spec idempotency expected_row_version",
                ),
                "operation_kind": "product_project.spec_update.v2",
                "operation_key_sha256": hashlib.sha256(operation_key.encode()).hexdigest(),
                "input_fingerprint": row["input_fingerprint"],
                "spec_sha256": row["spec_sha256"],
            }
            if payload != expected_payload:
                raise ProductProjectError("ProductProject spec idempotency audit drift")
            receipt_time = self._time(
                row["created_at"],
                label=f"ProductProject spec idempotency {version}",
            )
            if audit_time != receipt_time:
                raise ProductProjectError("ProductProject spec idempotency audit timestamp drift")
        return len(receipts)

    def _validate_idempotency(
        self,
        conn: Any,
        project_id: str,
        *,
        spec_rows: list[Any],
        decision_rows: list[Any],
        lifecycle: tuple[_LifecycleEvent, ...],
    ) -> int:
        create_rows = conn.execute(
            "SELECT operation_key,input_fingerprint,created_at "
            "FROM product_project_idempotency WHERE project_id=?",
            (project_id,),
        ).fetchall()
        if len(create_rows) != 1:
            raise ProductProjectError("ProductProject creation idempotency identity is missing")
        create_row = create_rows[0]
        if not self._valid_idempotency_row(create_row):
            raise ProductProjectError("invalid ProductProject creation idempotency record")
        project_row = conn.execute(
            "SELECT name FROM product_projects WHERE project_id=?",
            (project_id,),
        ).fetchone()
        if project_row is None:
            raise ProductProjectError("ProductProject creation idempotency project is missing")
        project_name = self._required_text(
            project_row["name"],
            label="ProductProject name",
        )
        if not spec_rows:
            raise ProductProjectError("ProductProject initial specification is missing")
        initial_version = self._required_json_int(
            spec_rows[0]["spec_version"],
            minimum=1,
            label="initial ProductProject spec_version",
        )
        if initial_version != 1:
            raise ProductProjectError("ProductProject initial specification version is invalid")
        initial_raw = self._json_object(
            spec_rows[0]["spec_json"],
            label="initial ProductProject specification",
        )
        try:
            initial_spec = ProductProjectSpec.from_dict(initial_raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductProjectError("invalid initial ProductProject specification") from exc
        expected_create_fingerprint = self._fingerprint(
            {
                "project_id": project_id,
                "name": project_name,
                "spec": initial_spec.to_dict(),
            }
        )
        if create_row["input_fingerprint"] != expected_create_fingerprint:
            raise ProductProjectError("ProductProject creation idempotency fingerprint drift")
        self._time(
            create_row["created_at"],
            label="ProductProject creation idempotency",
        )

        durable_decisions: dict[tuple[str, int], str] = {}
        for row in decision_rows:
            decision_id = self._required_text(
                row["decision_id"],
                label="product decision identity",
            )
            decision_version = self._required_json_int(
                row["decision_version"],
                minimum=1,
                label="product decision version",
            )
            try:
                state = ProductDecisionState(row["state"])
            except (TypeError, ValueError) as exc:
                raise ProductProjectError("invalid durable product decision state") from exc
            expected_fingerprint = self._fingerprint(
                {
                    "project_id": project_id,
                    "decision_id": decision_id,
                    "option_id": self._required_text(
                        row["option_id"],
                        label="product decision option_id",
                    ),
                    "state": state.value,
                    "rationale": self._required_text(
                        row["rationale"],
                        label="product decision rationale",
                    ),
                    "decided_by_ref": self._required_text(
                        row["decided_by_ref"],
                        label="product decision decided_by_ref",
                    ),
                }
            )
            durable_decisions[(decision_id, decision_version)] = expected_fingerprint

        lifecycle_by_version = {event.row_version: event for event in lifecycle}
        rows = conn.execute(
            "SELECT operation_key,operation_kind,entity_id,entity_version,"
            "input_fingerprint,created_at "
            "FROM product_project_mutation_idempotency "
            "WHERE project_id=? ORDER BY operation_key",
            (project_id,),
        ).fetchall()
        seen_keys: set[str] = set()
        seen_decisions: set[tuple[str, int]] = set()
        seen_lifecycle: set[int] = set()
        for row in rows:
            operation_key = self._required_text(
                row["operation_key"],
                label="ProductProject mutation operation_key",
            )
            operation_kind = self._required_text(
                row["operation_kind"],
                label="ProductProject mutation operation_kind",
            )
            entity_id = self._required_text(
                row["entity_id"],
                label="ProductProject mutation entity_id",
            )
            entity_version = self._required_json_int(
                row["entity_version"],
                minimum=1,
                label="ProductProject mutation entity_version",
            )
            if operation_key in seen_keys or not self._valid_idempotency_row(row):
                raise ProductProjectError("invalid ProductProject mutation idempotency record")
            seen_keys.add(operation_key)
            fingerprint = row["input_fingerprint"]
            self._time(
                row["created_at"],
                label=f"ProductProject mutation idempotency {operation_key}",
            )
            if operation_kind == "product_decision.record":
                key = (entity_id, entity_version)
                expected_fingerprint = durable_decisions.get(key)
                if expected_fingerprint is None or key in seen_decisions:
                    raise ProductProjectError(
                        "product decision idempotency record has no unique durable decision"
                    )
                if fingerprint != expected_fingerprint:
                    raise ProductProjectError("product decision idempotency fingerprint drift")
                seen_decisions.add(key)
            elif operation_kind == "product_project.status_transition":
                event = lifecycle_by_version.get(entity_version)
                if event is None or entity_id != project_id or entity_version in seen_lifecycle:
                    raise ProductProjectError(
                        "lifecycle idempotency record has no unique durable status audit"
                    )
                expected_fingerprint = self._fingerprint(
                    {
                        "project_id": project_id,
                        "new_state": event.new_state.value,
                        "reason": event.reason,
                        "changed_by_ref": event.changed_by_ref,
                    }
                )
                if fingerprint != expected_fingerprint:
                    raise ProductProjectError("lifecycle idempotency fingerprint drift")
                seen_lifecycle.add(entity_version)
            else:
                raise ProductProjectError(
                    "unsupported ProductProject mutation idempotency operation kind"
                )

        if seen_decisions != set(durable_decisions):
            raise ProductProjectError("product decision mutation lacks idempotency receipt")
        if seen_lifecycle != set(lifecycle_by_version):
            raise ProductProjectError("lifecycle mutation lacks idempotency receipt")
        spec_receipt_count = self._validate_spec_idempotency(
            conn,
            project_id,
            spec_rows=spec_rows,
        )
        return len(rows) + spec_receipt_count

    @staticmethod
    def _valid_sha256(value: Any) -> bool:
        return (
            type(value) is str
            and len(value) == 64
            and value == value.lower()
            and all(character in "0123456789abcdef" for character in value)
        )

    @classmethod
    def _valid_idempotency_row(cls, row: Any) -> bool:
        key = row["operation_key"]
        return (
            type(key) is str
            and bool(key.strip())
            and cls._valid_sha256(row["input_fingerprint"])
        )
