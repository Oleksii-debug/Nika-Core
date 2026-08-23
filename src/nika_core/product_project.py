from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

_SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|oauth)",
    re.IGNORECASE,
)
_TOKEN_VALUE = re.compile(
    r"(?:"
    r"gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")"
)


class ProductProjectError(ValueError):
    pass


class StaleProjectVersionError(ProductProjectError):
    pass


class ProductDecisionState(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class ProductRequirementKind(StrEnum):
    FUNCTIONAL = "functional"
    NON_FUNCTIONAL = "non_functional"
    SECURITY = "security"
    PRIVACY = "privacy"
    ACCESSIBILITY = "accessibility"
    PLATFORM = "platform"
    RELEASE = "release"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _exact_int(value: object, *, label: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ProductProjectError(
            f"{label} must be an exact integer greater than or equal to {minimum}"
        )
    return value


def _reject_secret_material(value: Any, path: str = "project") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key)):
                raise ProductProjectError(
                    f"raw credential material is forbidden at {path}.{key}; "
                    "store an opaque credential_ref"
                )
            _reject_secret_material(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secret_material(child, f"{path}[{index}]")
    elif isinstance(value, str) and _TOKEN_VALUE.search(value):
        raise ProductProjectError(
            f"token-shaped raw credential material is forbidden at {path}; "
            "store an opaque credential_ref"
        )


def _require_unique_nonempty(values: tuple[str, ...], *, label: str) -> None:
    if any(not value.strip() for value in values):
        raise ProductProjectError(f"{label} must not contain empty identifiers")
    if len(set(values)) != len(values):
        raise ProductProjectError(f"duplicate {label}")


def _validate_dag(nodes: dict[str, tuple[str, ...]], *, label: str) -> None:
    for node_id, dependencies in nodes.items():
        unknown = set(dependencies) - nodes.keys()
        if unknown:
            raise ProductProjectError(
                f"{label} {node_id} references unknown dependencies: {sorted(unknown)}"
            )
        if node_id in dependencies:
            raise ProductProjectError(f"{label} {node_id} cannot depend on itself")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            raise ProductProjectError(f"{label} dependency cycle detected at {node_id}")
        visiting.add(node_id)
        for dependency in nodes[node_id]:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in nodes:
        visit(node_id)


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    provenance_ref: str
    claim: str = ""

    def __post_init__(self) -> None:
        if not self.evidence_id.strip() or not self.provenance_ref.strip():
            raise ProductProjectError("research evidence requires evidence_id and provenance_ref")


@dataclass(frozen=True, slots=True)
class ResearchEvidencePackage:
    package_id: str
    evidence: tuple[EvidenceRef, ...]
    research_artifact_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.package_id.strip() or not self.evidence:
            raise ProductProjectError("research evidence package requires an id and evidence")
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        _require_unique_nonempty(evidence_ids, label="research evidence id")


@dataclass(frozen=True, slots=True)
class ProductOption:
    option_id: str
    title: str
    summary: str
    evidence_package_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.option_id.strip() or not self.title.strip() or not self.evidence_package_ids:
            raise ProductProjectError("product option requires identity, title and evidence")
        _require_unique_nonempty(
            self.evidence_package_ids,
            label="product option evidence package id",
        )


@dataclass(frozen=True, slots=True)
class ProductDecision:
    decision_id: str
    option_id: str
    state: ProductDecisionState
    rationale: str
    decided_by_ref: str


@dataclass(frozen=True, slots=True)
class ProductAcceptanceCriterion:
    criterion_id: str
    text: str
    verification_method: str = "deterministic_test"

    def __post_init__(self) -> None:
        if not self.criterion_id.strip() or not self.text.strip():
            raise ProductProjectError("acceptance criterion requires identity and text")
        if not self.verification_method.strip():
            raise ProductProjectError("acceptance criterion requires verification_method")


@dataclass(frozen=True, slots=True)
class ProductRequirement:
    requirement_id: str
    text: str
    acceptance: tuple[str, ...]
    evidence_package_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    kind: ProductRequirementKind = ProductRequirementKind.FUNCTIONAL
    acceptance_criteria: tuple[ProductAcceptanceCriterion, ...] = ()

    def __post_init__(self) -> None:
        if not self.requirement_id.strip() or not self.text.strip() or not self.acceptance:
            raise ProductProjectError(
                "product requirement requires identity, text and acceptance criteria"
            )
        if not isinstance(self.kind, ProductRequirementKind):
            raise ProductProjectError("product requirement kind must be ProductRequirementKind")
        _require_unique_nonempty(self.evidence_package_ids, label="requirement evidence package id")
        _require_unique_nonempty(self.decision_ids, label="requirement decision id")
        criterion_ids = tuple(item.criterion_id for item in self.acceptance_criteria)
        _require_unique_nonempty(criterion_ids, label="acceptance criterion id")


@dataclass(frozen=True, slots=True)
class ProductMilestone:
    milestone_id: str
    title: str
    depends_on_ids: tuple[str, ...] = ()
    acceptance_criterion_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.milestone_id.strip() or not self.title.strip():
            raise ProductProjectError("product milestone requires identity and title")
        _require_unique_nonempty(self.depends_on_ids, label="milestone dependency id")
        _require_unique_nonempty(
            self.acceptance_criterion_ids,
            label="milestone acceptance criterion id",
        )


@dataclass(frozen=True, slots=True)
class ProductBlocker:
    blocker_id: str
    summary: str
    blocking_milestone_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.blocker_id.strip() or not self.summary.strip():
            raise ProductProjectError("product blocker requires identity and summary")
        _require_unique_nonempty(
            self.blocking_milestone_ids,
            label="blocker milestone id",
        )
        _require_unique_nonempty(self.evidence_refs, label="blocker evidence ref")


@dataclass(frozen=True, slots=True)
class ProductArchitectureDecision:
    architecture_decision_id: str
    title: str
    rationale: str
    status: str = "accepted"
    evidence_package_ids: tuple[str, ...] = ()
    supersedes_decision_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.architecture_decision_id.strip()
            or not self.title.strip()
            or not self.rationale.strip()
            or not self.status.strip()
        ):
            raise ProductProjectError(
                "architecture decision requires identity, title, rationale and status"
            )
        if self.supersedes_decision_id == self.architecture_decision_id:
            raise ProductProjectError("architecture decision cannot supersede itself")
        _require_unique_nonempty(
            self.evidence_package_ids,
            label="architecture decision evidence package id",
        )


@dataclass(frozen=True, slots=True)
class ProductSpecRevision:
    spec_version: int
    supersedes_spec_version: int | None
    change_reason: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ProductProjectSpec:
    goal: str
    desired_outcome: str
    hypothesis: str = ""
    requirements: tuple[ProductRequirement, ...] = ()
    architecture_decision_refs: tuple[str, ...] = ()
    repository_refs: tuple[str, ...] = ()
    team_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    build_refs: tuple[str, ...] = ()
    release_refs: tuple[str, ...] = ()
    deployment_refs: tuple[str, ...] = ()
    incident_refs: tuple[str, ...] = ()
    credential_refs: tuple[str, ...] = ()
    budget: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    compliance: dict[str, Any] = field(default_factory=dict)
    milestones: tuple[ProductMilestone, ...] = ()
    blockers: tuple[ProductBlocker, ...] = ()
    architecture_decisions: tuple[ProductArchitectureDecision, ...] = ()
    supersedes_spec_version: int | None = None
    revision_reason: str = ""

    def __post_init__(self) -> None:
        if not self.goal.strip() or not self.desired_outcome.strip():
            raise ProductProjectError("goal and desired_outcome are required")
        requirement_ids = tuple(item.requirement_id for item in self.requirements)
        _require_unique_nonempty(requirement_ids, label="product requirement id")
        criterion_ids = tuple(
            criterion.criterion_id
            for requirement in self.requirements
            for criterion in requirement.acceptance_criteria
        )
        _require_unique_nonempty(criterion_ids, label="acceptance criterion id")
        milestone_ids = tuple(item.milestone_id for item in self.milestones)
        _require_unique_nonempty(milestone_ids, label="product milestone id")
        milestone_map = {item.milestone_id: item.depends_on_ids for item in self.milestones}
        _validate_dag(milestone_map, label="product milestone")
        known_criteria = set(criterion_ids)
        for milestone in self.milestones:
            unknown = set(milestone.acceptance_criterion_ids) - known_criteria
            if unknown:
                raise ProductProjectError(
                    f"product milestone {milestone.milestone_id} references unknown "
                    f"acceptance criteria: {sorted(unknown)}"
                )
        blocker_ids = tuple(item.blocker_id for item in self.blockers)
        _require_unique_nonempty(blocker_ids, label="product blocker id")
        known_milestones = set(milestone_ids)
        for blocker in self.blockers:
            unknown = set(blocker.blocking_milestone_ids) - known_milestones
            if unknown:
                raise ProductProjectError(
                    f"product blocker {blocker.blocker_id} references unknown milestones: "
                    f"{sorted(unknown)}"
                )
        architecture_ids = tuple(
            item.architecture_decision_id for item in self.architecture_decisions
        )
        _require_unique_nonempty(architecture_ids, label="architecture decision id")
        architecture_map = {
            item.architecture_decision_id: (
                () if item.supersedes_decision_id is None else (item.supersedes_decision_id,)
            )
            for item in self.architecture_decisions
        }
        _validate_dag(architecture_map, label="architecture decision")
        for ref_name, refs in (
            ("architecture decision ref", self.architecture_decision_refs),
            ("repository ref", self.repository_refs),
            ("team ref", self.team_refs),
            ("artifact ref", self.artifact_refs),
            ("build ref", self.build_refs),
            ("release ref", self.release_refs),
            ("deployment ref", self.deployment_refs),
            ("incident ref", self.incident_refs),
            ("credential ref", self.credential_refs),
        ):
            _require_unique_nonempty(refs, label=ref_name)
        if self.supersedes_spec_version is not None:
            _exact_int(
                self.supersedes_spec_version,
                label="supersedes_spec_version",
                minimum=1,
            )
        _reject_secret_material(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "desired_outcome": self.desired_outcome,
            "hypothesis": self.hypothesis,
            "requirements": [
                {
                    "requirement_id": item.requirement_id,
                    "text": item.text,
                    "acceptance": list(item.acceptance),
                    "evidence_package_ids": list(item.evidence_package_ids),
                    "decision_ids": list(item.decision_ids),
                    "kind": item.kind.value,
                    "acceptance_criteria": [
                        {
                            "criterion_id": criterion.criterion_id,
                            "text": criterion.text,
                            "verification_method": criterion.verification_method,
                        }
                        for criterion in item.acceptance_criteria
                    ],
                }
                for item in self.requirements
            ],
            "architecture_decision_refs": list(self.architecture_decision_refs),
            "repository_refs": list(self.repository_refs),
            "team_refs": list(self.team_refs),
            "artifact_refs": list(self.artifact_refs),
            "build_refs": list(self.build_refs),
            "release_refs": list(self.release_refs),
            "deployment_refs": list(self.deployment_refs),
            "incident_refs": list(self.incident_refs),
            "credential_refs": list(self.credential_refs),
            "budget": self.budget,
            "risk": self.risk,
            "compliance": self.compliance,
            "milestones": [
                {
                    "milestone_id": item.milestone_id,
                    "title": item.title,
                    "depends_on_ids": list(item.depends_on_ids),
                    "acceptance_criterion_ids": list(item.acceptance_criterion_ids),
                }
                for item in self.milestones
            ],
            "blockers": [
                {
                    "blocker_id": item.blocker_id,
                    "summary": item.summary,
                    "blocking_milestone_ids": list(item.blocking_milestone_ids),
                    "evidence_refs": list(item.evidence_refs),
                }
                for item in self.blockers
            ],
            "architecture_decisions": [
                {
                    "architecture_decision_id": item.architecture_decision_id,
                    "title": item.title,
                    "rationale": item.rationale,
                    "status": item.status,
                    "evidence_package_ids": list(item.evidence_package_ids),
                    "supersedes_decision_id": item.supersedes_decision_id,
                }
                for item in self.architecture_decisions
            ],
            "supersedes_spec_version": self.supersedes_spec_version,
            "revision_reason": self.revision_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProductProjectSpec:
        raw = dict(data)
        requirements = tuple(
            ProductRequirement(
                requirement_id=item["requirement_id"],
                text=item["text"],
                acceptance=tuple(item["acceptance"]),
                evidence_package_ids=tuple(item.get("evidence_package_ids", ())),
                decision_ids=tuple(item.get("decision_ids", ())),
                kind=ProductRequirementKind(item.get("kind", ProductRequirementKind.FUNCTIONAL)),
                acceptance_criteria=tuple(
                    ProductAcceptanceCriterion(
                        criterion_id=criterion["criterion_id"],
                        text=criterion["text"],
                        verification_method=criterion.get(
                            "verification_method",
                            "deterministic_test",
                        ),
                    )
                    for criterion in item.get("acceptance_criteria", ())
                ),
            )
            for item in raw.pop("requirements", [])
        )
        milestones = tuple(
            ProductMilestone(
                milestone_id=item["milestone_id"],
                title=item["title"],
                depends_on_ids=tuple(item.get("depends_on_ids", ())),
                acceptance_criterion_ids=tuple(item.get("acceptance_criterion_ids", ())),
            )
            for item in raw.pop("milestones", [])
        )
        blockers = tuple(
            ProductBlocker(
                blocker_id=item["blocker_id"],
                summary=item["summary"],
                blocking_milestone_ids=tuple(item.get("blocking_milestone_ids", ())),
                evidence_refs=tuple(item.get("evidence_refs", ())),
            )
            for item in raw.pop("blockers", [])
        )
        architecture_decisions = tuple(
            ProductArchitectureDecision(
                architecture_decision_id=item["architecture_decision_id"],
                title=item["title"],
                rationale=item["rationale"],
                status=item.get("status", "accepted"),
                evidence_package_ids=tuple(item.get("evidence_package_ids", ())),
                supersedes_decision_id=item.get("supersedes_decision_id"),
            )
            for item in raw.pop("architecture_decisions", [])
        )
        return cls(
            requirements=requirements,
            milestones=milestones,
            blockers=blockers,
            architecture_decisions=architecture_decisions,
            **{
                key: (tuple(value) if key.endswith("_refs") else value)
                for key, value in raw.items()
            },
        )


@dataclass(frozen=True, slots=True)
class ProductProject:
    project_id: str
    name: str
    spec_version: int
    row_version: int
    status: str
    spec: ProductProjectSpec
    created_at: str
    updated_at: str


class ProductProjectRepository:
    """Durable PF0/PF1 repository over Nika's canonical SQLiteStore."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def create(
        self,
        *,
        project_id: str,
        name: str,
        spec: ProductProjectSpec,
        idempotency_key: str,
    ) -> ProductProject:
        if not project_id.strip() or not name.strip() or not idempotency_key.strip():
            raise ProductProjectError("project_id, name and idempotency_key are required")
        stored_spec = replace(
            spec,
            supersedes_spec_version=None,
            revision_reason="initial specification",
        )
        fingerprint = hashlib.sha256(
            _canonical(
                {"project_id": project_id, "name": name, "spec": stored_spec.to_dict()}
            ).encode()
        ).hexdigest()
        now = _now()
        with self.store.connection() as conn:
            existing = conn.execute(
                "SELECT project_id, input_fingerprint FROM product_project_idempotency "
                "WHERE operation_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["input_fingerprint"] != fingerprint:
                    raise ProductProjectError(
                        "idempotency key was already used with different input"
                    )
                return self._get_conn(conn, existing["project_id"])
            if conn.execute(
                "SELECT 1 FROM product_projects WHERE project_id = ?", (project_id,)
            ).fetchone():
                raise ProductProjectError(f"product project already exists: {project_id}")
            conn.execute(
                "INSERT INTO product_projects(project_id,name,current_spec_version,row_version,"
                "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (project_id, name, 1, 0, "active", now, now),
            )
            conn.execute(
                "INSERT INTO product_project_specs(project_id,spec_version,spec_json,created_at) "
                "VALUES (?,?,?,?)",
                (project_id, 1, _canonical(stored_spec.to_dict()), now),
            )
            conn.execute(
                "INSERT INTO product_project_idempotency(operation_key,project_id,"
                "input_fingerprint,created_at) VALUES (?,?,?,?)",
                (idempotency_key, project_id, fingerprint, now),
            )
            self._audit(conn, project_id, "product_project.created", {"spec_version": 1})
            return self._get_conn(conn, project_id)

    def get(self, project_id: str) -> ProductProject:
        with self.store.connection() as conn:
            return self._get_conn(conn, project_id)

    def update_spec(
        self,
        project_id: str,
        spec: ProductProjectSpec,
        *,
        expected_row_version: int,
        change_reason: str = "specification revision",
    ) -> ProductProject:
        expected_row_version = _exact_int(
            expected_row_version,
            label="expected_row_version",
            minimum=0,
        )
        if not change_reason.strip():
            raise ProductProjectError("change_reason must not be empty")
        now = _now()
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT current_spec_version,row_version FROM product_projects "
                "WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise KeyError(project_id)
            durable_row_version = _exact_int(
                row["row_version"],
                label="durable ProductProject row_version",
                minimum=0,
            )
            if durable_row_version != expected_row_version:
                raise StaleProjectVersionError(
                    f"stale ProductProject write: expected {expected_row_version}, "
                    f"current {durable_row_version}"
                )
            previous_spec_version = _exact_int(
                row["current_spec_version"],
                label="durable ProductProject current_spec_version",
                minimum=1,
            )
            spec_version = previous_spec_version + 1
            stored_spec = replace(
                spec,
                supersedes_spec_version=previous_spec_version,
                revision_reason=change_reason,
            )
            cursor = conn.execute(
                "UPDATE product_projects SET current_spec_version=?, row_version=row_version+1, "
                "updated_at=? WHERE project_id=? AND row_version=?",
                (spec_version, now, project_id, expected_row_version),
            )
            if cursor.rowcount != 1:
                raise StaleProjectVersionError("concurrent ProductProject update")
            conn.execute(
                "INSERT INTO product_project_specs(project_id,spec_version,spec_json,created_at) "
                "VALUES (?,?,?,?)",
                (project_id, spec_version, _canonical(stored_spec.to_dict()), now),
            )
            self._audit(
                conn,
                project_id,
                "product_project.spec_versioned",
                {
                    "spec_version": spec_version,
                    "supersedes_spec_version": previous_spec_version,
                    "change_reason": change_reason,
                },
            )
            return self._get_conn(conn, project_id)

    def spec_history(self, project_id: str) -> tuple[ProductSpecRevision, ...]:
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT spec_version,spec_json,created_at FROM product_project_specs "
                "WHERE project_id=? ORDER BY spec_version",
                (project_id,),
            ).fetchall()
            if not rows and not conn.execute(
                "SELECT 1 FROM product_projects WHERE project_id=?",
                (project_id,),
            ).fetchone():
                raise KeyError(project_id)
            revisions: list[ProductSpecRevision] = []
            for row in rows:
                version = _exact_int(
                    row["spec_version"],
                    label="durable ProductProject spec_version",
                    minimum=1,
                )
                spec = ProductProjectSpec.from_dict(json.loads(row["spec_json"]))
                parent = spec.supersedes_spec_version
                reason = spec.revision_reason
                if version > 1 and parent is None:
                    parent = version - 1
                    reason = reason or "legacy sequential specification"
                revisions.append(
                    ProductSpecRevision(
                        spec_version=version,
                        supersedes_spec_version=parent,
                        change_reason=reason or "initial specification",
                        created_at=row["created_at"],
                    )
                )
            return tuple(revisions)

    def record_research_handoff(
        self,
        project_id: str,
        package: ResearchEvidencePackage,
        options: tuple[ProductOption, ...],
    ) -> None:
        option_ids = {option.option_id for option in options}
        if len(option_ids) != len(options):
            raise ProductProjectError("duplicate product option id")
        for option in options:
            if package.package_id not in option.evidence_package_ids:
                raise ProductProjectError(
                    "product option must reference supplied evidence package"
                )
        payload = {
            "package_id": package.package_id,
            "research_artifact_ref": package.research_artifact_ref,
            "evidence": [
                {
                    "evidence_id": e.evidence_id,
                    "provenance_ref": e.provenance_ref,
                    "claim": e.claim,
                }
                for e in package.evidence
            ],
            "options": [
                {
                    "option_id": o.option_id,
                    "title": o.title,
                    "summary": o.summary,
                    "evidence_package_ids": list(o.evidence_package_ids),
                }
                for o in options
            ],
        }
        with self.store.connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM product_projects WHERE project_id=?", (project_id,)
            ).fetchone():
                raise KeyError(project_id)
            for option in options:
                for package_id in option.evidence_package_ids:
                    if package_id == package.package_id:
                        continue
                    if not conn.execute(
                        "SELECT 1 FROM product_research_handoffs "
                        "WHERE project_id=? AND package_id=?",
                        (project_id, package_id),
                    ).fetchone():
                        raise ProductProjectError(
                            f"product option references unknown evidence package: {package_id}"
                        )
            conn.execute(
                "INSERT INTO product_research_handoffs(project_id,package_id,payload_json,"
                "created_at) VALUES (?,?,?,?)",
                (project_id, package.package_id, _canonical(payload), _now()),
            )
            self._audit(
                conn,
                project_id,
                "product_project.research_handoff",
                {"package_id": package.package_id, "option_ids": sorted(option_ids)},
            )

    def _get_conn(self, conn: Any, project_id: str) -> ProductProject:
        row = conn.execute(
            "SELECT p.*, s.spec_json FROM product_projects p JOIN product_project_specs s "
            "ON s.project_id=p.project_id AND s.spec_version=p.current_spec_version "
            "WHERE p.project_id=?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise KeyError(project_id)
        spec = ProductProjectSpec.from_dict(json.loads(row["spec_json"]))
        return ProductProject(
            row["project_id"],
            row["name"],
            _exact_int(
                row["current_spec_version"],
                label="durable ProductProject current_spec_version",
                minimum=1,
            ),
            _exact_int(
                row["row_version"],
                label="durable ProductProject row_version",
                minimum=0,
            ),
            row["status"],
            spec,
            row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _audit(conn: Any, project_id: str, event_type: str, payload: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_events(event_type,entity_type,entity_id,payload_json,created_at) "
            "VALUES (?,?,?,?,?)",
            (event_type, "product_project", project_id, _canonical(payload), _now()),
        )
