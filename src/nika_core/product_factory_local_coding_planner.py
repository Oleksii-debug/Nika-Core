from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    PrivacyClass,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.toolsmith.contracts import CodingJob
from nika_core.toolsmith.execution import _resolve_host_git_executable
from nika_core.toolsmith.local_worker import LocalCodingPlan, LocalFileEdit
from nika_core.toolsmith.workspace_security import (
    WorkspacePathPolicy,
    WorkspaceSecurityError,
    ensure_real_directory_root,
    normalize_job_relative_path,
    sterile_git_environment,
)

_PLAN_SCHEMA = "nika.local-coding-plan:v1"
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 20_000
_DEFAULT_MAX_SOURCE_FILES = 128
_DEFAULT_MAX_FILE_BYTES = 128 * 1024
_DEFAULT_MAX_SOURCE_BYTES = 512 * 1024
_DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_GIT_OUTPUT_BYTES = 2 * 1024 * 1024
_DEFAULT_GIT_TIMEOUT_SECONDS = 30


class ModelGatewayLocalCodingPlannerError(RuntimeError):
    """Raised when a model-backed local coding plan cannot be admitted safely."""


@dataclass(frozen=True, slots=True)
class _SnapshotFile:
    path: str
    content: str
    size_bytes: int


@dataclass(slots=True)
class ModelGatewayLocalCodingPlanner:
    """Provider-neutral bounded planner for the contained-local CodingWorker.

    The planner is intentionally not a mutation authority. It reads immutable Git
    objects at the exact CodingJob base SHA, sends only a bounded UTF-8 snapshot
    through the already-authorized ModelGateway route, and converts a strict JSON
    response into the incumbent LocalCodingPlan carrier. The contained-local worker
    remains responsible for workspace mutation, testing, recovery and evidence.
    """

    gateway: ModelGateway
    repositories: Mapping[str, pathlib.Path]
    provider_id: str | None = None
    provider_kind: ProviderKind | None = None
    model: str | None = None
    fallback_provider_ids: tuple[str, ...] = ()
    timeout_seconds: float = 60.0
    max_source_files: int = _DEFAULT_MAX_SOURCE_FILES
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES
    max_source_bytes: int = _DEFAULT_MAX_SOURCE_BYTES
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES
    max_git_output_bytes: int = _DEFAULT_MAX_GIT_OUTPUT_BYTES
    git_executable: str = "git"
    source_environment: Mapping[str, str] | None = None
    _repositories: Mapping[str, pathlib.Path] = field(init=False, repr=False)
    _environment: Mapping[str, str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.gateway, ModelGateway):
            raise TypeError("gateway must be a ModelGateway")
        if self.provider_id is None and self.provider_kind is None:
            raise ValueError("an explicit ModelGateway provider route is required")
        if self.provider_kind is not None and not isinstance(self.provider_kind, ProviderKind):
            raise TypeError("provider_kind must be a ProviderKind")
        if type(self.fallback_provider_ids) is not tuple:
            raise TypeError("fallback_provider_ids must be a tuple")
        for name, value in (
            ("max_source_files", self.max_source_files),
            ("max_file_bytes", self.max_file_bytes),
            ("max_source_bytes", self.max_source_bytes),
            ("max_response_bytes", self.max_response_bytes),
            ("max_git_output_bytes", self.max_git_output_bytes),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_file_bytes > self.max_source_bytes:
            raise ValueError("max_file_bytes cannot exceed max_source_bytes")
        if type(self.timeout_seconds) not in (int, float) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        copied: dict[str, pathlib.Path] = {}
        for repository_id, raw_root in self.repositories.items():
            identity = _canonical_text(repository_id, "repository identity", max_bytes=512)
            root = ensure_real_directory_root(
                pathlib.Path(raw_root),
                label=f"planner repository root {identity}",
            )
            if not (root / ".git").exists():
                raise ModelGatewayLocalCodingPlannerError(
                    "planner repository must expose trusted Git metadata"
                )
            copied[identity] = root
        if not copied:
            raise ModelGatewayLocalCodingPlannerError(
                "planner requires at least one trusted repository"
            )
        self._repositories = MappingProxyType(copied)
        self.git_executable = _resolve_host_git_executable(self.git_executable)
        self._environment = MappingProxyType(
            sterile_git_environment(
                os.environ if self.source_environment is None else self.source_environment
            )
        )

    async def plan(self, job: CodingJob) -> LocalCodingPlan:
        if type(job) is not CodingJob:
            raise ModelGatewayLocalCodingPlannerError(
                "planner requires the exact CodingJob carrier"
            )
        repository_root = self._repositories.get(job.repository.repository_id)
        if repository_root is None:
            raise ModelGatewayLocalCodingPlannerError(
                "coding repository is not explicitly authorized for this planner"
            )

        snapshot, snapshot_digest = self._snapshot_source(repository_root, job)
        request = self._request(job, snapshot, snapshot_digest)
        response = await self.gateway.complete(request)
        return self._decode_plan(
            response.text,
            job=job,
            existing_paths=tuple(item.path for item in snapshot),
        )

    def _snapshot_source(
        self,
        repository_root: pathlib.Path,
        job: CodingJob,
    ) -> tuple[tuple[_SnapshotFile, ...], str]:
        base_sha = _exact_git_sha(job.repository.base_sha, "base SHA")
        commit = self._git_text(
            repository_root,
            "rev-parse",
            "--verify",
            f"{base_sha}^{{commit}}",
        ).strip().casefold()
        if commit != base_sha:
            raise ModelGatewayLocalCodingPlannerError(
                "planner repository does not contain the exact requested base commit"
            )
        tree = self._git_text(
            repository_root,
            "rev-parse",
            "--verify",
            f"{base_sha}^{{tree}}",
        ).strip().casefold()
        if tree != job.repository.tree_digest.casefold():
            raise ModelGatewayLocalCodingPlannerError(
                "planner repository tree identity differs from the CodingJob"
            )

        roots = tuple(
            normalize_job_relative_path(root).as_posix()
            for root in job.allowed_paths.roots
        )
        policy = WorkspacePathPolicy(roots)
        listing = self._git_bytes(
            repository_root,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            base_sha,
            "--",
            *roots,
        )
        entries: list[tuple[str, str]] = []
        seen_paths: set[str] = set()
        for record in listing.split(b"\x00"):
            if not record:
                continue
            try:
                header, raw_path = record.split(b"\t", 1)
                mode_raw, type_raw, object_raw = header.split(b" ", 2)
                mode = mode_raw.decode("ascii", errors="strict")
                object_type = type_raw.decode("ascii", errors="strict")
                object_id = object_raw.decode("ascii", errors="strict").casefold()
                path = raw_path.decode("utf-8", errors="strict")
            except (UnicodeDecodeError, ValueError) as exc:
                raise ModelGatewayLocalCodingPlannerError(
                    "Git snapshot contains an undecodable tree entry"
                ) from exc
            normalized = normalize_job_relative_path(path)
            if normalized.as_posix() != path or not policy.allows(path):
                raise ModelGatewayLocalCodingPlannerError(
                    "Git snapshot path violates the declared component scope"
                )
            if any(ord(character) < 32 or ord(character) == 127 for character in path):
                raise ModelGatewayLocalCodingPlannerError(
                    "Git snapshot path contains control data"
                )
            folded = path.casefold()
            if folded in seen_paths:
                raise ModelGatewayLocalCodingPlannerError(
                    "Git snapshot repeats a case-insensitive path identity"
                )
            seen_paths.add(folded)
            if mode not in {"100644", "100755"} or object_type != "blob":
                raise ModelGatewayLocalCodingPlannerError(
                    "Git snapshot contains a symlink, submodule or unsupported entry"
                )
            _exact_git_object_id(object_id)
            entries.append((path, object_id))
            if len(entries) > self.max_source_files:
                raise ModelGatewayLocalCodingPlannerError(
                    "Git snapshot exceeds the source-file limit"
                )

        files: list[_SnapshotFile] = []
        total_bytes = 0
        digest = hashlib.sha256()
        for path, object_id in sorted(entries, key=lambda item: item[0].casefold()):
            raw_size = self._git_text(
                repository_root,
                "cat-file",
                "-s",
                object_id,
            ).strip()
            try:
                size = int(raw_size)
            except ValueError as exc:
                raise ModelGatewayLocalCodingPlannerError(
                    "Git blob size is invalid"
                ) from exc
            if size < 0 or size > self.max_file_bytes:
                raise ModelGatewayLocalCodingPlannerError(
                    f"source file exceeds the planner byte limit: {path}"
                )
            total_bytes += size
            if total_bytes > self.max_source_bytes:
                raise ModelGatewayLocalCodingPlannerError(
                    "Git snapshot exceeds the total source-byte limit"
                )
            raw = self._git_bytes(repository_root, "cat-file", "blob", object_id)
            if len(raw) != size:
                raise ModelGatewayLocalCodingPlannerError(
                    "Git blob size changed during planner snapshot"
                )
            if b"\x00" in raw:
                raise ModelGatewayLocalCodingPlannerError(
                    f"binary source is not admitted to the planner: {path}"
                )
            try:
                text = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ModelGatewayLocalCodingPlannerError(
                    f"non-UTF-8 source is not admitted to the planner: {path}"
                ) from exc
            digest.update(path.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(object_id.encode("ascii"))
            digest.update(b"\x00")
            digest.update(raw)
            digest.update(b"\x00")
            files.append(_SnapshotFile(path=path, content=text, size_bytes=size))

        tree_after = self._git_text(
            repository_root,
            "rev-parse",
            "--verify",
            f"{base_sha}^{{tree}}",
        ).strip().casefold()
        if tree_after != tree:
            raise ModelGatewayLocalCodingPlannerError(
                "planner source authority changed during snapshot"
            )
        return tuple(files), digest.hexdigest()

    def _request(
        self,
        job: CodingJob,
        snapshot: tuple[_SnapshotFile, ...],
        snapshot_digest: str,
    ) -> ModelRequest:
        user_payload = {
            "schema": "nika.local-coding-context:v1",
            "job_id": job.job_id,
            "goal": job.goal,
            "repository": {
                "repository_id": job.repository.repository_id,
                "base_sha": job.repository.base_sha.casefold(),
                "tree_digest": job.repository.tree_digest.casefold(),
                "snapshot_sha256": snapshot_digest,
            },
            "allowed_paths": [
                normalize_job_relative_path(root).as_posix()
                for root in job.allowed_paths.roots
            ],
            "acceptance_commands": [
                {
                    "argv": list(command.argv),
                    "cwd": command.cwd,
                    "timeout_seconds": command.timeout_seconds,
                }
                for command in job.acceptance_commands
            ],
            "files": [
                {
                    "path": item.path,
                    "content": item.content,
                    "size_bytes": item.size_bytes,
                }
                for item in snapshot
            ],
        }
        user_text = json.dumps(
            user_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        request_hash = hashlib.sha256(
            (job.job_id + "\x00" + user_text).encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:32]
        return ModelRequest(
            request_id=f"product-factory-local-plan-{request_hash}",
            messages=(
                ModelMessage(
                    role="system",
                    content=(
                        "You are a bounded source-edit planner. Return only one JSON object "
                        'with exact schema {"schema":"nika.local-coding-plan:v1",'
                        '"edits":[{"path":"relative/file","content":"full UTF-8 replacement"}]}. '
                        "Each edit is a complete file replacement or addition. Do not return "
                        "Markdown, explanations, deletions, binary data, .git paths, GitHub "
                        "workflow/action control-plane edits, or paths outside allowed_paths. "
                        "Use only the supplied immutable source snapshot and goal."
                    ),
                ),
                ModelMessage(role="user", content=user_text),
            ),
            model=self.model,
            provider_id=self.provider_id,
            provider_kind=self.provider_kind,
            fallback_provider_ids=self.fallback_provider_ids,
            privacy=PrivacyClass.PRIVATE,
            timeout_seconds=self.timeout_seconds,
            temperature=0.0,
            metadata={"purpose": "product-factory-local-coding-plan"},
        )

    def _decode_plan(
        self,
        text: str,
        *,
        job: CodingJob,
        existing_paths: tuple[str, ...],
    ) -> LocalCodingPlan:
        if type(text) is not str:
            raise ModelGatewayLocalCodingPlannerError(
                "model planner response must be text"
            )
        try:
            raw = text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ModelGatewayLocalCodingPlannerError(
                "model planner response is not valid UTF-8"
            ) from exc
        if not raw or len(raw) > self.max_response_bytes:
            raise ModelGatewayLocalCodingPlannerError(
                "model planner response exceeds the byte limit"
            )
        if not _json_depth_is_bounded(raw):
            raise ModelGatewayLocalCodingPlannerError(
                "model planner response exceeds the JSON depth limit"
            )
        try:
            payload = json.loads(
                raw,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ModelGatewayLocalCodingPlannerError(
                "model planner response is not strict JSON"
            ) from exc
        if _json_node_count(payload) > _MAX_JSON_NODES:
            raise ModelGatewayLocalCodingPlannerError(
                "model planner response exceeds the JSON node limit"
            )
        if type(payload) is not dict or set(payload) != {"schema", "edits"}:
            raise ModelGatewayLocalCodingPlannerError(
                "model planner response has unexpected fields"
            )
        if payload["schema"] != _PLAN_SCHEMA:
            raise ModelGatewayLocalCodingPlannerError(
                "model planner response schema is unsupported"
            )
        raw_edits = payload["edits"]
        if type(raw_edits) is not list or not raw_edits:
            raise ModelGatewayLocalCodingPlannerError(
                "model planner response requires at least one edit"
            )
        if len(raw_edits) > job.resource_budget.max_changed_files:
            raise ModelGatewayLocalCodingPlannerError(
                "model planner response exceeds the changed-file budget"
            )

        existing_case = {path.casefold(): path for path in existing_paths}
        edits: list[LocalFileEdit] = []
        for raw_edit in raw_edits:
            if type(raw_edit) is not dict or set(raw_edit) != {"path", "content"}:
                raise ModelGatewayLocalCodingPlannerError(
                    "model planner edit has unexpected fields"
                )
            path = raw_edit["path"]
            content = raw_edit["content"]
            if type(path) is not str or type(content) is not str:
                raise ModelGatewayLocalCodingPlannerError(
                    "model planner edit path/content must be text"
                )
            if not job.allowed_paths.allows(path):
                raise ModelGatewayLocalCodingPlannerError(
                    f"model planner edit escapes allowed scope: {path}"
                )
            prior_spelling = existing_case.get(path.casefold())
            if prior_spelling is not None and prior_spelling != path:
                raise ModelGatewayLocalCodingPlannerError(
                    "model planner edit changes existing path casing"
                )
            try:
                content_bytes = content.encode("utf-8", errors="strict")
                edit = LocalFileEdit(path=path, content=content_bytes)
            except (UnicodeEncodeError, ValueError, WorkspaceSecurityError) as exc:
                raise ModelGatewayLocalCodingPlannerError(
                    f"model planner edit is not admissible: {path}"
                ) from exc
            edits.append(edit)
        try:
            return LocalCodingPlan(tuple(edits))
        except ValueError as exc:
            raise ModelGatewayLocalCodingPlannerError(
                "model planner response does not form a valid local coding plan"
            ) from exc

    def _git_text(self, repository_root: pathlib.Path, *arguments: str) -> str:
        raw = self._git_bytes(repository_root, *arguments)
        try:
            return raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ModelGatewayLocalCodingPlannerError(
                "Git metadata output is not valid UTF-8"
            ) from exc

    def _git_bytes(self, repository_root: pathlib.Path, *arguments: str) -> bytes:
        try:
            with tempfile.SpooledTemporaryFile(
                max_size=self.max_git_output_bytes,
                mode="w+b",
            ) as output:
                result = subprocess.run(
                    (self.git_executable, *arguments),
                    cwd=repository_root,
                    env=dict(self._environment),
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
                    check=False,
                )
                if result.returncode != 0:
                    raise ModelGatewayLocalCodingPlannerError(
                        f"planner Git command failed (exit {result.returncode})"
                    )
                size = output.tell()
                if size > self.max_git_output_bytes:
                    raise ModelGatewayLocalCodingPlannerError(
                        "planner Git command output exceeds the byte limit"
                    )
                output.seek(0)
                raw = output.read(self.max_git_output_bytes + 1)
        except ModelGatewayLocalCodingPlannerError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise ModelGatewayLocalCodingPlannerError(
                "planner Git command could not be executed"
            ) from exc
        if len(raw) > self.max_git_output_bytes:
            raise ModelGatewayLocalCodingPlannerError(
                "planner Git command output exceeds the byte limit"
            )
        return bytes(raw)


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


def _exact_git_sha(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 40:
        raise ModelGatewayLocalCodingPlannerError(f"{label} must be a 40-character Git SHA")
    lowered = value.casefold()
    if any(character not in "0123456789abcdef" for character in lowered):
        raise ModelGatewayLocalCodingPlannerError(f"{label} must be hexadecimal")
    return lowered


def _exact_git_object_id(value: str) -> str:
    if len(value) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ModelGatewayLocalCodingPlannerError("Git object identity is invalid")
    return value


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON value is forbidden: {value}")


def _json_depth_is_bounded(raw: bytes) -> bool:
    depth = 0
    quoted = False
    escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                quoted = False
        elif byte == 0x22:
            quoted = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                return False
        elif byte in (0x5D, 0x7D):
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not quoted and not escaped


def _json_node_count(value: object) -> int:
    stack = [value]
    count = 0
    while stack:
        current = stack.pop()
        count += 1
        if count > _MAX_JSON_NODES:
            return count
        if type(current) is dict:
            stack.extend(current.keys())
            stack.extend(current.values())
        elif type(current) is list:
            stack.extend(current)
    return count
