from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from nika_core.product_command.reference_safety import safe_evidence_reference
from nika_core.product_factory_orchestration import RepositoryRef
from nika_core.product_factory_verification import (
    MAX_EVIDENCE_REF_LENGTH,
    CheckState as VerificationCheckState,
    ExactShaCheckEvidence,
)

_PROVIDER_CHECK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_OPAQUE_EVIDENCE_REF_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}:[A-Za-z0-9][A-Za-z0-9._/:-]{0,446}$"
)
_SENSITIVE_EVIDENCE_NAMESPACES = (
    "api-key:",
    "apikey:",
    "cookie:",
    "proxy-authorization:",
    "set-cookie:",
    "x-api-key:",
    "x-auth-token:",
)


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
        _require_positive_int(self.number, "issue number")
        _require_plain_nonempty_text(self.state, "issue state")
        if self.state not in {"open", "closed"}:
            raise GitHubFactoryError("issue state must be open or closed")


@dataclass(frozen=True, slots=True)
class GitHubCheck:
    check_id: str
    name: str
    head_sha: str
    state: CheckState
    evidence_ref: str

    def __post_init__(self) -> None:
        _require_provider_check_id(self.check_id)
        _require_plain_nonempty_text(self.name, "check name")
        _require_opaque_evidence_ref(self.evidence_ref, "check evidence reference")
        _validate_sha(self.head_sha, "check head_sha")
        if type(self.state) is not CheckState:
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
    head_repository_full_name: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.number, "pull request number")
        _require_plain_nonempty_text(self.head_branch, "pull request head branch")
        _require_plain_nonempty_text(self.base_branch, "pull request base branch")
        if self.head_repository_full_name is not None:
            _normalize_full_name(self.head_repository_full_name)
        _validate_sha(self.head_sha, "pull request head_sha")
        _validate_sha(self.base_sha, "pull request base_sha")
        if type(self.state) is not PullRequestState:
            raise GitHubFactoryError("pull request state must be a recognized PullRequestState")
        if self.state is PullRequestState.MERGED:
            if self.merge_sha is None:
                raise GitHubFactoryError("merged pull request requires merge_sha")
            _validate_sha(self.merge_sha, "pull request merge_sha")
        elif self.merge_sha is not None:
            raise GitHubFactoryError("unmerged pull request must not carry merge_sha")


@dataclass(frozen=True, slots=True)
class GitHubIntegrationEvidence:
    """Producer-native proof binding one frozen candidate to one integration identity."""

    pull_request_number: int
    candidate_sha: str
    integration_sha: str
    evidence_ref: str
    candidate_repository_full_name: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.pull_request_number, "integration pull request number")
        _validate_sha(self.candidate_sha, "integration candidate sha")
        _validate_sha(self.integration_sha, "integration sha")
        _require_opaque_evidence_ref(self.evidence_ref, "integration evidence reference")
        if self.candidate_repository_full_name is not None:
            _normalize_full_name(self.candidate_repository_full_name)


class GitHubIntegrationEvidencePort(Protocol):
    """Trusted provider seam that proves candidate-to-integration identity."""

    def prove_integration(
        self,
        *,
        repository_full_name: str,
        pull_request_number: int,
        candidate_sha: str,
        integration_sha: str,
    ) -> GitHubIntegrationEvidence:
        """Return producer-native evidence for the exact requested relation."""


@dataclass(frozen=True, slots=True)
class GitHubRepositoryObservation:
    owner: str
    name: str
    default_branch: str
    default_branch_sha: str
    issue: GitHubIssueRef | None = None
    candidate_branch: str | None = None
    candidate_sha: str | None = None
    candidate_repository_full_name: str | None = None
    pull_request: GitHubPullRequest | None = None
    checks: tuple[GitHubCheck, ...] = ()
    default_branch_ancestor_shas: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_plain_nonempty_text(self.owner, "repository owner")
        _require_plain_nonempty_text(self.name, "repository name")
        _require_plain_nonempty_text(self.default_branch, "default branch")
        _validate_sha(self.default_branch_sha, "default branch sha")
        if type(self.default_branch_ancestor_shas) is not tuple:
            raise GitHubFactoryError("default branch ancestor shas must be a tuple")
        for ancestor_sha in self.default_branch_ancestor_shas:
            _validate_sha(ancestor_sha, "default branch ancestor sha")
        if len(self.default_branch_ancestor_shas) != len(set(self.default_branch_ancestor_shas)):
            raise GitHubFactoryError("default branch ancestor shas must be unique")
        if self.issue is not None:
            if type(self.issue) is not GitHubIssueRef:
                raise GitHubFactoryError("issue must be GitHubIssueRef evidence")
            self.issue.__post_init__()
        if self.pull_request is not None:
            if type(self.pull_request) is not GitHubPullRequest:
                raise GitHubFactoryError("pull request must be GitHubPullRequest evidence")
            self.pull_request.__post_init__()
        if (self.candidate_branch is None) != (self.candidate_sha is None):
            raise GitHubFactoryError("candidate branch and sha must be present together")
        if self.candidate_repository_full_name is not None and self.candidate_branch is None:
            raise GitHubFactoryError("candidate repository requires candidate branch and sha")
        if self.candidate_branch is not None:
            _require_plain_nonempty_text(self.candidate_branch, "candidate branch")
            _validate_sha(self.candidate_sha, "candidate sha")
            if self.candidate_repository_full_name is not None:
                _normalize_full_name(self.candidate_repository_full_name)
        if type(self.checks) is not tuple:
            raise GitHubFactoryError("checks must contain GitHubCheck evidence")
        for check in self.checks:
            if type(check) is not GitHubCheck:
                raise GitHubFactoryError("checks must contain GitHubCheck evidence")
            check.__post_init__()
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
    default_branch_ancestor_shas: tuple[str, ...]
    issue_number: int | None
    candidate_repository_full_name: str | None
    candidate_branch: str | None
    candidate_sha: str | None
    pull_request_number: int | None
    pull_request_state: PullRequestState | None
    check_evidence: tuple[ExactShaCheckEvidence, ...]
    checks_state: CheckState | None
    integrated: bool
    integration_sha: str | None
    integration_evidence_ref: str | None


class GitHubFactoryAdapter:
    """Bind live GitHub observations to an existing ProductRepositoryGraph reference."""

    def __init__(
        self,
        *,
        integration_evidence_port: GitHubIntegrationEvidencePort | None = None,
    ) -> None:
        self._integration_evidence_port = integration_evidence_port

    def bind(
        self,
        repository: RepositoryRef,
        observation: GitHubRepositoryObservation,
    ) -> GitHubFactoryBinding:
        if type(repository) is not RepositoryRef:
            raise GitHubFactoryError("repository must be canonical RepositoryRef authority")
        if type(observation) is not GitHubRepositoryObservation:
            raise GitHubFactoryError("observation must be canonical GitHubRepositoryObservation authority")

        _require_plain_nonempty_text(repository.repository_id, "repository id")
        _require_plain_nonempty_text(repository.provider, "repository provider")
        _require_plain_nonempty_text(repository.locator, "repository locator")
        _require_plain_nonempty_text(repository.default_branch, "repository default branch")
        observation.__post_init__()

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
        candidate_repository_full_name: str | None = None
        if candidate_sha is not None:
            candidate_repository_full_name = (
                _normalize_full_name(observation.candidate_repository_full_name)
                if observation.candidate_repository_full_name is not None
                else observed
            )
            if (
                candidate_repository_full_name == observed
                and candidate_branch == observation.default_branch
            ):
                raise GitHubFactoryError(
                    "target default branch cannot be used as isolated candidate ref"
                )

        integration_evidence: GitHubIntegrationEvidence | None = None
        if pr is not None:
            if (
                candidate_sha is None
                or candidate_branch is None
                or candidate_repository_full_name is None
            ):
                raise GitHubFactoryError("pull request requires explicit candidate identity")
            pr_head_repository_full_name = (
                _normalize_full_name(pr.head_repository_full_name)
                if pr.head_repository_full_name is not None
                else observed
            )
            if pr_head_repository_full_name != candidate_repository_full_name:
                raise GitHubFactoryError(
                    "pull request head repository does not match candidate repository identity"
                )
            if pr.head_sha != candidate_sha or pr.head_branch != candidate_branch:
                raise GitHubFactoryError("pull request head does not match candidate identity")
            if pr.base_branch != observation.default_branch:
                raise GitHubFactoryError("pull request base does not match default branch")
            if pr.state is PullRequestState.MERGED:
                default_branch_history = {
                    observation.default_branch_sha,
                    *observation.default_branch_ancestor_shas,
                }
                if pr.base_sha not in default_branch_history:
                    raise GitHubFactoryError(
                        "pull request base sha is not contained in default branch history"
                    )
                if pr.merge_sha not in default_branch_history:
                    raise GitHubFactoryError(
                        "pull request merge sha is not contained in default branch history"
                    )
                if pr.merge_sha == pr.base_sha:
                    raise GitHubFactoryError("pull request merge sha must differ from base sha")
                if self._integration_evidence_port is None:
                    raise GitHubFactoryError(
                        "merged pull request requires trusted candidate integration evidence"
                    )
                integration_evidence = self._integration_evidence_port.prove_integration(
                    repository_full_name=observed,
                    pull_request_number=pr.number,
                    candidate_sha=candidate_sha,
                    integration_sha=pr.merge_sha,
                )
                if type(integration_evidence) is not GitHubIntegrationEvidence:
                    raise GitHubFactoryError(
                        "integration evidence port must return GitHubIntegrationEvidence"
                    )
                integration_evidence.__post_init__()
                if integration_evidence.pull_request_number != pr.number:
                    raise GitHubFactoryError("integration evidence pull request does not match")
                if integration_evidence.candidate_sha != candidate_sha:
                    raise GitHubFactoryError("integration evidence candidate sha does not match")
                integration_candidate_repository = (
                    _normalize_full_name(integration_evidence.candidate_repository_full_name)
                    if integration_evidence.candidate_repository_full_name is not None
                    else observed
                )
                if integration_candidate_repository != candidate_repository_full_name:
                    raise GitHubFactoryError(
                        "integration evidence candidate repository does not match"
                    )
                if integration_evidence.integration_sha != pr.merge_sha:
                    raise GitHubFactoryError("integration evidence sha does not match pull request")
            elif pr.base_sha != observation.default_branch_sha:
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
                    check_id=_provider_check_id(check.check_id),
                    candidate_sha=check.head_sha,
                    state=_verification_check_state(check.state),
                    evidence_ref=check.evidence_ref,
                    required=False,
                )
                for check in observation.checks
            )

        integrated = integration_evidence is not None
        integration_sha = integration_evidence.integration_sha if integrated else None
        integration_evidence_ref = integration_evidence.evidence_ref if integrated else None
        return GitHubFactoryBinding(
            repository_id=repository.repository_id,
            repository_full_name=observed,
            default_branch=observation.default_branch,
            default_branch_sha=observation.default_branch_sha,
            default_branch_ancestor_shas=observation.default_branch_ancestor_shas,
            issue_number=observation.issue.number if observation.issue is not None else None,
            candidate_repository_full_name=candidate_repository_full_name,
            candidate_branch=candidate_branch,
            candidate_sha=candidate_sha,
            pull_request_number=pr.number if pr is not None else None,
            pull_request_state=pr.state if pr is not None else None,
            check_evidence=check_evidence,
            checks_state=checks_state,
            integrated=integrated,
            integration_sha=integration_sha,
            integration_evidence_ref=integration_evidence_ref,
        )


def _provider_check_id(check_id: str) -> str:
    """Keep untrusted provider identity outside the canonical Product Factory gate namespace."""

    _require_provider_check_id(check_id)
    return f"github:{check_id}"


def _verification_check_state(state: CheckState) -> VerificationCheckState:
    return {
        CheckState.PENDING: VerificationCheckState.RUNNING,
        CheckState.PASS: VerificationCheckState.PASS,
        CheckState.FAIL: VerificationCheckState.FAIL,
    }[state]


def _require_plain_nonempty_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise GitHubFactoryError(f"{label} must be exact non-empty text")
    return value


def _require_provider_check_id(value: object) -> str:
    _require_plain_nonempty_text(value, "check id")
    if not _PROVIDER_CHECK_ID_RE.fullmatch(value):
        raise GitHubFactoryError("check id must be a bounded canonical machine identifier")
    return value


def _require_opaque_evidence_ref(value: object, label: str) -> str:
    _require_plain_nonempty_text(value, label)
    if len(value) > MAX_EVIDENCE_REF_LENGTH:
        raise GitHubFactoryError(f"{label} exceeds maximum length")
    normalized = value.casefold()
    if normalized.startswith(_SENSITIVE_EVIDENCE_NAMESPACES):
        raise GitHubFactoryError(f"{label} must not contain credential material")
    if safe_evidence_reference(value) != value:
        raise GitHubFactoryError(f"{label} must not contain credential material")
    if not _OPAQUE_EVIDENCE_REF_RE.fullmatch(value):
        raise GitHubFactoryError(f"{label} must be a bounded opaque reference")
    return value


def _require_positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise GitHubFactoryError(f"{label} must be a positive integer")
    return value


def _normalize_full_name(locator: str) -> str:
    _require_plain_nonempty_text(locator, "GitHub repository locator")
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


def _validate_sha(value: object, label: str) -> None:
    if type(value) is not str or len(value) != 40 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise GitHubFactoryError(f"{label} must be an exact lowercase 40-character git sha")
