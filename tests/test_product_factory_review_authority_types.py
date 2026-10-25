import pytest

from nika_core.product_factory_orchestration import TeamPlan, TeamRole
from nika_core.product_factory_review_authority import (
    ProductFactoryReviewAuthorityError,
    ProductFactoryReviewSubject,
    TeamPlanReviewAuthority,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64


class _HostileText(str):
    def __eq__(self, other):
        return other == "worker:trusted-reviewer"

    __hash__ = str.__hash__


class _AllowAllEvidence:
    def verify(self, subject, evidence_refs):
        return True


def _subject(**overrides):
    values = {
        "project_id": "project-1",
        "component_id": "core",
        "work_id": "work-1",
        "repository_id": "repo-1",
        "base_sha": SHA_A,
        "result_sha": SHA_B,
        "diff_digest": DIGEST,
        "attempt": 1,
        "producer_actor_id": "worker:builder",
        "reviewer_id": "worker:trusted-reviewer",
        "accepted": True,
    }
    values.update(overrides)
    return ProductFactoryReviewSubject(**values)


def _team_plan(role_id="team-role:reviewer"):
    return TeamPlan(
        project_id="project-1",
        plan_id="team-plan:1",
        roles=(
            TeamRole(
                role_id=role_id,
                capabilities=("qa",),
                component_ids=("core",),
                permissions=frozenset({"read_source", "run_tests"}),
                reasons=("independent review",),
                independent_review=True,
            ),
        ),
        permission_ceiling=frozenset({"read_source", "run_tests"}),
        reasons=("trusted plan",),
    )


def test_review_subject_rejects_bool_as_attempt() -> None:
    with pytest.raises(ProductFactoryReviewAuthorityError, match="exact positive integer"):
        _subject(attempt=True)


def test_review_subject_rejects_integer_as_accepted() -> None:
    with pytest.raises(ProductFactoryReviewAuthorityError, match="exact boolean"):
        _subject(accepted=1)


def test_review_subject_rejects_non_text_actor_identity_with_domain_error() -> None:
    with pytest.raises(ProductFactoryReviewAuthorityError, match="exact non-empty text"):
        _subject(producer_actor_id=1)


def test_review_subject_rejects_hostile_str_subclass_reviewer_identity() -> None:
    with pytest.raises(ProductFactoryReviewAuthorityError, match="exact non-empty text"):
        _subject(reviewer_id=_HostileText("attacker"))


def test_review_subject_rejects_hostile_str_subclass_sha() -> None:
    with pytest.raises(ProductFactoryReviewAuthorityError, match="exact 40-character"):
        _subject(base_sha=_HostileText(SHA_A))


def test_team_plan_authority_rejects_hostile_role_identity() -> None:
    with pytest.raises(ProductFactoryReviewAuthorityError, match="exact non-empty identities"):
        TeamPlanReviewAuthority(
            _team_plan(role_id=_HostileText("team-role:reviewer")),
            _AllowAllEvidence(),
            (("team-role:reviewer", "worker:trusted-reviewer"),),
        )


def test_team_plan_authority_rejects_hostile_principal_binding_text() -> None:
    with pytest.raises(ProductFactoryReviewAuthorityError, match="exact non-empty text"):
        TeamPlanReviewAuthority(
            _team_plan(),
            _AllowAllEvidence(),
            (("team-role:reviewer", _HostileText("worker:trusted-reviewer")),),
        )
