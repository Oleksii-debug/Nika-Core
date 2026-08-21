from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
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
        if not project_id.strip():
            raise ProductProjectError("project_id must not be empty")

        with self.store.connection() as conn:
            conn.execute("BEGIN")
            project = conn.execute(
                "SELECT current_spec_version,row_version,status FROM product_projects "
                "WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if project is None:
                raise KeyError(project_id)
            spec_version = int(project["current_spec_version"])
            row_version = int(project["row_version"])
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
                "SELECT decision_id,decision_version,option_id,state,"
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
                current_status=str(project["status"]),
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
        if expected_spec_version is not None and spec_version != expected_spec_version:
            raise StaleProjectVersionError(
                f"stale ProductProject spec: expected {expected_spec_version}, "
                f"current {spec_version}"
            )
        if expected_row_version is not None and row_version != expected_row_version:
            raise StaleProjectVersionError(
                f"stale ProductProject row: expected {expected_row_version}, "
                f"current {row_version}"
            )

    @staticmethod
    def _time(value: Any, *, label: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ProductProjectError(f"invalid timestamp for {label}") from exc
        if parsed.utcoffset() is None:
            raise ProductProjectError(f"naive timestamp for {label}")
        return parsed

    @staticmethod
    def _json_object(value: Any, *, label: str) -> dict[str, Any]:
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProductProjectError(f"invalid JSON for {label}") from exc
        if not isinstance(parsed, dict):
            raise ProductProjectError(f"invalid JSON object for {label}")
        return parsed

    def _package_times(self, rows: list[Any]) -> dict[str, datetime]:
        result: dict[str, datetime] = {}
        for row in rows:
            package_id = str(row["package_id"])
            if not package_id.strip() or package_id in result:
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
            decision_id = str(row["decision_id"])
            if not decision_id.strip():
                raise ProductProjectError("product decision identity must not be empty")
            try:
                state = ProductDecisionState(row["state"])
                evidence = tuple(json.loads(row["evidence_package_ids_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ProductProjectError(
                    f"invalid historical product decision: {decision_id}"
                ) from exc
            grouped[decision_id].append(
                _DecisionVersion(
                    decision_id=decision_id,
                    decision_version=int(row["decision_version"]),
                    state=state,
                    evidence_package_ids=evidence,
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
            version = int(row["spec_version"])
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
            by_type[str(row["event_type"])].append(row)

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
            decision_rows=decision_rows,
            lifecycle=lifecycle,
        )
        return lifecycle, idempotency_count

    def _validate_creation_audit(self, rows: list[Any]) -> None:
        if len(rows) != 1:
            raise ProductProjectError("ProductProject requires exactly one creation audit event")
        payload = self._json_object(rows[0]["payload_json"], label="ProductProject creation audit")
        if int(payload.get("spec_version", 0)) != 1:
            raise ProductProjectError("invalid ProductProject creation audit spec version")
        self._time(rows[0]["created_at"], label="ProductProject creation audit")

    def _validate_research_audits(self, audit_rows: list[Any], research_rows: list[Any]) -> None:
        package_ids = {str(row["package_id"]) for row in research_rows}
        audited: set[str] = set()
        for row in audit_rows:
            payload = self._json_object(row["payload_json"], label="research handoff audit")
            package_id = str(payload.get("package_id", ""))
            if not package_id.strip() or package_id in audited:
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
                version = int(payload["spec_version"])
                parent = int(payload["supersedes_spec_version"])
                reason = str(payload["change_reason"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ProductProjectError("invalid ProductProject spec revision audit") from exc
            if version in audited or parent != version - 1 or not reason.strip():
                raise ProductProjectError("invalid ProductProject spec revision audit")
            audited.add(version)
            self._time(row["created_at"], label=f"spec revision audit {version}")
        if audited != expected_versions:
            raise ProductProjectError("spec revision audit history does not match durable specs")

    def _validate_decision_audits(self, audit_rows: list[Any], decision_rows: list[Any]) -> None:
        durable = {
            (str(row["decision_id"]), int(row["decision_version"])): row
            for row in decision_rows
        }
        audited: set[tuple[str, int]] = set()
        for row in audit_rows:
            payload = self._json_object(row["payload_json"], label="product decision audit")
            try:
                key = (str(payload["decision_id"]), int(payload["decision_version"]))
                state = ProductDecisionState(payload["state"])
                evidence = tuple(payload["evidence_package_ids"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ProductProjectError("invalid product decision audit") from exc
            durable_row = durable.get(key)
            if durable_row is None or key in audited:
                raise ProductProjectError("product decision audit has no unique durable decision")
            if state.value != str(durable_row["state"]):
                raise ProductProjectError("product decision audit state drift")
            try:
                durable_evidence = tuple(json.loads(durable_row["evidence_package_ids_json"]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise ProductProjectError("invalid durable product decision evidence") from exc
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
                row_version = int(payload["row_version"])
                old_state = ProductProjectState(payload["previous_state"])
                new_state = ProductProjectState(payload["new_state"])
                reason = str(payload["reason"])
                actor = str(payload["changed_by_ref"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ProductProjectError("invalid ProductProject lifecycle audit") from exc
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

    def _validate_idempotency(
        self,
        conn: Any,
        project_id: str,
        *,
        decision_rows: list[Any],
        lifecycle: tuple[_LifecycleEvent, ...],
    ) -> int:
        create_rows = conn.execute(
            "SELECT operation_key,input_fingerprint FROM product_project_idempotency "
            "WHERE project_id=?",
            (project_id,),
        ).fetchall()
        if len(create_rows) != 1:
            raise ProductProjectError("ProductProject creation idempotency identity is missing")
        if not self._valid_idempotency_row(create_rows[0]):
            raise ProductProjectError("invalid ProductProject creation idempotency record")

        rows = conn.execute(
            "SELECT operation_key,operation_kind,entity_id,entity_version,input_fingerprint "
            "FROM product_project_mutation_idempotency WHERE project_id=? ORDER BY operation_key",
            (project_id,),
        ).fetchall()
        durable_decisions = {
            (str(row["decision_id"]), int(row["decision_version"])) for row in decision_rows
        }
        lifecycle_versions = {event.row_version for event in lifecycle}
        seen_keys: set[str] = set()
        for row in rows:
            operation_key = str(row["operation_key"])
            operation_kind = str(row["operation_kind"])
            entity_id = str(row["entity_id"])
            entity_version = int(row["entity_version"])
            if operation_key in seen_keys or not self._valid_idempotency_row(row):
                raise ProductProjectError("invalid ProductProject mutation idempotency record")
            seen_keys.add(operation_key)
            if operation_kind == "product_decision.record":
                if (entity_id, entity_version) not in durable_decisions:
                    raise ProductProjectError(
                        "product decision idempotency record has no durable decision"
                    )
            elif operation_kind == "product_project.status_transition":
                if entity_id != project_id or entity_version not in lifecycle_versions:
                    raise ProductProjectError(
                        "lifecycle idempotency record has no durable status audit"
                    )
            elif not operation_kind.strip() or not entity_id.strip() or entity_version < 1:
                raise ProductProjectError("invalid ProductProject mutation idempotency identity")
        return len(rows)

    @staticmethod
    def _valid_idempotency_row(row: Any) -> bool:
        key = str(row["operation_key"])
        fingerprint = str(row["input_fingerprint"])
        return (
            bool(key.strip())
            and len(fingerprint) == 64
            and all(character in "0123456789abcdef" for character in fingerprint.lower())
        )
