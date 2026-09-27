from __future__ import annotations

import pytest

from nika_core.product_factory_github import (
    GitHubFactoryAdapter,
    GitHubFactoryError,
    GitHubIntegrationEvidence,
    GitHubPullRequest,
    GitHubRepositoryObservation,
    PullRequestState,
)
from nika_core.product_factory_orchestration import RepositoryRef

BASE_SHA = "1" * 40
CANDIDATE_SHA = "2" * 40
MERGE_SHA = "3" * 40
TIP_SHA = "4" * 40
TARGET_REPOSITORY = "Oleksii-debug/Nika-Core"
FORK_REPOSITORY = "Contributor/Nika-Core"


class _IntegrationEvidencePort:
    def __init__(self, candidate_repository_full_name: str | None) -> None:
        self._candidate_repository_full_name = candidate_repository_full_name

    def prove_integration(self, **kwargs: object) -> GitHubIntegrationEvidence:
        return GitHubIntegrationEvidence(
            pull_request_number=int(kwargs["pull_request_number"]),
            candidate_sha=str(kwargs["candidate_sha"]),
            integration_sha=str(kwargs["integration_sha"]),
            evidence_ref="github:compare/candidate-to-integration",
            candidate_repository_full_name=self._candidate_repository_full_name,
        )


def _repository() -> RepositoryRef:
    return RepositoryRef(
        repository_id="core",
        provider="github",
        locator=f"https://github.com/{TARGET_REPOSITORY}.git",
        default_branch="main",
        credential_ref="credref:github-core",
    )


def _merged_fork_observation() -> GitHubRepositoryObservation:
    return GitHubRepositoryObservation(
        owner="Oleksii-debug",
        name="Nika-Core",
        default_branch="main",
        default_branch_sha=TIP_SHA,
        candidate_branch="main",
        candidate_sha=CANDIDATE_SHA,
        candidate_repository_full_name=FORK_REPOSITORY,
        pull_request=GitHubPullRequest(
            number=720,
            head_branch="main",
            head_sha=CANDIDATE_SHA,
            base_branch="main",
            base_sha=BASE_SHA,
            state=PullRequestState.MERGED,
            merge_sha=MERGE_SHA,
            head_repository_full_name=FORK_REPOSITORY,
        ),
        default_branch_ancestor_shas=(BASE_SHA, MERGE_SHA),
    )


def test_merged_fork_requires_trusted_candidate_repository_identity() -> None:
    binding = GitHubFactoryAdapter(
        integration_evidence_port=_IntegrationEvidencePort(FORK_REPOSITORY)
    ).bind(_repository(), _merged_fork_observation())

    assert binding.integrated is True
    assert binding.candidate_repository_full_name == "contributor/nika-core"
    assert binding.integration_sha == MERGE_SHA


@pytest.mark.parametrize(
    "trusted_candidate_repository",
    [None, "Other/Nika-Core"],
)
def test_merged_fork_rejects_missing_or_foreign_trusted_candidate_repository(
    trusted_candidate_repository: str | None,
) -> None:
    with pytest.raises(GitHubFactoryError, match="candidate repository"):
        GitHubFactoryAdapter(
            integration_evidence_port=_IntegrationEvidencePort(
                trusted_candidate_repository
            )
        ).bind(_repository(), _merged_fork_observation())
