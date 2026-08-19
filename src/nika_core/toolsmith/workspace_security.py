from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from os import environ, name
from pathlib import Path, PurePosixPath, PureWindowsPath
from stat import FILE_ATTRIBUTE_REPARSE_POINT, S_ISLNK

from .contracts import IsolationClass


_WINDOWS_RESERVED_BASENAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)
_SHELL_EXECUTABLES = frozenset(
    {
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "bash",
        "bash.exe",
        "sh",
        "sh.exe",
        "wsl",
        "wsl.exe",
    }
)
_GIT_CREDENTIAL_VARIABLES = frozenset(
    {
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GCM_INTERACTIVE",
        "GCM_CREDENTIAL_STORE",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITLAB_TOKEN",
        "BITBUCKET_TOKEN",
    }
)
_ALLOWED_ENVIRONMENT_VARIABLES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
    }
)


class WorkspaceSecurityError(ValueError):
    """Raised when a workspace or process request cannot be proven policy-safe."""


@dataclass(frozen=True, slots=True)
class WorkspacePathPolicy:
    allowed_roots: tuple[str, ...]
    reject_reparse_points: bool = True

    def __post_init__(self) -> None:
        if not self.allowed_roots:
            raise WorkspaceSecurityError("at least one allowed workspace root is required")
        for root in self.allowed_roots:
            normalize_job_relative_path(root)

    def allows(self, value: str) -> bool:
        candidate = normalize_job_relative_path(value)
        roots = tuple(normalize_job_relative_path(root) for root in self.allowed_roots)
        return any(candidate == root or root in candidate.parents for root in roots)


@dataclass(frozen=True, slots=True)
class SterileGitPlan:
    repository_root: Path
    private_git_dir: Path
    worktree_root: Path
    branch_name: str
    base_sha: str
    environment: Mapping[str, str]
    config_args: tuple[str, ...]
    isolation_class: IsolationClass = IsolationClass.POLICY_ONLY

    def __post_init__(self) -> None:
        if not self.branch_name.strip():
            raise WorkspaceSecurityError("branch_name must not be empty")
        if len(self.base_sha) != 40 or any(
            character not in "0123456789abcdef" for character in self.base_sha.lower()
        ):
            raise WorkspaceSecurityError("base_sha must be a 40-character hexadecimal SHA")
        if self.private_git_dir == self.repository_root / ".git":
            raise WorkspaceSecurityError("production .git metadata cannot be worker metadata")
        if self.private_git_dir == self.worktree_root / ".git":
            raise WorkspaceSecurityError("worker-visible .git metadata is forbidden")
        if self.isolation_class is not IsolationClass.POLICY_ONLY:
            raise WorkspaceSecurityError(
                "workspace plan is policy-only and must not overclaim isolation"
            )


@dataclass(frozen=True, slots=True)
class FileEvidence:
    path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class TreeEvidence:
    files: tuple[FileEvidence, ...]
    digest: str
    total_bytes: int


@dataclass(frozen=True, slots=True)
class ProductionIntegritySnapshot:
    base_sha: str
    tree_digest: str

    def __post_init__(self) -> None:
        if len(self.base_sha) != 40 or any(
            character not in "0123456789abcdef" for character in self.base_sha.lower()
        ):
            raise WorkspaceSecurityError("base_sha must be a 40-character hexadecimal SHA")
        if len(self.tree_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.tree_digest.lower()
        ):
            raise WorkspaceSecurityError("tree_digest must be a hexadecimal sha256")


def _windows_component_is_reserved(component: str) -> bool:
    trimmed = component.rstrip(" .")
    if trimmed != component or not trimmed:
        return True
    stem = trimmed.split(".", 1)[0].casefold()
    return stem in _WINDOWS_RESERVED_BASENAMES


def normalize_job_relative_path(value: str) -> PurePosixPath:
    stripped = value.strip()
    if not stripped or stripped != value:
        raise WorkspaceSecurityError("path must be non-empty and must not have outer whitespace")
    if "\x00" in stripped:
        raise WorkspaceSecurityError("NUL is forbidden in paths")

    windows = PureWindowsPath(stripped)
    normalized_text = stripped.replace("\\", "/")
    posix = PurePosixPath(normalized_text)
    parts = tuple(part for part in posix.parts if part not in {"", "."})
    lowered_parts = tuple(part.casefold() for part in parts)

    if windows.drive or windows.root or posix.is_absolute() or normalized_text.startswith("//"):
        raise WorkspaceSecurityError("absolute, drive-qualified, UNC and device paths are forbidden")
    if not parts or ".." in parts:
        raise WorkspaceSecurityError("path traversal is forbidden")
    if ".git" in lowered_parts:
        raise WorkspaceSecurityError(".git is forbidden in worker-visible paths")
    if any(":" in part for part in parts):
        raise WorkspaceSecurityError("Windows ADS and colon-qualified paths are forbidden")
    if any(_windows_component_is_reserved(part) for part in parts):
        raise WorkspaceSecurityError("Windows reserved or trailing-dot/space path is forbidden")

    return PurePosixPath(*parts)


def ensure_path_policy(
    root: Path,
    relative_path: str,
    policy: WorkspacePathPolicy,
    *,
    must_exist: bool = False,
) -> Path:
    normalized = normalize_job_relative_path(relative_path)
    if not policy.allows(normalized.as_posix()):
        raise WorkspaceSecurityError("path is outside the allowed workspace roots")

    root_resolved = root.resolve(strict=True)
    candidate = root_resolved.joinpath(*normalized.parts)
    if must_exist:
        candidate.resolve(strict=True)

    current = root_resolved
    for component in normalized.parts:
        current = current / component
        if not current.exists() and not current.is_symlink():
            continue
        file_stat = current.lstat()
        if S_ISLNK(file_stat.st_mode):
            raise WorkspaceSecurityError("symbolic links are forbidden in guarded workspace paths")
        attributes = getattr(file_stat, "st_file_attributes", 0)
        if (
            policy.reject_reparse_points
            and FILE_ATTRIBUTE_REPARSE_POINT
            and attributes & FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise WorkspaceSecurityError("Windows reparse points are forbidden in guarded paths")

    resolved_parent = candidate.parent.resolve(strict=False)
    try:
        resolved_parent.relative_to(root_resolved)
    except ValueError as exc:
        raise WorkspaceSecurityError("resolved path escapes the workspace root") from exc
    return candidate


def sterile_git_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    source_env = dict(environ if source is None else source)
    environment = {
        key: value
        for key, value in source_env.items()
        if key.upper() in _ALLOWED_ENVIRONMENT_VARIABLES
        and key.upper() not in _GIT_CREDENTIAL_VARIABLES
    }
    null_device = "NUL" if name == "nt" else "/dev/null"
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": null_device,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }
    )
    return environment


def make_sterile_git_plan(
    *,
    repository_root: Path,
    job_root: Path,
    branch_name: str,
    base_sha: str,
    source_environment: Mapping[str, str] | None = None,
) -> SterileGitPlan:
    repository_root = repository_root.resolve(strict=False)
    job_root = job_root.resolve(strict=False)
    private_git_dir = job_root / "_nika_private_git"
    worktree_root = job_root / "worktree"

    try:
        job_root.relative_to(repository_root)
    except ValueError:
        pass
    else:
        raise WorkspaceSecurityError("job workspace must not be inside the production repository")

    if repository_root == job_root:
        raise WorkspaceSecurityError("job workspace must be distinct from production repository")

    config_args = (
        "-c",
        "credential.helper=",
        "-c",
        "core.hooksPath=NUL" if name == "nt" else "core.hooksPath=/dev/null",
        "-c",
        "protocol.file.allow=never",
        "-c",
        "protocol.ext.allow=never",
    )
    return SterileGitPlan(
        repository_root=repository_root,
        private_git_dir=private_git_dir,
        worktree_root=worktree_root,
        branch_name=branch_name,
        base_sha=base_sha,
        environment=sterile_git_environment(source_environment),
        config_args=config_args,
    )


def validate_typed_argv(
    argv: Sequence[str],
    allowed_executables: Iterable[str],
) -> tuple[str, ...]:
    if not argv or any(not argument or "\x00" in argument for argument in argv):
        raise WorkspaceSecurityError("argv must contain non-empty NUL-free arguments")
    executable = argv[0]
    basename = PureWindowsPath(executable).name.casefold()
    if basename in _SHELL_EXECUTABLES:
        raise WorkspaceSecurityError("generic shell entrypoints are forbidden")

    allowlist = {item.casefold() for item in allowed_executables if item.strip()}
    if not allowlist:
        raise WorkspaceSecurityError("allowed executable set must not be empty")
    if executable.casefold() not in allowlist and basename not in allowlist:
        raise WorkspaceSecurityError("executable is not explicitly allowlisted")
    return tuple(argv)


def _hash_file(path: Path, *, max_file_bytes: int) -> tuple[str, int]:
    digest = sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            if size > max_file_bytes:
                raise WorkspaceSecurityError(f"file exceeds evidence size limit: {path.name}")
            digest.update(chunk)
    return digest.hexdigest(), size


def collect_tree_evidence(
    root: Path,
    *,
    max_files: int = 2000,
    max_file_bytes: int = 32 * 1024 * 1024,
    max_total_bytes: int = 256 * 1024 * 1024,
) -> TreeEvidence:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise WorkspaceSecurityError("tree evidence root must be a directory")

    records: list[FileEvidence] = []
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(root).as_posix()
        normalize_job_relative_path(relative)
        file_stat = path.lstat()
        attributes = getattr(file_stat, "st_file_attributes", 0)
        if S_ISLNK(file_stat.st_mode) or (
            FILE_ATTRIBUTE_REPARSE_POINT and attributes & FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise WorkspaceSecurityError("tree evidence refuses symlinks and reparse points")
        if path.is_dir():
            continue
        if not path.is_file():
            raise WorkspaceSecurityError("tree evidence refuses non-regular filesystem entries")
        if len(records) >= max_files:
            raise WorkspaceSecurityError("tree evidence file-count limit exceeded")
        file_sha256, size_bytes = _hash_file(path, max_file_bytes=max_file_bytes)
        total_bytes += size_bytes
        if total_bytes > max_total_bytes:
            raise WorkspaceSecurityError("tree evidence total-byte limit exceeded")
        records.append(FileEvidence(relative, file_sha256, size_bytes))

    tree_hasher = sha256()
    for record in records:
        tree_hasher.update(record.path.encode("utf-8"))
        tree_hasher.update(b"\x00")
        tree_hasher.update(record.sha256.encode("ascii"))
        tree_hasher.update(b"\x00")
        tree_hasher.update(str(record.size_bytes).encode("ascii"))
        tree_hasher.update(b"\n")
    return TreeEvidence(tuple(records), tree_hasher.hexdigest(), total_bytes)


def assert_production_integrity(
    before: ProductionIntegritySnapshot,
    after: ProductionIntegritySnapshot,
) -> None:
    if before != after:
        raise WorkspaceSecurityError("production repository identity changed during worker execution")
