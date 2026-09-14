from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.model_gateway.contracts import (
    ModelDownloadAuthorization,
    ModelRequest,
    ModelResourcePolicy,
    ModelResponse,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider, FoundryModelEvidence
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.multi_agent.model_gateway_runtime import ModelGatewayAgentRuntime
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeRequest
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore
from scripts.prove_foundry_local import (
    _installed_foundry_package,
    _model_evidence_for_report,
    _resource_delta,
    _resource_snapshot,
    _tree_sha256,
)


_SCHEMA = "nika-foundry-local-crash-reboot-proof-v1"
_SUITE_SCHEMA = "nika-foundry-local-crash-reboot-proof-suite-v1"
_ARM_SCHEMA = "nika-foundry-local-crash-arm-v1"
_CHILD_READY_SCHEMA = "nika-foundry-local-crash-child-ready-v1"
_CALL_COMPLETE_SCHEMA = "nika-foundry-local-crash-native-call-complete-v1"
_SCENARIO_INFERENCE = "inference"
_SCENARIO_MODEL_LOAD = "model-load"
_SCENARIOS = (_SCENARIO_INFERENCE, _SCENARIO_MODEL_LOAD)
_THREAD_ID = "foundry-physical-crash"
_PROVIDER_ID = "foundry-local"
_MODEL_PROFILE = "configured"
_PLAN = "crash-proof-plan.json"
_READY = "crash-child-ready.json"
_CALL_COMPLETE = "crash-native-call-complete.json"
_TERMINAL = "crash-child-terminal.json"
_ARM = "crash-arm-evidence.json"
_DB = "crash-proof.sqlite3"


def _atomic_json_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if type(raw) is not dict:
        raise RuntimeError(f"invalid proof state at {path.name}")
    return raw


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _boot_time() -> float:
    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError(
            "physical crash proof requires Nika's 'agent' optional component (psutil)"
        ) from exc
    return float(psutil.boot_time())


def _require_real_reboot(*, arm_boot_time: object, current_boot_time: float) -> None:
    if type(arm_boot_time) not in {int, float}:
        raise RuntimeError("arm evidence has no valid Windows boot timestamp")
    arm = float(arm_boot_time)
    if not current_boot_time > arm + 1.0:
        raise RuntimeError(
            "Windows reboot has not been proven: boot timestamp did not advance after crash arm"
        )


def _resume_token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _model_identity(model: FoundryModelEvidence) -> dict[str, object]:
    report = _model_evidence_for_report(model)
    return {
        "model_id": report["model_id"],
        "model_version": report["model_version"],
        "alias": report["alias"],
        "cached": report["cached"],
        "loaded": report["loaded"],
        "cache_path_available": report["cache_path_available"],
    }


def _require_same_model_identity(
    before: dict[str, object], after: FoundryModelEvidence
) -> dict[str, object]:
    current = _model_identity(after)
    for key in ("model_id", "model_version", "alias"):
        if current[key] != before.get(key):
            raise RuntimeError(f"Foundry model identity changed across crash/reboot: {key}")
    if current["cached"] is not True:
        raise RuntimeError("Foundry model is no longer cached after reboot")
    return current


def _model_resource_policy(plan: dict[str, object]) -> ModelResourcePolicy | None:
    raw = plan.get("resource_policy")
    if raw is None:
        return None
    if type(raw) is not dict:
        raise RuntimeError("invalid resource policy in crash proof plan")
    return ModelResourcePolicy(
        max_cpu_percent=raw.get("max_cpu_percent"),  # type: ignore[arg-type]
        max_memory_percent=raw.get("max_memory_percent"),  # type: ignore[arg-type]
        min_available_memory_bytes=raw.get("min_available_memory_bytes"),  # type: ignore[arg-type]
    )


def _resource_observer(policy: ModelResourcePolicy | None) -> object | None:
    if policy is None:
        return None
    try:
        from nika_core.resources.psutil_adapter import PsutilResourceObserver
    except ImportError as exc:
        raise RuntimeError(
            "configured crash-proof resource policy requires Nika's 'agent' optional component"
        ) from exc
    return PsutilResourceObserver()


def _ready_model_from_plan(plan: dict[str, object], *, loaded: bool) -> dict[str, object]:
    raw = plan.get("model_before")
    if type(raw) is not dict:
        raise RuntimeError("crash proof plan has no model identity")
    model = dict(raw)
    model["loaded"] = loaded
    return model


class _LoadProbeModel:
    """Bracket the real SDK model.load() call without changing its execution thread."""

    def __init__(
        self,
        inner: object,
        *,
        ready_path: Path,
        complete_path: Path,
        plan: dict[str, object],
    ) -> None:
        self._inner = inner
        self._ready_path = ready_path
        self._complete_path = complete_path
        self._plan = plan

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def load(self) -> Any:
        _atomic_json_write(
            self._ready_path,
            {
                "schema": _CHILD_READY_SCHEMA,
                "scenario": _SCENARIO_MODEL_LOAD,
                "pid": os.getpid(),
                "observed_epoch_seconds": time.time(),
                "model": _ready_model_from_plan(
                    self._plan,
                    loaded=bool(getattr(self._inner, "is_loaded", False)),
                ),
                "native_model_load_observed_inflight": True,
            },
        )
        try:
            return getattr(self._inner, "load")()
        finally:
            _atomic_json_write(
                self._complete_path,
                {
                    "schema": _CALL_COMPLETE_SCHEMA,
                    "scenario": _SCENARIO_MODEL_LOAD,
                    "pid": os.getpid(),
                    "completed_epoch_seconds": time.time(),
                },
            )


class _LoadProbeCatalog:
    def __init__(
        self,
        inner: object,
        *,
        ready_path: Path,
        complete_path: Path,
        plan: dict[str, object],
    ) -> None:
        self._inner = inner
        self._ready_path = ready_path
        self._complete_path = complete_path
        self._plan = plan

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def get_model(self, alias: str) -> object:
        model = getattr(self._inner, "get_model")(alias)
        if model is None:
            return model
        return _LoadProbeModel(
            model,
            ready_path=self._ready_path,
            complete_path=self._complete_path,
            plan=self._plan,
        )


class _LoadProbeManager:
    def __init__(
        self,
        inner: object,
        *,
        ready_path: Path,
        complete_path: Path,
        plan: dict[str, object],
    ) -> None:
        self._inner = inner
        self.catalog = _LoadProbeCatalog(
            getattr(inner, "catalog"),
            ready_path=ready_path,
            complete_path=complete_path,
            plan=plan,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _real_foundry_manager() -> object:
    from foundry_local_sdk import Configuration, FoundryLocalManager

    configuration = Configuration(app_name="NikaCore")
    FoundryLocalManager.initialize(configuration)
    return FoundryLocalManager.instance


def _provider_from_plan(
    plan: dict[str, object],
    *,
    load_probe_paths: dict[str, Path] | None = None,
) -> FoundryLocalProvider:
    model = plan.get("model")
    model_id = plan.get("model_id")
    if type(model) is not str or type(model_id) is not str:
        raise RuntimeError("invalid model identity in crash proof plan")
    policy = _model_resource_policy(plan)
    manager_factory = None
    if plan.get("scenario") == _SCENARIO_MODEL_LOAD and load_probe_paths is not None:
        manager_factory = lambda: _LoadProbeManager(
            _real_foundry_manager(),
            ready_path=load_probe_paths["ready"],
            complete_path=load_probe_paths["call_complete"],
            plan=plan,
        )
    return FoundryLocalProvider(
        default_model=model,
        expected_model_id=model_id,
        resource_policy=policy,
        resource_observer=_resource_observer(policy),  # type: ignore[arg-type]
        manager_factory=manager_factory,
    )


def _definitions(store: SQLiteStore, *, create: bool) -> AgentDefinitionRepository:
    repository = AgentDefinitionRepository(store)
    if not create:
        return repository
    definition = AgentDefinition(
        agent_id="foundry-crash-proof-worker",
        name="Foundry crash proof worker",
        goal="Exercise one real local model request for crash/recovery evidence.",
        instructions=(
            "Return a detailed response with at least 1200 words. "
            "Do not call tools. This request exists only to keep a real local inference "
            "active long enough for the physical crash proof controller to terminate Nika."
        ),
        model_profile=_MODEL_PROFILE,
    )
    compiler = AgentCompiler(tools=(), model_profiles={_MODEL_PROFILE})
    repository.save_draft(compiler.compile(definition))
    repository.activate(definition)
    return repository


def _runtime(
    *,
    gateway: ModelGateway,
    definitions: AgentDefinitionRepository,
    model: str,
    timeout_seconds: float,
) -> ModelGatewayAgentRuntime:
    return ModelGatewayAgentRuntime(
        gateway=gateway,
        definitions=definitions,
        provider_id=_PROVIDER_ID,
        provider_kind=ProviderKind.LOCAL,
        model=model,
        timeout_seconds=timeout_seconds,
        privacy=PrivacyClass.SENSITIVE,
        temperature=0.0,
    )


def _runtime_request(task_id: str, *, fresh: bool = False) -> RuntimeRequest:
    work = (
        "Fresh post-reboot inference. Reply with a concise confirmation that the local "
        "model route is usable after fail-closed crash recovery."
        if fresh
        else (
            "Physical crash-window inference. Produce the requested long deterministic "
            "acceptance response; the controller will terminate this Nika process while "
            "the selected native Foundry call is still unresolved."
        )
    )
    return RuntimeRequest(
        task_id=task_id,
        thread_id=_THREAD_ID if not fresh else f"{_THREAD_ID}-fresh",
        payload={
            "agent_id": "foundry-crash-proof-worker",
            "agent_version": 1,
            "handoff": {"work": work},
        },
    )


class _CrashWindowProvider:
    """Publish an inference marker only while the real Foundry request is unresolved."""

    def __init__(
        self,
        inner: FoundryLocalProvider,
        ready_path: Path,
        complete_path: Path,
    ) -> None:
        self._inner = inner
        self._ready_path = ready_path
        self._complete_path = complete_path

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._inner.capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        task = asyncio.create_task(self._inner.complete(request))
        ready_written = False
        while not task.done():
            model = self._inner.inspect_model()
            if model.loaded and not task.done():
                _atomic_json_write(
                    self._ready_path,
                    {
                        "schema": _CHILD_READY_SCHEMA,
                        "scenario": _SCENARIO_INFERENCE,
                        "pid": os.getpid(),
                        "observed_epoch_seconds": time.time(),
                        "request_id_sha256": hashlib.sha256(
                            request.request_id.encode("utf-8")
                        ).hexdigest(),
                        "model": _model_identity(model),
                        "native_request_observed_inflight": True,
                    },
                )
                ready_written = True
                break
            await asyncio.sleep(0.01)

        if not ready_written:
            return await task
        try:
            return await task
        finally:
            _atomic_json_write(
                self._complete_path,
                {
                    "schema": _CALL_COMPLETE_SCHEMA,
                    "scenario": _SCENARIO_INFERENCE,
                    "pid": os.getpid(),
                    "completed_epoch_seconds": time.time(),
                },
            )


def _proof_paths(directory: Path) -> dict[str, Path]:
    return {
        "plan": directory / _PLAN,
        "ready": directory / _READY,
        "call_complete": directory / _CALL_COMPLETE,
        "terminal": directory / _TERMINAL,
        "arm": directory / _ARM,
        "db": directory / _DB,
    }


def _ensure_new_proof_directory(directory: Path) -> None:
    paths = _proof_paths(directory)
    existing = [path.name for path in paths.values() if path.exists()]
    if existing:
        raise RuntimeError(
            "crash proof directory contains prior state; use a new empty --proof-dir: "
            + ", ".join(sorted(existing))
        )
    directory.mkdir(parents=True, exist_ok=True)


def _preflight_plan(args: argparse.Namespace) -> dict[str, object]:
    if platform.system() != "Windows":
        raise RuntimeError("physical Foundry crash/reboot proof must run on Windows")
    package_name, package_version = _installed_foundry_package()
    if package_name != "foundry-local-sdk-winml":
        raise RuntimeError(
            "physical Windows crash proof requires the adopted foundry-local-sdk-winml package"
        )

    policy = ModelResourcePolicy(
        max_cpu_percent=args.max_cpu_percent,
        max_memory_percent=args.max_memory_percent,
        min_available_memory_bytes=(
            int(args.min_available_memory_gb * 1024**3)
            if args.min_available_memory_gb is not None
            else None
        ),
    ) if any(
        value is not None
        for value in (
            args.max_cpu_percent,
            args.max_memory_percent,
            args.min_available_memory_gb,
        )
    ) else None

    provider = FoundryLocalProvider(
        default_model=args.model,
        expected_model_id=args.model_id,
        resource_policy=policy,
        resource_observer=_resource_observer(policy),  # type: ignore[arg-type]
    )
    try:
        before = provider.inspect_model()
        if before.loaded:
            raise RuntimeError(
                "selected model is already loaded by another Foundry consumer; "
                "crash proof will not disrupt external lifecycle ownership"
            )
        if not before.cached:
            if not args.allow_download:
                raise RuntimeError(
                    "selected model is not cached; rerun with --allow-download to execute the "
                    "separate explicit model-management action"
                )
            before = asyncio.run(
                provider.download_model(
                    ModelDownloadAuthorization(
                        provider_id=_PROVIDER_ID,
                        model=args.model,
                        license_reference=args.model_license,
                        expected_model_id=args.model_id,
                    ),
                    timeout_seconds=args.download_timeout,
                )
            )
        if not before.cached:
            raise RuntimeError("explicit Foundry download did not produce cached model evidence")

        plan: dict[str, object] = {
            "schema": _SCHEMA,
            "scenario": args.scenario,
            "run_id": str(uuid.uuid4()),
            "created_epoch_seconds": time.time(),
            "arm_boot_time": _boot_time(),
            "sdk": {"package": package_name, "version": package_version},
            "model": args.model,
            "model_id": args.model_id,
            "model_license_review": args.model_license,
            "model_before": _model_identity(before),
            "resource_policy": asdict(policy) if policy is not None else None,
            "timeout_seconds": args.timeout,
            "resources_before": _resource_snapshot(),
            "hash_model_cache": bool(args.hash_model_cache),
        }
        if args.hash_model_cache:
            if before.path is None:
                raise RuntimeError("cached model path unavailable; cannot hash model cache")
            plan["model_cache_digest_before"] = _tree_sha256(Path(before.path))
        return plan
    finally:
        provider.close()


async def _child_run(proof_dir: Path) -> int:
    paths = _proof_paths(proof_dir)
    plan = _read_json(paths["plan"])
    if plan.get("schema") != _SCHEMA or plan.get("scenario") not in _SCENARIOS:
        raise RuntimeError("unsupported crash proof plan")
    timeout = plan.get("timeout_seconds")
    model = plan.get("model")
    if type(timeout) not in {int, float} or float(timeout) <= 0 or type(model) is not str:
        raise RuntimeError("invalid crash proof plan runtime identity")

    store = SQLiteStore(paths["db"])
    store.initialize()
    queue = TaskQueue(store)
    audit = AuditLog(store)
    sessions = RuntimeSessionStore(store)
    definitions = _definitions(store, create=True)
    inner = _provider_from_plan(
        plan,
        load_probe_paths=paths if plan["scenario"] == _SCENARIO_MODEL_LOAD else None,
    )
    provider: object = inner
    if plan["scenario"] == _SCENARIO_INFERENCE:
        provider = _CrashWindowProvider(inner, paths["ready"], paths["call_complete"])
    gateway = ModelGateway()
    gateway.register(provider, default=True)  # type: ignore[arg-type]
    runtime = _runtime(
        gateway=gateway,
        definitions=definitions,
        model=model,
        timeout_seconds=float(timeout),
    )
    coordinator = TaskRuntimeCoordinator(queue, audit, session_store=sessions)
    task = queue.create(
        workspace_id="foundry-physical-crash-proof",
        agent_id="foundry-crash-proof-worker",
    )
    queue.transition(task.task_id, TaskState.READY)

    try:
        result = await coordinator.start(runtime, _runtime_request(task.task_id))
        _atomic_json_write(
            paths["terminal"],
            {
                "schema": "nika-foundry-local-crash-child-terminal-v1",
                "scenario": plan["scenario"],
                "pid": os.getpid(),
                "task_id": task.task_id,
                "outcome": result.outcome.value,
                "unexpected_normal_completion": True,
            },
        )
        return 9
    finally:
        try:
            inner.close()
        except RuntimeError:
            pass


def _wait_for_crash_window(
    *,
    process: subprocess.Popen[bytes],
    proof_dir: Path,
    timeout_seconds: float,
) -> tuple[dict[str, object], object]:
    paths = _proof_paths(proof_dir)
    plan = _read_json(paths["plan"])
    scenario = plan.get("scenario")
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            terminal = _read_json(paths["terminal"]) if paths["terminal"].exists() else None
            raise RuntimeError(
                "crash child exited before the controller captured the requested native window"
                + (f": {terminal}" if terminal is not None else "")
            )
        if paths["ready"].exists() and paths["db"].exists():
            try:
                ready = _read_json(paths["ready"])
                if ready.get("schema") != _CHILD_READY_SCHEMA:
                    raise RuntimeError("invalid child-ready evidence schema")
                if ready.get("scenario") != scenario:
                    raise RuntimeError("child-ready scenario does not match proof plan")
                if paths["call_complete"].exists():
                    raise RuntimeError(
                        "requested native call completed before the crash boundary was captured"
                    )
                task_id = ready.get("task_id")
                if type(task_id) is not str:
                    store = SQLiteStore(paths["db"])
                    store.initialize()
                    sessions = RuntimeSessionStore(store)
                    records = sessions.list_resumable()
                    if len(records) != 1:
                        raise RuntimeError("expected exactly one durable crash session")
                    task_id = records[0].task_id
                    ready["task_id"] = task_id
                store = SQLiteStore(paths["db"])
                store.initialize()
                session = RuntimeSessionStore(store).get(task_id)
                if session is None or not session.is_active:
                    raise RuntimeError("durable crash session is not ACTIVE")
                if not session.resume_token.startswith("model-gateway-inflight-v1:"):
                    raise RuntimeError("durable crash session lacks model-gateway inflight marker")
                if paths["call_complete"].exists():
                    raise RuntimeError(
                        "requested native call completed before durable session validation"
                    )
                return ready, session
            except RuntimeError as exc:
                if "completed before" in str(exc):
                    raise
                last_error = exc
            except Exception as exc:
                last_error = exc
        time.sleep(0.02)
    raise RuntimeError(
        "timed out waiting for requested Foundry native call + durable ACTIVE crash window"
        + (f"; last observation: {last_error}" if last_error is not None else "")
    )


def _require_same_active_session(before: object, after: object | None) -> object:
    if after is None or getattr(after, "is_active", False) is not True:
        raise RuntimeError("durable crash session was no longer ACTIVE at suspended kill boundary")
    for name in ("task_id", "runtime_id", "thread_id", "resume_token"):
        if getattr(after, name, None) != getattr(before, name, None):
            raise RuntimeError(f"durable crash session changed before hard kill: {name}")
    return after


def _suspend_and_kill(
    process: subprocess.Popen[bytes],
    *,
    proof_dir: Path,
    session_before_suspend: object,
) -> object:
    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError(
            "physical crash proof requires Nika's 'agent' optional component (psutil)"
        ) from exc
    target = psutil.Process(process.pid)
    target.suspend()
    try:
        if process.poll() is not None:
            raise RuntimeError("crash child exited before suspension was confirmed")
        paths = _proof_paths(proof_dir)
        if paths["call_complete"].exists():
            raise RuntimeError(
                "requested native call completed before the suspended hard-kill boundary"
            )
        store = SQLiteStore(paths["db"])
        store.initialize()
        frozen = RuntimeSessionStore(store).get(
            getattr(session_before_suspend, "task_id")
        )
        frozen = _require_same_active_session(session_before_suspend, frozen)
        if paths["call_complete"].exists():
            raise RuntimeError(
                "requested native call completed before frozen session validation"
            )
    except Exception:
        try:
            target.resume()
        except Exception:
            pass
        raise

    target.kill()
    process.wait(timeout=15)
    if process.returncode == 0:
        raise RuntimeError("crash child exited normally; hard process-loss proof was not exercised")
    return frozen


def _arm(args: argparse.Namespace) -> dict[str, object]:
    proof_dir = args.proof_dir.resolve()
    _ensure_new_proof_directory(proof_dir)
    paths = _proof_paths(proof_dir)
    plan = _preflight_plan(args)
    _atomic_json_write(paths["plan"], plan)

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_crash-child",
        "--proof-dir",
        str(proof_dir),
    ]
    process = subprocess.Popen(
        command,
        cwd=str(Path.cwd()),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ready, session = _wait_for_crash_window(
            process=process,
            proof_dir=proof_dir,
            timeout_seconds=args.arm_timeout,
        )
        if ready.get("pid") != process.pid:
            raise RuntimeError("child-ready process identity does not match spawned crash child")
        session = _suspend_and_kill(
            process,
            proof_dir=proof_dir,
            session_before_suspend=session,
        )
    except Exception:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=15)
        raise

    scenario = plan["scenario"]
    model_ready = ready.get("model")
    if type(model_ready) is not dict:
        raise RuntimeError("child-ready evidence has no model identity")
    if scenario == _SCENARIO_INFERENCE:
        if model_ready.get("loaded") is not True:
            raise RuntimeError("inference scenario did not prove the Foundry model loaded")
        if ready.get("native_request_observed_inflight") is not True:
            raise RuntimeError("inference scenario did not prove an unresolved model request")
    else:
        if ready.get("native_model_load_observed_inflight") is not True:
            raise RuntimeError("model-load scenario did not prove an unresolved SDK load call")

    task_id = getattr(session, "task_id")
    evidence: dict[str, object] = {
        "schema": _ARM_SCHEMA,
        "proof_schema": _SCHEMA,
        "scenario": scenario,
        "run_id": plan["run_id"],
        "arm_boot_time": plan["arm_boot_time"],
        "sdk": plan["sdk"],
        "model_license_review": plan["model_license_review"],
        "model_before": plan["model_before"],
        "model_at_crash_window": model_ready,
        "resources_before": plan["resources_before"],
        "task_id": task_id,
        "thread_id": getattr(session, "thread_id"),
        "runtime_id": getattr(session, "runtime_id"),
        "resume_token_sha256": _resume_token_sha256(getattr(session, "resume_token")),
        "durable_active_session_observed": True,
        "durable_active_session_rechecked_while_suspended": True,
        "native_call_completion_absent_at_suspended_kill_boundary": True,
        "native_request_observed_inflight": scenario == _SCENARIO_INFERENCE,
        "native_model_load_observed_inflight": scenario == _SCENARIO_MODEL_LOAD,
        "child_process_suspended_before_kill": True,
        "child_process_hard_killed": True,
        "child_exit_code": process.returncode,
        "reboot_verified": False,
        "recovery_checked": False,
        "fresh_post_reboot_inference_executed": False,
    }
    if "model_cache_digest_before" in plan:
        evidence["model_cache_digest_before"] = plan["model_cache_digest_before"]
    _atomic_json_write(paths["arm"], evidence)
    return evidence


async def _verify_async(args: argparse.Namespace) -> dict[str, object]:
    proof_dir = args.proof_dir.resolve()
    paths = _proof_paths(proof_dir)
    plan = _read_json(paths["plan"])
    arm = _read_json(paths["arm"])
    if plan.get("schema") != _SCHEMA or arm.get("schema") != _ARM_SCHEMA:
        raise RuntimeError("unsupported crash proof state schema")
    if plan.get("scenario") not in _SCENARIOS or arm.get("scenario") != plan.get("scenario"):
        raise RuntimeError("crash proof scenario state is inconsistent")
    if arm.get("run_id") != plan.get("run_id"):
        raise RuntimeError("crash proof arm/plan run identity mismatch")

    current_boot_time = _boot_time()
    _require_real_reboot(
        arm_boot_time=arm.get("arm_boot_time"),
        current_boot_time=current_boot_time,
    )

    package_name, package_version = _installed_foundry_package()
    sdk = plan.get("sdk")
    if type(sdk) is not dict or sdk != {"package": package_name, "version": package_version}:
        raise RuntimeError("Foundry SDK package/version changed across crash/reboot proof")

    provider = _provider_from_plan(plan)
    close_completed = False
    try:
        before_verify = provider.inspect_model()
        original_model = plan.get("model_before")
        if type(original_model) is not dict:
            raise RuntimeError("crash proof plan has no model identity")
        model_after_reboot = _require_same_model_identity(original_model, before_verify)
        if before_verify.loaded:
            raise RuntimeError(
                "model is already loaded after reboot; cannot prove isolated provider-owned reload"
            )

        if plan.get("hash_model_cache") is True:
            expected_digest = plan.get("model_cache_digest_before")
            if type(expected_digest) is not dict or before_verify.path is None:
                raise RuntimeError("missing cache digest evidence for reboot verification")
            actual_digest = _tree_sha256(Path(before_verify.path))
            if actual_digest != expected_digest:
                raise RuntimeError("Foundry model cache digest changed across crash/reboot")

        store = SQLiteStore(paths["db"])
        store.initialize()
        queue = TaskQueue(store)
        audit = AuditLog(store)
        sessions = RuntimeSessionStore(store)
        definitions = _definitions(store, create=False)
        gateway = ModelGateway()
        gateway.register(provider, default=True)
        model = plan.get("model")
        timeout = plan.get("timeout_seconds")
        if type(model) is not str or type(timeout) not in {int, float}:
            raise RuntimeError("invalid crash proof plan runtime identity")
        runtime = _runtime(
            gateway=gateway,
            definitions=definitions,
            model=model,
            timeout_seconds=float(timeout),
        )
        registry = RuntimeRegistry()
        registry.register(runtime)
        recovery = RuntimeRecoveryService(
            queue=queue,
            audit=audit,
            runtimes=registry,
            sessions=sessions,
        )

        task_id = arm.get("task_id")
        if type(task_id) is not str:
            raise RuntimeError("arm evidence has no durable task id")
        candidates = [candidate for candidate in recovery.inspect() if candidate.task_id == task_id]
        if len(candidates) != 1:
            raise RuntimeError("expected exactly one crash-left recovery candidate")
        if candidates[0].disposition is not RecoveryDisposition.AUTO_RESUME_CRASH:
            raise RuntimeError(
                "crash-left task is not auto-resume candidate: "
                f"{candidates[0].disposition.value}"
            )

        executions = await recovery.resume_safe_crash_sessions(max_count=4)
        matching = [item for item in executions if item.candidate.task_id == task_id]
        if len(matching) != 1:
            raise RuntimeError("crash-left task did not pass through recovery preflight")
        recovered = matching[0]
        if recovered.candidate.disposition is not RecoveryDisposition.CHECKPOINT_UNAVAILABLE:
            raise RuntimeError(
                "opaque Foundry operation did not fail closed at checkpoint preflight"
            )
        if recovered.result is not None:
            raise RuntimeError("crash recovery unexpectedly replayed opaque model work")
        persisted = sessions.get(task_id)
        if persisted is None or not persisted.is_active:
            raise RuntimeError("fail-closed recovery unexpectedly cleared the crash marker")
        if persisted.runtime_id != arm.get("runtime_id"):
            raise RuntimeError("durable crash runtime identity changed during recovery")
        if persisted.thread_id != arm.get("thread_id"):
            raise RuntimeError("durable crash thread identity changed during recovery")
        if _resume_token_sha256(persisted.resume_token) != arm.get("resume_token_sha256"):
            raise RuntimeError("durable crash marker changed during fail-closed recovery")

        with store.connection() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None or row["state"] != TaskState.RUNNING.value:
            raise RuntimeError("crash-left task did not remain truthfully RUNNING/fail-closed")

        resources_before_fresh = _resource_snapshot()
        fresh_task = queue.create(
            workspace_id="foundry-physical-crash-proof-fresh",
            agent_id="foundry-crash-proof-worker",
        )
        queue.transition(fresh_task.task_id, TaskState.READY)
        coordinator = TaskRuntimeCoordinator(queue, audit, session_store=sessions)
        fresh = await coordinator.start(runtime, _runtime_request(fresh_task.task_id, fresh=True))
        resources_after_fresh = _resource_snapshot()
        if fresh.outcome is not RuntimeOutcome.COMPLETED:
            raise RuntimeError(f"fresh post-reboot inference failed: {fresh.outcome.value}")
        text = fresh.output.get("text")
        if type(text) is not str or not text:
            raise RuntimeError("fresh post-reboot inference returned no text")

        provider.close()
        close_completed = True
        final_model = provider.inspect_model()
        if final_model.loaded:
            raise RuntimeError("Foundry model remained loaded after post-reboot provider close")
        _require_same_model_identity(original_model, final_model)

        scenario = plan["scenario"]
        return {
            "schema": _SCHEMA,
            "scenario": scenario,
            "run_id": plan["run_id"],
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
                "processor": platform.processor(),
                "python": sys.version,
            },
            "sdk": plan["sdk"],
            "model_license_review": plan["model_license_review"],
            "arm_boot_time": arm["arm_boot_time"],
            "verify_boot_time": current_boot_time,
            "reboot_verified": True,
            "model_before": original_model,
            "model_after_reboot": model_after_reboot,
            "recovery": {
                "task_id": task_id,
                "task_state": TaskState.RUNNING.value,
                "initial_disposition": RecoveryDisposition.AUTO_RESUME_CRASH.value,
                "final_disposition": RecoveryDisposition.CHECKPOINT_UNAVAILABLE.value,
                "result_present": False,
                "error_present": recovered.error is not None,
                "resume_token_sha256": arm["resume_token_sha256"],
                "opaque_work_replayed": False,
                "crash_task_completed": False,
            },
            "fresh_post_reboot_inference": {
                "text_nonempty": True,
                "text_length": len(text),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "provider_id": fresh.output.get("provider_id"),
                "provider_kind": fresh.output.get("provider_kind"),
                "model": fresh.output.get("model"),
            },
            "resources_before_fresh_inference": resources_before_fresh,
            "resources_after_fresh_inference": resources_after_fresh,
            "fresh_inference_resource_delta": _resource_delta(
                resources_before_fresh, resources_after_fresh
            ),
            "model_final": _model_identity(final_model),
            "durable_active_session_observed_before_crash": True,
            "durable_active_session_rechecked_while_suspended": True,
            "native_call_completion_absent_at_suspended_kill_boundary": True,
            "native_request_observed_inflight_before_crash": (
                scenario == _SCENARIO_INFERENCE
            ),
            "native_model_load_observed_inflight_before_crash": (
                scenario == _SCENARIO_MODEL_LOAD
            ),
            "child_process_suspended_before_kill": True,
            "child_process_hard_killed": True,
            "fail_closed_recovery_proven": True,
            "fresh_post_reboot_inference_executed": True,
            "evidence_artifact_contains_raw_prompt_or_response": False,
            "physical_windows_foundry_crash_scenario_proven": True,
            "physical_windows_foundry_crash_proven": False,
        }
    finally:
        if not close_completed:
            original_failure_active = sys.exc_info()[0] is not None
            try:
                provider.close()
            except RuntimeError:
                if not original_failure_active:
                    raise
            except Exception:
                if not original_failure_active:
                    raise


def _require_scenario_evidence(
    evidence: dict[str, object],
    *,
    scenario: str,
) -> None:
    if evidence.get("schema") != _SCHEMA or evidence.get("scenario") != scenario:
        raise RuntimeError(f"invalid {scenario} scenario evidence")
    required_true = (
        "reboot_verified",
        "fail_closed_recovery_proven",
        "fresh_post_reboot_inference_executed",
        "physical_windows_foundry_crash_scenario_proven",
    )
    for key in required_true:
        if evidence.get(key) is not True:
            raise RuntimeError(f"{scenario} evidence is missing required proof: {key}")
    if evidence.get("physical_windows_foundry_crash_proven") is not False:
        raise RuntimeError(f"{scenario} evidence incorrectly claims the overall physical gate")


def _combine_evidence(inference_path: Path, model_load_path: Path) -> dict[str, object]:
    inference = _read_json(inference_path)
    model_load = _read_json(model_load_path)
    _require_scenario_evidence(inference, scenario=_SCENARIO_INFERENCE)
    _require_scenario_evidence(model_load, scenario=_SCENARIO_MODEL_LOAD)

    for key in ("sdk", "model_license_review", "model_before"):
        if inference.get(key) != model_load.get(key):
            raise RuntimeError(f"physical scenario evidence does not agree on {key}")

    return {
        "schema": _SUITE_SCHEMA,
        "required_scenarios": list(_SCENARIOS),
        "sdk": inference["sdk"],
        "model_license_review": inference["model_license_review"],
        "model": inference["model_before"],
        "scenarios": {
            _SCENARIO_INFERENCE: {
                "run_id": inference["run_id"],
                "evidence_sha256": _file_sha256(inference_path),
                "reboot_verified": True,
            },
            _SCENARIO_MODEL_LOAD: {
                "run_id": model_load["run_id"],
                "evidence_sha256": _file_sha256(model_load_path),
                "reboot_verified": True,
            },
        },
        "physical_windows_foundry_crash_proven": True,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run staged physical Windows Foundry crash/reboot proof scenarios. The overall "
            "gate requires separate real inference and model-load crash scenarios, each with "
            "a Windows reboot, followed by --combine."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--arm", action="store_true")
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--combine", action="store_true")
    mode.add_argument("--_crash-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--proof-dir", type=Path)
    parser.add_argument("--scenario", choices=_SCENARIOS)
    parser.add_argument("--model")
    parser.add_argument("--model-id")
    parser.add_argument("--model-license")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--arm-timeout", type=float, default=120.0)
    parser.add_argument("--download-timeout", type=float, default=1800.0)
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--hash-model-cache", action="store_true")
    parser.add_argument("--max-cpu-percent", type=float)
    parser.add_argument("--max-memory-percent", type=float)
    parser.add_argument("--min-available-memory-gb", type=float)
    parser.add_argument("--inference-evidence", type=Path)
    parser.add_argument("--model-load-evidence", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("foundry-local-crash-reboot-evidence.json"),
    )
    return parser.parse_args()


def _validate_args(args: argparse.Namespace) -> None:
    if args.timeout <= 0:
        raise ValueError("--timeout must be greater than zero")
    if args.arm_timeout <= 0:
        raise ValueError("--arm-timeout must be greater than zero")
    if args.download_timeout <= 0:
        raise ValueError("--download-timeout must be greater than zero")
    if args.min_available_memory_gb is not None and args.min_available_memory_gb <= 0:
        raise ValueError("--min-available-memory-gb must be greater than zero")
    if args._crash_child:
        if args.proof_dir is None:
            raise ValueError("--proof-dir is required for the crash child")
        return
    if args.arm:
        if args.proof_dir is None:
            raise ValueError("--proof-dir is required for --arm")
        if args.scenario not in _SCENARIOS:
            raise ValueError("--scenario is required for --arm")
        for name in ("model", "model_id", "model_license"):
            value = getattr(args, name)
            if type(value) is not str or not value.strip() or value != value.strip():
                raise ValueError(f"--{name.replace('_', '-')} is required and must be exact")
        return
    if args.verify:
        if args.proof_dir is None:
            raise ValueError("--proof-dir is required for --verify")
        return
    if args.inference_evidence is None or args.model_load_evidence is None:
        raise ValueError(
            "--inference-evidence and --model-load-evidence are required for --combine"
        )


def main() -> int:
    args = _parse_args()
    _validate_args(args)
    if args._crash_child:
        return asyncio.run(_child_run(args.proof_dir.resolve()))
    if args.arm:
        evidence = _arm(args)
        print(
            f"{evidence['scenario']} crash arm captured. Reboot Windows before VERIFY. "
            f"Arm evidence: {args.proof_dir.resolve() / _ARM}"
        )
        return 0
    if args.verify:
        evidence = asyncio.run(_verify_async(args))
    else:
        evidence = _combine_evidence(
            args.inference_evidence.resolve(),
            args.model_load_evidence.resolve(),
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Foundry crash/reboot evidence written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
