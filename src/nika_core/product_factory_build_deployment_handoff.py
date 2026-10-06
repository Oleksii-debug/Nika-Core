from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from nika_core.product_factory_build_execution import (
    BuildExecutionDispatch,
    BuildExecutionRecord,
    BuildExecutionScopeRequest,
    ExecutionGrant,
    BuildExecutionSpec,
    BuildExecutionState,
)
from nika_core.product_factory_build_execution_host import DurableBuildExecutionHost
from nika_core.product_factory_deployment import (
    DeploymentIntent,
    DeploymentRecord,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionRequest,
    NormalizedBuildEvidence,
    ReleaseRef,
)
from nika_core.product_factory_deployment_checkpoint import DurableDeploymentFabric

_MAX_IDENTITY_BYTES = 1024
_MAX_RELEASE_VERSION_BYTES = 256
_MAX_MIGRATION_REFS = 256


class BuildDeploymentHandoffError(ValueError):
    """Raised when a PF5 build cannot safely enter canonical PF6 deployment."""


@dataclass(frozen=True, slots=True)
class BuildDeploymentAuthority:
    """Host-owned authority for converting one finished PF5 work item to staging."""

    project_id: str
    repository_id: str
    work_id: str
    release_version: str
    staging_environment: EnvironmentIdentity
    migration_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        project_id = _single_line_text(self.project_id, "project_id")
        repository_id = _single_line_text(self.repository_id, "repository_id")
        work_id = _single_line_text(self.work_id, "work_id")
        release_version = _single_line_text(
            self.release_version,
            "release_version",
            max_bytes=_MAX_RELEASE_VERSION_BYTES,
        )
        environment = _snapshot_staging_environment(
            self.staging_environment,
            project_id=project_id,
        )
        migration_refs = _text_tuple(
            self.migration_refs,
            "migration_refs",
            max_items=_MAX_MIGRATION_REFS,
        )
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "repository_id", repository_id)
        object.__setattr__(self, "work_id", work_id)
        object.__setattr__(self, "release_version", release_version)
        object.__setattr__(self, "staging_environment", environment)
        object.__setattr__(self, "migration_refs", migration_refs)


class TrustedBuildDeploymentAuthorityPort(Protocol):
    """Resolve release/staging authority outside candidate and model payloads."""

    def resolve(
        self,
        *,
        project_id: str,
        repository_id: str,
        work_id: str,
    ) -> BuildDeploymentAuthority: ...


@dataclass(slots=True)
class BuildDeploymentHandoff:
    """Thin PF5 -> PF6 composition with no second effect or persistence authority."""

    build_host: DurableBuildExecutionHost
    deployment: DurableDeploymentFabric
    authority: TrustedBuildDeploymentAuthorityPort

    def __post_init__(self) -> None:
        if type(self.build_host) is not DurableBuildExecutionHost:
            raise BuildDeploymentHandoffError(
                "PF5 handoff requires the canonical durable build host"
            )
        if type(self.deployment) is not DurableDeploymentFabric:
            raise BuildDeploymentHandoffError(
                "PF6 handoff requires the canonical durable deployment fabric"
            )

    def deploy_staging(self, work_id: str) -> DeploymentRecord:
        work_id = _single_line_text(work_id, "work_id")
        snapshot = self.build_host.snapshot()
        record = _find_record(snapshot.coordinator.records, work_id)
        build = _snapshot_succeeded_build(record, expected_work_id=work_id)

        try:
            raw_authority = self.authority.resolve(
                project_id=build.project_id,
                repository_id=build.repository_id,
                work_id=build.work_id,
            )
        except Exception as exc:
            raise BuildDeploymentHandoffError(
                "trusted PF5-to-PF6 handoff authority failed"
            ) from exc
        authority = _snapshot_authority(raw_authority)

        if (
            authority.project_id != build.project_id
            or authority.repository_id != build.repository_id
            or authority.work_id != build.work_id
        ):
            raise BuildDeploymentHandoffError(
                "trusted PF5-to-PF6 authority returned the wrong build identity"
            )

        release = ReleaseRef(
            project_id=build.project_id,
            version=authority.release_version,
            source_sha=build.source_sha,
            artifact_digest=build.artifact_digest,
        )
        intent = DeploymentIntent(
            intent_id=_handoff_intent_id(
                build.project_id,
                build.repository_id,
                build.work_id,
            ),
            project_id=build.project_id,
            environment=authority.staging_environment,
            release=release,
            migration_refs=authority.migration_refs,
        )
        return self.deployment.deploy(intent)


@dataclass(frozen=True, slots=True)
class _SucceededBuild:
    project_id: str
    repository_id: str
    work_id: str
    source_sha: str
    artifact_digest: str


def _find_record(
    records: object,
    expected_work_id: str,
) -> BuildExecutionRecord:
    if type(records) is not tuple:
        raise BuildDeploymentHandoffError("PF5 durable build snapshot records are invalid")

    match: BuildExecutionRecord | None = None
    for candidate in records:
        if type(candidate) is not BuildExecutionRecord:
            raise BuildDeploymentHandoffError(
                "PF5 durable build snapshot contains an invalid record carrier"
            )
        spec = candidate.spec
        if type(spec) is not BuildExecutionSpec or type(spec.request) is not ExecutionRequest:
            raise BuildDeploymentHandoffError(
                "PF5 durable build snapshot contains an invalid specification carrier"
            )
        candidate_work_id = _single_line_text(spec.request.work_id, "PF5 work_id")
        if candidate_work_id != expected_work_id:
            continue
        if match is not None:
            raise BuildDeploymentHandoffError(
                "PF5 durable build snapshot contains duplicate work identity"
            )
        match = candidate

    if match is None:
        raise BuildDeploymentHandoffError("PF5 durable build work was not found")
    return match


def _snapshot_succeeded_build(
    record: BuildExecutionRecord,
    *,
    expected_work_id: str,
) -> _SucceededBuild:
    if record.state is not BuildExecutionState.SUCCEEDED:
        raise BuildDeploymentHandoffError(
            "PF5 build is not durably successful and cannot be deployed"
        )
    if type(record.spec) is not BuildExecutionSpec:
        raise BuildDeploymentHandoffError("PF5 build specification carrier is invalid")
    spec = record.spec
    if type(spec.request) is not ExecutionRequest:
        raise BuildDeploymentHandoffError("PF5 execution request carrier is invalid")
    if type(spec.scope) is not BuildExecutionScopeRequest:
        raise BuildDeploymentHandoffError("PF5 execution scope carrier is invalid")
    if type(record.grant) is not ExecutionGrant:
        raise BuildDeploymentHandoffError("PF5 execution grant carrier is invalid")
    if type(record.dispatch) is not BuildExecutionDispatch:
        raise BuildDeploymentHandoffError(
            "successful PF5 build lacks exact dispatch identity"
        )
    if type(record.evidence) is not NormalizedBuildEvidence:
        raise BuildDeploymentHandoffError(
            "successful PF5 build lacks exact normalized evidence"
        )

    project_id = _single_line_text(spec.request.project_id, "PF5 project_id")
    work_id = _single_line_text(spec.request.work_id, "PF5 work_id")
    repository_id = _single_line_text(spec.scope.repository_id, "PF5 repository_id")
    source_sha = _single_line_text(spec.source_sha, "PF5 source_sha")
    node_id = _single_line_text(record.node_id, "PF5 node_id")
    if work_id != expected_work_id:
        raise BuildDeploymentHandoffError("PF5 build work identity changed during handoff")

    grant = record.grant
    if (
        _single_line_text(grant.project_id, "PF5 grant project_id") != project_id
        or _single_line_text(grant.repository_id, "PF5 grant repository_id") != repository_id
        or _single_line_text(grant.work_id, "PF5 grant work_id") != work_id
        or node_id not in grant.allowed_node_ids
    ):
        raise BuildDeploymentHandoffError(
            "PF5 successful build grant does not match exact durable specification"
        )

    evidence = _snapshot_build_evidence(record.evidence)
    dispatch = record.dispatch
    dispatch_project_id = _single_line_text(dispatch.project_id, "PF5 dispatch project_id")
    dispatch_work_id = _single_line_text(dispatch.work_id, "PF5 dispatch work_id")
    dispatch_node_id = _single_line_text(dispatch.node_id, "PF5 dispatch node_id")
    dispatch_source_sha = _single_line_text(dispatch.source_sha, "PF5 dispatch source_sha")
    if (
        dispatch_project_id != project_id
        or dispatch_work_id != work_id
        or dispatch_node_id != node_id
        or dispatch_source_sha != source_sha
        or dispatch.grant != grant
        or evidence.work_id != work_id
        or evidence.node_id != node_id
        or evidence.release_sha != source_sha
        or evidence.succeeded is not True
    ):
        raise BuildDeploymentHandoffError(
            "PF5 successful build evidence does not match exact durable dispatch"
        )

    return _SucceededBuild(
        project_id=project_id,
        repository_id=repository_id,
        work_id=work_id,
        source_sha=source_sha,
        artifact_digest=evidence.artifact_digest,
    )


def _snapshot_build_evidence(value: NormalizedBuildEvidence) -> NormalizedBuildEvidence:
    if type(value.succeeded) is not bool:
        raise BuildDeploymentHandoffError("PF5 build success carrier must be an exact boolean")
    evidence_refs = _text_tuple(value.evidence_refs, "PF5 evidence_refs", max_items=1024)
    try:
        return NormalizedBuildEvidence(
            work_id=_single_line_text(value.work_id, "PF5 evidence work_id"),
            node_id=_single_line_text(value.node_id, "PF5 evidence node_id"),
            release_sha=_single_line_text(value.release_sha, "PF5 evidence source_sha"),
            artifact_digest=_single_line_text(
                value.artifact_digest,
                "PF5 evidence artifact_digest",
            ),
            succeeded=value.succeeded,
            evidence_refs=evidence_refs,
        )
    except (TypeError, ValueError) as exc:
        raise BuildDeploymentHandoffError(
            "PF5 normalized build evidence failed readmission"
        ) from exc


def _snapshot_authority(value: object) -> BuildDeploymentAuthority:
    if type(value) is not BuildDeploymentAuthority:
        raise BuildDeploymentHandoffError(
            "trusted PF5-to-PF6 authority returned an invalid carrier"
        )
    try:
        return BuildDeploymentAuthority(
            project_id=value.project_id,
            repository_id=value.repository_id,
            work_id=value.work_id,
            release_version=value.release_version,
            staging_environment=value.staging_environment,
            migration_refs=value.migration_refs,
        )
    except (TypeError, ValueError) as exc:
        raise BuildDeploymentHandoffError(
            "trusted PF5-to-PF6 authority failed readmission"
        ) from exc


def _snapshot_staging_environment(
    value: object,
    *,
    project_id: str,
) -> EnvironmentIdentity:
    if type(value) is not EnvironmentIdentity:
        raise BuildDeploymentHandoffError(
            "trusted handoff staging environment carrier is invalid"
        )
    if type(value.tier) is not EnvironmentTier or value.tier is not EnvironmentTier.STAGING:
        raise BuildDeploymentHandoffError(
            "PF5-to-PF6 handoff authority must target staging"
        )
    environment = EnvironmentIdentity(
        environment_id=_single_line_text(value.environment_id, "staging environment_id"),
        project_id=_single_line_text(value.project_id, "staging project_id"),
        tier=value.tier,
        provider_ref=_single_line_text(value.provider_ref, "staging provider_ref"),
    )
    if environment.project_id != project_id:
        raise BuildDeploymentHandoffError(
            "staging environment project does not match handoff project"
        )
    return environment


def _handoff_intent_id(project_id: str, repository_id: str, work_id: str) -> str:
    payload = "\0".join((project_id, repository_id, work_id)).encode("utf-8")
    return "pf5-pf6:" + hashlib.sha256(payload).hexdigest()


def _text_tuple(value: object, label: str, *, max_items: int) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > max_items:
        raise BuildDeploymentHandoffError(
            f"{label} must be an exact tuple with at most {max_items} items"
        )
    result = tuple(_single_line_text(item, f"{label} item") for item in value)
    if len(result) != len(set(result)):
        raise BuildDeploymentHandoffError(f"{label} must not contain duplicates")
    return result


def _single_line_text(
    value: object,
    label: str,
    *,
    max_bytes: int = _MAX_IDENTITY_BYTES,
) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BuildDeploymentHandoffError(
            f"{label} must be exact normalized non-empty text"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BuildDeploymentHandoffError(f"{label} must be valid UTF-8 text") from exc
    if len(encoded) > max_bytes:
        raise BuildDeploymentHandoffError(f"{label} exceeds the byte limit")
    if any(
        ord(character) < 32
        or ord(character) == 127
        or character in "\u0085\u2028\u2029"
        for character in value
    ):
        raise BuildDeploymentHandoffError(f"{label} must be single-line text")
    return value
