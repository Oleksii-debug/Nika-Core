from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from nika_core.product_factory_orchestration import (
    ComponentBrief,
    DynamicTeamComposer,
    ProjectScale,
    TeamCompositionRequest,
    TeamPlan,
)
from nika_core.product_project import (
    ProductProject,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirementKind,
    StaleProjectVersionError,
)

TEAM_PLAN_REF_PREFIX = "pf-team-plan:v1:"
_PLANNING_PERMISSION_CEILING = frozenset({"read_project"})
_PLANNING_SCALE = ProjectScale.MEDIUM


class PackagedProductFactoryPlanningError(ValueError):
    """Raised when packaged planning cannot safely bind to durable ProductProject state."""


@dataclass(frozen=True, slots=True)
class PackagedTeamPlanResult:
    project_id: str
    spec_version: int
    state: str
    plan: TeamPlan
    binding_ref: str
    input_fingerprint: str

    @property
    def independent_review_count(self) -> int:
        return sum(role.independent_review for role in self.plan.roles)


class PackagedProductFactoryTeamPlanner:
    """Planning-only packaged adapter over PF1 durability and PF2 DynamicTeamComposer.

    The adapter never dispatches a worker, creates a repository graph, broadens permissions or
    persists a second team authority. The deterministic plan binding is stored as one namespaced
    ProductProject ``team_ref`` so PF1 immutable spec history remains the canonical durable owner.
    """

    def __init__(
        self,
        repository: ProductProjectRepository,
        *,
        composer: DynamicTeamComposer | None = None,
    ) -> None:
        self._repository = repository
        self._composer = composer or DynamicTeamComposer()

    def plan(self, project_id: str) -> PackagedTeamPlanResult:
        current = self._require_active_project(project_id)
        plan, binding_ref, fingerprint = self._compose(current)
        owned_refs = self._owned_refs(current.spec)
        if len(owned_refs) > 1:
            raise PackagedProductFactoryPlanningError(
                "ProductProject contains multiple packaged Product Factory plan bindings; "
                "explicit reconciliation is required."
            )
        if owned_refs == (binding_ref,):
            return self._result(current, plan, binding_ref, fingerprint)

        retained_refs = tuple(
            ref for ref in current.spec.team_refs if not ref.startswith(TEAM_PLAN_REF_PREFIX)
        )
        next_spec = replace(
            current.spec,
            team_refs=(*retained_refs, binding_ref),
        )
        try:
            updated = self._repository.update_spec(
                project_id,
                next_spec,
                expected_row_version=current.row_version,
                change_reason="packaged Product Factory team planning",
            )
        except StaleProjectVersionError as exc:
            raise PackagedProductFactoryPlanningError(
                "ProductProject changed while the Product Factory plan was persisted; "
                "retry the explicit planning command."
            ) from exc

        persisted_plan, persisted_ref, persisted_fingerprint = self._compose(updated)
        if persisted_ref != binding_ref or persisted_plan != plan:
            raise PackagedProductFactoryPlanningError(
                "persisted ProductProject no longer matches the deterministic Product Factory plan"
            )
        return self._result(
            updated,
            persisted_plan,
            persisted_ref,
            persisted_fingerprint,
        )

    def inspect(self, project_id: str) -> PackagedTeamPlanResult:
        current = self._require_active_project(project_id)
        plan, binding_ref, fingerprint = self._compose(current)
        owned_refs = self._owned_refs(current.spec)
        if not owned_refs:
            raise PackagedProductFactoryPlanningError(
                "План Product Factory для поточного ProductProject ще не збережено. "
                "Виконайте команду планування."
            )
        if len(owned_refs) > 1:
            raise PackagedProductFactoryPlanningError(
                "ProductProject contains multiple packaged Product Factory plan bindings; "
                "explicit reconciliation is required."
            )
        if owned_refs[0] != binding_ref:
            raise PackagedProductFactoryPlanningError(
                "Збережений план Product Factory застарів після зміни ProductProject. "
                "Виконайте явне повторне планування."
            )
        return self._result(current, plan, binding_ref, fingerprint)

    def _require_active_project(self, project_id: str) -> ProductProject:
        project = self._repository.get(project_id)
        if project.status != "active":
            raise PackagedProductFactoryPlanningError(
                "Product Factory planning requires an active ProductProject."
            )
        return project

    def _compose(self, project: ProductProject) -> tuple[TeamPlan, str, str]:
        fingerprint = _planning_input_fingerprint(project.spec)
        request = TeamCompositionRequest(
            project_id=project.project_id,
            components=(
                ComponentBrief(
                    component_id="product",
                    kind="product",
                    risk_tags=_risk_tags(project.spec),
                ),
            ),
            acceptance_criteria=_acceptance_criteria(project.spec),
            permission_ceiling=_PLANNING_PERMISSION_CEILING,
            scale=_PLANNING_SCALE,
            evidence_refs=(f"product-spec:{fingerprint}",),
        )
        plan = self._composer.compose(request)
        binding_ref = f"{TEAM_PLAN_REF_PREFIX}{plan.plan_id}:{fingerprint}"
        return plan, binding_ref, fingerprint

    @staticmethod
    def _owned_refs(spec: ProductProjectSpec) -> tuple[str, ...]:
        return tuple(ref for ref in spec.team_refs if ref.startswith(TEAM_PLAN_REF_PREFIX))

    @staticmethod
    def _result(
        project: ProductProject,
        plan: TeamPlan,
        binding_ref: str,
        fingerprint: str,
    ) -> PackagedTeamPlanResult:
        return PackagedTeamPlanResult(
            project_id=project.project_id,
            spec_version=project.spec_version,
            state=project.status,
            plan=plan,
            binding_ref=binding_ref,
            input_fingerprint=fingerprint,
        )


def _planning_input_fingerprint(spec: ProductProjectSpec) -> str:
    payload = spec.to_dict()
    payload.pop("team_refs", None)
    payload.pop("supersedes_spec_version", None)
    payload.pop("revision_reason", None)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _acceptance_criteria(spec: ProductProjectSpec) -> tuple[str, ...]:
    criteria: list[str] = []
    for requirement in spec.requirements:
        criteria.extend(item.strip() for item in requirement.acceptance if item.strip())
        criteria.extend(
            criterion.text.strip()
            for criterion in requirement.acceptance_criteria
            if criterion.text.strip()
        )
    if not criteria:
        criteria.append(spec.desired_outcome.strip())
    return tuple(dict.fromkeys(criteria))


def _risk_tags(spec: ProductProjectSpec) -> frozenset[str]:
    tags: set[str] = set()
    for requirement in spec.requirements:
        if requirement.kind is ProductRequirementKind.SECURITY:
            tags.add("security")
        elif requirement.kind is ProductRequirementKind.PRIVACY:
            tags.add("privacy")
        elif requirement.kind is ProductRequirementKind.ACCESSIBILITY:
            tags.add("accessibility")
        elif requirement.kind is ProductRequirementKind.RELEASE:
            tags.add("deployment")
    if spec.credential_refs:
        tags.add("credentials")
    return frozenset(tags)
