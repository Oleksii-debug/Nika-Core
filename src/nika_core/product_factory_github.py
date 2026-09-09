from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from nika_core.product_factory_orchestration import RepositoryRef
from nika_core.product_factory_verification import CheckState as VerificationCheckState
from nika_core.product_factory_verification import ExactShaCheckEvidence


class GitHubFactoryError(ValueError):
    """Raised when GitHub Factory evidence is incomplete or inconsistent."""


class PullRequestState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class CheckState(StrEnum):
    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class GitHubIssueRef:
    number: int
    state: str

    def __post_init__(self) -> None:
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number < 1:
            raise GitHubFactoryError("issue number must be a positive integer")
        if not isinstance(self.state, str) or self.state not in {"open", "closed"}:
            raise GitHubFactoryError("issue state must be open or closed")


@dataclass(frozen=True, slots=True)
class GitHubCheck:
    check_id: str
    name: str
    head_sha: str
    state: CheckState
    evidence_ref: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.check_id, str)
            or not self.check_id.strip()
            or not isinstance(self.name, str)
            or not self.name.strip()
            or not isinstance(self.evidence_ref, str)
            or not self.evidence_ref.strip()
        ):
            raise GitHubFactoryError("check identity and evidence reference must not be empty")
        _validate_sha(self.head_sha, "check head_sha")
        if not isinstance(self.state, CheckState):
            raise GitHubFactoryError("check state must be a recognized CheckState")
        try:
            ExactShaCheckEvidence(
                check_id=self.check_id,
                candidate_sha=self.head_sha,
                state=_verification_check_state(self.state),
                evidence_ref=self.evidence_ref,
                required=False,
            )
        except ValueError as exc:
            raise GitHubFactoryError(f"invalid check evidence: {exc}") from exc


@dataclass(frozen=True, slots=True)
class GitHubPullRequest:
    number: int
    head_branch: str
    head_sha: str
    base_branch: str
    base_sha: str
    state: PullRequestState
    merge_sha: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number < 1:
            raise GitHubFactoryError("pull request number must be a positive integer")
        if (
            not isinstance(self.head_branch, str)
            or not self.head_branch.strip()
            or not isinstance(self.base_branch, str)
            or not self.base_branch.strip()
        ):
            raise GitHubFactoryError("pull request branches must not be empty")
        _validate_sha(self.head_sha, "pull request head_sha")
        _validate_sha(self.base_sha, "pull request base_sha")
        if not isinstance(self.state, PullRequestState):
            raise GitHubFactoryError("pull request state must be a recognized PullRequestState")
        if self.state is PullRequestState.MERGED:
            if self.merge_sha is None:
                raise GitHubFactoryError("merged pull request requires merge_sha")
            _validate_sha(self.merge_sha, "pull request merge_sha")
        elif self.merge_sha is not None:
            raise GitHubFactoryError("unmerged pull request must not carry merge_sha")


@dataclass(frozen=True, slots=True)
class GitHubRepositoryObservation:
    owner: str
    name: str
    default_branch: str
    default_branch_sha: str
    issue: GitHubIssueRef | None = None
    candidate_branch: str | None = None
    candidate_sha: str | None = None
    pull_request: GitHubPullRequest | None = None
    checks: tuple[GitHubCheck, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.owner, str)
            or not self.owner.strip()
            or not isinstance(self.name, str)
            or not self.name.strip()
            or not isinstance(self.default_branch, str)
            or not self.default_branch.strip()
        ):
            raise GitHubFactoryError("repository identity must not be empty")
        _validate_sha(self.default_branch_sha, "default branch sha")
        if (self.candidate_branch is None) != (self.candidate_sha is None):
            raise GitHubFactoryError("candidate branch and sha must be present together")
        if self.candidate_branch is not None:
            if not isinstance(self.candidate_branch, str) or not self.candidate_branch.strip():
                raise GitHubFactoryError("candidate branch must not be empty")
            _validate_sha(self.candidate_sha or "", "candidate sha")
        if not isinstance(self.checks, tuple) or any(
            not isinstance(check, GitHubCheck) for check in self.checks
        ):
            raise GitHubFactoryError("checks must contain GitHubCheck evidence")
        check_ids = [check.check_id for check in self.checks]
        if len(check_ids) != len(set(check_ids)):
            raise GitHubFactoryError("check ids must be unique")
        evidence_refs = [check.evidence_ref for check in self.checks]
        if len(evidence_refs) != len(set(evidence_refs)):
            raise GitHubFactoryError("check evidence refs must be unique")


@dataclass(frozen=True, slots=True)
class GitHubFactoryBinding:
    repository_id: str
    repository_full_name: str
    default_branch: str
    default_branch_sha: str
    issue_number: int | None
    candidate_branch: str | None
    candidate_sha: str | None
    pull_request_number: int | None
    pull_request_state: PullRequestState | None
    check_evidence: tuple[ExactShaCheckEvidence, ...]
    checks_state: CheckState | None
    integrated: bool
    integration_sha: str | None


class GitHubFactoryAdapter:
    """Bind live GitHub observations to an existing ProductRepositoryGraph reference."""

    def bind(
        self,
        repository: RepositoryRef,
        observation: GitHubRepositoryObservation,
    ) -> GitHubFactoryBinding:
        if repository.provider.strip().casefold() != "github":
            raise GitHubFactoryError("repository provider must be github")
        expected = _normalize_full_name(repository.locator)
        observed = _normalize_full_name(f"{observation.owner}/{observation.name}")
        if expected != observed:
            raise GitHubFactoryError("GitHub observation does not match repository locator")
        if repository.default_branch != observation.default_branch:
            raise GitHubFactoryError("GitHub observation does not match default branch")

        pr = observation.pull_request
        candidate_sha = observation.candidate_sha
        candidate_branch = observation.candidate_branch
        if pr is not None:
            if candidate_sha is None or candidate_branch is None:
                raise GitHubFactoryError("pull request requires explicit candidate identity")
            if pr.head_sha != candidate_sha or pr.head_branch != candidate_branch:
                raise GitHubFactoryError("pull request head does not match candidate identity")
            if pr.base_branch != observation.default_branch:
                raise GitHubFactoryError("pull request base does not match default branch")
            if pr.base_sha != observation.default_branch_sha:
                raise GitHubFactoryError("pull request base sha does not match default branch sha")

        checks_state: CheckState | None = None
        check_evidence: tuple[ExactShaCheckEvidence, ...] = ()
        if observation.checks:
            if candidate_sha is None:
                raise GitHubFactoryError("checks require explicit candidate identity")
            if any(check.head_sha != candidate_sha for check in observation.checks):
                raise GitHubFactoryError("checks are not bound to exact candidate sha")
            states = {check.state for check in observation.checks}
            if CheckState.FAIL in states:
                checks_state = CheckState.FAIL
            elif CheckState.PENDING in states:
                checks_state = CheckState.PENDING
            elif states == {CheckState.PASS}:
                checks_state = CheckState.PASS
            else:
                raise GitHubFactoryError("checks contain an unrecognized state")
            check_evidence = tuple(
                ExactShaCheckEvidence(
                    check_id=check.check_id,
                    candidate_sha=check.head_sha,
                    state=_verification_check_state(check.state),
                    evidence_ref=check.evidence_ref,
                    required=False,
                )
                for check in observation.checks
            )

        integrated = pr is not None and pr.state is PullRequestState.MERGED
        integration_sha = pr.merge_sha if integrated else None
        return GitHubFactoryBinding(
            repository_id=repository.repository_id,
            repository_full_name=observed,
            default_branch=observation.default_branch,
            default_branch_sha=observation.default_branch_sha,
            issue_number=observation.issue.number if observation.issue is not None else None,
            candidate_branch=candidate_branch,
            candidate_sha=candidate_sha,
            pull_request_number=pr.number if pr is not None else None,
            pull_request_state=pr.state if pr is not None else None,
            check_evidence=check_evidence,
            checks_state=checks_state,
            integrated=integrated,
            integration_sha=integration_sha,
        )


def _verification_check_state(state: CheckState) -> VerificationCheckState:
    return {
        CheckState.PENDING: VerificationCheckState.RUNNING,
        CheckState.PASS: VerificationCheckState.PASS,
        CheckState.FAIL: VerificationCheckState.FAIL,
    }[state]


def _normalize_full_name(locator: str) -> str:
    if not isinstance(locator, str):
        raise GitHubFactoryError("GitHub repository locator must be text")
    value = locator.strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if value.casefold().startswith(prefix.casefold()):
            value = value[len(prefix) :]
            break
    value = value.removesuffix(".git")
    parts = value.split("/")
    if len(parts) != 2 or not all(part.strip() for part in parts):
        raise GitHubFactoryError("GitHub repository locator must identify owner/repository")
    return f"{parts[0].casefold()}/{parts[1].casefold()}"


def _validate_sha(value: str, label: str) -> None:
    if not isinstance(value, str) or len(value) != 40 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise GitHubFactoryError(f"{label} must be an exact lowercase 40-character git sha")
