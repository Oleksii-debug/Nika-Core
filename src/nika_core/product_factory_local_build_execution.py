from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol

from nika_core.product_factory_build_execution import (
    BuildExecutionDispatch,
    BuildExecutionPortError,
    BuildExecutionResult,
    ExecutionGrant,
)
from nika_core.product_factory_deployment import Platform
from nika_core.product_factory_local_coding import ContainedLocalCodingPolicy
from nika_core.product_factory_local_repository_binding import (
    ProductFactoryLocalRepositoryBinding,
)
from nika_core.toolsmith.contracts import (
    ChangedFile,
    ProcessPolicy,
    ResourceBudget,
    normalize_relative_path,
)
from nika_core.toolsmith.execution import (
    ProcessExecutionError,
    _git,
    _resolve_host_git_executable,
    cleanup_private_git_workspace,
    prepare_private_git_workspace,
    run_typed_process,
)
from nika_core.toolsmith.workspace_security import (
    WorkspacePathPolicy,
    WorkspaceSecurityError,
    ensure_path_policy,
    ensure_real_directory_root,
    ensure_worker_mutation_path,
    make_sterile_git_plan,
    validate_typed_argv,
)

_RECEIPT_SCHEMA = "nika.product-factory.local-build-node.v1"
_STATE_DIR_NAME = "_nika_pf5_build_node"
_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_LOCAL_BUILD_FILE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_IDENTITY_BYTES = 512


class LocalBuildRepositoryBindingPort(Protocol):
    """Read-only bridge to the canonical ProductFactoryLocalRepositoryBindings authority."""

    def require(
        self,
        project_id: str,
        repository_id: str,
    ) -> ProductFactoryLocalRepositoryBinding: ...


@dataclass(frozen=True, slots=True)
class LocalBuildArtifactBinding:
    """Host-owned output identity for one already-approved PF5 command.

    The command argv itself remains owned by ProjectExecutionAuthority. This binding only
    says which repository-relative regular file is the build artifact for that command
    and how large that one artifact may be.
    """

    command_id: str
    artifact_relpath: str
    max_artifact_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            type(self.command_id) is not str
            or not self.command_id
            or self.command_id != self.command_id.strip()
            or len(self.command_id.encode("utf-8")) > _MAX_IDENTITY_BYTES
        ):
            raise ValueError("local build command identity must be canonical bounded text")
        if type(self.artifact_relpath) is not str:
            raise ValueError("local build artifact path must be text")
        try:
            normalized = normalize_relative_path(self.artifact_relpath).as_posix()
            ensure_worker_mutation_path(normalized)
        except (ValueError, WorkspaceSecurityError) as exc:
            raise ValueError("local build artifact path must be a safe repository-relative path") from exc
        if normalized != self.artifact_relpath.replace("\\", "/"):
            raise ValueError("local build artifact path must already be canonical")
        if (
            type(self.max_artifact_bytes) is not int
            or not 1 <= self.max_artifact_bytes <= _MAX_LOCAL_BUILD_FILE_BYTES
        ):
            raise ValueError("local build artifact size limit is invalid")
        object.__setattr__(self, "artifact_relpath", normalized)


@dataclass(frozen=True, slots=True)
class _LocalBuildReceipt:
    dispatch_fingerprint: str
    result: BuildExecutionResult
    changed_files: tuple[ChangedFile, ...]


@dataclass(slots=True)
class ContainedLocalBuildExecutionNode:
    """Concrete PF5 local node over incumbent Toolsmith containment.

    This adapter deliberately does not implement a hostile-code or network sandbox.
    It accepts only empty PF5 network/credential scope, clones the exact source SHA into
    a sterile private Git workspace, launches only a startup-allowlisted absolute
    executable through Toolsmith, persists a provider-side result receipt, and removes
    the private workspace before returning. ProductFactoryLocalRepositoryBindings remains
    the only repository filesystem authority.

    The object also implements BuildExecutionFileEvidencePort: changed-file evidence is
    captured before cleanup and recovered from the same durable receipt after restart.
    """

    node_id: str
    platform: Platform
    workspace_parent: pathlib.Path
    repository_bindings: LocalBuildRepositoryBindingPort
    policy: ContainedLocalCodingPolicy
    artifact_bindings: tuple[LocalBuildArtifactBinding, ...]
    git_executable: str
    source_environment: Mapping[str, str] | None = None
    _state_root: pathlib.Path = field(init=False, repr=False)
    _artifact_by_command: Mapping[str, LocalBuildArtifactBinding] = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if (
            type(self.node_id) is not str
            or not self.node_id
            or self.node_id != self.node_id.strip()
            or len(self.node_id.encode("utf-8")) > _MAX_IDENTITY_BYTES
        ):
            raise ValueError("local build node identity must be canonical bounded text")
        if type(self.platform) is not Platform or self.platform is not local_build_platform():
            raise ValueError("local build node platform must match the physical host")
        if type(self.policy) is not ContainedLocalCodingPolicy:
            raise TypeError("local build policy carrier is invalid")
        self.policy.__post_init__()
        if type(self.artifact_bindings) is not tuple or not self.artifact_bindings:
            raise ValueError("local build node requires explicit artifact bindings")
        if any(type(item) is not LocalBuildArtifactBinding for item in self.artifact_bindings):
            raise TypeError("local build artifact binding carrier is invalid")
        ids = tuple(item.command_id for item in self.artifact_bindings)
        if len(ids) != len(set(ids)):
            raise ValueError("local build command artifact identities must be unique")

        parent = ensure_real_directory_root(
            pathlib.Path(self.workspace_parent),
            label="local build workspace parent",
        )
        git = _resolve_host_git_executable(self.git_executable)
        environment = _snapshot_environment(self.source_environment)
        state_root = parent / _STATE_DIR_NAME
        try:
            state_root.mkdir(parents=False, exist_ok=True)
        except OSError as exc:
            raise ValueError("local build durable state directory is unavailable") from exc
        state_root = ensure_real_directory_root(
            state_root,
            label="local build durable state root",
        )
        object.__setattr__(self, "workspace_parent", parent)
        object.__setattr__(self, "git_executable", str(git.executable))
        object.__setattr__(self, "source_environment", MappingProxyType(environment))
        object.__setattr__(self, "_state_root", state_root)
        object.__setattr__(
            self,
            "_artifact_by_command",
            MappingProxyType({item.command_id: item for item in self.artifact_bindings}),
        )

    def run(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult:
        admitted = _readmit_dispatch(dispatch)
        artifact_binding = self._admit_dispatch(admitted)
        fingerprint = _dispatch_fingerprint(admitted)

        if self._receipt_path(fingerprint).exists():
            raise BuildExecutionPortError(
                "local build dispatch already has durable result; inspection is required"
            )

        job_root = self._create_job_root(fingerprint)
        plan = None
        try:
            binding = self._require_binding(admitted)
            plan = make_sterile_git_plan(
                repository_root=binding.root,
                job_root=job_root,
                branch_name=f"nika-pf5-build-{fingerprint[:24]}",
                base_sha=admitted.source_sha,
                source_environment=self.source_environment,
            )
            prepared = prepare_private_git_workspace(
                plan,
                git_executable=self.git_executable,
            )
            plan = prepared.plan
            self._require_same_binding(admitted, binding)

            cwd = ensure_path_policy(
                plan.worktree_root,
                admitted.grant.workspace_relpath,
                WorkspacePathPolicy((admitted.grant.workspace_relpath,)),
                must_exist=True,
            )
            if not cwd.is_dir():
                raise WorkspaceSecurityError(
                    "local build command cwd must be a directory"
                )

            # The durable provider claim is the last step before the external process
            # effect.  If the process starts and acknowledgement/receipt publication is
            # lost, this marker survives restart and prevents a blind replay.
            self._claim_dispatch(fingerprint)
            process = run_typed_process(
                admitted.grant.argv,
                process_policy=ProcessPolicy(
                    tuple(self.policy.allowed_executables)
                ),
                resource_budget=ResourceBudget(
                    timeout_seconds=self.policy.resource_budget.timeout_seconds,
                    max_output_bytes=self.policy.resource_budget.max_output_bytes,
                    max_changed_files=self.policy.resource_budget.max_changed_files,
                ),
                cwd=cwd,
                environment=self.source_environment,
                workspace_root=plan.worktree_root,
            )
        except (
            BuildExecutionPortError,
            OSError,
            ProcessExecutionError,
            ValueError,
            WorkspaceSecurityError,
        ) as exc:
            if plan is not None:
                self._cleanup_plan(plan, job_root)
            else:
                self._cleanup_unprepared_job_root(job_root)
            raise BuildExecutionPortError(
                "contained local build provider could not prove a safe exact outcome"
            ) from exc

        process_digest = _process_evidence_digest(admitted, process)
        try:
            self._require_same_binding(admitted, binding)
        except BuildExecutionPortError as exc:
            # The process effect happened under an authority that changed before its
            # outcome could be trusted. Persist uncertainty for inspection; never
            # convert this into a definite success/failure or replay it.
            uncertain = _uncertain_result(admitted, process_digest)
            self._cleanup_plan(plan, job_root)
            self._save_receipt(fingerprint, uncertain, ())
            raise BuildExecutionPortError(
                "local repository authority changed after build execution"
            ) from exc

        process_succeeded = (
            process.returncode == 0
            and not process.timed_out
            and not process.cancelled
            and not process.output_limit_exceeded
        )
        changed_files: tuple[ChangedFile, ...] = ()
        try:
            changed_files = self._collect_changed_files(
                plan,
                max_changed_files=self.policy.resource_budget.max_changed_files,
            )
            if process_succeeded:
                result = self._success_or_invalid_artifact(
                    admitted,
                    artifact_binding,
                    plan.worktree_root,
                    changed_files,
                    process_digest,
                )
            else:
                result = _definite_failed_result(
                    admitted,
                    process_digest,
                    reason="process",
                )
        except (
            BuildExecutionPortError,
            OSError,
            ValueError,
            WorkspaceSecurityError,
        ):
            # The process returned a definite status but its output cannot satisfy the
            # trusted evidence contract. This is a definite failed build, not an
            # uncertain transport outcome.
            changed_files = ()
            result = _definite_failed_result(
                admitted,
                process_digest,
                reason="evidence",
            )

        cleanup_error = self._cleanup_plan(plan, job_root)
        if cleanup_error is not None:
            changed_files = ()
            result = _definite_failed_result(
                admitted,
                process_digest,
                reason="cleanup",
            )

        self._save_receipt(fingerprint, result, changed_files)
        return result

    def inspect(self, dispatch: BuildExecutionDispatch) -> BuildExecutionResult | None:
        admitted = _readmit_dispatch(dispatch)
        self._admit_dispatch(admitted)
        receipt = self._load_receipt(_dispatch_fingerprint(admitted))
        return None if receipt is None else receipt.result

    def collect(
        self,
        dispatch: BuildExecutionDispatch,
        result: BuildExecutionResult,
    ) -> tuple[ChangedFile, ...]:
        admitted = _readmit_dispatch(dispatch)
        self._admit_dispatch(admitted)
        if type(result) is not BuildExecutionResult:
            raise BuildExecutionPortError("local build result carrier is invalid")
        receipt = self._load_receipt(_dispatch_fingerprint(admitted))
        if receipt is None:
            raise BuildExecutionPortError("local build changed-file receipt is unavailable")
        if receipt.result != result:
            raise BuildExecutionPortError("local build result does not match durable provider receipt")
        return receipt.changed_files

    def _admit_dispatch(
        self,
        dispatch: BuildExecutionDispatch,
    ) -> LocalBuildArtifactBinding:
        if dispatch.node_id != self.node_id or dispatch.platform is not self.platform:
            raise BuildExecutionPortError("dispatch targets a different physical local build node")
        if dispatch.grant.network_scopes:
            raise BuildExecutionPortError(
                "contained local build node does not implement approved-host network enforcement"
            )
        if dispatch.grant.credential_refs:
            raise BuildExecutionPortError(
                "contained local build node does not inject build credentials"
            )
        try:
            validate_typed_argv(
                dispatch.grant.argv,
                self.policy.allowed_executables,
            )
        except WorkspaceSecurityError as exc:
            raise BuildExecutionPortError(
                "local build executable is outside startup process authority"
            ) from exc
        binding = self._artifact_by_command.get(dispatch.grant.command_id)
        if binding is None:
            raise BuildExecutionPortError(
                "local build command lacks a trusted artifact binding"
            )
        return binding

    def _require_binding(
        self,
        dispatch: BuildExecutionDispatch,
    ) -> ProductFactoryLocalRepositoryBinding:
        try:
            binding = self.repository_bindings.require(
                dispatch.project_id,
                dispatch.grant.repository_id,
            )
        except Exception as exc:
            raise BuildExecutionPortError(
                "canonical local repository binding is unavailable"
            ) from exc
        if type(binding) is not ProductFactoryLocalRepositoryBinding:
            raise BuildExecutionPortError("local repository binding carrier is invalid")
        if (
            binding.project_id != dispatch.project_id
            or binding.repository_id != dispatch.grant.repository_id
        ):
            raise BuildExecutionPortError(
                "local repository binding identity does not match dispatch"
            )
        root = ensure_real_directory_root(
            pathlib.Path(binding.root),
            label="local build repository root",
        )
        if root != binding.root:
            raise BuildExecutionPortError("local repository binding root is not canonical")
        if not (root / ".git").exists():
            raise BuildExecutionPortError("local build repository lacks trusted Git metadata")
        return binding

    def _require_same_binding(
        self,
        dispatch: BuildExecutionDispatch,
        expected: ProductFactoryLocalRepositoryBinding,
    ) -> None:
        current = self._require_binding(dispatch)
        if current != expected:
            raise BuildExecutionPortError(
                "local repository binding changed during private build execution"
            )

    def _create_job_root(self, fingerprint: str) -> pathlib.Path:
        root = self.workspace_parent / f"pf5-build-{fingerprint}"
        try:
            root.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise BuildExecutionPortError(
                "local build workspace already exists; inspection is required"
            ) from exc
        except OSError as exc:
            raise BuildExecutionPortError("local build workspace could not be created") from exc
        return ensure_real_directory_root(root, label="local build job root")

    @staticmethod
    def _cleanup_unprepared_job_root(job_root: pathlib.Path) -> None:
        try:
            job_root.rmdir()
        except OSError:
            pass

    def _collect_changed_files(
        self,
        plan,
        *,
        max_changed_files: int,
    ) -> tuple[ChangedFile, ...]:
        prefix = (
            self.git_executable,
            *plan.config_args,
            "--git-dir",
            str(plan.private_git_dir),
            "--work-tree",
            str(plan.worktree_root),
        )
        cwd = plan.private_git_dir.parent
        modified = _git(
            (
                *prefix,
                "diff",
                "--name-only",
                "--no-renames",
                "--diff-filter=ACMRTUXB",
                "-z",
                "HEAD",
                "--",
            ),
            cwd=cwd,
            environment=plan.environment,
        ).stdout
        deleted = _git(
            (
                *prefix,
                "diff",
                "--name-only",
                "--no-renames",
                "--diff-filter=D",
                "-z",
                "HEAD",
                "--",
            ),
            cwd=cwd,
            environment=plan.environment,
        ).stdout
        if _nul_paths(deleted):
            raise BuildExecutionPortError(
                "contained local build deleted source paths; output is not admissible"
            )
        untracked = _git(
            (
                *prefix,
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
            ),
            cwd=cwd,
            environment=plan.environment,
        ).stdout
        raw_paths = {*_nul_paths(modified), *_nul_paths(untracked)}
        if len(raw_paths) > max_changed_files:
            raise BuildExecutionPortError(
                "contained local build exceeded the changed-file budget"
            )

        records: list[ChangedFile] = []
        canonical_seen: set[str] = set()
        for raw in sorted(raw_paths, key=str.casefold):
            try:
                canonical = normalize_relative_path(raw).as_posix()
                ensure_worker_mutation_path(canonical)
            except (ValueError, WorkspaceSecurityError) as exc:
                raise BuildExecutionPortError(
                    "contained local build produced an unsafe changed-file path"
                ) from exc
            if canonical in canonical_seen:
                raise BuildExecutionPortError(
                    "contained local build produced duplicate changed-file identity"
                )
            canonical_seen.add(canonical)
            path = ensure_path_policy(
                plan.worktree_root,
                canonical,
                WorkspacePathPolicy((canonical,)),
                must_exist=True,
            )
            digest, size = _hash_regular_file(
                path,
                max_bytes=_MAX_LOCAL_BUILD_FILE_BYTES,
            )
            records.append(ChangedFile(canonical, digest, size))
        return tuple(records)

    def _success_or_invalid_artifact(
        self,
        dispatch: BuildExecutionDispatch,
        artifact_binding: LocalBuildArtifactBinding,
        worktree_root: pathlib.Path,
        changed_files: tuple[ChangedFile, ...],
        process_digest: str,
    ) -> BuildExecutionResult:
        artifact_path = ensure_path_policy(
            worktree_root,
            artifact_binding.artifact_relpath,
            WorkspacePathPolicy((artifact_binding.artifact_relpath,)),
            must_exist=True,
        )
        artifact_digest, artifact_size = _hash_regular_file(
            artifact_path,
            max_bytes=artifact_binding.max_artifact_bytes,
        )
        changed = {item.path: item for item in changed_files}
        artifact_evidence = changed.get(artifact_binding.artifact_relpath)
        if (
            artifact_evidence is None
            or artifact_evidence.sha256 != artifact_digest
            or artifact_evidence.size_bytes != artifact_size
        ):
            return _definite_failed_result(
                dispatch,
                process_digest,
                reason="artifact-not-produced",
            )
        return BuildExecutionResult(
            source_sha=dispatch.source_sha,
            artifact_digest=artifact_digest,
            succeeded=True,
            uncertain=False,
            evidence_refs=(
                f"local-build-process:sha256:{process_digest}",
                f"local-build-artifact:sha256:{artifact_digest}",
            ),
            completed_at=datetime.now(UTC),
        )

    def _cleanup_plan(self, plan, job_root: pathlib.Path) -> Exception | None:
        try:
            cleanup_private_git_workspace(plan)
            try:
                job_root.rmdir()
            except OSError:
                # Any unexplained residue means cleanup cannot be certified.
                return RuntimeError("local build job root retained unexpected residue")
            return None
        except Exception as exc:
            return exc

    def _claim_dispatch(self, fingerprint: str) -> None:
        path = self._state_root / f"dispatch-{fingerprint}.lock"
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise BuildExecutionPortError(
                "local build dispatch was already claimed; inspection is required"
            ) from exc
        except OSError as exc:
            raise BuildExecutionPortError(
                "local build dispatch claim could not be persisted"
            ) from exc
        try:
            payload = (fingerprint + "\n").encode("ascii")
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _receipt_path(self, fingerprint: str) -> pathlib.Path:
        return self._state_root / f"receipt-{fingerprint}.json"

    def _save_receipt(
        self,
        fingerprint: str,
        result: BuildExecutionResult,
        changed_files: tuple[ChangedFile, ...],
    ) -> None:
        payload = {
            "schema": _RECEIPT_SCHEMA,
            "dispatch_fingerprint": fingerprint,
            "result": {
                "source_sha": result.source_sha,
                "artifact_digest": result.artifact_digest,
                "succeeded": result.succeeded,
                "uncertain": result.uncertain,
                "evidence_refs": list(result.evidence_refs),
                "completed_at": result.completed_at.astimezone(UTC).isoformat(),
            },
            "changed_files": [
                {
                    "path": item.path,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in changed_files
            ],
        }
        canonical = _canonical_json(payload)
        envelope = {
            "payload": payload,
            "checksum": hashlib.sha256(canonical).hexdigest(),
        }
        raw = _canonical_json(envelope)
        if len(raw) > _MAX_RECEIPT_BYTES:
            raise BuildExecutionPortError("local build provider receipt exceeds safe size")

        target = self._receipt_path(fingerprint)
        if target.exists() or target.is_symlink():
            raise BuildExecutionPortError("local build provider receipt already exists")
        temporary = self._state_root / f".receipt-{fingerprint}-{os.getpid()}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                os.write(descriptor, raw)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if target.exists() or target.is_symlink():
                raise BuildExecutionPortError("local build provider receipt raced")
            os.replace(temporary, target)
        except Exception as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            if isinstance(exc, BuildExecutionPortError):
                raise
            raise BuildExecutionPortError(
                "local build provider result could not be persisted"
            ) from exc

    def _load_receipt(self, fingerprint: str) -> _LocalBuildReceipt | None:
        path = self._receipt_path(fingerprint)
        if not path.exists() and not path.is_symlink():
            return None
        try:
            _require_regular_file(path)
            raw = path.read_bytes()
            if not raw or len(raw) > _MAX_RECEIPT_BYTES:
                raise ValueError("receipt size is invalid")
            text = raw.decode("utf-8", errors="strict")
            envelope = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_json_constant,
            )
            if type(envelope) is not dict or set(envelope) != {"payload", "checksum"}:
                raise ValueError("receipt envelope schema is invalid")
            payload = envelope["payload"]
            checksum = envelope["checksum"]
            if (
                type(checksum) is not str
                or checksum != hashlib.sha256(_canonical_json(payload)).hexdigest()
            ):
                raise ValueError("receipt checksum is invalid")
            receipt = _receipt_from_payload(payload)
            if receipt.dispatch_fingerprint != fingerprint:
                raise ValueError("receipt dispatch identity is invalid")
            return receipt
        except Exception as exc:
            raise BuildExecutionPortError(
                "local build provider receipt is corrupt or untrusted"
            ) from exc


def local_build_platform() -> Platform:
    if os.name == "nt":
        return Platform.WINDOWS
    if sys.platform.startswith("linux"):
        return Platform.LINUX
    if sys.platform == "darwin":
        return Platform.MACOS
    raise ValueError("contained local build node does not support this host platform")


def _readmit_dispatch(dispatch: object) -> BuildExecutionDispatch:
    if type(dispatch) is not BuildExecutionDispatch:
        raise BuildExecutionPortError("local build dispatch carrier is invalid")
    grant = dispatch.grant
    if type(grant) is not ExecutionGrant:
        raise BuildExecutionPortError("local build execution grant carrier is invalid")
    tuple_fields = (
        grant.allowed_node_ids,
        grant.network_scopes,
        grant.credential_refs,
        grant.argv,
        grant.authority_evidence_refs,
    )
    if any(type(value) is not tuple for value in tuple_fields):
        raise BuildExecutionPortError("local build grant collections must be exact tuples")
    if any(
        type(value) is not str
        for value in (
            dispatch.dispatch_id,
            dispatch.project_id,
            dispatch.work_id,
            dispatch.node_id,
            dispatch.source_sha,
            grant.project_id,
            grant.repository_id,
            grant.work_id,
            grant.workspace_relpath,
            grant.command_id,
            *grant.allowed_node_ids,
            *grant.network_scopes,
            *grant.credential_refs,
            *grant.argv,
            *grant.authority_evidence_refs,
        )
    ):
        raise BuildExecutionPortError("local build dispatch identities must be exact text")
    if type(dispatch.platform) is not Platform or type(dispatch.attempt) is not int:
        raise BuildExecutionPortError("local build dispatch scalar carriers are invalid")
    try:
        copied_grant = ExecutionGrant(
            project_id=grant.project_id,
            repository_id=grant.repository_id,
            work_id=grant.work_id,
            workspace_relpath=grant.workspace_relpath,
            allowed_node_ids=tuple(grant.allowed_node_ids),
            network_scopes=tuple(grant.network_scopes),
            credential_refs=tuple(grant.credential_refs),
            command_id=grant.command_id,
            argv=tuple(grant.argv),
            authority_evidence_refs=tuple(grant.authority_evidence_refs),
        )
        return BuildExecutionDispatch(
            dispatch_id=dispatch.dispatch_id,
            project_id=dispatch.project_id,
            work_id=dispatch.work_id,
            node_id=dispatch.node_id,
            platform=dispatch.platform,
            source_sha=dispatch.source_sha,
            grant=copied_grant,
            attempt=dispatch.attempt,
        )
    except (TypeError, ValueError) as exc:
        raise BuildExecutionPortError("local build dispatch is structurally invalid") from exc


def _snapshot_environment(source: Mapping[str, str] | None) -> dict[str, str]:
    try:
        raw = dict(os.environ if source is None else source)
    except (TypeError, ValueError) as exc:
        raise ValueError("local build environment must be a mapping") from exc
    result: dict[str, str] = {}
    for key, value in raw.items():
        if type(key) is not str or type(value) is not str or "\x00" in key or "\x00" in value:
            raise ValueError("local build environment must contain exact NUL-free text")
        result[key] = value
    return result


def _dispatch_fingerprint(dispatch: BuildExecutionDispatch) -> str:
    payload = {
        "dispatch_id": dispatch.dispatch_id,
        "project_id": dispatch.project_id,
        "work_id": dispatch.work_id,
        "node_id": dispatch.node_id,
        "platform": dispatch.platform.value,
        "source_sha": dispatch.source_sha,
        "attempt": dispatch.attempt,
        "grant": {
            "project_id": dispatch.grant.project_id,
            "repository_id": dispatch.grant.repository_id,
            "work_id": dispatch.grant.work_id,
            "workspace_relpath": dispatch.grant.workspace_relpath,
            "allowed_node_ids": list(dispatch.grant.allowed_node_ids),
            "network_scopes": list(dispatch.grant.network_scopes),
            "credential_refs": list(dispatch.grant.credential_refs),
            "command_id": dispatch.grant.command_id,
            "argv": list(dispatch.grant.argv),
            "authority_evidence_refs": list(dispatch.grant.authority_evidence_refs),
        },
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _process_evidence_digest(dispatch: BuildExecutionDispatch, process) -> str:
    payload = {
        "dispatch_fingerprint": _dispatch_fingerprint(dispatch),
        "argv": list(process.argv),
        "returncode": process.returncode,
        "stdout_sha256": hashlib.sha256(process.stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(process.stderr.encode("utf-8")).hexdigest(),
        "timed_out": process.timed_out,
        "cancelled": process.cancelled,
        "output_limit_exceeded": process.output_limit_exceeded,
        "isolation_class": process.isolation_class.value,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _definite_failed_result(
    dispatch: BuildExecutionDispatch,
    process_digest: str,
    *,
    reason: str,
) -> BuildExecutionResult:
    return BuildExecutionResult(
        source_sha=dispatch.source_sha,
        artifact_digest=process_digest,
        succeeded=False,
        uncertain=False,
        evidence_refs=(f"local-build-{reason}:sha256:{process_digest}",),
        completed_at=datetime.now(UTC),
    )


def _uncertain_result(
    dispatch: BuildExecutionDispatch,
    process_digest: str,
) -> BuildExecutionResult:
    digest = hashlib.sha256(
        ("uncertain\0" + _dispatch_fingerprint(dispatch) + "\0" + process_digest).encode(
            "ascii"
        )
    ).hexdigest()
    return BuildExecutionResult(
        source_sha=dispatch.source_sha,
        artifact_digest=digest,
        succeeded=False,
        uncertain=True,
        evidence_refs=(f"local-build-uncertain:sha256:{digest}",),
        completed_at=datetime.now(UTC),
    )


def _nul_paths(raw: str) -> tuple[str, ...]:
    values = tuple(item for item in raw.split("\x00") if item)
    if any(type(item) is not str or not item for item in values):
        raise BuildExecutionPortError("local build Git evidence contains invalid paths")
    return values


def _hash_regular_file(path: pathlib.Path, *, max_bytes: int) -> tuple[str, int]:
    _require_regular_file(path)
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            if size > max_bytes:
                raise BuildExecutionPortError("local build output exceeds trusted size limit")
            digest.update(chunk)
    return digest.hexdigest(), size


def _require_regular_file(path: pathlib.Path) -> None:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(info.st_mode) or bool(reparse_flag and attributes & reparse_flag):
        raise BuildExecutionPortError("local build evidence refuses links or reparse points")
    if not stat.S_ISREG(info.st_mode):
        raise BuildExecutionPortError("local build evidence requires a regular file")


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("local build receipt payload is not canonical JSON") from exc


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("local build receipt contains duplicate/nontext keys")
        result[key] = value
    return result


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON constant is invalid: {value}")


def _receipt_from_payload(payload: object) -> _LocalBuildReceipt:
    if type(payload) is not dict or set(payload) != {
        "schema",
        "dispatch_fingerprint",
        "result",
        "changed_files",
    }:
        raise ValueError("local build receipt payload schema is invalid")
    if payload["schema"] != _RECEIPT_SCHEMA:
        raise ValueError("local build receipt schema version is invalid")
    fingerprint = payload["dispatch_fingerprint"]
    if (
        type(fingerprint) is not str
        or len(fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in fingerprint)
    ):
        raise ValueError("local build receipt fingerprint is invalid")
    result_payload = payload["result"]
    if type(result_payload) is not dict or set(result_payload) != {
        "source_sha",
        "artifact_digest",
        "succeeded",
        "uncertain",
        "evidence_refs",
        "completed_at",
    }:
        raise ValueError("local build receipt result schema is invalid")
    refs = result_payload["evidence_refs"]
    if type(refs) is not list or any(type(item) is not str for item in refs):
        raise ValueError("local build receipt evidence references are invalid")
    if (
        type(result_payload["succeeded"]) is not bool
        or type(result_payload["uncertain"]) is not bool
        or type(result_payload["completed_at"]) is not str
    ):
        raise ValueError("local build receipt result scalar carriers are invalid")
    completed = datetime.fromisoformat(result_payload["completed_at"])
    result = BuildExecutionResult(
        source_sha=result_payload["source_sha"],
        artifact_digest=result_payload["artifact_digest"],
        succeeded=result_payload["succeeded"],
        uncertain=result_payload["uncertain"],
        evidence_refs=tuple(refs),
        completed_at=completed,
    )
    changed_raw = payload["changed_files"]
    if type(changed_raw) is not list:
        raise ValueError("local build receipt changed-files collection is invalid")
    changed: list[ChangedFile] = []
    seen: set[str] = set()
    for item in changed_raw:
        if type(item) is not dict or set(item) != {"path", "sha256", "size_bytes"}:
            raise ValueError("local build receipt changed-file schema is invalid")
        if (
            type(item["path"]) is not str
            or type(item["sha256"]) is not str
            or type(item["size_bytes"]) is not int
        ):
            raise ValueError("local build receipt changed-file scalar carriers are invalid")
        record = ChangedFile(item["path"], item["sha256"], item["size_bytes"])
        if record.path in seen:
            raise ValueError("local build receipt repeats changed-file identity")
        seen.add(record.path)
        changed.append(record)
    return _LocalBuildReceipt(
        dispatch_fingerprint=fingerprint,
        result=result,
        changed_files=tuple(changed),
    )
