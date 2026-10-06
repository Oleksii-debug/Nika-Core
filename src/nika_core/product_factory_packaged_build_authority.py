from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.product_factory_build_admission import (
    ReviewedBuildExecutionPolicy,
    reviewed_component_build_spec,
)
from nika_core.product_factory_build_execution import (
    ApprovedBuildCommand,
    BuildExecutionSpec,
    ProjectExecutionAuthority,
)
from nika_core.product_factory_build_execution_host import BuildOutputPolicy
from nika_core.product_factory_coding_worker_adapter import RepositoryPathIdentity
from nika_core.product_factory_coordinator import ProductFactoryCoordinator
from nika_core.product_factory_deployment import ExecutionNode, Platform, ResourceEnvelope
from nika_core.product_factory_multi_repository import RepositoryGraphAuthority
from nika_core.product_factory_packaged_local_startup import (
    PackagedLocalProductFactoryStartup,
)
from nika_core.toolsmith.contracts import AllowedPathPolicy, normalize_relative_path

_SCHEMA_VERSION = 2
_MAX_REVISION = (1 << 63) - 1
_SCHEMA = "nika.product-factory.packaged-build-authority.v1"
_SENSITIVE_ARG_MARKERS = (
    "authorization:",
    "api_key=",
    "api-key=",
    "apikey=",
    "access_token=",
    "refresh_token=",
    "password=",
    "passwd=",
    "secret=",
    "token=",
)
_SENSITIVE_ARG_OPTIONS = frozenset(
    {
        "--access-token",
        "--api-key",
        "--apikey",
        "--authorization",
        "--passwd",
        "--password",
        "--refresh-token",
        "--secret",
        "--token",
    }
)


class PackagedBuildAuthorityError(ValueError):
    """Raised when packaged PF5 host authority cannot be proven exactly."""


@dataclass(frozen=True, slots=True)
class PackagedBuildAuthorityTemplate:
    """Host-owned reusable PF5 policy for one ProductProject component.

    Candidate identity never enters this template. PF4 supplies the exact accepted source
    and review identity, while this template supplies only the host-owned execution ceiling.
    """

    project_id: str
    repository_id: str
    component_id: str
    node_id: str
    platform: Platform
    workspace_relpath: str
    required_features: frozenset[str]
    required_toolchains: frozenset[str]
    resources: ResourceEnvelope
    command_id: str
    argv: tuple[str, ...]
    output_paths: tuple[str, ...]
    max_changed_files: int
    lease_seconds: int
    require_gpu: bool = False

    def __post_init__(self) -> None:
        for label, value in (
            ("project_id", self.project_id),
            ("repository_id", self.repository_id),
            ("component_id", self.component_id),
            ("node_id", self.node_id),
            ("command_id", self.command_id),
        ):
            _exact_text(value, label)
        if type(self.platform) is not Platform:
            raise PackagedBuildAuthorityError(
                "build authority platform must be exact Platform"
            )
        try:
            workspace = normalize_relative_path(self.workspace_relpath).as_posix()
        except ValueError as exc:
            raise PackagedBuildAuthorityError(
                "build authority workspace path is invalid"
            ) from exc
        object.__setattr__(self, "workspace_relpath", workspace)
        _exact_text_set(self.required_features, "required_features")
        _exact_text_set(self.required_toolchains, "required_toolchains")
        if "build" not in self.required_features:
            raise PackagedBuildAuthorityError(
                "build authority must explicitly require the build feature"
            )
        if type(self.resources) is not ResourceEnvelope:
            raise PackagedBuildAuthorityError(
                "build authority resources must be exact ResourceEnvelope"
            )
        for label, value in (
            ("cpu_cores", self.resources.cpu_cores),
            ("memory_mb", self.resources.memory_mb),
            ("disk_mb", self.resources.disk_mb),
        ):
            if type(value) is not int or value <= 0:
                raise PackagedBuildAuthorityError(
                    f"build authority {label} must be an exact positive integer"
                )
        if type(self.require_gpu) is not bool:
            raise PackagedBuildAuthorityError(
                "build authority require_gpu must be exact bool"
            )
        try:
            ApprovedBuildCommand(self.command_id, self.argv)
        except (TypeError, ValueError) as exc:
            raise PackagedBuildAuthorityError(
                "build authority command is not a valid typed PF5 command"
            ) from exc
        _reject_sensitive_argv(self.argv)
        try:
            output_policy = AllowedPathPolicy(self.output_paths)
        except ValueError as exc:
            raise PackagedBuildAuthorityError(
                "build authority output paths are invalid"
            ) from exc
        object.__setattr__(self, "output_paths", output_policy.roots)
        if (
            type(self.max_changed_files) is not int
            or not 1 <= self.max_changed_files <= 10_000
        ):
            raise PackagedBuildAuthorityError(
                "build authority max_changed_files must be 1..10000"
            )
        if type(self.lease_seconds) is not int or self.lease_seconds <= 0:
            raise PackagedBuildAuthorityError(
                "build authority lease_seconds must be a positive integer"
            )


@dataclass(frozen=True, slots=True)
class PackagedBuildAuthoritySnapshot:
    template: PackagedBuildAuthorityTemplate
    revision: int
    digest: str

    def __post_init__(self) -> None:
        template = _snapshot_template(self.template)
        revision = _revision(self.revision)
        digest = _digest(self.digest, "template digest")
        if _digest_payload(_encode_template(template)) != digest:
            raise PackagedBuildAuthorityError(
                "packaged build authority snapshot digest does not match template"
            )
        object.__setattr__(self, "template", template)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "digest", digest)


@dataclass(frozen=True, slots=True)
class _BoundBuildAuthority:
    project_id: str
    repository_id: str
    component_id: str
    work_id: str
    candidate_work_id: str
    source_sha: str
    review_fingerprint: str
    spec_version: int
    row_version: int
    graph_digest: str
    template_revision: int
    template_digest: str


class PackagedBuildAuthorityStore:
    """Durable packaged host authority for reviewed PF5 build execution."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        node: ExecutionNode,
        startup: PackagedLocalProductFactoryStartup,
    ) -> None:
        if type(store) is not SQLiteStore:
            raise TypeError("store must be SQLiteStore")
        if type(node) is not ExecutionNode:
            raise TypeError("node must be exact ExecutionNode")
        if type(startup) is not PackagedLocalProductFactoryStartup:
            raise TypeError("startup must be exact PackagedLocalProductFactoryStartup")
        startup.__post_init__()
        self._store = store
        self._node = node
        self._startup = startup
        self._audit = AuditLog(store)
        self._initialize()

    def configure(
        self,
        template: PackagedBuildAuthorityTemplate,
        *,
        expected_revision: int,
    ) -> PackagedBuildAuthoritySnapshot:
        if type(template) is not PackagedBuildAuthorityTemplate:
            raise TypeError("template must be exact PackagedBuildAuthorityTemplate")
        template = _snapshot_template(template)
        if type(expected_revision) is not int or expected_revision < 0:
            raise PackagedBuildAuthorityError(
                "expected build authority revision must be non-negative"
            )
        self._validate_runtime(template)
        payload = _encode_template(template)
        digest = _digest_payload(payload)
        configured_at = datetime.now(UTC).isoformat()
        try:
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT revision FROM product_factory_build_authority_templates "
                    "WHERE project_id = ? AND repository_id = ? AND component_id = ?",
                    (
                        template.project_id,
                        template.repository_id,
                        template.component_id,
                    ),
                ).fetchone()
                current = 0 if row is None else _revision(row["revision"])
                if current != expected_revision:
                    raise PackagedBuildAuthorityError(
                        "packaged build authority changed; reload before updating"
                    )
                if current >= _MAX_REVISION:
                    raise PackagedBuildAuthorityError(
                        "packaged build authority revision counter is exhausted"
                    )
                revision = current + 1
                conn.execute(
                    "INSERT INTO product_factory_build_authority_templates "
                    "(project_id, repository_id, component_id, revision, template_json, "
                    "template_digest, configured_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(project_id, repository_id, component_id) DO UPDATE SET "
                    "revision=excluded.revision, template_json=excluded.template_json, "
                    "template_digest=excluded.template_digest, "
                    "configured_at=excluded.configured_at",
                    (
                        template.project_id,
                        template.repository_id,
                        template.component_id,
                        revision,
                        payload,
                        digest,
                        configured_at,
                    ),
                )
                conn.execute(
                    "INSERT INTO product_factory_build_authority_template_history "
                    "(project_id, repository_id, component_id, revision, template_json, "
                    "template_digest, configured_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        template.project_id,
                        template.repository_id,
                        template.component_id,
                        revision,
                        payload,
                        digest,
                        configured_at,
                    ),
                )
                self._audit.append_with_connection(
                    conn,
                    event_type="product_factory.build_authority.configured",
                    entity_type="product_factory_build_authority",
                    entity_id=(
                        f"{template.project_id}:{template.repository_id}:"
                        f"{template.component_id}"
                    ),
                    payload={
                        "revision": revision,
                        "template_digest": digest,
                    },
                )
        except sqlite3.Error as exc:
            raise PackagedBuildAuthorityError(
                "packaged build authority could not be persisted"
            ) from exc
        return PackagedBuildAuthoritySnapshot(template, revision, digest)

    def snapshot(
        self,
        *,
        project_id: str,
        repository_id: str,
        component_id: str,
    ) -> PackagedBuildAuthoritySnapshot:
        for label, value in (
            ("project_id", project_id),
            ("repository_id", repository_id),
            ("component_id", component_id),
        ):
            _exact_text(value, label)
        try:
            with self._store.connection() as conn:
                row = conn.execute(
                    "SELECT revision, template_json, template_digest "
                    "FROM product_factory_build_authority_templates "
                    "WHERE project_id = ? AND repository_id = ? AND component_id = ?",
                    (project_id, repository_id, component_id),
                ).fetchone()
        except sqlite3.Error as exc:
            raise PackagedBuildAuthorityError(
                "packaged build authority could not be read"
            ) from exc
        if row is None:
            raise PackagedBuildAuthorityError(
                "no packaged build authority is configured for this component"
            )
        revision = _revision(row["revision"])
        payload = row["template_json"]
        digest = row["template_digest"]
        if type(payload) is not str or type(digest) is not str:
            raise PackagedBuildAuthorityError(
                "stored packaged build authority is corrupt"
            )
        if _digest_payload(payload) != digest:
            raise PackagedBuildAuthorityError(
                "stored packaged build authority digest does not match"
            )
        template = _decode_template(payload)
        if (
            template.project_id != project_id
            or template.repository_id != repository_id
            or template.component_id != component_id
        ):
            raise PackagedBuildAuthorityError(
                "stored packaged build authority identity does not match its key"
            )
        self._validate_runtime(template)
        return PackagedBuildAuthoritySnapshot(template, revision, digest)

    def bind(
        self,
        *,
        spec: BuildExecutionSpec,
        component_id: str,
        candidate_work_id: str,
        review_fingerprint: str,
        spec_version: int,
        row_version: int,
        graph_digest: str,
        authority: PackagedBuildAuthoritySnapshot,
    ) -> None:
        if type(spec) is not BuildExecutionSpec:
            raise TypeError("spec must be exact BuildExecutionSpec")
        _exact_text(component_id, "component_id")
        _exact_text(candidate_work_id, "candidate_work_id")
        _digest(review_fingerprint, "review_fingerprint")
        _digest(graph_digest, "graph_digest")
        if type(spec_version) is not int or spec_version < 1:
            raise PackagedBuildAuthorityError("spec_version must be positive")
        if type(row_version) is not int or row_version < 0:
            raise PackagedBuildAuthorityError("row_version must be non-negative")
        if type(authority) is not PackagedBuildAuthoritySnapshot:
            raise TypeError("authority must be exact PackagedBuildAuthoritySnapshot")
        authority = PackagedBuildAuthoritySnapshot(
            authority.template,
            authority.revision,
            authority.digest,
        )
        template = authority.template
        if (
            component_id != template.component_id
            or spec.request.project_id != template.project_id
            or spec.request.platform is not template.platform
            or spec.request.required_features != template.required_features
            or spec.request.required_toolchains != template.required_toolchains
            or spec.request.resources != template.resources
            or spec.request.require_gpu is not template.require_gpu
            or spec.scope.repository_id != template.repository_id
            or spec.scope.requested_node_ids != (template.node_id,)
            or spec.scope.workspace_relpath != template.workspace_relpath
            or spec.scope.network_scopes != ()
            or spec.scope.credential_refs != ()
            or spec.scope.command_id != template.command_id
            or spec.lease_seconds != template.lease_seconds
        ):
            raise PackagedBuildAuthorityError(
                "PF5 spec does not match the packaged build authority snapshot"
            )
        bound = _BoundBuildAuthority(
            project_id=template.project_id,
            repository_id=template.repository_id,
            component_id=component_id,
            work_id=spec.request.work_id,
            candidate_work_id=candidate_work_id,
            source_sha=spec.source_sha,
            review_fingerprint=review_fingerprint,
            spec_version=spec_version,
            row_version=row_version,
            graph_digest=graph_digest,
            template_revision=authority.revision,
            template_digest=authority.digest,
        )
        try:
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT revision, template_digest "
                    "FROM product_factory_build_authority_templates "
                    "WHERE project_id = ? AND repository_id = ? AND component_id = ?",
                    (bound.project_id, bound.repository_id, bound.component_id),
                ).fetchone()
                if (
                    current is None
                    or _revision(current["revision"]) != bound.template_revision
                    or current["template_digest"] != bound.template_digest
                ):
                    raise PackagedBuildAuthorityError(
                        "packaged build authority changed during PF4/PF5 admission"
                    )
                existing = conn.execute(
                    "SELECT * FROM product_factory_build_authority_bindings "
                    "WHERE work_id = ?",
                    (bound.work_id,),
                ).fetchone()
                if existing is not None:
                    if _binding_from_row(existing) != bound:
                        raise PackagedBuildAuthorityError(
                            "PF5 work id is already bound to different packaged authority"
                        )
                    return
                conn.execute(
                    "INSERT INTO product_factory_build_authority_bindings "
                    "(work_id, project_id, repository_id, component_id, candidate_work_id, "
                    "source_sha, review_fingerprint, spec_version, row_version, graph_digest, "
                    "template_revision, template_digest, bound_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        bound.work_id,
                        bound.project_id,
                        bound.repository_id,
                        bound.component_id,
                        bound.candidate_work_id,
                        bound.source_sha,
                        bound.review_fingerprint,
                        bound.spec_version,
                        bound.row_version,
                        bound.graph_digest,
                        bound.template_revision,
                        bound.template_digest,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                self._audit.append_with_connection(
                    conn,
                    event_type="product_factory.build_authority.bound",
                    entity_type="product_factory_build_work",
                    entity_id=bound.work_id,
                    payload={
                        "project_id": bound.project_id,
                        "repository_id": bound.repository_id,
                        "component_id": bound.component_id,
                        "template_revision": bound.template_revision,
                        "template_digest": bound.template_digest,
                        "source_sha": bound.source_sha,
                        "review_fingerprint": bound.review_fingerprint,
                    },
                )
        except sqlite3.Error as exc:
            raise PackagedBuildAuthorityError(
                "PF5 packaged authority binding could not be persisted"
            ) from exc

    def bound_snapshot(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
    ) -> tuple[_BoundBuildAuthority, PackagedBuildAuthoritySnapshot]:
        bound = self._read_bound(
            project_id=project_id,
            repository_id=repository_id,
            work_id=work_id,
        )
        current = self.snapshot(
            project_id=bound.project_id,
            repository_id=bound.repository_id,
            component_id=bound.component_id,
        )
        if (
            current.revision != bound.template_revision
            or current.digest != bound.template_digest
        ):
            raise PackagedBuildAuthorityError(
                "packaged build authority changed after PF5 work admission"
            )
        return bound, current

    def bound_historical_snapshot(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
    ) -> tuple[_BoundBuildAuthority, PackagedBuildAuthoritySnapshot]:
        """Resolve the immutable template bound to work without granting new execution.

        This path exists for receipt/output inspection after configuration drift. New
        process effects must continue to use bound_snapshot(), which requires the bound
        authority to remain current.
        """

        bound = self._read_bound(
            project_id=project_id,
            repository_id=repository_id,
            work_id=work_id,
        )
        try:
            with self._store.connection() as conn:
                row = conn.execute(
                    "SELECT revision, template_json, template_digest "
                    "FROM product_factory_build_authority_template_history "
                    "WHERE project_id = ? AND repository_id = ? AND component_id = ? "
                    "AND revision = ?",
                    (
                        bound.project_id,
                        bound.repository_id,
                        bound.component_id,
                        bound.template_revision,
                    ),
                ).fetchone()
        except sqlite3.Error as exc:
            raise PackagedBuildAuthorityError(
                "historical packaged build authority could not be read"
            ) from exc
        if row is None:
            raise PackagedBuildAuthorityError(
                "historical packaged build authority is unavailable"
            )
        revision = _revision(row["revision"])
        payload = row["template_json"]
        digest = row["template_digest"]
        if type(payload) is not str or type(digest) is not str:
            raise PackagedBuildAuthorityError(
                "stored historical packaged build authority is corrupt"
            )
        if (
            revision != bound.template_revision
            or digest != bound.template_digest
            or _digest_payload(payload) != digest
        ):
            raise PackagedBuildAuthorityError(
                "stored historical packaged build authority does not match binding"
            )
        template = _decode_template(payload)
        if (
            template.project_id != bound.project_id
            or template.repository_id != bound.repository_id
            or template.component_id != bound.component_id
        ):
            raise PackagedBuildAuthorityError(
                "stored historical packaged build authority identity does not match binding"
            )
        return bound, PackagedBuildAuthoritySnapshot(template, revision, digest)

    def _read_bound(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
    ) -> _BoundBuildAuthority:
        for label, value in (
            ("project_id", project_id),
            ("repository_id", repository_id),
            ("work_id", work_id),
        ):
            _exact_text(value, label)
        try:
            with self._store.connection() as conn:
                row = conn.execute(
                    "SELECT * FROM product_factory_build_authority_bindings "
                    "WHERE work_id = ?",
                    (work_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise PackagedBuildAuthorityError(
                "PF5 packaged authority binding could not be read"
            ) from exc
        if row is None:
            raise PackagedBuildAuthorityError(
                "PF5 work has no durable packaged authority binding"
            )
        bound = _binding_from_row(row)
        if bound.project_id != project_id or bound.repository_id != repository_id:
            raise PackagedBuildAuthorityError(
                "PF5 packaged authority binding identity does not match request"
            )
        return bound

    def _initialize(self) -> None:
        try:
            with self._store.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS product_factory_build_authority_schema ("
                    "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                current = (
                    conn.execute(
                        "SELECT MAX(version) FROM product_factory_build_authority_schema"
                    ).fetchone()[0]
                    or 0
                )
                if type(current) is not int or current < 0:
                    raise PackagedBuildAuthorityError(
                        "packaged build authority schema version is invalid"
                    )
                if current > _SCHEMA_VERSION:
                    raise PackagedBuildAuthorityError(
                        "packaged build authority schema is newer than this program"
                    )
                if current < 1:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS "
                        "product_factory_build_authority_templates ("
                        "project_id TEXT NOT NULL, repository_id TEXT NOT NULL, "
                        "component_id TEXT NOT NULL, revision INTEGER NOT NULL "
                        "CHECK(revision > 0), template_json TEXT NOT NULL, "
                        "template_digest TEXT NOT NULL, configured_at TEXT NOT NULL, "
                        "PRIMARY KEY(project_id, repository_id, component_id))"
                    )
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS "
                        "product_factory_build_authority_bindings ("
                        "work_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
                        "repository_id TEXT NOT NULL, component_id TEXT NOT NULL, "
                        "candidate_work_id TEXT NOT NULL, source_sha TEXT NOT NULL, "
                        "review_fingerprint TEXT NOT NULL, spec_version INTEGER NOT NULL, "
                        "row_version INTEGER NOT NULL, graph_digest TEXT NOT NULL, "
                        "template_revision INTEGER NOT NULL, template_digest TEXT NOT NULL, "
                        "bound_at TEXT NOT NULL)"
                    )
                    conn.execute(
                        "INSERT INTO product_factory_build_authority_schema VALUES (?, ?)",
                        (1, datetime.now(UTC).isoformat()),
                    )
                if current < 2:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS "
                        "product_factory_build_authority_template_history ("
                        "project_id TEXT NOT NULL, repository_id TEXT NOT NULL, "
                        "component_id TEXT NOT NULL, revision INTEGER NOT NULL "
                        "CHECK(revision > 0), template_json TEXT NOT NULL, "
                        "template_digest TEXT NOT NULL, configured_at TEXT NOT NULL, "
                        "PRIMARY KEY(project_id, repository_id, component_id, revision))"
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO "
                        "product_factory_build_authority_template_history "
                        "(project_id, repository_id, component_id, revision, template_json, "
                        "template_digest, configured_at) "
                        "SELECT project_id, repository_id, component_id, revision, "
                        "template_json, template_digest, configured_at "
                        "FROM product_factory_build_authority_templates"
                    )
                    conn.execute(
                        "INSERT INTO product_factory_build_authority_schema VALUES (?, ?)",
                        (2, datetime.now(UTC).isoformat()),
                    )
        except sqlite3.Error as exc:
            raise PackagedBuildAuthorityError(
                "packaged build authority schema could not be initialized"
            ) from exc

    def _validate_runtime(self, template: PackagedBuildAuthorityTemplate) -> None:
        node = self._node
        startup = self._startup
        if (
            node.identity.node_id != template.node_id
            or node.identity.platform is not template.platform
            or node.enabled is not True
        ):
            raise PackagedBuildAuthorityError(
                "packaged build authority does not match the current execution node"
            )
        if template.required_features - node.capabilities.features:
            raise PackagedBuildAuthorityError(
                "packaged build authority requires unavailable node features"
            )
        if template.required_toolchains - node.capabilities.toolchains:
            raise PackagedBuildAuthorityError(
                "packaged build authority requires unavailable node toolchains"
            )
        if template.require_gpu and not node.capabilities.gpu:
            raise PackagedBuildAuthorityError(
                "packaged build authority requires unavailable GPU capability"
            )
        if not node.resources.fits(template.resources):
            raise PackagedBuildAuthorityError(
                "packaged build authority exceeds current node resources"
            )
        if template.argv[0] not in tuple(startup.policy.allowed_executables):
            raise PackagedBuildAuthorityError(
                "packaged build command executable is not startup-allowlisted"
            )
        if template.max_changed_files > startup.policy.resource_budget.max_changed_files:
            raise PackagedBuildAuthorityError(
                "packaged build output ceiling exceeds startup resource policy"
            )
        if template.lease_seconds > startup.policy.lease_seconds:
            raise PackagedBuildAuthorityError(
                "packaged build lease exceeds startup lease policy"
            )


@dataclass(frozen=True, slots=True)
class _ReviewedResolution:
    snapshot: PackagedBuildAuthoritySnapshot
    policy: ReviewedBuildExecutionPolicy


@dataclass(slots=True)
class PackagedReviewedBuildExecutionPolicyPort:
    authorities: PackagedBuildAuthorityStore

    def resolve(
        self,
        *,
        project_id: str,
        repository_id: str,
        component_id: str,
        candidate_work_id: str,
        source_sha: str,
        review_fingerprint: str,
        spec_version: int,
        row_version: int,
        graph_digest: str,
    ) -> ReviewedBuildExecutionPolicy:
        return self.resolve_with_snapshot(
            project_id=project_id,
            repository_id=repository_id,
            component_id=component_id,
            candidate_work_id=candidate_work_id,
            source_sha=source_sha,
            review_fingerprint=review_fingerprint,
            spec_version=spec_version,
            row_version=row_version,
            graph_digest=graph_digest,
        ).policy

    def resolve_with_snapshot(
        self,
        *,
        project_id: str,
        repository_id: str,
        component_id: str,
        candidate_work_id: str,
        source_sha: str,
        review_fingerprint: str,
        spec_version: int,
        row_version: int,
        graph_digest: str,
    ) -> _ReviewedResolution:
        snapshot = self.authorities.snapshot(
            project_id=project_id,
            repository_id=repository_id,
            component_id=component_id,
        )
        template = snapshot.template
        return _ReviewedResolution(
            snapshot,
            ReviewedBuildExecutionPolicy(
                project_id=project_id,
                repository_id=repository_id,
                component_id=component_id,
                candidate_work_id=candidate_work_id,
                source_sha=source_sha,
                review_fingerprint=review_fingerprint,
                spec_version=spec_version,
                row_version=row_version,
                graph_digest=graph_digest,
                platform=template.platform,
                requested_node_ids=(template.node_id,),
                workspace_relpath=template.workspace_relpath,
                required_features=template.required_features,
                required_toolchains=template.required_toolchains,
                resources=template.resources,
                network_scopes=(),
                credential_refs=(),
                command_id=template.command_id,
                require_gpu=template.require_gpu,
                lease_seconds=template.lease_seconds,
            ),
        )


@dataclass(slots=True)
class PackagedTrustedExecutionAuthorityPort:
    authorities: PackagedBuildAuthorityStore

    def resolve(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
    ) -> ProjectExecutionAuthority:
        _bound, snapshot = self.authorities.bound_snapshot(
            project_id=project_id,
            repository_id=repository_id,
            work_id=work_id,
        )
        template = snapshot.template
        return ProjectExecutionAuthority(
            project_id=project_id,
            repository_id=repository_id,
            work_id=work_id,
            permissions=frozenset({"build_release"}),
            allowed_node_ids=(template.node_id,),
            allowed_workspace_paths=(template.workspace_relpath,),
            network_scopes=(),
            credential_refs=(),
            commands=(ApprovedBuildCommand(template.command_id, template.argv),),
            evidence_refs=(_evidence_ref(snapshot),),
        )


@dataclass(slots=True)
class PackagedRecoveryExecutionAuthorityPort:
    """Resolve the exact bound grant only for durable post-effect recovery."""

    authorities: PackagedBuildAuthorityStore

    def resolve(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
    ) -> ProjectExecutionAuthority:
        _bound, snapshot = self.authorities.bound_historical_snapshot(
            project_id=project_id,
            repository_id=repository_id,
            work_id=work_id,
        )
        template = snapshot.template
        return ProjectExecutionAuthority(
            project_id=project_id,
            repository_id=repository_id,
            work_id=work_id,
            permissions=frozenset({"build_release"}),
            allowed_node_ids=(template.node_id,),
            allowed_workspace_paths=(template.workspace_relpath,),
            network_scopes=(),
            credential_refs=(),
            commands=(ApprovedBuildCommand(template.command_id, template.argv),),
            evidence_refs=(_evidence_ref(snapshot),),
        )


@dataclass(slots=True)
class PackagedTrustedBuildOutputPolicyPort:
    authorities: PackagedBuildAuthorityStore

    def resolve(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
    ) -> BuildOutputPolicy:
        _bound, snapshot = self.authorities.bound_historical_snapshot(
            project_id=project_id,
            repository_id=repository_id,
            work_id=work_id,
        )
        template = snapshot.template
        path_identity = (
            RepositoryPathIdentity.CASE_INSENSITIVE
            if template.platform is Platform.WINDOWS
            else RepositoryPathIdentity.CASE_SENSITIVE
        )
        return BuildOutputPolicy(
            project_id=project_id,
            repository_id=repository_id,
            work_id=work_id,
            allowed_paths=AllowedPathPolicy(template.output_paths),
            max_changed_files=template.max_changed_files,
            path_identity=path_identity,
        )


@dataclass(slots=True)
class PackagedBuildAuthorityRuntime:
    """One production authority bundle used by PF4 admission and PF5 durable host."""

    authorities: PackagedBuildAuthorityStore
    reviewed_policies: PackagedReviewedBuildExecutionPolicyPort = field(init=False)
    trusted_execution: PackagedTrustedExecutionAuthorityPort = field(init=False)
    recovery_execution: PackagedRecoveryExecutionAuthorityPort = field(init=False)
    output_policies: PackagedTrustedBuildOutputPolicyPort = field(init=False)

    def __post_init__(self) -> None:
        if type(self.authorities) is not PackagedBuildAuthorityStore:
            raise TypeError("authorities must be exact PackagedBuildAuthorityStore")
        self.reviewed_policies = PackagedReviewedBuildExecutionPolicyPort(
            self.authorities
        )
        self.trusted_execution = PackagedTrustedExecutionAuthorityPort(
            self.authorities
        )
        self.recovery_execution = PackagedRecoveryExecutionAuthorityPort(
            self.authorities
        )
        self.output_policies = PackagedTrustedBuildOutputPolicyPort(
            self.authorities
        )

    def admit_reviewed_component(
        self,
        *,
        authority: RepositoryGraphAuthority,
        coordinator: ProductFactoryCoordinator,
        component_id: str,
    ) -> BuildExecutionSpec:
        capture = _CapturingReviewedPolicyPort(self.reviewed_policies)
        spec = reviewed_component_build_spec(
            authority=authority,
            coordinator=coordinator,
            component_id=component_id,
            policies=capture,
        )
        resolution = capture.resolution
        if resolution is None:
            raise PackagedBuildAuthorityError(
                "reviewed PF4 admission did not resolve packaged build authority"
            )
        policy = resolution.policy
        self.authorities.bind(
            spec=spec,
            component_id=component_id,
            candidate_work_id=policy.candidate_work_id,
            review_fingerprint=policy.review_fingerprint,
            spec_version=policy.spec_version,
            row_version=policy.row_version,
            graph_digest=policy.graph_digest,
            authority=resolution.snapshot,
        )
        return spec


@dataclass(slots=True)
class _CapturingReviewedPolicyPort:
    delegate: PackagedReviewedBuildExecutionPolicyPort
    resolution: _ReviewedResolution | None = None

    def resolve(self, **kwargs) -> ReviewedBuildExecutionPolicy:
        if self.resolution is not None:
            raise PackagedBuildAuthorityError(
                "reviewed PF4 admission resolved build authority more than once"
            )
        self.resolution = self.delegate.resolve_with_snapshot(**kwargs)
        return self.resolution.policy


def _snapshot_template(
    value: object,
) -> PackagedBuildAuthorityTemplate:
    if type(value) is not PackagedBuildAuthorityTemplate:
        raise PackagedBuildAuthorityError(
            "packaged build authority template carrier is invalid"
        )
    try:
        return PackagedBuildAuthorityTemplate(
            project_id=value.project_id,
            repository_id=value.repository_id,
            component_id=value.component_id,
            node_id=value.node_id,
            platform=value.platform,
            workspace_relpath=value.workspace_relpath,
            required_features=value.required_features,
            required_toolchains=value.required_toolchains,
            resources=ResourceEnvelope(
                value.resources.cpu_cores,
                value.resources.memory_mb,
                value.resources.disk_mb,
            ),
            command_id=value.command_id,
            argv=value.argv,
            output_paths=value.output_paths,
            max_changed_files=value.max_changed_files,
            lease_seconds=value.lease_seconds,
            require_gpu=value.require_gpu,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        if isinstance(exc, PackagedBuildAuthorityError):
            raise
        raise PackagedBuildAuthorityError(
            "packaged build authority template failed canonical re-admission"
        ) from exc


def _binding_from_row(row: sqlite3.Row) -> _BoundBuildAuthority:
    return _BoundBuildAuthority(
        project_id=_exact_text(row["project_id"], "binding project_id"),
        repository_id=_exact_text(
            row["repository_id"],
            "binding repository_id",
        ),
        component_id=_exact_text(row["component_id"], "binding component_id"),
        work_id=_exact_text(row["work_id"], "binding work_id"),
        candidate_work_id=_exact_text(
            row["candidate_work_id"],
            "binding candidate_work_id",
        ),
        source_sha=_sha(row["source_sha"], "binding source_sha"),
        review_fingerprint=_digest(
            row["review_fingerprint"],
            "binding review_fingerprint",
        ),
        spec_version=_positive_int(
            row["spec_version"],
            "binding spec_version",
        ),
        row_version=_non_negative_int(
            row["row_version"],
            "binding row_version",
        ),
        graph_digest=_digest(row["graph_digest"], "binding graph_digest"),
        template_revision=_revision(row["template_revision"]),
        template_digest=_digest(
            row["template_digest"],
            "binding template_digest",
        ),
    )


def _encode_template(template: PackagedBuildAuthorityTemplate) -> str:
    return json.dumps(
        {
            "schema": _SCHEMA,
            "project_id": template.project_id,
            "repository_id": template.repository_id,
            "component_id": template.component_id,
            "node_id": template.node_id,
            "platform": template.platform.value,
            "workspace_relpath": template.workspace_relpath,
            "required_features": sorted(template.required_features),
            "required_toolchains": sorted(template.required_toolchains),
            "resources": {
                "cpu_cores": template.resources.cpu_cores,
                "memory_mb": template.resources.memory_mb,
                "disk_mb": template.resources.disk_mb,
            },
            "command_id": template.command_id,
            "argv": list(template.argv),
            "output_paths": list(template.output_paths),
            "max_changed_files": template.max_changed_files,
            "lease_seconds": template.lease_seconds,
            "require_gpu": template.require_gpu,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _decode_template(payload: str) -> PackagedBuildAuthorityTemplate:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PackagedBuildAuthorityError(
            "stored packaged build authority JSON is invalid"
        ) from exc
    expected = {
        "schema",
        "project_id",
        "repository_id",
        "component_id",
        "node_id",
        "platform",
        "workspace_relpath",
        "required_features",
        "required_toolchains",
        "resources",
        "command_id",
        "argv",
        "output_paths",
        "max_changed_files",
        "lease_seconds",
        "require_gpu",
    }
    if type(value) is not dict or set(value) != expected:
        raise PackagedBuildAuthorityError(
            "stored packaged build authority fields are not exact"
        )
    if value["schema"] != _SCHEMA:
        raise PackagedBuildAuthorityError(
            "stored packaged build authority schema is unsupported"
        )
    resources = value["resources"]
    if type(resources) is not dict or set(resources) != {
        "cpu_cores",
        "memory_mb",
        "disk_mb",
    }:
        raise PackagedBuildAuthorityError(
            "stored packaged build authority resources are invalid"
        )
    features = _text_list(value["required_features"], "required_features")
    toolchains = _text_list(value["required_toolchains"], "required_toolchains")
    argv = _text_list(value["argv"], "argv", allow_duplicates=True)
    output_paths = _text_list(value["output_paths"], "output_paths")
    try:
        platform = Platform(value["platform"])
        resource_envelope = ResourceEnvelope(
            _positive_int(resources["cpu_cores"], "cpu_cores"),
            _positive_int(resources["memory_mb"], "memory_mb"),
            _positive_int(resources["disk_mb"], "disk_mb"),
        )
    except (TypeError, ValueError) as exc:
        raise PackagedBuildAuthorityError(
            "stored packaged build authority platform/resources are invalid"
        ) from exc
    return PackagedBuildAuthorityTemplate(
        project_id=_exact_text(value["project_id"], "project_id"),
        repository_id=_exact_text(value["repository_id"], "repository_id"),
        component_id=_exact_text(value["component_id"], "component_id"),
        node_id=_exact_text(value["node_id"], "node_id"),
        platform=platform,
        workspace_relpath=_exact_text(
            value["workspace_relpath"],
            "workspace_relpath",
        ),
        required_features=frozenset(features),
        required_toolchains=frozenset(toolchains),
        resources=resource_envelope,
        command_id=_exact_text(value["command_id"], "command_id"),
        argv=argv,
        output_paths=output_paths,
        max_changed_files=_non_negative_int(
            value["max_changed_files"],
            "max_changed_files",
        ),
        lease_seconds=_positive_int(
            value["lease_seconds"],
            "lease_seconds",
        ),
        require_gpu=_exact_bool(value["require_gpu"], "require_gpu"),
    )


def _digest_payload(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evidence_ref(snapshot: PackagedBuildAuthoritySnapshot) -> str:
    return (
        "authority://packaged-pf5/"
        f"revision/{snapshot.revision}/sha256/{snapshot.digest}"
    )


def _text_list(
    value: object,
    label: str,
    *,
    allow_duplicates: bool = False,
) -> tuple[str, ...]:
    if type(value) is not list:
        raise PackagedBuildAuthorityError(
            f"{label} must be an exact JSON array"
        )
    items = tuple(_exact_text(item, f"{label} item") for item in value)
    if not allow_duplicates and len(items) != len(set(items)):
        raise PackagedBuildAuthorityError(
            f"{label} must not contain duplicates"
        )
    return items


def _reject_sensitive_argv(argv: tuple[str, ...]) -> None:
    for arg in argv:
        folded = arg.casefold()
        option = folded.split("=", 1)[0]
        if (
            option in _SENSITIVE_ARG_OPTIONS
            or any(marker in folded for marker in _SENSITIVE_ARG_MARKERS)
        ):
            raise PackagedBuildAuthorityError(
                "packaged build argv must not contain inline credential material"
            )


def _exact_text_set(value: object, label: str) -> None:
    if type(value) is not frozenset or any(
        type(item) is not str
        or not item
        or item != item.strip()
        or any(
            ord(character) < 32
            or ord(character) == 127
            or character in "\u0085\u2028\u2029"
            for character in item
        )
        for item in value
    ):
        raise PackagedBuildAuthorityError(
            f"{label} must be an exact frozenset of canonical text"
        )


def _exact_text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PackagedBuildAuthorityError(
            f"{label} must be canonical non-empty text"
        )
    if any(
        ord(character) < 32
        or ord(character) == 127
        or character in "\u0085\u2028\u2029"
        for character in value
    ):
        raise PackagedBuildAuthorityError(
            f"{label} must be single-line text"
        )
    return value


def _sha(value: object, label: str) -> str:
    text = _exact_text(value, label)
    if len(text) != 40 or any(
        character not in "0123456789abcdef"
        for character in text.lower()
    ):
        raise PackagedBuildAuthorityError(
            f"{label} must be a 40-character SHA"
        )
    return text


def _digest(value: object, label: str) -> str:
    text = _exact_text(value, label)
    if len(text) != 64 or any(
        character not in "0123456789abcdef"
        for character in text.lower()
    ):
        raise PackagedBuildAuthorityError(
            f"{label} must be a 64-character digest"
        )
    return text


def _revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_REVISION:
        raise PackagedBuildAuthorityError(
            "packaged build authority revision is invalid"
        )
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise PackagedBuildAuthorityError(
            f"{label} must be a positive integer"
        )
    return value


def _non_negative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise PackagedBuildAuthorityError(
            f"{label} must be a non-negative integer"
        )
    return value


def _exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise PackagedBuildAuthorityError(
            f"{label} must be an exact boolean"
        )
    return value
