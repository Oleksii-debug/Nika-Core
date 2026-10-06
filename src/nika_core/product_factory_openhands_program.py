from __future__ import annotations

import hashlib
import os
import pathlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import httpx

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_coding_program import (
    build_product_factory_coding_program_host,
)
from nika_core.product_factory_coding_worker_adapter import (
    CodingWorkerDispatchContext,
    CodingWorkerExecutionEvidence,
    RepositoryPathIdentity,
)
from nika_core.product_factory_coordinator import ComponentWorkRequest
from nika_core.product_factory_openhands_recovery import (
    ProductFactoryOpenHandsRecoveryProbe,
)
from nika_core.product_factory_orchestration import OwnershipLease
from nika_core.product_factory_program_host import ProductFactoryProgramHost
from nika_core.product_factory_review_authority import ProductFactoryReviewAuthorityPort
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.toolsmith.contracts import (
    ChangedFile,
    CodingJob,
    CodingResult,
    IsolationClass,
    NetworkMode,
    NetworkPolicy,
    ProcessPolicy,
    ResourceBudget,
    WorkspaceLease,
)
from nika_core.toolsmith.execution import (
    _git,
    _resolve_host_git_executable,
    cleanup_private_git_workspace,
    prepare_private_git_workspace,
)
from nika_core.toolsmith.openhands_remote_worker import (
    OpenHandsRemoteCodingWorker,
    OpenHandsSandboxEndpoint,
    OpenHandsSandboxProviderPort,
    SandboxedAcceptanceRuntimePort,
)
from nika_core.toolsmith.openhands_sdk_runtime import OpenHandsAgentServerRuntime
from nika_core.toolsmith.workspace_security import (
    SterileGitPlan,
    TreeEvidence,
    WorkspacePathPolicy,
    WorkspaceSecurityError,
    collect_tree_delta_evidence,
    collect_tree_evidence,
    ensure_real_directory_root,
    make_sterile_git_plan,
    sterile_git_environment,
)


class OpenHandsProductFactoryError(RuntimeError):
    """Raised when the trusted OpenHands Product Factory host cannot prove authority."""


@dataclass(frozen=True, slots=True)
class OpenHandsProductFactoryPolicy:
    """Explicit host-owned policy for remote Product Factory coding.

    Repository roots, network hosts, executable identities and resource budgets are
    supplied by trusted composition. They are never inferred from ProductProject prose
    or from an external coding engine.
    """

    allowed_executables: tuple[str, ...]
    approved_hosts: tuple[str, ...]
    resource_budget: ResourceBudget
    lease_seconds: int = 3600
    producer_actor_id: str = "openhands-remote-coding-worker"

    def __post_init__(self) -> None:
        if type(self.allowed_executables) is not tuple or not self.allowed_executables:
            raise ValueError("OpenHands Product Factory requires pinned executables")
        for item in self.allowed_executables:
            if (
                type(item) is not str
                or not item
                or item != item.strip()
                or "\x00" in item
            ):
                raise ValueError("OpenHands executable identities must be canonical text")
        ProcessPolicy(self.allowed_executables).__post_init__()
        if len({item.casefold() for item in self.allowed_executables}) != len(
            self.allowed_executables
        ):
            raise ValueError("OpenHands executable identities must be unique")

        if type(self.approved_hosts) is not tuple or not self.approved_hosts:
            raise ValueError("OpenHands Product Factory requires approved network hosts")
        canonical_hosts = tuple(_canonical_host(item) for item in self.approved_hosts)
        if canonical_hosts != self.approved_hosts:
            raise ValueError("OpenHands approved hosts must already be canonical")
        if len(set(canonical_hosts)) != len(canonical_hosts):
            raise ValueError("OpenHands approved hosts must be unique")
        NetworkPolicy(NetworkMode.APPROVED_HOSTS, canonical_hosts).__post_init__()

        if type(self.resource_budget) is not ResourceBudget:
            raise ValueError("OpenHands resource budget carrier is invalid")
        self.resource_budget.__post_init__()
        if type(self.lease_seconds) is not int or not 1 <= self.lease_seconds <= 3600:
            raise ValueError("OpenHands lease duration must be 1..3600 seconds")
        _canonical_text(
            self.producer_actor_id,
            "OpenHands producer actor identity",
            max_bytes=256,
        )


@dataclass(slots=True)
class OpenHandsProductFactoryPorts:
    """Trusted repository staging plus exact candidate-evidence authority.

    External OpenHands code never sees production Git metadata. Each dispatch starts
    from a fresh private clone whose remotes are removed and whose worker-visible
    worktree contains no '.git' entry. A restart may reconstruct the same base
    checkout; a dirty or unsafe prior checkout is removed only through the incumbent
    guarded cleanup routine.

    The remote worker validates and applies returned bytes in this private worktree.
    This port independently re-computes the bounded tree delta and creates a private
    candidate commit for Product Factory review. It never pushes or mutates the
    configured production repository.
    """

    workspace_parent: pathlib.Path
    repositories: Mapping[str, pathlib.Path]
    policy: OpenHandsProductFactoryPolicy
    git_executable: str = "git"
    source_environment: Mapping[str, str] | None = None
    _baselines: dict[str, tuple[str, TreeEvidence]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.workspace_parent = ensure_real_directory_root(
            pathlib.Path(self.workspace_parent),
            label="OpenHands Product Factory workspace parent",
        )
        if type(self.policy) is not OpenHandsProductFactoryPolicy:
            raise OpenHandsProductFactoryError("OpenHands Product Factory policy is invalid")
        self.policy.__post_init__()

        copied: dict[str, pathlib.Path] = {}
        for repository_id, value in self.repositories.items():
            identity = _canonical_text(
                repository_id,
                "Product Factory repository identity",
                max_bytes=512,
            )
            root = ensure_real_directory_root(
                pathlib.Path(value),
                label=f"Product Factory repository root {identity}",
            )
            if not (root / ".git").exists():
                raise OpenHandsProductFactoryError(
                    "configured Product Factory repository lacks trusted Git metadata"
                )
            if _paths_overlap(root, self.workspace_parent):
                raise OpenHandsProductFactoryError(
                    "Product Factory repository and OpenHands workspace must be disjoint"
                )
            copied[identity] = root
        if not copied:
            raise OpenHandsProductFactoryError(
                "OpenHands Product Factory requires at least one trusted repository"
            )
        self.repositories = MappingProxyType(copied)
        self.git_executable = _resolve_host_git_executable(self.git_executable)
        self.source_environment = MappingProxyType(
            sterile_git_environment(self.source_environment)
        )

    async def context_for(
        self,
        request: ComponentWorkRequest,
    ) -> CodingWorkerDispatchContext:
        """Prepare one exact private base checkout and return explicit worker authority."""

        if type(request) is not ComponentWorkRequest:
            raise OpenHandsProductFactoryError("component work request carrier is invalid")
        self.policy.__post_init__()
        try:
            plan, baseline = self._fresh_private_base(request)
        except OpenHandsProductFactoryError:
            raise
        except Exception as exc:
            raise OpenHandsProductFactoryError(
                "OpenHands private repository staging could not be proven"
            ) from exc

        self._baselines[request.work_id] = (request.base_sha.casefold(), baseline)
        expiry = datetime.now(UTC) + timedelta(seconds=self.policy.lease_seconds)
        return CodingWorkerDispatchContext(
            repository_tree_digest=baseline.digest,
            ownership_lease=OwnershipLease(
                lease_id=f"openhands-assignment:{request.work_id}",
                worker_id=self.policy.producer_actor_id,
                component_ids=(request.component_id,),
                allowed_paths=request.allowed_paths,
            ),
            lease=WorkspaceLease(
                lease_id=f"openhands-staging:{request.work_id}",
                workspace_root=plan.worktree_root,
                isolation_class=IsolationClass.PROCESS_CONTAINED,
                expires_at=expiry.isoformat(),
            ),
            process_policy=ProcessPolicy(self.policy.allowed_executables),
            network_policy=NetworkPolicy(
                NetworkMode.APPROVED_HOSTS,
                self.policy.approved_hosts,
            ),
            resource_budget=ResourceBudget(
                self.policy.resource_budget.timeout_seconds,
                self.policy.resource_budget.max_output_bytes,
                self.policy.resource_budget.max_changed_files,
            ),
            path_identity=(
                RepositoryPathIdentity.CASE_INSENSITIVE
                if os.name == "nt"
                else RepositoryPathIdentity.CASE_SENSITIVE
            ),
        )

    async def collect(
        self,
        request: ComponentWorkRequest,
        job: CodingJob,
        result: CodingResult,
    ) -> CodingWorkerExecutionEvidence:
        """Create exact private candidate evidence from already validated returned bytes."""

        if type(request) is not ComponentWorkRequest:
            raise OpenHandsProductFactoryError("component work request carrier is invalid")
        if type(job) is not CodingJob or type(result) is not CodingResult:
            raise OpenHandsProductFactoryError("coding evidence carriers are invalid")
        if result.job_id != request.work_id or job.job_id != request.work_id:
            raise OpenHandsProductFactoryError("coding evidence work identity changed")
        if (
            job.repository.repository_id != request.repository_id
            or job.repository.base_sha.casefold() != request.base_sha.casefold()
        ):
            raise OpenHandsProductFactoryError("coding evidence repository identity changed")

        baseline_record = self._baselines.get(request.work_id)
        if baseline_record is None or baseline_record[0] != request.base_sha.casefold():
            raise OpenHandsProductFactoryError(
                "OpenHands base-tree evidence is unavailable; reconcile the work item"
            )

        plan = self._plan_for(request)
        try:
            self._require_private_base(plan, request.base_sha)
            baseline = baseline_record[1]
            after = collect_tree_evidence(plan.worktree_root)
            delta = collect_tree_delta_evidence(
                baseline,
                after,
                path_policy=WorkspacePathPolicy(request.allowed_paths),
                max_changed_files=self.policy.resource_budget.max_changed_files,
            )
            self._require_result_matches_delta(result, delta, after)
            result_sha = self._commit_private_candidate(request, plan, delta, after)
        except OpenHandsProductFactoryError:
            raise
        except (OSError, ValueError, WorkspaceSecurityError) as exc:
            raise OpenHandsProductFactoryError(
                "OpenHands candidate evidence could not be proven"
            ) from exc

        return CodingWorkerExecutionEvidence(
            work_id=request.work_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha.casefold(),
            result_sha=result_sha,
            diff_digest=delta.digest,
        )

    def candidate_worktree(self, work_id: str) -> pathlib.Path:
        """Return the guarded private candidate worktree for independent review."""

        root = self.workspace_root_for(work_id)
        plan_root = ensure_real_directory_root(root, label="OpenHands Product Factory job root")
        worktree = ensure_real_directory_root(
            plan_root / "worktree",
            label="OpenHands private candidate worktree",
        )
        if (worktree / ".git").exists():
            raise OpenHandsProductFactoryError(
                "OpenHands private candidate unexpectedly exposes Git metadata"
            )
        return worktree

    def workspace_root_for(self, work_id: str) -> pathlib.Path:
        identity = _canonical_text(work_id, "Product Factory work identity", max_bytes=2048)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self.workspace_parent / f"job-{digest}"

    def _fresh_private_base(
        self,
        request: ComponentWorkRequest,
    ) -> tuple[SterileGitPlan, TreeEvidence]:
        plan = self._plan_for(request)
        job_root = plan.private_git_dir.parent
        if not job_root.exists():
            job_root.mkdir(parents=False, exist_ok=False)
        ensure_real_directory_root(job_root, label="OpenHands Product Factory job root")

        if plan.private_git_dir.exists() or plan.worktree_root.exists():
            cleanup_private_git_workspace(plan)
        remaining = tuple(job_root.iterdir())
        if remaining:
            raise OpenHandsProductFactoryError(
                "OpenHands Product Factory job root contains unexpected state"
            )

        prepared = prepare_private_git_workspace(
            plan,
            git_executable=self.git_executable,
        )
        if prepared.head.casefold() != request.base_sha.casefold():
            raise OpenHandsProductFactoryError(
                "OpenHands private checkout does not match the requested base SHA"
            )
        if prepared.remaining_remotes:
            raise OpenHandsProductFactoryError(
                "OpenHands private checkout retained a Git remote"
            )
        if (prepared.plan.worktree_root / ".git").exists():
            raise OpenHandsProductFactoryError(
                "OpenHands worker-visible checkout exposes Git metadata"
            )
        return prepared.plan, prepared.tree_evidence

    def _plan_for(self, request: ComponentWorkRequest) -> SterileGitPlan:
        repository_root = self.repositories.get(request.repository_id)
        if repository_root is None:
            raise OpenHandsProductFactoryError(
                "component repository is not explicitly authorized on this host"
            )
        job_root = self.workspace_root_for(request.work_id)
        if not job_root.exists():
            job_root.mkdir(parents=False, exist_ok=False)
        ensure_real_directory_root(job_root, label="OpenHands Product Factory job root")
        return make_sterile_git_plan(
            repository_root=repository_root,
            job_root=job_root,
            branch_name=_private_branch_name(request.work_id),
            base_sha=request.base_sha,
            source_environment=self.source_environment,
        )

    def _require_private_base(self, plan: SterileGitPlan, base_sha: str) -> None:
        ensure_real_directory_root(
            plan.private_git_dir,
            label="OpenHands private Git metadata",
        )
        ensure_real_directory_root(
            plan.worktree_root,
            label="OpenHands private candidate worktree",
        )
        if (plan.worktree_root / ".git").exists():
            raise OpenHandsProductFactoryError(
                "OpenHands worker-visible checkout exposes Git metadata"
            )
        prefix = self._git_prefix(plan)
        remotes = _lines(
            _git(
                (*prefix, "remote"),
                cwd=plan.private_git_dir.parent,
                environment=plan.environment,
            ).stdout
        )
        if remotes:
            raise OpenHandsProductFactoryError(
                "OpenHands private candidate unexpectedly has a Git remote"
            )
        head = _git(
            (*prefix, "rev-parse", "--verify", "HEAD^{commit}"),
            cwd=plan.private_git_dir.parent,
            environment=plan.environment,
        ).stdout.strip()
        if head.casefold() != base_sha.casefold():
            raise OpenHandsProductFactoryError(
                "OpenHands private candidate HEAD is no longer the requested base"
            )

    def _require_result_matches_delta(
        self,
        result: CodingResult,
        delta,
        after: TreeEvidence,
    ) -> None:
        after_by_path = {item.path: item for item in after.files}
        expected: list[ChangedFile] = []
        for change in delta.changes:
            if change.kind == "deleted":
                raise OpenHandsProductFactoryError(
                    "OpenHands candidate deletion is not admitted by the current worker"
                )
            current = after_by_path.get(change.path)
            if current is None:
                raise OpenHandsProductFactoryError(
                    "OpenHands candidate changed-file evidence is incomplete"
                )
            expected.append(
                ChangedFile(
                    path=current.path,
                    sha256=current.sha256,
                    size_bytes=current.size_bytes,
                )
            )
        if type(result.changed_files) is not tuple or result.changed_files != tuple(expected):
            raise OpenHandsProductFactoryError(
                "OpenHands worker changed-file evidence differs from the private tree"
            )

    def _commit_private_candidate(
        self,
        request: ComponentWorkRequest,
        plan: SterileGitPlan,
        delta,
        after: TreeEvidence,
    ) -> str:
        if not delta.changes:
            return request.base_sha.casefold()

        paths = tuple(change.path for change in delta.changes)
        prefix = self._git_prefix(plan)
        _git(
            (*prefix, "add", "-f", "-A", "--", *paths),
            cwd=plan.private_git_dir.parent,
            environment=plan.environment,
        )
        staged = _nul_paths(
            _git(
                (*prefix, "diff", "--cached", "--name-only", "--no-renames", "-z"),
                cwd=plan.private_git_dir.parent,
                environment=plan.environment,
            ).stdout
        )
        if _sorted_paths(staged) != _sorted_paths(paths):
            raise OpenHandsProductFactoryError(
                "private Git staging differs from validated candidate paths"
            )

        _git(
            (
                *prefix,
                "-c",
                "user.name=Nika Product Factory",
                "-c",
                "user.email=nika-product-factory@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--no-gpg-sign",
                "--no-verify",
                "-m",
                "Nika Product Factory private candidate",
            ),
            cwd=plan.private_git_dir.parent,
            environment=plan.environment,
        )
        result_sha = _git(
            (*prefix, "rev-parse", "--verify", "HEAD^{commit}"),
            cwd=plan.private_git_dir.parent,
            environment=plan.environment,
        ).stdout.strip().casefold()
        parent_sha = _git(
            (*prefix, "rev-parse", "--verify", "HEAD^1"),
            cwd=plan.private_git_dir.parent,
            environment=plan.environment,
        ).stdout.strip().casefold()
        if parent_sha != request.base_sha.casefold():
            raise OpenHandsProductFactoryError(
                "private candidate commit is not an exact child of the requested base"
            )

        committed_paths = _nul_paths(
            _git(
                (
                    *prefix,
                    "diff",
                    "--name-only",
                    "--no-renames",
                    "-z",
                    request.base_sha,
                    result_sha,
                    "--",
                ),
                cwd=plan.private_git_dir.parent,
                environment=plan.environment,
            ).stdout
        )
        if _sorted_paths(committed_paths) != _sorted_paths(paths):
            raise OpenHandsProductFactoryError(
                "private candidate commit differs from validated candidate paths"
            )
        status = _git(
            (
                *prefix,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ),
            cwd=plan.private_git_dir.parent,
            environment=plan.environment,
        ).stdout
        if status:
            raise OpenHandsProductFactoryError(
                "private candidate commit does not match validated worktree bytes"
            )
        for change in delta.changes:
            worktree_blob = _git(
                (
                    *prefix,
                    "hash-object",
                    "--no-filters",
                    str(plan.worktree_root / change.path),
                ),
                cwd=plan.private_git_dir.parent,
                environment=plan.environment,
            ).stdout.strip().casefold()
            committed_blob = _git(
                (
                    *prefix,
                    "rev-parse",
                    "--verify",
                    f"{result_sha}:{change.path}",
                ),
                cwd=plan.private_git_dir.parent,
                environment=plan.environment,
            ).stdout.strip().casefold()
            if worktree_blob != committed_blob:
                raise OpenHandsProductFactoryError(
                    "private candidate commit blob differs from validated worktree bytes"
                )
        tracked = _nul_paths(
            _git(
                (*prefix, "ls-files", "-z"),
                cwd=plan.private_git_dir.parent,
                environment=plan.environment,
            ).stdout
        )
        if _sorted_paths(tracked) != _sorted_paths(
            tuple(item.path for item in after.files)
        ):
            raise OpenHandsProductFactoryError(
                "private candidate commit does not bind the complete worker-visible tree"
            )
        if collect_tree_evidence(plan.worktree_root) != after:
            raise OpenHandsProductFactoryError(
                "private candidate worktree changed while commit evidence was created"
            )
        if _lines(
            _git(
                (*prefix, "remote"),
                cwd=plan.private_git_dir.parent,
                environment=plan.environment,
            ).stdout
        ):
            raise OpenHandsProductFactoryError(
                "private candidate gained a Git remote during evidence creation"
            )
        return result_sha

    def _git_prefix(self, plan: SterileGitPlan) -> tuple[str, ...]:
        return (
            self.git_executable,
            *plan.config_args,
            "--git-dir",
            str(plan.private_git_dir),
            "--work-tree",
            str(plan.worktree_root),
        )


@dataclass(frozen=True, slots=True)
class OpenHandsProductFactoryProgram:
    """Production composition retaining the authorities needed for recovery/review."""

    host: ProductFactoryProgramHost
    worker: OpenHandsRemoteCodingWorker
    runtime: OpenHandsAgentServerRuntime
    ports: OpenHandsProductFactoryPorts
    recovery: ProductFactoryOpenHandsRecoveryProbe


def build_openhands_product_factory_program(
    store: SQLiteStore,
    *,
    workspace_parent: pathlib.Path,
    repositories: Mapping[str, pathlib.Path],
    sandbox_provider: OpenHandsSandboxProviderPort,
    client_factory: Callable[[OpenHandsSandboxEndpoint], httpx.Client],
    agent_profile_id_factory: Callable[[CodingJob, OpenHandsSandboxEndpoint], str],
    acceptance_runtime: SandboxedAcceptanceRuntimePort,
    policy: OpenHandsProductFactoryPolicy,
    idempotency: IdempotencyLedger | None = None,
    review_evidence_authority: ProductFactoryReviewAuthorityPort | None = None,
    git_executable: str = "git",
    source_environment: Mapping[str, str] | None = None,
    max_iterations: int = 96,
    poll_interval_seconds: float = 0.2,
) -> OpenHandsProductFactoryProgram:
    """Compose the canonical Product Factory host with the remote OpenHands backend.

    Authentication remains inside the injected 'client_factory'. This builder never
    accepts or persists raw tokens, passwords or model credentials. Repository roots,
    network hosts and execution policy are explicit trusted-host inputs.
    """

    if not callable(client_factory) or not callable(agent_profile_id_factory):
        raise OpenHandsProductFactoryError(
            "OpenHands client/profile authority must be injected callables"
        )
    if not isinstance(sandbox_provider, OpenHandsSandboxProviderPort):
        raise OpenHandsProductFactoryError(
            "OpenHands sandbox provider does not satisfy the trusted host contract"
        )
    if not isinstance(acceptance_runtime, SandboxedAcceptanceRuntimePort):
        raise OpenHandsProductFactoryError(
            "OpenHands independent acceptance runtime does not satisfy its contract"
        )

    ledger = idempotency if idempotency is not None else IdempotencyLedger(store)
    recovery = ProductFactoryOpenHandsRecoveryProbe(ledger)
    ports = OpenHandsProductFactoryPorts(
        workspace_parent=workspace_parent,
        repositories=repositories,
        policy=policy,
        git_executable=git_executable,
        source_environment=source_environment,
    )
    runtime = OpenHandsAgentServerRuntime(
        client_factory=client_factory,
        agent_profile_id_factory=agent_profile_id_factory,
        recovery_binding_store=recovery,
        max_iterations=max_iterations,
        poll_interval_seconds=poll_interval_seconds,
    )
    worker = OpenHandsRemoteCodingWorker(
        sandbox_provider,
        runtime,
        acceptance_runtime=acceptance_runtime,
        recovery_probe=recovery,
        recovery_binding_store=recovery,
    )
    host = build_product_factory_coding_program_host(
        store,
        worker=worker,
        contexts=ports,
        evidence=ports,
        idempotency=ledger,
        review_evidence_authority=review_evidence_authority,
    )
    return OpenHandsProductFactoryProgram(
        host=host,
        worker=worker,
        runtime=runtime,
        ports=ports,
        recovery=recovery,
    )


def _canonical_host(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("OpenHands approved host must be canonical text")
    candidate = value.casefold().rstrip(".")
    if (
        candidate != value
        or "/" in candidate
        or ":" in candidate
        or any(character.isspace() for character in candidate)
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
    ):
        raise ValueError("OpenHands approved host must be a canonical bare host name")
    return candidate


def _canonical_text(value: object, label: str, *, max_bytes: int) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be canonical non-empty text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 text") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds the byte limit")
    return value


def _private_branch_name(work_id: str) -> str:
    identity = _canonical_text(work_id, "Product Factory work identity", max_bytes=2048)
    return f"nika-openhands-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def _paths_overlap(first: pathlib.Path, second: pathlib.Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _nul_paths(value: str) -> tuple[str, ...]:
    return tuple(item for item in value.split("\x00") if item)


def _lines(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.splitlines() if item.strip())


def _sorted_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(values, key=str.casefold))
