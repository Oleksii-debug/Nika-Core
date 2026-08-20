from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import json
import re
from typing import Any


_SECRET_KEY = re.compile(r"(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|oauth)", re.I)


class ProductProjectError(ValueError):
    pass


class StaleProjectVersionError(ProductProjectError):
    pass


class ProductDecisionState(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reject_secret_material(value: Any, path: str = "project") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key)):
                raise ProductProjectError(f"raw credential material is forbidden at {path}.{key}; store an opaque credential_ref")
            _reject_secret_material(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secret_material(child, f"{path}[{index}]")


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


@dataclass(frozen=True, slots=True)
class ProductOption:
    option_id: str
    title: str
    summary: str
    evidence_package_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.option_id.strip() or not self.title.strip() or not self.evidence_package_ids:
            raise ProductProjectError("product option requires identity, title and evidence")


@dataclass(frozen=True, slots=True)
class ProductDecision:
    decision_id: str
    option_id: str
    state: ProductDecisionState
    rationale: str
    decided_by_ref: str


@dataclass(frozen=True, slots=True)
class ProductRequirement:
    requirement_id: str
    text: str
    acceptance: tuple[str, ...]
    evidence_package_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.requirement_id.strip() or not self.text.strip() or not self.acceptance:
            raise ProductProjectError("product requirement requires identity, text and acceptance criteria")


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

    def __post_init__(self) -> None:
        if not self.goal.strip() or not self.desired_outcome.strip():
            raise ProductProjectError("goal and desired_outcome are required")
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
        }


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

    def create(self, *, project_id: str, name: str, spec: ProductProjectSpec, idempotency_key: str) -> ProductProject:
        if not project_id.strip() or not name.strip() or not idempotency_key.strip():
            raise ProductProjectError("project_id, name and idempotency_key are required")
        fingerprint = hashlib.sha256(_canonical({"project_id": project_id, "name": name, "spec": spec.to_dict()}).encode()).hexdigest()
        now = _now()
        with self.store.connection() as conn:
            existing = conn.execute("SELECT project_id, input_fingerprint FROM product_project_idempotency WHERE operation_key = ?", (idempotency_key,)).fetchone()
            if existing is not None:
                if existing["input_fingerprint"] != fingerprint:
                    raise ProductProjectError("idempotency key was already used with different input")
                return self._get_conn(conn, existing["project_id"])
            if conn.execute("SELECT 1 FROM product_projects WHERE project_id = ?", (project_id,)).fetchone():
                raise ProductProjectError(f"product project already exists: {project_id}")
            conn.execute("INSERT INTO product_projects(project_id,name,current_spec_version,row_version,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (project_id, name, 1, 0, "active", now, now))
            conn.execute("INSERT INTO product_project_specs(project_id,spec_version,spec_json,created_at) VALUES (?,?,?,?)", (project_id, 1, _canonical(spec.to_dict()), now))
            conn.execute("INSERT INTO product_project_idempotency(operation_key,project_id,input_fingerprint,created_at) VALUES (?,?,?,?)", (idempotency_key, project_id, fingerprint, now))
            self._audit(conn, project_id, "product_project.created", {"spec_version": 1})
            return self._get_conn(conn, project_id)

    def get(self, project_id: str) -> ProductProject:
        with self.store.connection() as conn:
            return self._get_conn(conn, project_id)

    def update_spec(self, project_id: str, spec: ProductProjectSpec, *, expected_row_version: int) -> ProductProject:
        now = _now()
        with self.store.connection() as conn:
            row = conn.execute("SELECT current_spec_version,row_version FROM product_projects WHERE project_id = ?", (project_id,)).fetchone()
            if row is None:
                raise KeyError(project_id)
            if int(row["row_version"]) != expected_row_version:
                raise StaleProjectVersionError(f"stale ProductProject write: expected {expected_row_version}, current {row['row_version']}")
            spec_version = int(row["current_spec_version"]) + 1
            cursor = conn.execute("UPDATE product_projects SET current_spec_version=?, row_version=row_version+1, updated_at=? WHERE project_id=? AND row_version=?", (spec_version, now, project_id, expected_row_version))
            if cursor.rowcount != 1:
                raise StaleProjectVersionError("concurrent ProductProject update")
            conn.execute("INSERT INTO product_project_specs(project_id,spec_version,spec_json,created_at) VALUES (?,?,?,?)", (project_id, spec_version, _canonical(spec.to_dict()), now))
            self._audit(conn, project_id, "product_project.spec_versioned", {"spec_version": spec_version})
            return self._get_conn(conn, project_id)

    def record_research_handoff(self, project_id: str, package: ResearchEvidencePackage, options: tuple[ProductOption, ...]) -> None:
        option_ids = {option.option_id for option in options}
        if len(option_ids) != len(options):
            raise ProductProjectError("duplicate product option id")
        for option in options:
            if package.package_id not in option.evidence_package_ids:
                raise ProductProjectError("product option must reference supplied evidence package")
        payload = {"package_id": package.package_id, "research_artifact_ref": package.research_artifact_ref, "evidence": [{"evidence_id": e.evidence_id, "provenance_ref": e.provenance_ref, "claim": e.claim} for e in package.evidence], "options": [{"option_id": o.option_id, "title": o.title, "summary": o.summary, "evidence_package_ids": list(o.evidence_package_ids)} for o in options]}
        with self.store.connection() as conn:
            if not conn.execute("SELECT 1 FROM product_projects WHERE project_id=?", (project_id,)).fetchone():
                raise KeyError(project_id)
            conn.execute("INSERT INTO product_research_handoffs(project_id,package_id,payload_json,created_at) VALUES (?,?,?,?)", (project_id, package.package_id, _canonical(payload), _now()))
            self._audit(conn, project_id, "product_project.research_handoff", {"package_id": package.package_id, "option_ids": sorted(option_ids)})

    def _get_conn(self, conn: Any, project_id: str) -> ProductProject:
        row = conn.execute("SELECT p.*, s.spec_json FROM product_projects p JOIN product_project_specs s ON s.project_id=p.project_id AND s.spec_version=p.current_spec_version WHERE p.project_id=?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(project_id)
        data = json.loads(row["spec_json"])
        requirements = tuple(ProductRequirement(r["requirement_id"], r["text"], tuple(r["acceptance"]), tuple(r.get("evidence_package_ids", ())), tuple(r.get("decision_ids", ()))) for r in data.pop("requirements", []))
        spec = ProductProjectSpec(requirements=requirements, **{key: (tuple(value) if key.endswith("_refs") else value) for key, value in data.items()})
        return ProductProject(row["project_id"], row["name"], int(row["current_spec_version"]), int(row["row_version"]), row["status"], spec, row["created_at"], row["updated_at"])

    @staticmethod
    def _audit(conn: Any, project_id: str, event_type: str, payload: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_events(event_type,entity_type,entity_id,payload_json,created_at) VALUES (?,?,?,?,?)", (event_type, "product_project", project_id, _canonical(payload), _now()))
