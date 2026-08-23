from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from nika_core.product_project import (
    ProductDecisionState,
    ProductProjectError,
    ProductProjectSpec,
    StaleProjectVersionError,
)


@dataclass(frozen=True, slots=True)
class ProductProjectIntegrityReport:
    project_id: str
    spec_version: int
    row_version: int
    spec_revision_count: int
    legacy_lineage_count: int
    research_package_count: int
    research_option_count: int
    decision_count: int
    approved_decision_ids: tuple[str, ...]
    requirement_count: int
    acceptance_criterion_count: int
    milestone_count: int
    blocker_count: int
    architecture_decision_count: int


@dataclass(frozen=True, slots=True)
class _DecisionView:
    decision_id: str
    option_id: str
    state: ProductDecisionState
    evidence_package_ids: tuple[str, ...]


class ProductProjectIntegrityService:
    """Fail-closed PF1 reconciliation over one durable ProductProject snapshot."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def validate(
        self,
        project_id: str,
        *,
        expected_spec_version: int | None = None,
        expected_row_version: int | None = None,
    ) -> ProductProjectIntegrityReport:
        if not project_id.strip():
            raise ProductProjectError("project_id must not be empty")
        with self.store.connection() as conn:
            conn.execute("BEGIN")
            project_row = conn.execute(
                "SELECT project_id,current_spec_version,row_version "
                "FROM product_projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if project_row is None:
                raise KeyError(project_id)
            try:
                spec_version = int(project_row["current_spec_version"])
                row_version = int(project_row["row_version"])
            except (TypeError, ValueError) as exc:
                raise ProductProjectError(
                    "invalid durable ProductProject version metadata"
                ) from exc
            if spec_version < 1 or row_version < 0:
                raise ProductProjectError("invalid durable ProductProject version metadata")
            spec_row = conn.execute(
                "SELECT spec_json FROM product_project_specs "
                "WHERE project_id=? AND spec_version=?",
                (project_id, spec_version),
            ).fetchone()
            if spec_row is None:
                raise ProductProjectError(
                    "current ProductProject specification is missing: "
                    f"project_id={project_id}, spec_version={spec_version}"
                )
            self._validate_expected_versions(
                spec_version,
                row_version,
                expected_spec_version=expected_spec_version,
                expected_row_version=expected_row_version,
            )
            spec = self._parse_spec(spec_row["spec_json"], label="current specification")
            revision_count, legacy_lineage_count = self._validate_spec_lineage(
                conn,
                project_id,
                current_spec_version=spec_version,
            )
            package_ids, options = self._research_index(conn, project_id)
            decisions = self._decision_index(
                conn,
                project_id,
                package_ids=package_ids,
                options=options,
            )
            self._validate_spec_references(
                spec,
                package_ids=package_ids,
                decisions=decisions,
            )
            approved = tuple(
                sorted(
                    decision_id
                    for decision_id, decision in decisions.items()
                    if decision.state is ProductDecisionState.APPROVED
                )
            )
            return ProductProjectIntegrityReport(
                project_id=project_id,
                spec_version=spec_version,
                row_version=row_version,
                spec_revision_count=revision_count,
                legacy_lineage_count=legacy_lineage_count,
                research_package_count=len(package_ids),
                research_option_count=len(options),
                decision_count=len(decisions),
                approved_decision_ids=approved,
                requirement_count=len(spec.requirements),
                acceptance_criterion_count=sum(
                    len(requirement.acceptance_criteria)
                    for requirement in spec.requirements
                ),
                milestone_count=len(spec.milestones),
                blocker_count=len(spec.blockers),
                architecture_decision_count=len(spec.architecture_decisions),
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
    def _parse_spec(raw: str, *, label: str) -> ProductProjectSpec:
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProductProjectError(f"invalid {label} JSON") from exc
        if not isinstance(data, dict):
            raise ProductProjectError(f"invalid {label}: expected object")
        try:
            return ProductProjectSpec.from_dict(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductProjectError(f"invalid {label}: {type(exc).__name__}") from exc

    def _validate_spec_lineage(
        self,
        conn: Any,
        project_id: str,
        *,
        current_spec_version: int,
    ) -> tuple[int, int]:
        rows = conn.execute(
            "SELECT spec_version,spec_json FROM product_project_specs "
            "WHERE project_id=? ORDER BY spec_version",
            (project_id,),
        ).fetchall()
        versions = tuple(int(row["spec_version"]) for row in rows)
        expected = tuple(range(1, current_spec_version + 1))
        if versions != expected:
            raise ProductProjectError(
                "ProductProject spec lineage is not contiguous through current version"
            )
        legacy_lineage_count = 0
        for row in rows:
            version = int(row["spec_version"])
            spec = self._parse_spec(
                row["spec_json"],
                label=f"specification version {version}",
            )
            parent = spec.supersedes_spec_version
            if version == 1:
                if parent is not None:
                    raise ProductProjectError("initial ProductProject specification has a parent")
                continue
            if parent is None:
                legacy_lineage_count += 1
                continue
            if parent != version - 1:
                raise ProductProjectError(
                    f"specification version {version} supersedes {parent}, "
                    f"expected {version - 1}"
                )
            if not spec.revision_reason.strip():
                raise ProductProjectError(
                    f"specification version {version} has no revision reason"
                )
        return len(rows), legacy_lineage_count

    @staticmethod
    def _research_index(
        conn: Any,
        project_id: str,
    ) -> tuple[set[str], dict[str, tuple[str, ...]]]:
        rows = conn.execute(
            "SELECT package_id,payload_json FROM product_research_handoffs "
            "WHERE project_id=? ORDER BY package_id",
            (project_id,),
        ).fetchall()
        package_ids = {str(row["package_id"]) for row in rows}
        if any(not package_id.strip() for package_id in package_ids):
            raise ProductProjectError("research package identity must not be empty")
        options: dict[str, tuple[str, ...]] = {}
        for row in rows:
            package_id = str(row["package_id"])
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ProductProjectError(
                    f"invalid research handoff JSON: {package_id}"
                ) from exc
            if not isinstance(payload, dict) or payload.get("package_id") != package_id:
                raise ProductProjectError(
                    f"research handoff package identity mismatch: {package_id}"
                )
            evidence = payload.get("evidence")
            raw_options = payload.get("options")
            if not isinstance(evidence, list) or not evidence:
                raise ProductProjectError(f"research package has no evidence: {package_id}")
            if not isinstance(raw_options, list):
                raise ProductProjectError(f"research package has invalid options: {package_id}")
            evidence_ids: set[str] = set()
            for item in evidence:
                if not isinstance(item, dict):
                    raise ProductProjectError(
                        f"research package has invalid evidence: {package_id}"
                    )
                evidence_id = str(item.get("evidence_id", ""))
                provenance_ref = str(item.get("provenance_ref", ""))
                if not evidence_id.strip() or not provenance_ref.strip():
                    raise ProductProjectError(
                        f"research package has incomplete evidence: {package_id}"
                    )
                if evidence_id in evidence_ids:
                    raise ProductProjectError(
                        f"research package has duplicate evidence id: {evidence_id}"
                    )
                evidence_ids.add(evidence_id)
            for item in raw_options:
                if not isinstance(item, dict):
                    raise ProductProjectError(
                        f"research package has invalid product option: {package_id}"
                    )
                option_id = str(item.get("option_id", ""))
                evidence_refs = tuple(item.get("evidence_package_ids", ()))
                if not option_id.strip() or not evidence_refs:
                    raise ProductProjectError(
                        f"research package has incomplete product option: {package_id}"
                    )
                if option_id in options:
                    raise ProductProjectError(
                        f"ambiguous product option identity across research handoffs: {option_id}"
                    )
                if any(
                    not isinstance(ref, str) or not ref.strip()
                    for ref in evidence_refs
                ):
                    raise ProductProjectError(
                        f"product option has invalid evidence reference: {option_id}"
                    )
                if len(set(evidence_refs)) != len(evidence_refs):
                    raise ProductProjectError(
                        f"product option has duplicate evidence reference: {option_id}"
                    )
                options[option_id] = evidence_refs
        for option_id, evidence_refs in options.items():
            missing = set(evidence_refs) - package_ids
            if missing:
                raise ProductProjectError(
                    f"product option {option_id} references missing research packages: "
                    f"{sorted(missing)}"
                )
        return package_ids, options

    def _decision_index(
        self,
        conn: Any,
        project_id: str,
        *,
        package_ids: set[str],
        options: dict[str, tuple[str, ...]],
    ) -> dict[str, _DecisionView]:
        rows = conn.execute(
            "SELECT decision_id,decision_version,option_id,state,rationale,decided_by_ref,"
            "evidence_package_ids_json FROM product_decisions WHERE project_id=? "
            "ORDER BY decision_id,decision_version",
            (project_id,),
        ).fetchall()
        grouped: dict[str, list[Any]] = defaultdict(list)
        for row in rows:
            grouped[str(row["decision_id"])].append(row)
        latest: dict[str, _DecisionView] = {}
        approved: list[str] = []
        for decision_id, history in grouped.items():
            if not decision_id.strip():
                raise ProductProjectError("product decision identity must not be empty")
            versions = tuple(int(row["decision_version"]) for row in history)
            if versions != tuple(range(1, len(history) + 1)):
                raise ProductProjectError(
                    f"product decision history is not contiguous: {decision_id}"
                )
            option_ids = {str(row["option_id"]) for row in history}
            if len(option_ids) != 1:
                raise ProductProjectError(
                    f"product decision changed option identity: {decision_id}"
                )
            option_id = next(iter(option_ids))
            if option_id not in options:
                raise ProductProjectError(
                    f"product decision references unknown or obsolete option: {option_id}"
                )
            states: list[ProductDecisionState] = []
            evidence_versions: list[tuple[str, ...]] = []
            for row in history:
                try:
                    state = ProductDecisionState(row["state"])
                except ValueError as exc:
                    raise ProductProjectError(
                        f"product decision has invalid state: {decision_id}"
                    ) from exc
                if not str(row["rationale"]).strip() or not str(row["decided_by_ref"]).strip():
                    raise ProductProjectError(
                        f"product decision has incomplete audit identity: {decision_id}"
                    )
                try:
                    evidence_refs = tuple(json.loads(row["evidence_package_ids_json"]))
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ProductProjectError(
                        f"product decision has invalid evidence JSON: {decision_id}"
                    ) from exc
                if any(
                    not isinstance(ref, str) or not ref.strip()
                    for ref in evidence_refs
                ):
                    raise ProductProjectError(
                        f"product decision has invalid evidence reference: {decision_id}"
                    )
                if len(set(evidence_refs)) != len(evidence_refs):
                    raise ProductProjectError(
                        f"product decision has duplicate evidence reference: {decision_id}"
                    )
                if set(evidence_refs) - package_ids:
                    raise ProductProjectError(
                        f"product decision references missing evidence: {decision_id}"
                    )
                states.append(state)
                evidence_versions.append(evidence_refs)
            if len(states) > 2:
                raise ProductProjectError(
                    f"product decision has impossible version history: {decision_id}"
                )
            if len(states) == 2 and (
                states[0] is not ProductDecisionState.PROPOSED
                or states[1] is ProductDecisionState.PROPOSED
            ):
                raise ProductProjectError(
                    f"product decision has invalid transition history: {decision_id}"
                )
            if len(states) == 1 and states[0] is ProductDecisionState.PROPOSED:
                pass
            expected_evidence = options[option_id]
            if any(evidence_refs != expected_evidence for evidence_refs in evidence_versions):
                raise ProductProjectError(
                    f"product decision evidence drifted from research option: {decision_id}"
                )
            view = _DecisionView(
                decision_id=decision_id,
                option_id=option_id,
                state=states[-1],
                evidence_package_ids=evidence_versions[-1],
            )
            latest[decision_id] = view
            if view.state is ProductDecisionState.APPROVED:
                approved.append(decision_id)
        if len(approved) > 1:
            raise ProductProjectError(
                f"multiple current approved product decisions: {sorted(approved)}"
            )
        return latest

    @staticmethod
    def _validate_spec_references(
        spec: ProductProjectSpec,
        *,
        package_ids: set[str],
        decisions: dict[str, _DecisionView],
    ) -> None:
        for requirement in spec.requirements:
            missing_packages = set(requirement.evidence_package_ids) - package_ids
            if missing_packages:
                raise ProductProjectError(
                    f"requirement {requirement.requirement_id} references missing research "
                    f"packages: {sorted(missing_packages)}"
                )
            for decision_id in requirement.decision_ids:
                decision = decisions.get(decision_id)
                if decision is None:
                    raise ProductProjectError(
                        f"requirement {requirement.requirement_id} references unknown "
                        f"product decision: {decision_id}"
                    )
                if decision.state is not ProductDecisionState.APPROVED:
                    raise ProductProjectError(
                        f"requirement {requirement.requirement_id} references non-approved "
                        f"product decision: {decision_id}"
                    )
        for decision in spec.architecture_decisions:
            missing_packages = set(decision.evidence_package_ids) - package_ids
            if missing_packages:
                raise ProductProjectError(
                    f"architecture decision {decision.architecture_decision_id} references "
                    f"missing research packages: {sorted(missing_packages)}"
                )
