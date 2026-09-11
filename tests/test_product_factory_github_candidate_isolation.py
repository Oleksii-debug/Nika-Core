from __future__ import annotations

import pytest

from nika_core.product_factory_github import (
    GitHubFactoryAdapter,
    GitHubFactoryError,
    GitHubPullRequest,
    GitHubRepositoryObservation,
    PullRequestState,
)
from nika_core.product_factory_orchestration import RepositoryRef

MAIN_SHA = "1" * 40
CANDIDATE_SHA = "2" * 40
TARGET_REPOSITORY = "Oleksii-debug/Nika-Core"
FORK_REPOSITORY = "Contributor/Nika-Core"


def _repository() -> RepositoryRef:
    return RepositoryRef(
        repository_id="core",
        provider="github",
        locator=f"https://github.com/{TARGET_REPOSITORY}.git",
        default_branch="main",
        credential_ref="credref:github-core",
    )


def _observation(**overrides: object) -> GitHubRepositoryObservation:
    values: dict[str, object] = {
        "owner": "Oleksii-debug",
        "name": "Nika-Core",
        "default_branch": "main",
        "default_branch_sha": MAIN_SHA,
        "candidate_branch": "work/isolation-proof",
        "candidate_sha": CANDIDATE_SHA,
    }
    values.update(overrides)
    return GitHubRepositoryObservation(**values)  # type: ignore[arg-type]


def test_target_default_branch_cannot_be_projected_as_candidate() -> None:
    with pytest.raises(GitHubFactoryError, match="target default branch"):
        GitHubFactoryAdapter().bind(
            _repository(),
            _observation(candidate_branch="main", candidate_sha=MAIN_SHA),
        )


def test_same_repository_feature_branch_binds_target_repository_identity() -> None:
    binding = GitHubFactoryAdapter().bind(_repository(), _observation())

    assert binding.candidate_repository_full_name == "oleksii-debug/nika-core"
    assert binding.candidate_branch == "work/isolation-proof"
    assert binding.candidate_sha == CANDIDATE_SHA


def test_explicit_fork_can_use_same_named_default_branch_without_aliasing_target() -> None:
    pull_request = GitHubPullRequest(
        number=720,
        head_branch="main",
        head_sha=CANDIDATE_SHA,
        base_branch="main",
        base_sha=MAIN_SHA,
        state=PullRequestState.OPEN,
        head_repository_full_name=FORK_REPOSITORY,
    )
    binding = GitHubFactoryAdapter().bind(
        _repository(),
        _observation(
            candidate_repository_full_name=FORK_REPOSITORY,
            candidate_branch="main",
            pull_request=pull_request,
        ),
    )

    assert binding.candidate_repository_full_name == "contributor/nika-core"
    assert binding.candidate_branch == "main"
    assert binding.pull_request_number == 720


def test_pull_request_head_repository_must_match_candidate_repository() -> None:
    pull_request = GitHubPullRequest(
        number=720,
        head_branch="work/isolation-proof",
        head_sha=CANDIDATE_SHA,
        base_branch="main",
        base_sha=MAIN_SHA,
        state=PullRequestState.OPEN,
        head_repository_full_name=FORK_REPOSITORY,
    )

    with pytest.raises(GitHubFactoryError, match="head repository"):
        GitHubFactoryAdapter().bind(
            _repository(),
            _observation(pull_request=pull_request),
        )
