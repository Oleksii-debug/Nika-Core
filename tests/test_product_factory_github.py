from __future__ import annotations

import pytest

from nika_core.product_factory_github import (
    CheckState,
    GitHubCheck,
    GitHubFactoryAdapter,
    GitHubFactoryError,
    GitHubIssueRef,
    GitHubPullRequest,
    GitHubRepositoryObservation,
    PullRequestState,
)
from nika_core.product_factory_orchestration import RepositoryRef


MAIN_SHA = "1" * 40
CANDIDATE_SHA = "2" * 40
MERGE_SHA = "3" * 40


def _repository(**overrides: object) -> RepositoryRef:
    values: dict[str, object] = {
        "repository_id": "core",
        "provider": "github",
        "locator": "https://github.com/Oleksii-debug/Nika-Core.git",
        "default_branch": "main",
        "credential_ref": "credref:github-core",
    }
    values.update(overrides)
    return RepositoryRef(**values)  # type: ignore[arg-type]


def _observation(**overrides: object) -> GitHubRepositoryObservation:
    values: dict[str, object] = {
        "owner": "Oleksii-debug",
        "name": "Nika-Core",
        "default_branch": "main",
        "default_branch_sha": MAIN_SHA,
        "issue": GitHubIssueRef(number=553, state="open"),
        "candidate_branch": "automation/dev04",
        "candidate_sha": CANDIDATE_SHA,
        "pull_request": GitHubPullRequest(
            number=720,
            head_branch="automation/dev04",
            head_sha=CANDIDATE_SHA,
            base_branch="main",
            state=PullRequestState.OPEN,
        ),
        "checks": (
            GitHubCheck(name="Core Ubuntu", head_sha=CANDIDATE_SHA, state=CheckState.PASS),
            GitHubCheck(name="Core Windows", head_sha=CANDIDATE_SHA, state=CheckState.PASS),
        ),
    }
    values.update(overrides)
    return GitHubRepositoryObservation(**values)  # type: ignore[arg-type]


def test_binds_exact_repository_candidate_pr_and_checks() -> None:
    binding = GitHubFactoryAdapter().bind(_repository(), _observation())

    assert binding.repository_id == "core"
    assert binding.repository_full_name == "oleksii-debug/nika-core"
    assert binding.default_branch_sha == MAIN_SHA
    assert binding.issue_number == 553
    assert binding.candidate_sha == CANDIDATE_SHA
    assert binding.pull_request_number == 720
    assert binding.checks_state is CheckState.PASS
    assert binding.integrated is False
    assert binding.integration_sha is None


def test_rejects_repository_locator_substitution() -> None:
    with pytest.raises(GitHubFactoryError, match="repository locator"):
        GitHubFactoryAdapter().bind(_repository(), _observation(owner="attacker"))


def test_rejects_stale_pr_head() -> None:
    stale_pr = GitHubPullRequest(
        number=720,
        head_branch="automation/dev04",
        head_sha="4" * 40,
        base_branch="main",
        state=PullRequestState.OPEN,
    )
    with pytest.raises(GitHubFactoryError, match="head does not match"):
        GitHubFactoryAdapter().bind(_repository(), _observation(pull_request=stale_pr))


def test_rejects_mixed_sha_checks() -> None:
    checks = (
        GitHubCheck(name="Core Ubuntu", head_sha=CANDIDATE_SHA, state=CheckState.PASS),
        GitHubCheck(name="Core Windows", head_sha="5" * 40, state=CheckState.PASS),
    )
    with pytest.raises(GitHubFactoryError, match="exact candidate sha"):
        GitHubFactoryAdapter().bind(_repository(), _observation(checks=checks))


def test_failed_check_prevents_green_projection() -> None:
    checks = (
        GitHubCheck(name="Core Ubuntu", head_sha=CANDIDATE_SHA, state=CheckState.PASS),
        GitHubCheck(name="Core Windows", head_sha=CANDIDATE_SHA, state=CheckState.FAIL),
    )
    binding = GitHubFactoryAdapter().bind(_repository(), _observation(checks=checks))
    assert binding.checks_state is CheckState.FAIL


def test_pending_check_prevents_green_projection() -> None:
    checks = (
        GitHubCheck(name="Core Ubuntu", head_sha=CANDIDATE_SHA, state=CheckState.PASS),
        GitHubCheck(name="Core Windows", head_sha=CANDIDATE_SHA, state=CheckState.PENDING),
    )
    binding = GitHubFactoryAdapter().bind(_repository(), _observation(checks=checks))
    assert binding.checks_state is CheckState.PENDING


def test_merged_pr_projects_exact_integration_identity() -> None:
    merged_pr = GitHubPullRequest(
        number=720,
        head_branch="automation/dev04",
        head_sha=CANDIDATE_SHA,
        base_branch="main",
        state=PullRequestState.MERGED,
        merge_sha=MERGE_SHA,
    )
    binding = GitHubFactoryAdapter().bind(_repository(), _observation(pull_request=merged_pr))
    assert binding.integrated is True
    assert binding.integration_sha == MERGE_SHA


def test_credentials_remain_opaque_repository_metadata() -> None:
    repository = _repository(credential_ref="credref:factory-github")
    binding = GitHubFactoryAdapter().bind(repository, _observation())

    assert repository.credential_ref == "credref:factory-github"
    assert "credential" not in repr(binding).casefold()
    assert "credref" not in repr(binding).casefold()


def test_non_github_repository_is_rejected() -> None:
    with pytest.raises(GitHubFactoryError, match="provider must be github"):
        GitHubFactoryAdapter().bind(_repository(provider="gitlab"), _observation())
