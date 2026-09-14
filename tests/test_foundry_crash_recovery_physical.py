from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.foundry_local import FoundryModelEvidence
from scripts.prove_foundry_crash_recovery import (
    _CALL_COMPLETE_SCHEMA,
    _CHILD_READY_SCHEMA,
    _CrashWindowProvider,
    _ensure_new_proof_directory,
    _model_identity,
    _require_real_reboot,
    _require_same_active_session,
    _validate_args,
)


class _BarrierFoundry:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.loaded = False

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="foundry-local",
            kind=ProviderKind.LOCAL,
            supports_private_data=True,
            supports_hard_cancellation=False,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.loaded = True
        await self.release.wait()
        return ModelResponse(
            request_id=request.request_id,
            text="finished",
            provider_id="foundry-local",
            provider_kind=ProviderKind.LOCAL,
            model=request.model or "test-model",
        )

    def inspect_model(self) -> FoundryModelEvidence:
        return FoundryModelEvidence(
            model_id="test-model-cpu:7",
            model_version="7",
            alias="test-model",
            cached=True,
            loaded=self.loaded,
            path=r"C:\Users\sample\.foundry\models\test-model",
            context_length=4096,
            input_modalities="text",
            output_modalities="text",
            capability_tags="chat",
            supports_tool_calling=False,
        )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="fixture-request-id",
        messages=(ModelMessage(role="user", content="fixture prompt text"),),
        model="test-model",
        provider_id="foundry-local",
        provider_kind=ProviderKind.LOCAL,
    )


def test_reboot_gate_rejects_same_boot_and_accepts_advanced_boot() -> None:
    with pytest.raises(RuntimeError, match="reboot has not been proven"):
        _require_real_reboot(arm_boot_time=1000.0, current_boot_time=1000.0)
    with pytest.raises(RuntimeError, match="reboot has not been proven"):
        _require_real_reboot(arm_boot_time=1000.0, current_boot_time=1001.0)

    _require_real_reboot(arm_boot_time=1000.0, current_boot_time=1001.01)


def test_suspended_kill_boundary_rejects_cleared_or_changed_session() -> None:
    before = SimpleNamespace(
        is_active=True,
        task_id="task-1",
        runtime_id="model-gateway:foundry-local",
        thread_id="thread-1",
        resume_token="model-gateway-inflight-v1:abc",
    )
    same = SimpleNamespace(
        is_active=True,
        task_id="task-1",
        runtime_id="model-gateway:foundry-local",
        thread_id="thread-1",
        resume_token="model-gateway-inflight-v1:abc",
    )
    cleared = SimpleNamespace(
        is_active=False,
        task_id="task-1",
        runtime_id="model-gateway:foundry-local",
        thread_id="thread-1",
        resume_token="model-gateway-inflight-v1:abc",
    )
    changed = SimpleNamespace(
        is_active=True,
        task_id="task-1",
        runtime_id="model-gateway:foundry-local",
        thread_id="thread-1",
        resume_token="model-gateway-inflight-v1:def",
    )

    assert _require_same_active_session(before, same) is same
    with pytest.raises(RuntimeError, match="no longer ACTIVE"):
        _require_same_active_session(before, cleared)
    with pytest.raises(RuntimeError, match="resume_token"):
        _require_same_active_session(before, changed)


def test_model_identity_never_exports_foundry_cache_path() -> None:
    path = r"C:\Users\sample\.foundry\models\test-model"
    model = FoundryModelEvidence(
        model_id="test-model-cpu:7",
        model_version="7",
        alias="test-model",
        cached=True,
        loaded=True,
        path=path,
        context_length=4096,
        input_modalities="text",
        output_modalities="text",
        capability_tags="chat",
        supports_tool_calling=False,
    )

    evidence = _model_identity(model)

    assert evidence["model_id"] == "test-model-cpu:7"
    assert evidence["cache_path_available"] is True
    assert "path" not in evidence
    assert path not in repr(evidence)


def test_crash_window_marker_requires_real_inflight_provider_call_and_hashes_request(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        inner = _BarrierFoundry()
        ready_path = tmp_path / "ready.json"
        complete_path = tmp_path / "complete.json"
        provider = _CrashWindowProvider(  # type: ignore[arg-type]
            inner,
            ready_path,
            complete_path,
        )
        request = _request()

        execution = asyncio.create_task(provider.complete(request))
        for _ in range(100):
            if ready_path.exists():
                break
            await asyncio.sleep(0.001)

        assert ready_path.exists()
        assert not execution.done()
        assert not complete_path.exists()
        evidence = json.loads(ready_path.read_text(encoding="utf-8"))
        assert evidence["schema"] == _CHILD_READY_SCHEMA
        assert evidence["native_request_observed_inflight"] is True
        assert evidence["model"]["loaded"] is True
        rendered = ready_path.read_text(encoding="utf-8")
        assert "fixture prompt text" not in rendered
        assert "fixture-request-id" not in rendered
        assert len(evidence["request_id_sha256"]) == 64

        inner.release.set()
        response = await execution
        assert response.text == "finished"
        assert complete_path.exists()
        completed = json.loads(complete_path.read_text(encoding="utf-8"))
        assert completed["schema"] == _CALL_COMPLETE_SCHEMA

    asyncio.run(scenario())


def test_new_proof_directory_refuses_to_overwrite_prior_evidence(tmp_path: Path) -> None:
    proof_dir = tmp_path / "proof"
    proof_dir.mkdir()
    (proof_dir / "crash-arm-evidence.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="prior state"):
        _ensure_new_proof_directory(proof_dir)


def test_verify_cli_does_not_require_reentering_model_or_license() -> None:
    args = argparse.Namespace(
        timeout=300.0,
        arm_timeout=120.0,
        download_timeout=1800.0,
        min_available_memory_gb=None,
        _crash_child=False,
        arm=False,
        verify=True,
        model=None,
        model_id=None,
        model_license=None,
    )

    _validate_args(args)


def test_arm_cli_requires_exact_model_identity_and_license() -> None:
    args = argparse.Namespace(
        timeout=300.0,
        arm_timeout=120.0,
        download_timeout=1800.0,
        min_available_memory_gb=None,
        _crash_child=False,
        arm=True,
        verify=False,
        model=" model ",
        model_id="variant",
        model_license="review",
    )

    with pytest.raises(ValueError, match="--model"):
        _validate_args(args)
