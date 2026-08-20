from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib import import_module
from pathlib import Path, PurePath
from types import ModuleType
from typing import Any, Protocol

from nika_core.product_factory_deployment import (
    DeploymentFabricError,
    DeploymentIntent,
    EnvironmentTier,
    HealthEvidence,
    ProviderDeploymentResult,
    ProviderInspection,
    RollbackEvidence,
)


class StagingAdapterError(DeploymentFabricError):
    """Raised when the narrow authorized staging adapter fails closed."""


@dataclass(frozen=True, slots=True)
class AuthorizedStagingTarget:
    project_id: str
    environment_id: str
    provider_ref: str
    inventory: str
    authorization_ref: str

    def __post_init__(self) -> None:
        values = (
            self.project_id,
            self.environment_id,
            self.provider_ref,
            self.inventory,
            self.authorization_ref,
        )
        if not all(value.strip() for value in values):
            raise StagingAdapterError("authorized staging target fields must not be empty")
        if _looks_secret(self.authorization_ref):
            raise StagingAdapterError("authorization_ref must be an opaque reference, not a secret")


@dataclass(frozen=True, slots=True)
class AnsibleRunnerConfig:
    private_data_dir: Path
    deploy_playbook: str = "nika_pf3_deploy.yml"
    health_playbook: str = "nika_pf3_health.yml"
    rollback_playbook: str = "nika_pf3_rollback.yml"
    inspect_playbook: str = "nika_pf3_inspect.yml"

    def __post_init__(self) -> None:
        if not self.private_data_dir.is_absolute():
            raise StagingAdapterError("private_data_dir must be an absolute trusted local path")
        for playbook in (
            self.deploy_playbook,
            self.health_playbook,
            self.rollback_playbook,
            self.inspect_playbook,
        ):
            if not _safe_leaf(playbook):
                raise StagingAdapterError("playbook names must be trusted relative leaf filenames")


@dataclass(frozen=True, slots=True)
class RunnerExecution:
    status: str
    rc: int | None
    contract: Mapping[str, object] | None
    evidence_ref: str


class RunnerExecutionPort(Protocol):
    def execute(
        self,
        *,
        private_data_dir: Path,
        playbook: str,
        inventory: str,
        ident: str,
        extravars: Mapping[str, object],
    ) -> RunnerExecution: ...


@dataclass(slots=True)
class AnsibleRunnerClient:
    """Thin optional bridge to ansible-runner; raw stdout/events never escape this class."""

    module: ModuleType | None = None

    def execute(
        self,
        *,
        private_data_dir: Path,
        playbook: str,
        inventory: str,
        ident: str,
        extravars: Mapping[str, object],
    ) -> RunnerExecution:
        module = self.module or _load_ansible_runner()
        result = module.run(
            private_data_dir=str(private_data_dir),
            playbook=playbook,
            inventory=inventory,
            ident=ident,
            extravars=dict(extravars),
            quiet=True,
        )
        contract = _extract_contract(result.events)
        status = str(result.status)
        rc = result.rc if isinstance(result.rc, int) else None
        evidence_ref = _evidence_ref(ident, status, rc, contract)
        return RunnerExecution(status, rc, contract, evidence_ref)


@dataclass(slots=True)
class AuthorizedAnsibleStagingAdapter:
    target: AuthorizedStagingTarget
    config: AnsibleRunnerConfig
    runner: RunnerExecutionPort

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self._validate_intent(intent)
        execution = self._run("deploy", self.config.deploy_playbook, intent)
        if execution.status != "successful" or execution.rc != 0:
            return ProviderDeploymentResult(False, True, (execution.evidence_ref,))
        contract = _require_contract(execution, "deploy")
        applied = _require_bool(contract, "applied")
        return ProviderDeploymentResult(applied, False, (execution.evidence_ref,))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        self._validate_intent(intent)
        execution = self._run("health", self.config.health_playbook, intent)
        if execution.status != "successful" or execution.rc != 0:
            raise StagingAdapterError("staging health inspection did not complete successfully")
        contract = _require_contract(execution, "health")
        release_sha = _require_sha(contract, "release_sha")
        if release_sha != intent.release.source_sha:
            raise StagingAdapterError("staging health reported a different release SHA")
        healthy = _require_bool(contract, "healthy")
        return HealthEvidence(
            intent.environment.environment_id,
            release_sha,
            healthy,
            (execution.evidence_ref,),
            _contract_time(contract),
        )

    def rollback(
        self, intent: DeploymentIntent, previous_release_sha: str | None
    ) -> RollbackEvidence:
        self._validate_intent(intent)
        execution = self._run(
            "rollback",
            self.config.rollback_playbook,
            intent,
            previous_release_sha=previous_release_sha,
        )
        if execution.status != "successful" or execution.rc != 0:
            return RollbackEvidence(
                intent.environment.environment_id,
                intent.release.source_sha,
                previous_release_sha,
                False,
                (execution.evidence_ref,),
            )
        contract = _require_contract(execution, "rollback")
        succeeded = _require_bool(contract, "succeeded")
        restored = contract.get("restored_release_sha")
        if restored is not None:
            if not isinstance(restored, str):
                raise StagingAdapterError("rollback restored_release_sha must be text or null")
            _require_sha({"value": restored}, "value")
        if succeeded and restored != previous_release_sha:
            raise StagingAdapterError("rollback did not restore the requested previous release")
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            restored,
            succeeded,
            (execution.evidence_ref,),
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        self._validate_intent(intent)
        execution = self._run("inspect", self.config.inspect_playbook, intent)
        if execution.status != "successful" or execution.rc != 0:
            raise StagingAdapterError("staging inspection did not complete successfully")
        contract = _require_contract(execution, "inspect")
        release_sha = contract.get("release_sha")
        if release_sha is not None:
            if not isinstance(release_sha, str):
                raise StagingAdapterError("inspection release_sha must be text or null")
            _require_sha({"value": release_sha}, "value")
        healthy = contract.get("healthy")
        if healthy is not None and not isinstance(healthy, bool):
            raise StagingAdapterError("inspection healthy must be boolean or null")
        return ProviderInspection(release_sha, healthy, (execution.evidence_ref,))

    def _validate_intent(self, intent: DeploymentIntent) -> None:
        environment = intent.environment
        if environment.tier is not EnvironmentTier.STAGING:
            raise StagingAdapterError("this adapter is restricted to staging environments")
        expected = (
            self.target.project_id,
            self.target.environment_id,
            self.target.provider_ref,
        )
        actual = (intent.project_id, environment.environment_id, environment.provider_ref)
        if actual != expected:
            raise StagingAdapterError("deployment intent is outside the authorized staging target")

    def _run(
        self,
        operation: str,
        playbook: str,
        intent: DeploymentIntent,
        *,
        previous_release_sha: str | None = None,
    ) -> RunnerExecution:
        extravars: dict[str, object] = {
            "nika_pf3_operation": operation,
            "nika_project_id": intent.project_id,
            "nika_environment_id": intent.environment.environment_id,
            "nika_provider_ref": intent.environment.provider_ref,
            "nika_intent_id": intent.intent_id,
            "nika_release_sha": intent.release.source_sha,
            "nika_artifact_digest": intent.release.artifact_digest,
            "nika_authorization_ref": self.target.authorization_ref,
        }
        if previous_release_sha is not None:
            extravars["nika_previous_release_sha"] = previous_release_sha
        _reject_secret_values(extravars)
        ident = _runner_ident(operation, intent.intent_id, intent.release.source_sha)
        return self.runner.execute(
            private_data_dir=self.config.private_data_dir,
            playbook=playbook,
            inventory=self.target.inventory,
            ident=ident,
            extravars=extravars,
        )


def _load_ansible_runner() -> ModuleType:
    try:
        return import_module("ansible_runner")
    except ModuleNotFoundError as exc:
        raise StagingAdapterError(
            "ansible-runner optional dependency is not installed; use the deployment extra"
        ) from exc


def _extract_contract(events: Any) -> Mapping[str, object] | None:
    contract: Mapping[str, object] | None = None
    for event in events:
        if not isinstance(event, Mapping) or event.get("event") != "runner_on_ok":
            continue
        event_data = event.get("event_data")
        if not isinstance(event_data, Mapping) or event_data.get("task") != "nika_pf3_result":
            continue
        result = event_data.get("res")
        if not isinstance(result, Mapping):
            continue
        candidate = result.get("nika_pf3")
        if isinstance(candidate, Mapping):
            contract = {str(key): value for key, value in candidate.items()}
    return contract


def _require_contract(execution: RunnerExecution, operation: str) -> Mapping[str, object]:
    if execution.contract is None:
        raise StagingAdapterError(f"{operation} playbook omitted the nika_pf3 result contract")
    return execution.contract


def _require_bool(contract: Mapping[str, object], key: str) -> bool:
    value = contract.get(key)
    if not isinstance(value, bool):
        raise StagingAdapterError(f"contract field {key} must be boolean")
    return value


def _require_sha(contract: Mapping[str, object], key: str) -> str:
    value = contract.get(key)
    if not isinstance(value, str) or len(value) != 40:
        raise StagingAdapterError(f"contract field {key} must be a lowercase 40-character SHA")
    if any(character not in "0123456789abcdef" for character in value):
        raise StagingAdapterError(f"contract field {key} must be a lowercase 40-character SHA")
    return value


def _contract_time(contract: Mapping[str, object]) -> datetime:
    value = contract.get("observed_at")
    if not isinstance(value, str):
        raise StagingAdapterError("health contract must include observed_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StagingAdapterError("health observed_at is not valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StagingAdapterError("health observed_at must be timezone-aware")
    return parsed.astimezone(UTC)


def _runner_ident(operation: str, intent_id: str, release_sha: str) -> str:
    digest = sha256(f"{operation}\0{intent_id}\0{release_sha}".encode()).hexdigest()[:20]
    return f"nika-pf3-{operation}-{digest}"


def _evidence_ref(
    ident: str,
    status: str,
    rc: int | None,
    contract: Mapping[str, object] | None,
) -> str:
    safe_contract = "none" if contract is None else repr(sorted(contract.items()))
    digest = sha256(f"{ident}\0{status}\0{rc}\0{safe_contract}".encode()).hexdigest()
    return f"ansible-runner:{digest}"


def _safe_leaf(value: str) -> bool:
    path = PurePath(value)
    return (
        bool(value.strip())
        and not path.is_absolute()
        and len(path.parts) == 1
        and value not in {".", ".."}
    )


def _looks_secret(value: str) -> bool:
    lowered = value.lower()
    prefixes = ("ghp_", "github_pat_", "sk-", "xoxb-", "xapp-", "akia")
    return lowered.startswith(prefixes) or "-----begin " in lowered


def _reject_secret_values(values: Mapping[str, object]) -> None:
    for key, value in values.items():
        if isinstance(value, str) and _looks_secret(value):
            raise StagingAdapterError(f"raw secret-like value rejected for {key}")
