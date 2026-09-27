from __future__ import annotations

import pytest

from nika_core.product_factory_github import (
    CheckState,
    GitHubCheck,
    GitHubFactoryAdapter,
    GitHubFactoryError,
    GitHubIntegrationEvidence,
    GitHubIssueRef,
    GitHubPullRequest,
    GitHubRepositoryObservation,
    PullRequestState,
)
from nika_core.product_factory_orchestration import RepositoryRef

MAIN_SHA = "1" * 40
CANDIDATE_SHA = "2" * 40
MERGE_SHA = "3" * 40
DESCENDANT_SHA = "4" * 40


class _HostileStr(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        del args, kwargs
        raise AssertionError("hostile str method executed")

    def __eq__(self, other: object) -> bool:
        del other
        return True

    def __ne__(self, other: object) -> bool:
        del other
        return False

    __hash__ = str.__hash__


class _HostileInt(int):
    def __lt__(self, other: object) -> bool:
        del other
        raise AssertionError("hostile int comparison executed")

    def __eq__(self, other: object) -> bool:
        del other
        return True

    def __ne__(self, other: object) -> bool:
        del other
        return False

    __hash__ = int.__hash__


class _IntegrationEvidencePort:
    def __init__(self, evidence: GitHubIntegrationEvidence | None = None) -> None:
        self.evidence = evidence
        self.calls: list[dict[str, object]] = []

    def prove_integration(self, **kwargs: object) -> GitHubIntegrationEvidence:
        self.calls.append(dict(kwargs))
        if self.evidence is not None:
            return self.evidence
        return GitHubIntegrationEvidence(
            pull_request_number=720,
            candidate_sha=CANDIDATE_SHA,
            integration_sha=MERGE_SHA,
            evidence_ref="github:compare/candidate-to-integration",
        )


def _repository() -> RepositoryRef:
    return RepositoryRef(
        repository_id="core",
        provider="github",
        locator="https://github.com/Oleksii-debug/Nika-Core.git",
        default_branch="main",
        credential_ref="credref:github-core",
    )


def _open_pr() -> GitHubPullRequest:
    return GitHubPullRequest(
        number=720,
        head_branch="automation/dev04",
        head_sha=CANDIDATE_SHA,
        base_branch="main",
        base_sha=MAIN_SHA,
        state=PullRequestState.OPEN,
    )


def _merged_pr() -> GitHubPullRequest:
    return GitHubPullRequest(
        number=720,
        head_branch="automation/dev04",
        head_sha=CANDIDATE_SHA,
        base_branch="main",
        base_sha=MAIN_SHA,
        state=PullRequestState.MERGED,
        merge_sha=MERGE_SHA,
    )


def _check() -> GitHubCheck:
    return GitHubCheck(
        check_id="core",
        name="Core CI",
        head_sha=CANDIDATE_SHA,
        state=CheckState.PASS,
        evidence_ref="actions:run/core",
    )


def _observation(*, merged: bool = False) -> GitHubRepositoryObservation:
    return GitHubRepositoryObservation(
        owner="Oleksii-debug",
        name="Nika-Core",
        default_branch="main",
        default_branch_sha=DESCENDANT_SHA if merged else MAIN_SHA,
        issue=GitHubIssueRef(number=553, state="open"),
        candidate_branch="automation/dev04",
        candidate_sha=CANDIDATE_SHA,
        pull_request=_merged_pr() if merged else _open_pr(),
        checks=(_check(),),
        default_branch_ancestor_shas=(MAIN_SHA, MERGE_SHA) if merged else (),
    )


def test_authority_constructors_reject_primitive_subclasses_before_methods_execute() -> None:
    with pytest.raises(GitHubFactoryError):
        GitHubIssueRef(number=_HostileInt(553), state="open")

    with pytest.raises(GitHubFactoryError):
        GitHubPullRequest(
            number=720,
            head_branch="automation/dev04",
            head_sha=_HostileStr(CANDIDATE_SHA),
            base_branch="main",
            base_sha=MAIN_SHA,
            state=PullRequestState.OPEN,
        )

    with pytest.raises(GitHubFactoryError):
        GitHubCheck(
            check_id=_HostileStr("core"),
            name="Core CI",
            head_sha=CANDIDATE_SHA,
            state=CheckState.PASS,
            evidence_ref="actions:run/core",
        )

    with pytest.raises(GitHubFactoryError):
        GitHubIntegrationEvidence(
            pull_request_number=720,
            candidate_sha=CANDIDATE_SHA,
            integration_sha=MERGE_SHA,
            evidence_ref=_HostileStr("github:compare/candidate-to-integration"),
        )

    with pytest.raises(GitHubFactoryError):
        GitHubRepositoryObservation(
            owner=_HostileStr("Oleksii-debug"),
            name="Nika-Core",
            default_branch="main",
            default_branch_sha=MAIN_SHA,
        )


def test_bind_revalidates_mutated_repository_identity_before_normalization() -> None:
    repository = _repository()
    object.__setattr__(
        repository,
        "locator",
        _HostileStr("https://github.com/attacker/Nika-Core.git"),
    )

    with pytest.raises(GitHubFactoryError, match="repository locator"):
        GitHubFactoryAdapter().bind(repository, _observation())


def test_bind_revalidates_mutated_observation_identity_before_projection() -> None:
    observation = _observation()
    object.__setattr__(observation, "owner", _HostileStr("attacker"))

    with pytest.raises(GitHubFactoryError, match="repository owner"):
        GitHubFactoryAdapter().bind(_repository(), observation)


def test_foreign_hostile_pr_head_cannot_reach_trusted_integration_proof() -> None:
    observation = _observation(merged=True)
    assert observation.pull_request is not None
    object.__setattr__(observation.pull_request, "head_sha", _HostileStr("5" * 40))
    port = _IntegrationEvidencePort()

    with pytest.raises(GitHubFactoryError, match="pull request head_sha"):
        GitHubFactoryAdapter(integration_evidence_port=port).bind(
            _repository(), observation
        )

    assert port.calls == []


def test_hostile_pr_number_cannot_reach_trusted_integration_proof() -> None:
    observation = _observation(merged=True)
    assert observation.pull_request is not None
    object.__setattr__(observation.pull_request, "number", _HostileInt(720))
    port = _IntegrationEvidencePort()

    with pytest.raises(GitHubFactoryError, match="pull request number"):
        GitHubFactoryAdapter(integration_evidence_port=port).bind(
            _repository(), observation
        )

    assert port.calls == []


def test_foreign_hostile_check_head_cannot_project_check_evidence() -> None:
    observation = _observation()
    object.__setattr__(observation.checks[0], "head_sha", _HostileStr("5" * 40))

    with pytest.raises(GitHubFactoryError, match="check head_sha"):
        GitHubFactoryAdapter().bind(_repository(), observation)


def test_trusted_integration_evidence_is_revalidated_before_identity_comparison() -> None:
    evidence = GitHubIntegrationEvidence(
        pull_request_number=720,
        candidate_sha=CANDIDATE_SHA,
        integration_sha=MERGE_SHA,
        evidence_ref="github:compare/candidate-to-integration",
    )
    object.__setattr__(evidence, "candidate_sha", _HostileStr("5" * 40))
    port = _IntegrationEvidencePort(evidence)

    with pytest.raises(GitHubFactoryError, match="integration candidate sha"):
        GitHubFactoryAdapter(integration_evidence_port=port).bind(
            _repository(), _observation(merged=True)
        )

    assert len(port.calls) == 1
