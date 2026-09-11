from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace

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
from nika_core.product_factory_verification import (
    PRODUCT_FACTORY_REQUIRED_CHECK_IDS,
    VerificationState,
    classify_candidate_verification,
)

MAIN_SHA = "1" * 40
CANDIDATE_SHA = "2" * 40
MERGE_SHA = "3" * 40
DESCENDANT_SHA = "4" * 40


class _IntegrationEvidencePort:
    def __init__(self, evidence: GitHubIntegrationEvidence | object | None = None) -> None:
        self.evidence = evidence
        self.calls: list[dict[str, object]] = []

    def prove_integration(self, **kwargs: object) -> GitHubIntegrationEvidence:
        self.calls.append(dict(kwargs))
        if self.evidence is not None:
            return self.evidence  # type: ignore[return-value]
        return GitHubIntegrationEvidence(
            pull_request_number=int(kwargs["pull_request_number"]),
            candidate_sha=str(kwargs["candidate_sha"]),
            integration_sha=str(kwargs["integration_sha"]),
            evidence_ref="github:compare/candidate-to-integration",
        )


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
            base_sha=MAIN_SHA,
            state=PullRequestState.OPEN,
        ),
        "checks": (
            GitHubCheck(
                check_id="core",
                name="Core CI",
                head_sha=CANDIDATE_SHA,
                state=CheckState.PASS,
                evidence_ref="actions:run/core",
            ),
            GitHubCheck(
                check_id="factory",
                name="Product Factory acceptance",
                head_sha=CANDIDATE_SHA,
                state=CheckState.PASS,
                evidence_ref="actions:run/factory",
            ),
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
    assert tuple(item.check_id for item in binding.check_evidence) == (
        "github:core",
        "github:factory",
    )
    assert tuple(item.evidence_ref for item in binding.check_evidence) == (
        "actions:run/core",
        "actions:run/factory",
    )
    assert all(item.required is False for item in binding.check_evidence)
    assert binding.integrated is False
    assert binding.integration_sha is None
    assert binding.integration_evidence_ref is None


def test_rejects_repository_locator_substitution() -> None:
    with pytest.raises(GitHubFactoryError, match="repository locator"):
        GitHubFactoryAdapter().bind(_repository(), _observation(owner="attacker"))


def test_rejects_stale_pr_head() -> None:
    stale_pr = GitHubPullRequest(
        number=720,
        head_branch="automation/dev04",
        head_sha="4" * 40,
        base_branch="main",
        base_sha=MAIN_SHA,
        state=PullRequestState.OPEN,
    )
    with pytest.raises(GitHubFactoryError, match="head does not match"):
        GitHubFactoryAdapter().bind(_repository(), _observation(pull_request=stale_pr))


def test_rejects_stale_pr_base_sha_even_when_branch_name_matches() -> None:
    stale_pr = GitHubPullRequest(
        number=720,
        head_branch="automation/dev04",
        head_sha=CANDIDATE_SHA,
        base_branch="main",
        base_sha="4" * 40,
        state=PullRequestState.OPEN,
    )
    with pytest.raises(GitHubFactoryError, match="base sha"):
        GitHubFactoryAdapter().bind(_repository(), _observation(pull_request=stale_pr))


def test_rejects_mixed_sha_checks() -> None:
    checks = (
        GitHubCheck(
            check_id="core",
            name="Core Ubuntu",
            head_sha=CANDIDATE_SHA,
            state=CheckState.PASS,
            evidence_ref="actions:run/core",
        ),
        GitHubCheck(
            check_id="factory",
            name="Core Windows",
            head_sha="5" * 40,
            state=CheckState.PASS,
            evidence_ref="actions:run/factory",
        ),
    )
    with pytest.raises(GitHubFactoryError, match="exact candidate sha"):
        GitHubFactoryAdapter().bind(_repository(), _observation(checks=checks))


def test_unknown_check_state_fails_closed() -> None:
    with pytest.raises(GitHubFactoryError, match="recognized CheckState"):
        GitHubCheck(
            check_id="core",
            name="Core Ubuntu",
            head_sha=CANDIDATE_SHA,
            state="unknown",  # type: ignore[arg-type]
            evidence_ref="actions:run/core",
        )


def test_failed_check_prevents_green_projection() -> None:
    checks = (
        GitHubCheck(
            check_id="core",
            name="Core Ubuntu",
            head_sha=CANDIDATE_SHA,
            state=CheckState.PASS,
            evidence_ref="actions:run/core",
        ),
        GitHubCheck(
            check_id="factory",
            name="Core Windows",
            head_sha=CANDIDATE_SHA,
            state=CheckState.FAIL,
            evidence_ref="actions:run/factory",
        ),
    )
    binding = GitHubFactoryAdapter().bind(_repository(), _observation(checks=checks))
    assert binding.checks_state is CheckState.FAIL


def test_pending_check_prevents_green_projection() -> None:
    checks = (
        GitHubCheck(
            check_id="core",
            name="Core Ubuntu",
            head_sha=CANDIDATE_SHA,
            state=CheckState.PASS,
            evidence_ref="actions:run/core",
        ),
        GitHubCheck(
            check_id="factory",
            name="Core Windows",
            head_sha=CANDIDATE_SHA,
            state=CheckState.PENDING,
            evidence_ref="actions:run/factory",
        ),
    )
    binding = GitHubFactoryAdapter().bind(_repository(), _observation(checks=checks))
    assert binding.checks_state is CheckState.PENDING


def _merged_pr(
    *,
    base_sha: str = MAIN_SHA,
    merge_sha: str = MERGE_SHA,
) -> GitHubPullRequest:
    return GitHubPullRequest(
        number=720,
        head_branch="automation/dev04",
        head_sha=CANDIDATE_SHA,
        base_branch="main",
        base_sha=base_sha,
        state=PullRequestState.MERGED,
        merge_sha=merge_sha,
    )


def test_merged_pr_projects_only_trusted_candidate_integration_identity() -> None:
    port = _IntegrationEvidencePort()
    binding = GitHubFactoryAdapter(integration_evidence_port=port).bind(
        _repository(),
        _observation(
            pull_request=_merged_pr(),
            default_branch_sha=DESCENDANT_SHA,
            default_branch_ancestor_shas=(MAIN_SHA, MERGE_SHA),
        ),
    )

    assert binding.integrated is True
    assert binding.integration_sha == MERGE_SHA
    assert binding.integration_evidence_ref == "github:compare/candidate-to-integration"
    assert binding.default_branch_sha == DESCENDANT_SHA
    assert binding.default_branch_ancestor_shas == (MAIN_SHA, MERGE_SHA)
    assert port.calls == [
        {
            "repository_full_name": "oleksii-debug/nika-core",
            "pull_request_number": 720,
            "candidate_sha": CANDIDATE_SHA,
            "integration_sha": MERGE_SHA,
        }
    ]


def test_generic_merged_observation_cannot_mint_integration_truth() -> None:
    with pytest.raises(GitHubFactoryError, match="trusted candidate integration evidence"):
        GitHubFactoryAdapter().bind(
            _repository(),
            _observation(
                pull_request=_merged_pr(),
                default_branch_sha=DESCENDANT_SHA,
                default_branch_ancestor_shas=(MAIN_SHA, MERGE_SHA),
            ),
        )


def test_merged_pr_rejects_unrelated_provider_integration_evidence() -> None:
    foreign = GitHubIntegrationEvidence(
        pull_request_number=720,
        candidate_sha="5" * 40,
        integration_sha=MERGE_SHA,
        evidence_ref="github:compare/foreign-candidate",
    )
    port = _IntegrationEvidencePort(foreign)

    with pytest.raises(GitHubFactoryError, match="candidate sha does not match"):
        GitHubFactoryAdapter(integration_evidence_port=port).bind(
            _repository(),
            _observation(
                pull_request=_merged_pr(),
                default_branch_sha=DESCENDANT_SHA,
                default_branch_ancestor_shas=(MAIN_SHA, MERGE_SHA),
            ),
        )


def test_merged_pr_rejects_foreign_integration_sha_evidence() -> None:
    foreign = GitHubIntegrationEvidence(
        pull_request_number=720,
        candidate_sha=CANDIDATE_SHA,
        integration_sha="5" * 40,
        evidence_ref="github:compare/foreign-integration",
    )
    port = _IntegrationEvidencePort(foreign)

    with pytest.raises(GitHubFactoryError, match="sha does not match pull request"):
        GitHubFactoryAdapter(integration_evidence_port=port).bind(
            _repository(),
            _observation(
                pull_request=_merged_pr(),
                default_branch_sha=DESCENDANT_SHA,
                default_branch_ancestor_shas=(MAIN_SHA, MERGE_SHA),
            ),
        )


def test_merged_pr_rejects_base_sha_as_fake_merge_identity() -> None:
    port = _IntegrationEvidencePort()
    aliased_pr = _merged_pr(merge_sha=MAIN_SHA)

    with pytest.raises(GitHubFactoryError, match="must differ from base sha"):
        GitHubFactoryAdapter(integration_evidence_port=port).bind(
            _repository(),
            _observation(
                pull_request=aliased_pr,
                default_branch_sha=DESCENDANT_SHA,
                default_branch_ancestor_shas=(MAIN_SHA,),
            ),
        )
    assert port.calls == []


def test_merged_pr_requires_canonical_integration_evidence_type() -> None:
    port = _IntegrationEvidencePort(
        SimpleNamespace(
            pull_request_number=720,
            candidate_sha=CANDIDATE_SHA,
            integration_sha=MERGE_SHA,
            evidence_ref="github:compare/structural-only",
        )
    )

    with pytest.raises(GitHubFactoryError, match="GitHubIntegrationEvidence"):
        GitHubFactoryAdapter(integration_evidence_port=port).bind(
            _repository(),
            _observation(
                pull_request=_merged_pr(),
                default_branch_sha=DESCENDANT_SHA,
                default_branch_ancestor_shas=(MAIN_SHA, MERGE_SHA),
            ),
        )


def test_merged_pr_requires_historical_base_in_default_branch_history() -> None:
    merged_pr = _merged_pr(base_sha=DESCENDANT_SHA)

    with pytest.raises(GitHubFactoryError, match="base sha is not contained"):
        GitHubFactoryAdapter().bind(
            _repository(),
            _observation(
                pull_request=merged_pr,
                default_branch_sha=MERGE_SHA,
                default_branch_ancestor_shas=(MAIN_SHA,),
            ),
        )


def test_merged_pr_requires_merge_commit_in_default_branch_history() -> None:
    merged_pr = _merged_pr()

    with pytest.raises(GitHubFactoryError, match="merge sha is not contained"):
        GitHubFactoryAdapter().bind(
            _repository(),
            _observation(
                pull_request=merged_pr,
                default_branch_sha=DESCENDANT_SHA,
                default_branch_ancestor_shas=(MAIN_SHA,),
            ),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("issue", {"number": 553, "state": "open"}, "GitHubIssueRef"),
        (
            "pull_request",
            SimpleNamespace(
                number=720,
                head_branch="automation/dev04",
                head_sha=CANDIDATE_SHA,
                base_branch="main",
                base_sha=MAIN_SHA,
                state=PullRequestState.MERGED,
                merge_sha=MERGE_SHA,
            ),
            "GitHubPullRequest",
        ),
    ],
)
def test_nested_github_authority_requires_canonical_types(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(GitHubFactoryError, match=message):
        _observation(**{field: value})


def test_credentials_remain_opaque_repository_metadata() -> None:
    repository = _repository(credential_ref="credref:factory-github")
    binding = GitHubFactoryAdapter().bind(repository, _observation())

    assert repository.credential_ref == "credref:factory-github"
    assert "credential" not in repr(binding).casefold()
    assert "credref" not in repr(binding).casefold()


def test_non_github_repository_is_rejected() -> None:
    with pytest.raises(GitHubFactoryError, match="provider must be github"):
        GitHubFactoryAdapter().bind(_repository(provider="gitlab"), _observation())


@pytest.mark.parametrize("duplicate_field", ["check_id", "evidence_ref"])
def test_duplicate_check_provenance_fails_closed(duplicate_field: str) -> None:
    first, second = _observation().checks
    values = asdict(second)
    values[duplicate_field] = getattr(first, duplicate_field)
    values["state"] = CheckState(values["state"])

    with pytest.raises(GitHubFactoryError, match="must be unique"):
        _observation(checks=(first, GitHubCheck(**values)))


def test_restart_replay_preserves_distinct_check_identity_and_provenance() -> None:
    saved = json.dumps([asdict(check) for check in _observation().checks])
    restored = tuple(
        GitHubCheck(**{**item, "state": CheckState(item["state"])})
        for item in json.loads(saved)
    )

    binding = GitHubFactoryAdapter().bind(_repository(), _observation(checks=restored))

    assert tuple(item.check_id for item in binding.check_evidence) == (
        "github:core",
        "github:factory",
    )
    assert tuple(item.evidence_ref for item in binding.check_evidence) == (
        "actions:run/core",
        "actions:run/factory",
    )


def test_missing_or_substituted_required_check_cannot_project_pass() -> None:
    binding = GitHubFactoryAdapter().bind(
        _repository(),
        _observation(checks=(_observation().checks[0],)),
    )

    result = classify_candidate_verification(
        CANDIDATE_SHA,
        binding.check_evidence,
        required_check_ids=PRODUCT_FACTORY_REQUIRED_CHECK_IDS,
    )

    assert result.state is VerificationState.UNKNOWN
    assert result.merge_clearance is False


def test_provider_labels_cannot_impersonate_canonical_required_gates() -> None:
    binding = GitHubFactoryAdapter().bind(_repository(), _observation())

    assert tuple(item.check_id for item in binding.check_evidence) == (
        "github:core",
        "github:factory",
    )
    result = classify_candidate_verification(
        CANDIDATE_SHA,
        binding.check_evidence,
        required_check_ids=PRODUCT_FACTORY_REQUIRED_CHECK_IDS,
    )

    assert result.state is VerificationState.UNKNOWN
    assert result.merge_clearance is False


def test_top_level_observation_requires_canonical_runtime_authority() -> None:
    merged_pr = GitHubPullRequest(
        number=720,
        head_branch="automation/dev04",
        head_sha=CANDIDATE_SHA,
        base_branch="main",
        base_sha="4" * 40,
        state=PullRequestState.MERGED,
        merge_sha=MERGE_SHA,
    )
    canonical = _observation(
        pull_request=merged_pr,
        default_branch_ancestor_shas=(MERGE_SHA,),
    )
    structural_observation = SimpleNamespace(
        owner=canonical.owner,
        name=canonical.name,
        default_branch=canonical.default_branch,
        default_branch_sha=canonical.default_branch_sha,
        issue=canonical.issue,
        candidate_branch=canonical.candidate_branch,
        candidate_sha=canonical.candidate_sha,
        pull_request=canonical.pull_request,
        checks=canonical.checks,
        default_branch_ancestor_shas=canonical.default_branch_ancestor_shas,
    )

    with pytest.raises(GitHubFactoryError, match="GitHubRepositoryObservation"):
        GitHubFactoryAdapter().bind(
            _repository(), structural_observation  # type: ignore[arg-type]
        )


def test_top_level_repository_requires_canonical_runtime_authority() -> None:
    structural_repository = SimpleNamespace(
        repository_id="forged-core",
        provider="github",
        locator="https://github.com/Oleksii-debug/Nika-Core.git",
        default_branch="main",
        credential_ref=None,
    )

    with pytest.raises(GitHubFactoryError, match="RepositoryRef"):
        GitHubFactoryAdapter().bind(  # type: ignore[arg-type]
            structural_repository, _observation()
        )
