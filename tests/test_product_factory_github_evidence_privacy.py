from __future__ import annotations

import pytest

from nika_core.product_factory_github import (
    CheckState,
    GitHubCheck,
    GitHubFactoryAdapter,
    GitHubFactoryError,
    GitHubIntegrationEvidence,
    GitHubPullRequest,
    GitHubRepositoryObservation,
    PullRequestState,
)
from nika_core.product_factory_orchestration import RepositoryRef

MAIN_SHA = "1" * 40
CANDIDATE_SHA = "2" * 40
MERGE_SHA = "3" * 40
DESCENDANT_SHA = "4" * 40
CANARY = "CANARY_NOT_A_REAL_SECRET"


class _IntegrationEvidencePort:
    def __init__(self, evidence: GitHubIntegrationEvidence) -> None:
        self.evidence = evidence

    def prove_integration(self, **kwargs: object) -> GitHubIntegrationEvidence:
        del kwargs
        return self.evidence


def _repository() -> RepositoryRef:
    return RepositoryRef(
        repository_id="core",
        provider="github",
        locator="https://github.com/Oleksii-debug/Nika-Core.git",
        default_branch="main",
        credential_ref="credref:github-core",
    )


def _check() -> GitHubCheck:
    return GitHubCheck(
        check_id="core.windows-2025",
        name="Core Windows",
        head_sha=CANDIDATE_SHA,
        state=CheckState.PASS,
        evidence_ref="actions:run/core/34747394954",
    )


def _merged_observation() -> GitHubRepositoryObservation:
    return GitHubRepositoryObservation(
        owner="Oleksii-debug",
        name="Nika-Core",
        default_branch="main",
        default_branch_sha=DESCENDANT_SHA,
        candidate_branch="automation/dev04",
        candidate_sha=CANDIDATE_SHA,
        pull_request=GitHubPullRequest(
            number=717,
            head_branch="automation/dev04",
            head_sha=CANDIDATE_SHA,
            base_branch="main",
            base_sha=MAIN_SHA,
            state=PullRequestState.MERGED,
            merge_sha=MERGE_SHA,
        ),
        checks=(_check(),),
        default_branch_ancestor_shas=(MAIN_SHA, MERGE_SHA),
    )


def test_canonical_provider_identity_and_opaque_references_remain_supported() -> None:
    check = _check()
    integration = GitHubIntegrationEvidence(
        pull_request_number=717,
        candidate_sha=CANDIDATE_SHA,
        integration_sha=MERGE_SHA,
        evidence_ref="github:compare/candidate-to-integration",
    )
    digest_ref = GitHubIntegrationEvidence(
        pull_request_number=717,
        candidate_sha=CANDIDATE_SHA,
        integration_sha=MERGE_SHA,
        evidence_ref=f"evidence-sha256:{'a' * 64}",
    )

    assert check.check_id == "core.windows-2025"
    assert check.evidence_ref == "actions:run/core/34747394954"
    assert integration.evidence_ref == "github:compare/candidate-to-integration"
    assert digest_ref.evidence_ref.startswith("evidence-sha256:")


@pytest.mark.parametrize(
    "check_id",
    (
        "core windows",
        "core:windows",
        "core/windows",
        "core\nwindows",
        "https://checks.invalid/core",
        "a" * 129,
    ),
)
def test_provider_check_id_rejects_non_machine_identity(check_id: str) -> None:
    with pytest.raises(GitHubFactoryError, match="canonical machine identifier"):
        GitHubCheck(
            check_id=check_id,
            name="Core CI",
            head_sha=CANDIDATE_SHA,
            state=CheckState.PASS,
            evidence_ref="actions:run/core",
        )


@pytest.mark.parametrize(
    "evidence_ref",
    (
        f"Authorization: Bearer {CANARY}",
        f"Cookie: session={CANARY}",
        f"X-Api-Key:{CANARY}",
        f"actions:run/core\nCookie: session={CANARY}",
        f"https://example.invalid/run?access_token={CANARY}",
        f"actions:run/core?token={CANARY}",
        "actions:run/core#fragment",
        "a" * 513,
    ),
)
def test_check_evidence_rejects_secret_header_control_url_and_oversize_shapes(
    evidence_ref: str,
) -> None:
    with pytest.raises(GitHubFactoryError) as exc_info:
        GitHubCheck(
            check_id="core",
            name="Core CI",
            head_sha=CANDIDATE_SHA,
            state=CheckState.PASS,
            evidence_ref=evidence_ref,
        )

    assert CANARY not in str(exc_info.value)


@pytest.mark.parametrize(
    "evidence_ref",
    (
        f"Authorization: Bearer {CANARY}",
        f"Cookie: session={CANARY}",
        f"X-Auth-Token:{CANARY}",
        f"github:compare/run?token={CANARY}",
    ),
)
def test_integration_evidence_rejects_credential_shapes(evidence_ref: str) -> None:
    with pytest.raises(GitHubFactoryError) as exc_info:
        GitHubIntegrationEvidence(
            pull_request_number=717,
            candidate_sha=CANDIDATE_SHA,
            integration_sha=MERGE_SHA,
            evidence_ref=evidence_ref,
        )

    assert CANARY not in str(exc_info.value)


def test_bind_revalidates_mutated_check_evidence_before_projection() -> None:
    observation = GitHubRepositoryObservation(
        owner="Oleksii-debug",
        name="Nika-Core",
        default_branch="main",
        default_branch_sha=MAIN_SHA,
        candidate_branch="automation/dev04",
        candidate_sha=CANDIDATE_SHA,
        pull_request=GitHubPullRequest(
            number=717,
            head_branch="automation/dev04",
            head_sha=CANDIDATE_SHA,
            base_branch="main",
            base_sha=MAIN_SHA,
            state=PullRequestState.OPEN,
        ),
        checks=(_check(),),
    )
    object.__setattr__(
        observation.checks[0],
        "evidence_ref",
        f"Cookie: session={CANARY}",
    )

    with pytest.raises(GitHubFactoryError) as exc_info:
        GitHubFactoryAdapter().bind(_repository(), observation)

    assert CANARY not in str(exc_info.value)


def test_bind_revalidates_trusted_integration_evidence_before_projection() -> None:
    evidence = GitHubIntegrationEvidence(
        pull_request_number=717,
        candidate_sha=CANDIDATE_SHA,
        integration_sha=MERGE_SHA,
        evidence_ref="github:compare/candidate-to-integration",
    )
    object.__setattr__(
        evidence,
        "evidence_ref",
        f"Authorization: Bearer {CANARY}",
    )

    with pytest.raises(GitHubFactoryError) as exc_info:
        GitHubFactoryAdapter(
            integration_evidence_port=_IntegrationEvidencePort(evidence)
        ).bind(_repository(), _merged_observation())

    assert CANARY not in str(exc_info.value)
