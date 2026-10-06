from __future__ import annotations

import hashlib
import json
import pathlib
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from urllib.parse import unquote, unquote_plus, urlsplit

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_orchestration import RepositoryRef
from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryExecutionPlan,
)
from nika_core.product_project import ProductProject, ProductProjectRepository
from nika_core.toolsmith.workspace_security import (
    WorkspaceSecurityError,
    ensure_real_directory_root,
)

_MAX_TEXT_BYTES = 2048
_MAX_GITFILE_BYTES = 4096
_SENSITIVE_LOCATOR_MARKERS = (
    "access_token",
    "refresh_token",
    "api_key",
    "api-key",
    "apikey",
    "client_secret",
    "password",
    "passwd",
    "secret=",
    "token=",
)


class ProductFactoryLocalRepositoryBindingError(ValueError):
    """Trusted local repository binding cannot be admitted or resolved safely."""


@dataclass(frozen=True, slots=True)
class ProductFactoryLocalRepositoryBinding:
    project_id: str
    repository_id: str
    provider: str
    locator: str
    root: pathlib.Path
    binding_version: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class _FilesystemIdentity:
    root_path: str
    root_device: str
    root_inode: str
    git_metadata_kind: str
    git_metadata_device: str
    git_metadata_inode: str
    gitfile_sha256: str | None


class ProductFactoryLocalRepositoryBindings:
    """Durable ProductProject-scoped authority mapping repository identity to local roots.

    Execution plans never supply filesystem authority. A trusted caller explicitly binds
    one exact repository identity from the ProductProject to one canonical local Git root.
    Resolution revalidates both durable project identity and filesystem identity.
    """

    def __init__(
        self,
        store: SQLiteStore,
        projects: ProductProjectRepository | None = None,
    ) -> None:
        if not isinstance(store, SQLiteStore):
            raise TypeError("store must be SQLiteStore")
        self._store = store
        self._projects = projects or ProductProjectRepository(store)

    def bind(
        self,
        *,
        project_id: str,
        repository: RepositoryRef,
        root: pathlib.Path,
        expected_binding_version: int | None,
        expected_project_spec_version: int | None = None,
        expected_project_row_version: int | None = None,
    ) -> ProductFactoryLocalRepositoryBinding:
        project_id = _canonical_text(project_id, "project_id")
        repository = _snapshot_repository(repository)
        expected_spec_version = (
            None
            if expected_project_spec_version is None
            else _positive_int(
                expected_project_spec_version,
                "expected_project_spec_version",
            )
        )
        expected_row_version = (
            None
            if expected_project_row_version is None
            else _non_negative_int(
                expected_project_row_version,
                "expected_project_row_version",
            )
        )
        project = self._require_project_repository(project_id, repository.locator)
        _require_expected_project_versions(
            project,
            expected_spec_version=expected_spec_version,
            expected_row_version=expected_row_version,
        )
        if project.status != "active":
            raise ProductFactoryLocalRepositoryBindingError(
                "local repository binding requires an active ProductProject"
            )
        identity = _filesystem_identity(root)
        now = datetime.now(UTC).isoformat()

        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_project = self._require_project_repository(
                project_id,
                repository.locator,
            )
            _require_expected_project_versions(
                current_project,
                expected_spec_version=expected_spec_version,
                expected_row_version=expected_row_version,
            )
            if current_project != project or current_project.status != "active":
                raise ProductFactoryLocalRepositoryBindingError(
                    "ProductProject changed while binding local repository"
                )
            _require_filesystem_identity(root, identity)
            alias_rows = conn.execute(
                "SELECT * FROM product_factory_local_repository_bindings "
                "WHERE project_id = ? AND repository_id <> ?",
                (project_id, repository.repository_id),
            ).fetchall()
            for alias_row in alias_rows:
                _, alias_identity = _binding_from_row(alias_row)
                if _same_physical_repository(identity, alias_identity):
                    raise ProductFactoryLocalRepositoryBindingError(
                        "local repository root is already bound to another repository identity"
                    )
            row = conn.execute(
                "SELECT binding_version FROM product_factory_local_repository_bindings "
                "WHERE project_id = ? AND repository_id = ?",
                (project_id, repository.repository_id),
            ).fetchone()
            if row is None:
                if expected_binding_version is not None:
                    raise ProductFactoryLocalRepositoryBindingError(
                        "local repository binding does not exist at the expected version"
                    )
                version = 1
                conn.execute(
                    "INSERT INTO product_factory_local_repository_bindings("
                    "project_id,repository_id,provider,locator,root_path,root_device,"
                    "root_inode,git_metadata_kind,git_metadata_device,git_metadata_inode,"
                    "gitfile_sha256,binding_version,updated_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        project_id,
                        repository.repository_id,
                        repository.provider,
                        repository.locator,
                        identity.root_path,
                        identity.root_device,
                        identity.root_inode,
                        identity.git_metadata_kind,
                        identity.git_metadata_device,
                        identity.git_metadata_inode,
                        identity.gitfile_sha256,
                        version,
                        now,
                    ),
                )
            else:
                current = _stored_positive_int(
                    row["binding_version"],
                    "binding_version",
                )
                if expected_binding_version != current:
                    raise ProductFactoryLocalRepositoryBindingError(
                        "local repository binding version changed"
                    )
                version = current + 1
                conn.execute(
                    "UPDATE product_factory_local_repository_bindings SET "
                    "provider=?,locator=?,root_path=?,root_device=?,root_inode=?,"
                    "git_metadata_kind=?,git_metadata_device=?,git_metadata_inode=?,"
                    "gitfile_sha256=?,binding_version=?,updated_at=? "
                    "WHERE project_id=? AND repository_id=? AND binding_version=?",
                    (
                        repository.provider,
                        repository.locator,
                        identity.root_path,
                        identity.root_device,
                        identity.root_inode,
                        identity.git_metadata_kind,
                        identity.git_metadata_device,
                        identity.git_metadata_inode,
                        identity.gitfile_sha256,
                        version,
                        now,
                        project_id,
                        repository.repository_id,
                        current,
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ProductFactoryLocalRepositoryBindingError(
                        "local repository binding version changed"
                    )
            conn.execute(
                "INSERT INTO audit_events("
                "event_type,entity_type,entity_id,payload_json,created_at"
                ") VALUES ('product_factory.local_repository.bound','product_project',?,?,?)",
                (
                    project_id,
                    _audit_payload(repository, identity, version),
                    now,
                ),
            )

        current = self.require(project_id, repository.repository_id)
        if current.binding_version != version:
            raise ProductFactoryLocalRepositoryBindingError(
                "local repository binding changed after commit"
            )
        return current

    def unbind(
        self,
        *,
        project_id: str,
        repository_id: str,
        expected_binding_version: int,
        expected_repository: RepositoryRef | None = None,
        expected_project_spec_version: int | None = None,
        expected_project_row_version: int | None = None,
    ) -> None:
        project_id = _canonical_text(project_id, "project_id")
        repository_id = _canonical_text(repository_id, "repository_id")
        expected = _positive_int(expected_binding_version, "expected_binding_version")
        expected_repository_snapshot = (
            None
            if expected_repository is None
            else _snapshot_repository(expected_repository)
        )
        if (
            expected_repository_snapshot is not None
            and expected_repository_snapshot.repository_id != repository_id
        ):
            raise ProductFactoryLocalRepositoryBindingError(
                "expected repository identity does not match repository_id"
            )
        expected_spec_version = (
            None
            if expected_project_spec_version is None
            else _positive_int(
                expected_project_spec_version,
                "expected_project_spec_version",
            )
        )
        expected_row_version = (
            None
            if expected_project_row_version is None
            else _non_negative_int(
                expected_project_row_version,
                "expected_project_row_version",
            )
        )
        if expected_spec_version is not None or expected_row_version is not None:
            project = self._projects.get(project_id)
            _require_expected_project_versions(
                project,
                expected_spec_version=expected_spec_version,
                expected_row_version=expected_row_version,
            )
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if expected_spec_version is not None or expected_row_version is not None:
                current_project = self._projects.get(project_id)
                _require_expected_project_versions(
                    current_project,
                    expected_spec_version=expected_spec_version,
                    expected_row_version=expected_row_version,
                )
            row = conn.execute(
                "SELECT binding_version,provider,locator "
                "FROM product_factory_local_repository_bindings "
                "WHERE project_id=? AND repository_id=?",
                (project_id, repository_id),
            ).fetchone()
            if row is None:
                raise KeyError((project_id, repository_id))
            current = _stored_positive_int(row["binding_version"], "binding_version")
            if current != expected:
                raise ProductFactoryLocalRepositoryBindingError(
                    "local repository binding version changed"
                )
            if expected_repository_snapshot is not None:
                stored_provider = _stored_text(row["provider"], "provider")
                stored_locator = _safe_locator(
                    _stored_text(row["locator"], "locator")
                )
                if (
                    stored_provider != expected_repository_snapshot.provider
                    or stored_locator != expected_repository_snapshot.locator
                ):
                    raise ProductFactoryLocalRepositoryBindingError(
                        "local repository binding does not match execution-plan repository"
                    )
            conn.execute(
                "DELETE FROM product_factory_local_repository_bindings "
                "WHERE project_id=? AND repository_id=? AND binding_version=?",
                (project_id, repository_id, current),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise ProductFactoryLocalRepositoryBindingError(
                    "local repository binding version changed"
                )
            conn.execute(
                "INSERT INTO audit_events("
                "event_type,entity_type,entity_id,payload_json,created_at"
                ") VALUES ('product_factory.local_repository.unbound','product_project',?,?,?)",
                (
                    project_id,
                    json.dumps(
                        {
                            "binding_version": current,
                            "repository_id": repository_id,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )

    def current_binding_version(
        self,
        project_id: str,
        repository_id: str,
    ) -> int | None:
        """Return persisted CAS metadata without accepting filesystem authority."""

        project_id = _canonical_text(project_id, "project_id")
        repository_id = _canonical_text(repository_id, "repository_id")
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT binding_version FROM product_factory_local_repository_bindings "
                "WHERE project_id=? AND repository_id=?",
                (project_id, repository_id),
            ).fetchone()
        if row is None:
            return None
        return _stored_positive_int(row["binding_version"], "binding_version")

    def require(
        self,
        project_id: str,
        repository_id: str,
    ) -> ProductFactoryLocalRepositoryBinding:
        project_id = _canonical_text(project_id, "project_id")
        repository_id = _canonical_text(repository_id, "repository_id")
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM product_factory_local_repository_bindings "
                "WHERE project_id=? AND repository_id=?",
                (project_id, repository_id),
            ).fetchone()
        if row is None:
            raise KeyError((project_id, repository_id))
        binding, identity = _binding_from_row(row)
        _require_filesystem_identity(binding.root, identity)
        with self._store.connection() as conn:
            current_row = conn.execute(
                "SELECT * FROM product_factory_local_repository_bindings "
                "WHERE project_id=? AND repository_id=?",
                (project_id, repository_id),
            ).fetchone()
        if current_row is None:
            raise ProductFactoryLocalRepositoryBindingError(
                "local repository binding changed while resolving"
            )
        current_binding, current_identity = _binding_from_row(current_row)
        if current_binding != binding or current_identity != identity:
            raise ProductFactoryLocalRepositoryBindingError(
                "local repository binding changed while resolving"
            )
        return binding

    def validate_plan(
        self,
        plan: PackagedProductFactoryExecutionPlan,
    ) -> None:
        """Reject an execution plan that is stale for the current ProductProject."""

        if type(plan) is not PackagedProductFactoryExecutionPlan:
            raise TypeError("plan must be an exact PackagedProductFactoryExecutionPlan")
        project = self._projects.get(plan.project_id)
        _require_plan_project(plan, project)

    def resolve_for_plan(
        self,
        plan: PackagedProductFactoryExecutionPlan,
    ) -> MappingProxyType[str, pathlib.Path]:
        if type(plan) is not PackagedProductFactoryExecutionPlan:
            raise TypeError("plan must be an exact PackagedProductFactoryExecutionPlan")
        project = self._projects.get(plan.project_id)
        _require_plan_project(plan, project)

        resolved: dict[str, pathlib.Path] = {}
        for repository in plan.graph.repositories:
            binding = self.require(plan.project_id, repository.repository_id)
            if (
                binding.provider != repository.provider
                or binding.locator != repository.locator
            ):
                raise ProductFactoryLocalRepositoryBindingError(
                    "local repository binding does not match the execution-plan repository"
                )
            if repository.locator not in project.spec.repository_refs:
                raise ProductFactoryLocalRepositoryBindingError(
                    "execution-plan repository is not present in current ProductProject"
                )
            resolved[repository.repository_id] = binding.root

        project_after = self._projects.get(plan.project_id)
        _require_plan_project(plan, project_after)
        return MappingProxyType(resolved)

    def _require_project_repository(
        self,
        project_id: str,
        locator: str,
    ) -> ProductProject:
        project = self._projects.get(project_id)
        if locator not in project.spec.repository_refs:
            raise ProductFactoryLocalRepositoryBindingError(
                "repository locator is not present in current ProductProject"
            )
        return project


def _require_plan_project(
    plan: PackagedProductFactoryExecutionPlan,
    project: ProductProject,
) -> None:
    if (
        project.project_id != plan.project_id
        or project.spec_version != plan.expected_spec_version
        or project.row_version != plan.expected_row_version
        or project.status != "active"
        or any(
            repository.locator not in project.spec.repository_refs
            for repository in plan.graph.repositories
        )
    ):
        raise ProductFactoryLocalRepositoryBindingError(
            "execution plan is stale for the current ProductProject"
        )


def _require_expected_project_versions(
    project: ProductProject,
    *,
    expected_spec_version: int | None,
    expected_row_version: int | None,
) -> None:
    if (
        expected_spec_version is not None
        and project.spec_version != expected_spec_version
    ) or (
        expected_row_version is not None
        and project.row_version != expected_row_version
    ):
        raise ProductFactoryLocalRepositoryBindingError(
            "execution plan is stale for the current ProductProject"
        )


def _snapshot_repository(repository: RepositoryRef) -> RepositoryRef:
    if type(repository) is not RepositoryRef:
        raise TypeError("repository must be an exact RepositoryRef")
    provider = _canonical_text(repository.provider, "provider")
    locator = _safe_locator(repository.locator)
    return RepositoryRef(
        repository_id=_canonical_text(repository.repository_id, "repository_id"),
        provider=provider,
        locator=locator,
        default_branch=_canonical_text(repository.default_branch, "default_branch"),
        credential_ref=repository.credential_ref,
        case_sensitive_paths=repository.case_sensitive_paths,
    )


def _filesystem_identity(root: pathlib.Path) -> _FilesystemIdentity:
    if not isinstance(root, pathlib.Path):
        raise TypeError("root must be pathlib.Path")
    try:
        resolved = ensure_real_directory_root(root, label="Product Factory repository root")
    except WorkspaceSecurityError as exc:
        raise ProductFactoryLocalRepositoryBindingError(str(exc)) from exc
    root_stat = resolved.stat()
    metadata = resolved / ".git"
    try:
        metadata_stat = metadata.lstat()
    except OSError as exc:
        raise ProductFactoryLocalRepositoryBindingError(
            "Product Factory repository root must expose .git metadata"
        ) from exc
    if stat.S_ISLNK(metadata_stat.st_mode) or _is_reparse_point(metadata_stat):
        raise ProductFactoryLocalRepositoryBindingError(
            "Product Factory .git metadata must not be a symbolic link or reparse point"
        )
    if stat.S_ISDIR(metadata_stat.st_mode):
        kind = "directory"
        gitfile_sha256 = None
    elif stat.S_ISREG(metadata_stat.st_mode):
        kind = "file"
        try:
            raw = metadata.read_bytes()
        except OSError as exc:
            raise ProductFactoryLocalRepositoryBindingError(
                "Product Factory .git metadata file is unreadable"
            ) from exc
        if not raw or len(raw) > _MAX_GITFILE_BYTES:
            raise ProductFactoryLocalRepositoryBindingError(
                "Product Factory .git metadata file is invalid"
            )
        gitfile_sha256 = hashlib.sha256(raw).hexdigest()
    else:
        raise ProductFactoryLocalRepositoryBindingError(
            "Product Factory .git metadata has an unsupported file type"
        )
    return _FilesystemIdentity(
        root_path=str(resolved),
        root_device=str(root_stat.st_dev),
        root_inode=str(root_stat.st_ino),
        git_metadata_kind=kind,
        git_metadata_device=str(metadata_stat.st_dev),
        git_metadata_inode=str(metadata_stat.st_ino),
        gitfile_sha256=gitfile_sha256,
    )


def _require_filesystem_identity(
    root: pathlib.Path,
    expected: _FilesystemIdentity,
) -> None:
    current = _filesystem_identity(root)
    if current != expected:
        raise ProductFactoryLocalRepositoryBindingError(
            "bound local repository filesystem identity changed"
        )


def _same_physical_repository(
    first: _FilesystemIdentity,
    second: _FilesystemIdentity,
) -> bool:
    return (
        first.root_device == second.root_device
        and first.root_inode == second.root_inode
        and first.git_metadata_kind == second.git_metadata_kind
        and first.git_metadata_device == second.git_metadata_device
        and first.git_metadata_inode == second.git_metadata_inode
        and first.gitfile_sha256 == second.gitfile_sha256
    )


def _binding_from_row(row: object) -> tuple[
    ProductFactoryLocalRepositoryBinding,
    _FilesystemIdentity,
]:
    try:
        project_id = _stored_text(row["project_id"], "project_id")
        repository_id = _stored_text(row["repository_id"], "repository_id")
        provider = _stored_text(row["provider"], "provider")
        locator = _safe_locator(_stored_text(row["locator"], "locator"))
        root_path = _stored_text(row["root_path"], "root_path")
        root_device = _stored_decimal(row["root_device"], "root_device")
        root_inode = _stored_decimal(row["root_inode"], "root_inode")
        kind = _stored_text(row["git_metadata_kind"], "git_metadata_kind")
        if kind not in {"directory", "file"}:
            raise ProductFactoryLocalRepositoryBindingError(
                "invalid persisted Git metadata kind"
            )
        git_device = _stored_decimal(row["git_metadata_device"], "git_metadata_device")
        git_inode = _stored_decimal(row["git_metadata_inode"], "git_metadata_inode")
        gitfile_sha256 = row["gitfile_sha256"]
        if gitfile_sha256 is not None:
            if (
                type(gitfile_sha256) is not str
                or len(gitfile_sha256) != 64
                or any(char not in "0123456789abcdef" for char in gitfile_sha256)
            ):
                raise ProductFactoryLocalRepositoryBindingError(
                    "invalid persisted Git metadata digest"
                )
        if (kind == "file") != (gitfile_sha256 is not None):
            raise ProductFactoryLocalRepositoryBindingError(
                "persisted Git metadata kind/digest mismatch"
            )
        version = _stored_positive_int(row["binding_version"], "binding_version")
        updated_at = _stored_text(row["updated_at"], "updated_at")
    except (KeyError, TypeError) as exc:
        raise ProductFactoryLocalRepositoryBindingError(
            "invalid persisted local repository binding"
        ) from exc

    root = pathlib.Path(root_path)
    if not root.is_absolute():
        raise ProductFactoryLocalRepositoryBindingError(
            "invalid persisted root_path"
        )
    identity = _FilesystemIdentity(
        root_path=root_path,
        root_device=root_device,
        root_inode=root_inode,
        git_metadata_kind=kind,
        git_metadata_device=git_device,
        git_metadata_inode=git_inode,
        gitfile_sha256=gitfile_sha256,
    )
    return (
        ProductFactoryLocalRepositoryBinding(
            project_id=project_id,
            repository_id=repository_id,
            provider=provider,
            locator=locator,
            root=root,
            binding_version=version,
            updated_at=updated_at,
        ),
        identity,
    )


def _canonical_text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ProductFactoryLocalRepositoryBindingError(
            f"{label} must be canonical non-empty text"
        )
    try:
        raw = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProductFactoryLocalRepositoryBindingError(
            f"{label} must be valid UTF-8 text"
        ) from exc
    if len(raw) > _MAX_TEXT_BYTES:
        raise ProductFactoryLocalRepositoryBindingError(
            f"{label} exceeds the byte limit"
        )
    return value


def _safe_locator(value: object) -> str:
    locator = _canonical_text(value, "locator")
    normalized = locator.casefold()
    for _ in range(_MAX_TEXT_BYTES):
        if any(marker in normalized for marker in _SENSITIVE_LOCATOR_MARKERS):
            raise ProductFactoryLocalRepositoryBindingError(
                "repository locator must not contain credential material"
            )
        try:
            if "://" in normalized or normalized.startswith("//"):
                parsed = urlsplit(normalized)
                if parsed.username is not None or parsed.password is not None:
                    raise ProductFactoryLocalRepositoryBindingError(
                        "repository locator must not contain URL credentials"
                    )
        except ValueError as exc:
            raise ProductFactoryLocalRepositoryBindingError(
                "repository locator URL authority is invalid"
            ) from exc
        decoded = unquote(unquote_plus(normalized)).casefold()
        if decoded == normalized:
            return locator
        normalized = decoded
    raise ProductFactoryLocalRepositoryBindingError(
        "repository locator decoding exceeds the bounded limit"
    )


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ProductFactoryLocalRepositoryBindingError(
            f"{label} must be a positive integer"
        )
    return value


def _non_negative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ProductFactoryLocalRepositoryBindingError(
            f"{label} must be a non-negative integer"
        )
    return value


def _stored_positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ProductFactoryLocalRepositoryBindingError(
            f"invalid persisted {label}"
        )
    return value


def _stored_text(value: object, label: str) -> str:
    try:
        return _canonical_text(value, label)
    except ProductFactoryLocalRepositoryBindingError as exc:
        raise ProductFactoryLocalRepositoryBindingError(
            f"invalid persisted {label}"
        ) from exc


def _stored_decimal(value: object, label: str) -> str:
    text = _stored_text(value, label)
    if not text.isascii() or not text.isdecimal():
        raise ProductFactoryLocalRepositoryBindingError(
            f"invalid persisted {label}"
        )
    return text


def _is_reparse_point(file_stat: object) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and getattr(file_stat, "st_file_attributes", 0) & flag)


def _audit_payload(
    repository: RepositoryRef,
    identity: _FilesystemIdentity,
    version: int,
) -> str:
    path_digest = hashlib.sha256(
        identity.root_path.encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return json.dumps(
        {
            "binding_version": version,
            "locator": repository.locator,
            "provider": repository.provider,
            "repository_id": repository.repository_id,
            "root_sha256": path_digest,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
