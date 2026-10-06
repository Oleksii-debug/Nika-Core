from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditIntegrityError, AuditLog
from nika_core.product_factory_build_execution import (
    BuildExecutionDispatch,
    BuildExecutionPortError,
    BuildExecutionResult,
    ProjectExecutionAuthority,
    TrustedExecutionAuthorityPort,
)
from nika_core.product_factory_build_execution_host import (
    BuildOutputPolicy,
    TrustedBuildOutputPolicyPort,
)
from nika_core.product_factory_deployment import Platform
from nika_core.product_factory_local_repository_binding import (
    ProductFactoryLocalRepositoryBindingError,
    ProductFactoryLocalRepositoryBindings,
)
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.product_project import ProductProjectRepository
from nika_core.toolsmith.contracts import ChangedFile, ProcessPolicy, ResourceBudget
from nika_core.toolsmith.execution import (
    ProcessExecutionError,
    cleanup_private_git_workspace,
    prepare_private_git_workspace,
    run_typed_process,
)
from nika_core.toolsmith.workspace_security import (
    ProductionIntegritySnapshot,
    WorkspacePathPolicy,
    WorkspaceSecurityError,
    assert_cleanup_tree_safe,
    assert_production_integrity,
    collect_tree_delta_evidence,
    collect_tree_evidence,
    ensure_path_policy,
    ensure_real_directory_root,
    make_sterile_git_plan,
    sterile_process_environment,
    validate_typed_argv,
)

_RECEIPT_SCHEMA = "nika.product-factory.local-build-receipt.v1"
_STARTED_EVENT = "product_factory.local_build.started"
_RECEIPT_EVENT = "product_factory.local_build.receipt"
_CLEANUP_EVENT = "product_factory.local_build.cleanup_failed"
_ENTITY_TYPE = "product_factory_build_dispatch"
_MAX_TREE_FILES = 50_000
_MAX_TREE_FILE_BYTES = 64 * 1024 * 1024
_MAX_TREE_TOTAL_BYTES = 1024 * 1024 * 1024


class LocalBuildRepositoryAuthorityPort(Protocol):
    def resolve(self, dispatch: BuildExecutionDispatch) -> pathlib.Path: ...


@dataclass(slots=True)
class ProductFactoryLocalBuildRepositoryAuthority:
    """Resolve only the durable ProductProject-scoped local repository binding."""

    bindings: ProductFactoryLocalRepositoryBindings
    projects: ProductProjectRepository

    def resolve(self, dispatch: BuildExecutionDispatch) -> pathlib.Path:
        binding = self.bindings.require(
            dispatch.project_id,
            dispatch.grant.repository_id,
        )
        project = self.projects.get(dispatch.project_id)
        if (
            project.status != "active"
            or binding.project_id != project.project_id
            or binding.locator not in project.spec.repository_refs
        ):
            raise ProductFactoryLocalRepositoryBindingError(
                "local build repository binding is stale for the current ProductProject"
            )
        return binding.root


@dataclass(frozen=True, slots=True)
class _StoredReceipt:
    result: BuildExecutionResult
    changed_files: tuple[ChangedFile, ...]


@dataclass(slots=True)
class PackagedLocalBuildExecutionNode:
    """Production PF5 node adapter for one host-controlled local execution node.

    PF5 remains the execution state machine and durable pre-effect authority. This
    adapter only performs the node effect after PF5 has persisted EFFECT_IN_FLIGHT.
    It reuses the durable ProductProject repository binding, Toolsmith sterile Git
    workspace/process containment and the same host-owned build-output policy used
    by DurableBuildExecutionHost.

    The append-only receipt is written after the process and before returning. A
    restart/lost acknowledgement can therefore be reconciled by inspect(). If an
    effect happened but no receipt was durably written, inspect() returns None and
    PF5 remains RECONCILE_REQUIRED; run() never blindly replays a started effect.
    """

    store: SQLiteStore
    node_id: str
    startup: PackagedLocalProductFactoryStartup
    repositories: LocalBuildRepositoryAuthorityPort
    trusted_authority: TrustedExecutionAuthorityPort
    output_policies: TrustedBuildOutputPolicyPort
    audit: AuditLog | None = None

    def __post_init__(self) -> None:
        if type(self.store) is not SQLiteStore:
            raise TypeError("store must be SQLiteStore")
        if (
            type(self.node_id) is not str
            or not self.node_id
            or self.node_id != self.node_id.strip()
        ):
            raise ValueError("local build node id must be canonical non-empty text")
        if type(self.startup) is not PackagedLocalProductFactoryStartup:
            raise TypeError("startup must be PackagedLocalProductFactoryStartup")
        self.startup.__post_init__()
        if not callable(getattr(self.repositories, "resolve", None)):
            raise TypeError("repositories must implement LocalBuildRepositoryAuthorityPort")
        if not callable(getattr(self.trusted_authority, "resolve", None)):
            raise TypeError("trusted_authority must implement resolve")
        if not callable(getattr(self.output_policies, "resolve", None)):
            raise TypeError("output_policies must implement resolve")
        if self.audit is None:
            self.audit = AuditLog(self.store)
        elif type(self.audit) is not AuditLog:
            raise TypeError("audit must be AuditLog")

    def run(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult:
        stored = self._stored_receipt(dispatch)
        if stored is not None:
            return stored.result
        if self._effect_started(dispatch):
            raise BuildExecutionPortError(
                "local build effect was already started without a durable result receipt"
            )

        try:
            repository_root = self._admit_dispatch(dispatch)
            output_policy = self._output_policy(dispatch)
            job_root = self._create_job_root(dispatch)
            plan = make_sterile_git_plan(
                repository_root=repository_root,
                job_root=job_root,
                branch_name=_job_branch(dispatch),
                base_sha=dispatch.source_sha,
            )
            prepared = prepare_private_git_workspace(
                plan,
                git_executable=str(self.startup.git_executable),
            )
            before = collect_tree_evidence(
                prepared.plan.worktree_root,
                max_files=_MAX_TREE_FILES,
                max_file_bytes=_MAX_TREE_FILE_BYTES,
                max_total_bytes=_MAX_TREE_TOTAL_BYTES,
            )
            cwd = ensure_path_policy(
                prepared.plan.worktree_root,
                dispatch.grant.workspace_relpath,
                WorkspacePathPolicy((dispatch.grant.workspace_relpath,)),
                must_exist=True,
            )
            if not cwd.is_dir():
                raise WorkspaceSecurityError("build workspace path must be a directory")
            temp_root = job_root / "tmp"
            temp_root.mkdir(exist_ok=False)
            ensure_real_directory_root(temp_root, label="local build temp root")
            environment = sterile_process_environment(
                prepared.plan.environment,
                temp_root=temp_root,
            )
            allowed_executables = tuple(self.startup.policy.allowed_executables)
            validate_typed_argv(dispatch.grant.argv, allowed_executables)
            process_policy = ProcessPolicy(allowed_executables)
            resource_budget = ResourceBudget(
                self.startup.policy.resource_budget.timeout_seconds,
                self.startup.policy.resource_budget.max_output_bytes,
                self.startup.policy.resource_budget.max_changed_files,
            )
            production_before = _production_integrity_snapshot(
                repository_root,
                git_executable=self.startup.git_executable,
                environment=environment,
                resource_budget=resource_budget,
            )
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            ProcessExecutionError,
            ProductFactoryLocalRepositoryBindingError,
            WorkspaceSecurityError,
        ) as exc:
            self._cleanup_unclaimed(locals().get("plan"), locals().get("job_root"))
            raise BuildExecutionPortError(
                f"local build preparation failed before effect: {type(exc).__name__}"
            ) from None

        dispatch_digest = _dispatch_digest(dispatch)
        self._claim_effect(dispatch, dispatch_digest)

        # Close the authority/configuration TOCTOU as far as the existing contracts
        # permit. Drift after the durable start marker produces a definitive failed
        # receipt without launching a process.
        try:
            current_root = self._admit_dispatch(dispatch)
            current_policy = self._output_policy(dispatch)
            if current_root != repository_root or current_policy != output_policy:
                raise BuildExecutionPortError(
                    "local build authority changed at the effect boundary"
                )
        except Exception:
            result = _failed_result(
                dispatch,
                dispatch_digest,
                reason_code="authority_changed_before_effect",
            )
            self._write_receipt(dispatch, dispatch_digest, result, ())
            self._cleanup_after_receipt(plan, job_root, dispatch)
            return result

        process_result = None
        process_error: Exception | None = None
        try:
            process_result = run_typed_process(
                dispatch.grant.argv,
                process_policy=process_policy,
                resource_budget=resource_budget,
                cwd=cwd,
                environment=environment,
                workspace_root=prepared.plan.worktree_root,
            )
        except (ProcessExecutionError, OSError, RuntimeError, ValueError) as exc:
            process_error = exc

        # Process containment is not a filesystem sandbox. Re-admit the production
        # repository after every attempted effect so an absolute-path escape or a
        # concurrent source mutation can never be reported as a successful build.
        try:
            production_after = _production_integrity_snapshot(
                repository_root,
                git_executable=self.startup.git_executable,
                environment=environment,
                resource_budget=resource_budget,
            )
            assert_production_integrity(production_before, production_after)
        except (OSError, ProcessExecutionError, ValueError, WorkspaceSecurityError):
            result = _failed_result(
                dispatch,
                dispatch_digest,
                reason_code="production_integrity_not_preserved",
            )
            self._write_receipt(
                dispatch,
                dispatch_digest,
                result,
                (),
                process_summary={"reason_code": "production_integrity_not_preserved"},
            )
            self._cleanup_after_receipt(plan, job_root, dispatch)
            return result

        if process_error is not None:
            # The process boundary may already have been crossed. No blind retry:
            # leave the started marker without a fabricated result receipt.
            raise BuildExecutionPortError(
                "local build process outcome requires inspection: "
                f"{type(process_error).__name__}"
            )
        if process_result is None:
            raise BuildExecutionPortError("local build process returned no result")

        try:
            after = collect_tree_evidence(
                prepared.plan.worktree_root,
                max_files=_MAX_TREE_FILES,
                max_file_bytes=_MAX_TREE_FILE_BYTES,
                max_total_bytes=_MAX_TREE_TOTAL_BYTES,
            )
            delta = collect_tree_delta_evidence(
                before,
                after,
                path_policy=WorkspacePathPolicy(output_policy.allowed_paths.roots),
                max_changed_files=max(1, output_policy.max_changed_files),
            )
            changed_files = _changed_files(delta.changes)
            output_valid = bool(changed_files) and len(changed_files) == len(delta.changes)
            process_succeeded = (
                process_result.returncode == 0
                and not process_result.timed_out
                and not process_result.cancelled
                and not process_result.output_limit_exceeded
            )
            succeeded = process_succeeded and output_valid
            artifact_digest = (
                delta.digest
                if output_valid
                else _failure_digest(dispatch_digest, "invalid_or_empty_output")
            )
            reason_code = (
                "ok"
                if succeeded
                else (
                    "process_failed"
                    if not process_succeeded
                    else "invalid_or_empty_output"
                )
            )
        except (OSError, TypeError, ValueError, WorkspaceSecurityError):
            changed_files = ()
            succeeded = False
            artifact_digest = _failure_digest(
                dispatch_digest,
                "invalid_output_evidence",
            )
            reason_code = "invalid_output_evidence"

        result = BuildExecutionResult(
            source_sha=dispatch.source_sha,
            artifact_digest=artifact_digest,
            succeeded=succeeded,
            uncertain=False,
            evidence_refs=(_receipt_ref(dispatch),),
            completed_at=datetime.now(UTC),
        )
        self._write_receipt(
            dispatch,
            dispatch_digest,
            result,
            changed_files,
            process_summary={
                "returncode": process_result.returncode,
                "timed_out": process_result.timed_out,
                "cancelled": process_result.cancelled,
                "output_limit_exceeded": process_result.output_limit_exceeded,
                "reason_code": reason_code,
            },
        )
        self._cleanup_after_receipt(plan, job_root, dispatch)
        return result

    def inspect(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult | None:
        receipt = self._stored_receipt(dispatch)
        if receipt is not None:
            return receipt.result
        # A started marker without a receipt intentionally remains unresolved.
        # PF5 keeps RECONCILE_REQUIRED and never calls run() again blindly.
        return None

    def collect(
        self,
        dispatch: BuildExecutionDispatch,
        result: BuildExecutionResult,
    ) -> tuple[ChangedFile, ...]:
        receipt = self._stored_receipt(dispatch)
        if receipt is None or receipt.result != result:
            raise BuildExecutionPortError(
                "local build changed-file evidence lacks the exact durable receipt"
            )
        return receipt.changed_files

    def _admit_dispatch(self, dispatch: BuildExecutionDispatch) -> pathlib.Path:
        if type(dispatch) is not BuildExecutionDispatch:
            raise TypeError("dispatch must be BuildExecutionDispatch")
        if dispatch.node_id != self.node_id:
            raise ValueError("dispatch targets a different local build node")
        expected_platform = _host_platform()
        if dispatch.platform is not expected_platform:
            raise ValueError("dispatch platform does not match this local build host")
        if dispatch.grant.credential_refs:
            raise ValueError(
                "local build node does not admit credential-bearing execution grants"
            )
        if dispatch.grant.network_scopes:
            raise ValueError(
                "local build node only supports hermetic no-network build grants"
            )
        _require_current_authority(dispatch, self.trusted_authority)
        root = pathlib.Path(self.repositories.resolve(dispatch))
        return ensure_real_directory_root(root, label="local build repository root")

    def _output_policy(self, dispatch: BuildExecutionDispatch) -> BuildOutputPolicy:
        policy = self.output_policies.resolve(
            project_id=dispatch.project_id,
            repository_id=dispatch.grant.repository_id,
            work_id=dispatch.work_id,
        )
        if type(policy) is not BuildOutputPolicy:
            raise TypeError("build output policy carrier is invalid")
        if (
            policy.project_id != dispatch.project_id
            or policy.repository_id != dispatch.grant.repository_id
            or policy.work_id != dispatch.work_id
            or policy.max_changed_files < 1
        ):
            raise ValueError("build output policy does not match the exact dispatch")
        return policy

    def _create_job_root(self, dispatch: BuildExecutionDispatch) -> pathlib.Path:
        parent = ensure_real_directory_root(
            self.startup.workspace_parent,
            label="local build workspace parent",
        )
        build_parent = parent / "pf5-build"
        try:
            build_parent.mkdir(exist_ok=True)
        except OSError as exc:
            raise WorkspaceSecurityError("unable to create local build workspace") from exc
        build_parent = ensure_real_directory_root(
            build_parent,
            label="local build workspace root",
        )
        job_root = build_parent / hashlib.sha256(
            dispatch.dispatch_id.encode("utf-8")
        ).hexdigest()
        try:
            job_root.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise WorkspaceSecurityError(
                "local build workspace already exists for this dispatch"
            ) from exc
        return ensure_real_directory_root(job_root, label="local build job root")

    def _claim_effect(self, dispatch: BuildExecutionDispatch, dispatch_digest: str) -> None:
        assert self.audit is not None
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT event_type FROM audit_events "
                "WHERE entity_type=? AND entity_id=? ORDER BY event_id",
                (_ENTITY_TYPE, dispatch.dispatch_id),
            ).fetchall()
            if rows:
                raise BuildExecutionPortError(
                    "local build dispatch already has durable effect evidence"
                )
            self.audit.append_with_connection(
                conn,
                event_type=_STARTED_EVENT,
                entity_type=_ENTITY_TYPE,
                entity_id=dispatch.dispatch_id,
                payload={
                    "schema": _RECEIPT_SCHEMA,
                    "dispatch_digest": dispatch_digest,
                },
            )

    def _write_receipt(
        self,
        dispatch: BuildExecutionDispatch,
        dispatch_digest: str,
        result: BuildExecutionResult,
        changed_files: tuple[ChangedFile, ...],
        *,
        process_summary: dict[str, object] | None = None,
    ) -> None:
        assert self.audit is not None
        payload: dict[str, object] = {
            "schema": _RECEIPT_SCHEMA,
            "dispatch_digest": dispatch_digest,
            "source_sha": result.source_sha,
            "artifact_digest": result.artifact_digest,
            "succeeded": result.succeeded,
            "uncertain": result.uncertain,
            "evidence_refs": list(result.evidence_refs),
            "completed_at": result.completed_at.astimezone(UTC).isoformat(),
            "changed_files": [
                {
                    "path": item.path,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in changed_files
            ],
        }
        if process_summary is not None:
            payload["process"] = process_summary
        try:
            with self.store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT event_type FROM audit_events "
                    "WHERE entity_type=? AND entity_id=? ORDER BY event_id",
                    (_ENTITY_TYPE, dispatch.dispatch_id),
                ).fetchall()
                event_types = tuple(row["event_type"] for row in rows)
                if event_types != (_STARTED_EVENT,):
                    raise BuildExecutionPortError(
                        "local build durable start marker changed before receipt"
                    )
                self.audit.append_with_connection(
                    conn,
                    event_type=_RECEIPT_EVENT,
                    entity_type=_ENTITY_TYPE,
                    entity_id=dispatch.dispatch_id,
                    payload=payload,
                )
        except BuildExecutionPortError:
            raise
        except Exception as exc:
            raise BuildExecutionPortError(
                f"local build result receipt could not be persisted: {type(exc).__name__}"
            ) from None

    def _stored_receipt(self, dispatch: BuildExecutionDispatch) -> _StoredReceipt | None:
        assert self.audit is not None
        try:
            events = self.audit.list_for(
                entity_type=_ENTITY_TYPE,
                entity_id=dispatch.dispatch_id,
            )
            relevant = tuple(
                event
                for event in events
                if event.event_type in {_STARTED_EVENT, _RECEIPT_EVENT}
            )
            unknown = tuple(
                event
                for event in events
                if event.event_type not in {_STARTED_EVENT, _RECEIPT_EVENT, _CLEANUP_EVENT}
            )
            if unknown:
                raise ValueError("unknown local build durable event type")
            if not relevant:
                return None
            if relevant[0].event_type != _STARTED_EVENT or len(
                tuple(event for event in relevant if event.event_type == _STARTED_EVENT)
            ) != 1:
                raise ValueError("local build durable start marker is invalid")
            _require_marker_payload(
                relevant[0].payload,
                dispatch_digest=_dispatch_digest(dispatch),
            )
            receipts = tuple(
                event for event in relevant if event.event_type == _RECEIPT_EVENT
            )
            if not receipts:
                return None
            if len(receipts) != 1 or relevant[-1].event_type != _RECEIPT_EVENT:
                raise ValueError("local build durable receipt multiplicity is invalid")
            return _decode_receipt(
                receipts[0].payload,
                dispatch=dispatch,
            )
        except BuildExecutionPortError:
            raise
        except (AuditIntegrityError, KeyError, TypeError, ValueError, OverflowError):
            raise BuildExecutionPortError(
                "durable local build receipt is invalid"
            ) from None

    def _effect_started(self, dispatch: BuildExecutionDispatch) -> bool:
        assert self.audit is not None
        try:
            events = self.audit.list_for(
                entity_type=_ENTITY_TYPE,
                entity_id=dispatch.dispatch_id,
            )
        except AuditIntegrityError:
            raise BuildExecutionPortError("durable local build evidence is corrupt") from None
        return any(event.event_type == _STARTED_EVENT for event in events)

    def _cleanup_after_receipt(
        self,
        plan,
        job_root: pathlib.Path,
        dispatch: BuildExecutionDispatch,
    ) -> None:
        try:
            cleanup_private_git_workspace(plan)
            temp_root = job_root / "tmp"
            if temp_root.exists() or temp_root.is_symlink():
                assert_cleanup_tree_safe(temp_root)
                shutil.rmtree(temp_root)
            job_root.rmdir()
        except (OSError, TypeError, ValueError, WorkspaceSecurityError):
            assert self.audit is not None
            try:
                self.audit.append(
                    event_type=_CLEANUP_EVENT,
                    entity_type=_ENTITY_TYPE,
                    entity_id=dispatch.dispatch_id,
                    payload={"schema": _RECEIPT_SCHEMA},
                )
            except Exception:
                pass

    @staticmethod
    def _cleanup_unclaimed(plan, job_root) -> None:
        if plan is None or job_root is None:
            return
        try:
            cleanup_private_git_workspace(plan)
            temp_root = pathlib.Path(job_root) / "tmp"
            if temp_root.exists() or temp_root.is_symlink():
                assert_cleanup_tree_safe(temp_root)
                shutil.rmtree(temp_root)
            pathlib.Path(job_root).rmdir()
        except (OSError, TypeError, ValueError, WorkspaceSecurityError):
            return


def build_packaged_local_build_execution_node(
    store: SQLiteStore,
    *,
    node_id: str,
    startup: PackagedLocalProductFactoryStartup,
    trusted_authority: TrustedExecutionAuthorityPort,
    output_policies: TrustedBuildOutputPolicyPort,
) -> PackagedLocalBuildExecutionNode:
    """Compose the production local PF5 node from existing canonical authorities."""

    bindings = ProductFactoryLocalRepositoryBindings(store)
    projects = ProductProjectRepository(store)
    return PackagedLocalBuildExecutionNode(
        store=store,
        node_id=node_id,
        startup=startup,
        repositories=ProductFactoryLocalBuildRepositoryAuthority(bindings, projects),
        trusted_authority=trusted_authority,
        output_policies=output_policies,
    )



def _production_integrity_snapshot(
    repository_root: pathlib.Path,
    *,
    git_executable: pathlib.Path,
    environment: dict[str, str],
    resource_budget: ResourceBudget,
) -> ProductionIntegritySnapshot:
    """Snapshot production HEAD plus tracked/index/untracked working-tree state."""

    git = str(git_executable)
    policy = ProcessPolicy((git,))
    budget = ResourceBudget(
        min(resource_budget.timeout_seconds, 60),
        min(resource_budget.max_output_bytes, 8 * 1024 * 1024),
        1,
    )
    head = run_typed_process(
        (git, "rev-parse", "--verify", "HEAD^{commit}"),
        process_policy=policy,
        resource_budget=budget,
        cwd=repository_root,
        environment=environment,
        workspace_root=repository_root,
    )
    status = run_typed_process(
        (
            git,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=no",
        ),
        process_policy=policy,
        resource_budget=budget,
        cwd=repository_root,
        environment=environment,
        workspace_root=repository_root,
    )
    for result in (head, status):
        if (
            result.returncode != 0
            or result.timed_out
            or result.cancelled
            or result.output_limit_exceeded
        ):
            raise WorkspaceSecurityError(
                "production repository integrity snapshot could not be proven"
            )
    commit = head.stdout.strip().casefold()
    if len(commit) != 40 or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise WorkspaceSecurityError("production repository HEAD identity is invalid")
    status_digest = hashlib.sha256(status.stdout.encode("utf-8")).hexdigest()
    return ProductionIntegritySnapshot(commit, status_digest)


def _host_platform() -> Platform:
    if os.name == "nt":
        return Platform.WINDOWS
    if os.name == "posix":
        return Platform.LINUX
    raise ValueError("local build node does not support this host platform")


def _require_current_authority(
    dispatch: BuildExecutionDispatch,
    authority_port: TrustedExecutionAuthorityPort,
) -> ProjectExecutionAuthority:
    authority = authority_port.resolve(
        project_id=dispatch.project_id,
        repository_id=dispatch.grant.repository_id,
        work_id=dispatch.work_id,
    )
    if type(authority) is not ProjectExecutionAuthority:
        raise TypeError("trusted execution authority carrier is invalid")
    grant = dispatch.grant
    commands = {item.command_id: item.argv for item in authority.commands}
    workspace = pathlib.PurePosixPath(grant.workspace_relpath)
    allowed_workspaces = tuple(
        pathlib.PurePosixPath(item) for item in authority.allowed_workspace_paths
    )
    if (
        authority.project_id != dispatch.project_id
        or authority.repository_id != grant.repository_id
        or authority.work_id != dispatch.work_id
        or dispatch.node_id not in authority.allowed_node_ids
        or not set(grant.allowed_node_ids).issubset(authority.allowed_node_ids)
        or not any(workspace == root or root in workspace.parents for root in allowed_workspaces)
        or not set(grant.network_scopes).issubset(authority.network_scopes)
        or not set(grant.credential_refs).issubset(authority.credential_refs)
        or commands.get(grant.command_id) != grant.argv
        or tuple(authority.evidence_refs) != grant.authority_evidence_refs
    ):
        raise ValueError("trusted execution authority changed before local build effect")
    return authority


def _dispatch_digest(dispatch: BuildExecutionDispatch) -> str:
    command_digest = hashlib.sha256(
        json.dumps(
            list(dispatch.grant.argv),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    payload = {
        "dispatch_id": dispatch.dispatch_id,
        "project_id": dispatch.project_id,
        "repository_id": dispatch.grant.repository_id,
        "work_id": dispatch.work_id,
        "node_id": dispatch.node_id,
        "platform": dispatch.platform.value,
        "source_sha": dispatch.source_sha,
        "workspace_relpath": dispatch.grant.workspace_relpath,
        "command_id": dispatch.grant.command_id,
        "command_sha256": command_digest,
        "attempt": dispatch.attempt,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _job_branch(dispatch: BuildExecutionDispatch) -> str:
    suffix = hashlib.sha256(dispatch.dispatch_id.encode("utf-8")).hexdigest()[:24]
    return f"nika-pf5-{suffix}"


def _receipt_ref(dispatch: BuildExecutionDispatch) -> str:
    identity = hashlib.sha256(dispatch.dispatch_id.encode("utf-8")).hexdigest()
    return f"pf5-local-build://receipt/{identity}"


def _failure_digest(dispatch_digest: str, reason_code: str) -> str:
    return hashlib.sha256(
        f"pf5-local-build-failure\0{dispatch_digest}\0{reason_code}".encode("utf-8")
    ).hexdigest()


def _failed_result(
    dispatch: BuildExecutionDispatch,
    dispatch_digest: str,
    *,
    reason_code: str,
) -> BuildExecutionResult:
    return BuildExecutionResult(
        source_sha=dispatch.source_sha,
        artifact_digest=_failure_digest(dispatch_digest, reason_code),
        succeeded=False,
        uncertain=False,
        evidence_refs=(_receipt_ref(dispatch),),
        completed_at=datetime.now(UTC),
    )


def _changed_files(changes) -> tuple[ChangedFile, ...]:
    result: list[ChangedFile] = []
    for change in changes:
        if change.after_sha256 is None or change.after_size_bytes is None:
            return ()
        result.append(
            ChangedFile(
                path=change.path,
                sha256=change.after_sha256,
                size_bytes=change.after_size_bytes,
            )
        )
    return tuple(result)


def _require_marker_payload(payload: dict[str, object], *, dispatch_digest: str) -> None:
    if (
        type(payload) is not dict
        or set(payload) != {"schema", "dispatch_digest"}
        or payload.get("schema") != _RECEIPT_SCHEMA
        or payload.get("dispatch_digest") != dispatch_digest
    ):
        raise ValueError("local build durable start marker does not match dispatch")


def _decode_receipt(
    payload: dict[str, object],
    *,
    dispatch: BuildExecutionDispatch,
) -> _StoredReceipt:
    required = {
        "schema",
        "dispatch_digest",
        "source_sha",
        "artifact_digest",
        "succeeded",
        "uncertain",
        "evidence_refs",
        "completed_at",
        "changed_files",
    }
    if type(payload) is not dict or not required <= set(payload):
        raise ValueError("local build receipt schema is incomplete")
    if set(payload) - required - {"process"}:
        raise ValueError("local build receipt schema has unknown fields")
    if (
        payload["schema"] != _RECEIPT_SCHEMA
        or payload["dispatch_digest"] != _dispatch_digest(dispatch)
        or payload["source_sha"] != dispatch.source_sha
        or type(payload["artifact_digest"]) is not str
        or type(payload["succeeded"]) is not bool
        or type(payload["uncertain"]) is not bool
        or payload["uncertain"]
    ):
        raise ValueError("local build receipt identity/result fields are invalid")
    evidence_refs_raw = payload["evidence_refs"]
    if type(evidence_refs_raw) is not list or evidence_refs_raw != [_receipt_ref(dispatch)]:
        raise ValueError("local build receipt evidence reference is invalid")
    completed_raw = payload["completed_at"]
    if type(completed_raw) is not str:
        raise ValueError("local build receipt completion time is invalid")
    completed = datetime.fromisoformat(completed_raw)
    if (
        completed.tzinfo is None
        or completed.utcoffset() != timedelta(0)
        or completed.isoformat() != completed_raw
    ):
        raise ValueError("local build receipt completion time is not canonical UTC")
    changed_raw = payload["changed_files"]
    if type(changed_raw) is not list:
        raise ValueError("local build receipt changed files are invalid")
    changed: list[ChangedFile] = []
    for item in changed_raw:
        if type(item) is not dict or set(item) != {"path", "sha256", "size_bytes"}:
            raise ValueError("local build receipt changed file is invalid")
        if (
            type(item["path"]) is not str
            or type(item["sha256"]) is not str
            or type(item["size_bytes"]) is not int
        ):
            raise ValueError("local build receipt changed file types are invalid")
        changed.append(
            ChangedFile(
                item["path"],
                item["sha256"],
                item["size_bytes"],
            )
        )
    result = BuildExecutionResult(
        source_sha=dispatch.source_sha,
        artifact_digest=payload["artifact_digest"],
        succeeded=payload["succeeded"],
        uncertain=False,
        evidence_refs=(_receipt_ref(dispatch),),
        completed_at=completed,
    )
    return _StoredReceipt(result=result, changed_files=tuple(changed))
