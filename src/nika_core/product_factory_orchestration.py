from __future__ import annotations

import hashlib
import json
import posixpath
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar


class ProjectScale(StrEnum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class IntegrationDecisionKind(StrEnum):
    SERIALIZE = "serialize"
    RECONCILE = "reconcile"


class RepositoryGraphError(ValueError):
    """Raised when a repository graph violates a deterministic PF3 invariant."""


class TeamCompositionError(ValueError):
    """Raised when a team request is internally inconsistent."""


@dataclass(frozen=True, slots=True)
class ComponentBrief:
    component_id: str
    kind: str
    risk_tags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.component_id.strip():
            raise TeamCompositionError("component_id must not be empty")
        if not self.kind.strip():
            raise TeamCompositionError("component kind must not be empty")


@dataclass(frozen=True, slots=True)
class TeamCompositionRequest:
    project_id: str
    components: tuple[ComponentBrief, ...]
    acceptance_criteria: tuple[str, ...]
    permission_ceiling: frozenset[str]
    scale: ProjectScale = ProjectScale.MEDIUM
    requested_specializations: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.project_id.strip():
            raise TeamCompositionError("project_id must not be empty")
        ids = [component.component_id for component in self.components]
        if len(ids) != len(set(ids)):
            raise TeamCompositionError("component ids must be unique")


@dataclass(frozen=True, slots=True)
class TeamRole:
    role_id: str
    capabilities: tuple[str, ...]
    component_ids: tuple[str, ...]
    permissions: frozenset[str]
    reasons: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()
    independent_review: bool = False


@dataclass(frozen=True, slots=True)
class TeamPlan:
    project_id: str
    plan_id: str
    roles: tuple[TeamRole, ...]
    permission_ceiling: frozenset[str]
    reasons: tuple[str, ...]


class DynamicTeamComposer:
    """Deterministic PF2 team composition without owning an agent runtime."""

    _RISK_SPECIALISTS: ClassVar[dict[str, str]] = {
        "accessibility": "accessibility",
        "credentials": "security",
        "deployment": "release",
        "network": "security",
        "payments": "security",
        "privacy": "security",
        "security": "security",
    }
    _KIND_SPECIALISTS: ClassVar[dict[str, str]] = {
        "android": "mobile",
        "backend": "backend",
        "data": "data",
        "desktop": "windows",
        "infra": "release",
        "ios": "mobile",
        "mobile": "mobile",
        "web": "web",
        "windows": "windows",
    }
    _PERMISSIONS: ClassVar[dict[str, frozenset[str]]] = {
        "accessibility": frozenset({"read_source", "run_tests"}),
        "architecture": frozenset({"read_source"}),
        "backend": frozenset({"read_source", "write_source", "run_tests"}),
        "coordination": frozenset({"read_project", "update_project"}),
        "data": frozenset({"read_source", "write_source", "run_tests"}),
        "implementation": frozenset({"read_source", "write_source", "run_tests"}),
        "mobile": frozenset({"read_source", "write_source", "run_tests"}),
        "qa": frozenset({"read_source", "run_tests"}),
        "release": frozenset({"read_source", "run_tests", "build_release"}),
        "security": frozenset({"read_source", "run_tests"}),
        "web": frozenset({"read_source", "write_source", "run_tests"}),
        "windows": frozenset({"read_source", "write_source", "run_tests"}),
    }

    def compose(self, request: TeamCompositionRequest) -> TeamPlan:
        capabilities = {"coordination", "architecture", "implementation", "qa"}
        reasons: list[str] = ["baseline product planning, implementation and independent review"]

        for component in request.components:
            specialist = self._KIND_SPECIALISTS.get(component.kind.casefold())
            if specialist is not None:
                capabilities.add(specialist)
                reasons.append(f"component {component.component_id} requires {specialist}")
            for risk in sorted(component.risk_tags):
                specialist = self._RISK_SPECIALISTS.get(risk.casefold())
                if specialist is not None:
                    capabilities.add(specialist)
                    reasons.append(f"risk {risk} requires {specialist}")

        criteria_text = " ".join(request.acceptance_criteria).casefold()
        if any(token in criteria_text for token in ("accessibility", "accessible", "nvda", "uia")):
            capabilities.add("accessibility")
            reasons.append("acceptance criteria require accessibility specialization")
        if any(token in criteria_text for token in ("deploy", "release", "package", "rollback")):
            capabilities.add("release")
            reasons.append("acceptance criteria require release specialization")

        for specialization in request.requested_specializations:
            normalized = specialization.strip().casefold()
            if not normalized:
                raise TeamCompositionError("requested specialization must not be empty")
            capabilities.add(normalized)
            reasons.append(f"project explicitly requested {normalized}")

        roles = self._shape_roles(request, capabilities)
        plan_id = _stable_id(
            "team-plan",
            request.project_id,
            [(role.role_id, role.capabilities, role.component_ids, sorted(role.permissions)) for role in roles],
        )
        return TeamPlan(
            project_id=request.project_id,
            plan_id=plan_id,
            roles=roles,
            permission_ceiling=request.permission_ceiling,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def add_specialist(
        self,
        plan: TeamPlan,
        *,
        specialization: str,
        component_ids: Iterable[str],
        requested_permissions: Iterable[str],
        reason: str,
        evidence_refs: Iterable[str] = (),
    ) -> TeamPlan:
        capability = specialization.strip().casefold()
        if not capability:
            raise TeamCompositionError("specialization must not be empty")
        if not reason.strip():
            raise TeamCompositionError("specialist reason must not be empty")
        component_tuple = tuple(sorted(set(component_ids)))
        if not component_tuple:
            raise TeamCompositionError("specialist must own at least one component")
        known_components = {component_id for role in plan.roles for component_id in role.component_ids}
        unknown_components = set(component_tuple) - known_components
        if unknown_components:
            unknown = ", ".join(sorted(unknown_components))
            raise TeamCompositionError(f"specialist references unknown component(s): {unknown}")
        requested = frozenset(requested_permissions)
        permissions = requested & plan.permission_ceiling
        role_id = _stable_id("team-role", plan.project_id, capability, component_tuple, len(plan.roles))
        role = TeamRole(
            role_id=role_id,
            capabilities=(capability,),
            component_ids=component_tuple,
            permissions=permissions,
            reasons=(reason,),
            evidence_refs=tuple(evidence_refs),
        )
        roles = (*plan.roles, role)
        return TeamPlan(
            project_id=plan.project_id,
            plan_id=_stable_id("team-plan", plan.project_id, [item.role_id for item in roles]),
            roles=roles,
            permission_ceiling=plan.permission_ceiling,
            reasons=(*plan.reasons, f"added specialist {capability}: {reason}"),
        )

    def _shape_roles(
        self, request: TeamCompositionRequest, capabilities: set[str]
    ) -> tuple[TeamRole, ...]:
        all_components = tuple(sorted(component.component_id for component in request.components))
        specialist_components: dict[str, set[str]] = {}
        for component in request.components:
            specialist = self._KIND_SPECIALISTS.get(component.kind.casefold())
            if specialist is not None:
                specialist_components.setdefault(specialist, set()).add(component.component_id)

        if request.scale is ProjectScale.SMALL:
            builder_caps = tuple(sorted(capabilities - {"qa"}))
            return (
                self._role(request, "builder", builder_caps, all_components, False),
                self._role(request, "reviewer", ("qa",), all_components, True),
            )

        ordered = ["coordination", "architecture", "implementation"]
        ordered.extend(sorted(capabilities - set(ordered) - {"qa"}))
        roles: list[TeamRole] = []
        for capability in ordered:
            components = tuple(sorted(specialist_components.get(capability, set()))) or all_components
            roles.append(self._role(request, capability, (capability,), components, False))
        roles.append(self._role(request, "qa", ("qa",), all_components, True))

        if request.scale is ProjectScale.LARGE:
            expanded: list[TeamRole] = []
            for role in roles:
                if role.capabilities == ("implementation",) and len(all_components) > 1:
                    for component_id in all_components:
                        expanded.append(
                            self._role(
                                request,
                                f"implementation-{component_id}",
                                ("implementation",),
                                (component_id,),
                                False,
                            )
                        )
                else:
                    expanded.append(role)
            roles = expanded
        return tuple(roles)

    def _role(
        self,
        request: TeamCompositionRequest,
        role_label: str,
        capabilities: tuple[str, ...],
        component_ids: tuple[str, ...],
        independent_review: bool,
    ) -> TeamRole:
        requested = frozenset().union(*(self._PERMISSIONS.get(cap, frozenset()) for cap in capabilities))
        permissions = requested & request.permission_ceiling
        return TeamRole(
            role_id=_stable_id("team-role", request.project_id, role_label, component_ids),
            capabilities=capabilities,
            component_ids=component_ids,
            permissions=permissions,
            reasons=(f"deterministic {role_label} role for project scope",),
            evidence_refs=request.evidence_refs,
            independent_review=independent_review,
        )


@dataclass(frozen=True, slots=True)
class RepositoryRef:
    repository_id: str
    provider: str
    locator: str
    default_branch: str
    credential_ref: str | None = None
    case_sensitive_paths: bool = True
    windows_path_semantics: bool = False

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.repository_id, self.provider, self.locator, self.default_branch)
        ):
            raise RepositoryGraphError("repository identity fields must not be empty")
        if self.credential_ref is not None:
            if self.credential_ref != self.credential_ref.strip():
                raise RepositoryGraphError("repository credential reference must not contain edge whitespace")
            if not self.credential_ref.startswith("credref:") or not self.credential_ref[8:].strip():
                raise RepositoryGraphError("repository credentials must use non-empty opaque credref: references")


@dataclass(frozen=True, slots=True)
class ProductComponent:
    component_id: str
    repository_id: str
    paths: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    build_commands: tuple[tuple[str, ...], ...] = ()
    test_commands: tuple[tuple[str, ...], ...] = ()
    release_identity: str | None = None

    def __post_init__(self) -> None:
        if not self.component_id.strip() or not self.repository_id.strip():
            raise RepositoryGraphError("component identity fields must not be empty")


@dataclass(frozen=True, slots=True)
class OwnershipLease:
    lease_id: str
    worker_id: str
    component_ids: tuple[str, ...]
    allowed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OwnershipConflict:
    repository_id: str
    candidate_lease_id: str
    active_lease_id: str
    path_a: str
    path_b: str


@dataclass(frozen=True, slots=True)
class IntegrationDecision:
    decision_id: str
    kind: IntegrationDecisionKind
    lease_ids: tuple[str, ...]
    reason: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.decision_id.strip() or not self.reason.strip() or not self.evidence_refs:
            raise RepositoryGraphError("integration decisions require identity, reason and evidence")
        if len(self.lease_ids) < 2 or len(set(self.lease_ids)) != len(self.lease_ids):
            raise RepositoryGraphError("integration decisions require distinct involved leases")
        if any(not lease_id.strip() for lease_id in self.lease_ids):
            raise RepositoryGraphError("integration decision lease identity must not be empty")


@dataclass(frozen=True, slots=True)
class LeaseAssessment:
    conflicts: tuple[OwnershipConflict, ...]
    decision: IntegrationDecision | None

    @property
    def grantable(self) -> bool:
        return not self.conflicts

    @property
    def requires_integration(self) -> bool:
        return bool(self.conflicts) and self.decision is not None


@dataclass(slots=True)
class ProductRepositoryGraph:
    project_id: str
    repositories: tuple[RepositoryRef, ...]
    components: tuple[ProductComponent, ...]
    _repositories_by_id: dict[str, RepositoryRef] = field(init=False, repr=False)
    _components_by_id: dict[str, ProductComponent] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.project_id.strip():
            raise RepositoryGraphError("project_id must not be empty")
        self._repositories_by_id = _unique_by(self.repositories, "repository_id")
        self._components_by_id = _unique_by(self.components, "component_id")
        self._validate_physical_repository_identity()
        self._validate_components()
        self._validate_acyclic()
        self._validate_component_overlap()

    def dependency_order(self) -> tuple[str, ...]:
        remaining = {key: set(value.dependencies) for key, value in self._components_by_id.items()}
        result: list[str] = []
        while remaining:
            ready = sorted(key for key, deps in remaining.items() if not deps)
            if not ready:
                raise RepositoryGraphError("component dependency graph contains a cycle")
            result.extend(ready)
            for key in ready:
                remaining.pop(key)
            for deps in remaining.values():
                deps.difference_update(ready)
        return tuple(result)

    def assess_lease(
        self,
        candidate: OwnershipLease,
        active_leases: Iterable[OwnershipLease],
        *,
        decision: IntegrationDecision | None = None,
    ) -> LeaseAssessment:
        candidate_paths = self._lease_paths(candidate)
        conflicts: list[OwnershipConflict] = []
        active_tuple = tuple(active_leases)
        active_ids = [lease.lease_id for lease in active_tuple]
        if len(active_ids) != len(set(active_ids)):
            raise RepositoryGraphError("active lease ids must be unique")
        if candidate.lease_id in active_ids:
            raise RepositoryGraphError("candidate lease id is already active")
        active_by_id = {lease.lease_id: lease for lease in active_tuple}
        for active in active_by_id.values():
            active_paths = self._lease_paths(active)
            for repo_id, candidate_path in candidate_paths:
                repository = self._repositories_by_id[repo_id]
                for active_repo_id, active_path in active_paths:
                    if repo_id != active_repo_id:
                        continue
                    if _paths_overlap(candidate_path, active_path, repository.case_sensitive_paths):
                        conflicts.append(
                            OwnershipConflict(
                                repository_id=repo_id,
                                candidate_lease_id=candidate.lease_id,
                                active_lease_id=active.lease_id,
                                path_a=candidate_path,
                                path_b=active_path,
                            )
                        )
        unique_conflicts = tuple(
            sorted(
                set(conflicts),
                key=lambda item: (item.repository_id, item.active_lease_id, item.path_a, item.path_b),
            )
        )
        if decision is not None:
            expected = {candidate.lease_id} | {item.active_lease_id for item in unique_conflicts}
            if set(decision.lease_ids) != expected:
                raise RepositoryGraphError(
                    "integration decision must cover candidate and every conflicting active lease"
                )
        return LeaseAssessment(conflicts=unique_conflicts, decision=decision)

    def _validate_physical_repository_identity(self) -> None:
        seen: dict[tuple[str, str], str] = {}
        for repository in self.repositories:
            provider = repository.provider.strip().casefold()
            locator = repository.locator.strip().rstrip("/")
            if provider == "github":
                locator = locator.casefold()
            key = (provider, locator)
            previous = seen.get(key)
            if previous is not None and previous != repository.repository_id:
                raise RepositoryGraphError(
                    "physical repository is aliased by multiple repository ids: "
                    f"{previous}, {repository.repository_id}"
                )
            seen[key] = repository.repository_id

    def _validate_components(self) -> None:
        for component in self.components:
            repository = self._repositories_by_id.get(component.repository_id)
            if repository is None:
                raise RepositoryGraphError(
                    f"component {component.component_id} references unknown repository"
                )
            if not component.paths:
                raise RepositoryGraphError(f"component {component.component_id} requires owned paths")
            normalized = [
                _normalize_repo_path(
                    path,
                    windows_path_semantics=repository.windows_path_semantics,
                )
                for path in component.paths
            ]
            if len(normalized) != len(set(normalized)):
                raise RepositoryGraphError(f"component {component.component_id} repeats an owned path")
            if len(component.dependencies) != len(set(component.dependencies)):
                raise RepositoryGraphError(f"component {component.component_id} repeats a dependency")
            for dependency in component.dependencies:
                if dependency == component.component_id or dependency not in self._components_by_id:
                    raise RepositoryGraphError(
                        f"component {component.component_id} has invalid dependency {dependency}"
                    )
            for command in (*component.build_commands, *component.test_commands):
                if not command or any(not part for part in command):
                    raise RepositoryGraphError("build/test commands must be non-empty argv tuples")

    def _validate_acyclic(self) -> None:
        self.dependency_order()

    def _validate_component_overlap(self) -> None:
        by_repo: dict[str, list[tuple[str, str]]] = {}
        for component in self.components:
            repository = self._repositories_by_id[component.repository_id]
            for path in component.paths:
                by_repo.setdefault(component.repository_id, []).append(
                    (
                        component.component_id,
                        _normalize_repo_path(
                            path,
                            windows_path_semantics=repository.windows_path_semantics,
                        ),
                    )
                )
        for repository_id, entries in by_repo.items():
            repository = self._repositories_by_id[repository_id]
            for index, (component_a, path_a) in enumerate(entries):
                for component_b, path_b in entries[index + 1 :]:
                    if component_a == component_b:
                        continue
                    if _paths_overlap(path_a, path_b, repository.case_sensitive_paths):
                        raise RepositoryGraphError(
                            f"components {component_a} and {component_b} overlap in "
                            f"repository {repository_id}: {path_a} vs {path_b}"
                        )

    def _lease_paths(self, lease: OwnershipLease) -> tuple[tuple[str, str], ...]:
        if not lease.lease_id.strip() or not lease.worker_id.strip():
            raise RepositoryGraphError("lease identity must not be empty")
        if not lease.component_ids or not lease.allowed_paths:
            raise RepositoryGraphError("lease requires components and allowed paths")
        if len(lease.component_ids) != len(set(lease.component_ids)):
            raise RepositoryGraphError("lease component ids must be unique")
        components = []
        for component_id in lease.component_ids:
            component = self._components_by_id.get(component_id)
            if component is None:
                raise RepositoryGraphError(f"lease references unknown component {component_id}")
            components.append(component)
        repos = {component.repository_id for component in components}
        result: list[tuple[str, str]] = []
        for raw_path in lease.allowed_paths:
            path = _normalize_repo_path(raw_path)
            matches = [
                component
                for component in components
                if any(
                    _path_within(
                        path,
                        _normalize_repo_path(root),
                        self._repositories_by_id[component.repository_id].case_sensitive_paths,
                    )
                    for root in component.paths
                )
            ]
            if not matches:
                raise RepositoryGraphError(f"lease path {path} is outside component ownership")
            matching_repos = {component.repository_id for component in matches}
            if len(matching_repos) != 1:
                raise RepositoryGraphError(f"lease path {path} is ambiguous across repositories")
            repository_id = next(iter(matching_repos))
            repository = self._repositories_by_id[repository_id]
            path = _normalize_repo_path(
                raw_path,
                windows_path_semantics=repository.windows_path_semantics,
            )
            result.append((repository_id, path))
        if not {repo_id for repo_id, _ in result}.issubset(repos):
            raise RepositoryGraphError("lease path repository mismatch")
        return tuple(result)


def _normalize_repo_path(path: str, *, windows_path_semantics: bool = False) -> str:
    raw_candidate = path.replace("\\", "/")
    if windows_path_semantics:
        _validate_windows_repo_path(raw_candidate, original=path)
    candidate = raw_candidate if windows_path_semantics else raw_candidate.strip()
    if not candidate or candidate.startswith("/") or ":" in candidate.split("/", 1)[0]:
        raise RepositoryGraphError(f"repository path must be relative: {path!r}")
    normalized = posixpath.normpath(candidate)
    if normalized in {".", ".."} or normalized.startswith("../"):
        raise RepositoryGraphError(f"repository path escapes root: {path!r}")
    normalized = normalized.rstrip("/")
    if windows_path_semantics:
        _validate_windows_repo_path(normalized, original=path)
    return normalized


def _validate_windows_repo_path(path: str, *, original: str) -> None:
    reserved_stems = {
        "aux",
        "con",
        "conin$",
        "conout$",
        "nul",
        "prn",
        *(f"com{index}" for index in range(10)),
        *(f"lpt{index}" for index in range(10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
    invalid_characters = frozenset('<>:"|?*')
    for component in path.split("/"):
        if component.startswith(" ") or component.endswith((" ", ".")):
            raise RepositoryGraphError(
                f"Windows repository path component has unsafe edge identity: {original!r}"
            )
        if any(character in invalid_characters or ord(character) < 32 for character in component):
            raise RepositoryGraphError(
                f"Windows repository path component contains reserved syntax: {original!r}"
            )
        stem = component.split(".", 1)[0].casefold()
        if stem in reserved_stems:
            raise RepositoryGraphError(
                f"Windows repository path component uses a reserved device name: {original!r}"
            )


def _path_within(path: str, root: str, case_sensitive: bool) -> bool:
    if not case_sensitive:
        path = path.casefold()
        root = root.casefold()
    return path == root or path.startswith(root + "/")


def _paths_overlap(path_a: str, path_b: str, case_sensitive: bool) -> bool:
    return _path_within(path_a, path_b, case_sensitive) or _path_within(
        path_b, path_a, case_sensitive
    )


def _unique_by(values: Iterable[object], attribute: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for value in values:
        key = getattr(value, attribute)
        if key in result:
            raise RepositoryGraphError(f"duplicate {attribute}: {key}")
        result[key] = value
    return result


def _stable_id(namespace: str, *parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{namespace}\0{payload}".encode()).hexdigest()[:20]
    return f"{namespace}:{digest}"
