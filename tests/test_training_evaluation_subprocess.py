from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import pytest

import nika_core.training_evaluation_subprocess as evaluation_subprocess
from nika_core.artifacts import ArtifactRegistry
from nika_core.data.sqlite import SQLiteStore
from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelIntegrityBasis,
)
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    PrivacyClass,
    ProviderKind,
)
from nika_core.training_evaluation_attestation import AttestedTrainingCandidateGateway
from nika_core.training_evaluation_binding import TrainingEvaluationBinding
from nika_core.training_evaluation_subprocess import RegistrySubprocessLoadedModelAttestor


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


_HELD_OUT_SHA256 = _sha(b"held-out")
_PROVIDER_MANIFEST_SHA256 = _sha(b"ollama-provider-manifest")


def _script(tmp_path: Path, body: str, *, name: str = "evaluator.py") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _candidate(tmp_path: Path) -> tuple[Path, ModelArtifactDescriptor]:
    body = b"exact-trained-candidate-model-bytes\n"
    path = tmp_path / "candidate model.bin"
    path.write_bytes(body)
    descriptor = ModelArtifactDescriptor(
        kind=ModelArtifactKind.EXTERNAL_LOCAL,
        provider_id="ollama",
        model_id="candidate-model",
        model_version="job-1",
        source_reference="local-training:job-1",
        license_reference="license:project-1",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256=_sha(body),
        size_bytes=len(body),
    )
    return path, descriptor


def _binding(descriptor: ModelArtifactDescriptor) -> TrainingEvaluationBinding:
    assert descriptor.sha256 is not None
    assert descriptor.size_bytes is not None
    return TrainingEvaluationBinding(
        job_id="job-1",
        base_candidate_id="models/base",
        base_provider_id="ollama",
        base_model_id="base-model",
        challenger_candidate_id="models/candidate/job-1",
        challenger_provider_id=descriptor.provider_id,
        challenger_model_id=descriptor.model_id,
        base_sha256=_sha(b"base-model"),
        challenger_sha256=descriptor.sha256,
        candidate_artifact_ref="models/candidate/job-1",
        frozen_package_sha256=_sha(b"package"),
        scale_authorization_sha256=_sha(b"scale-authorization"),
        execution_plan_sha256=_sha(b"training-execution-plan"),
        evaluation_set_sha256=_HELD_OUT_SHA256,
        base_descriptor_digest=_sha(b"base-descriptor"),
        base_descriptor_registry_key=_sha(b"base-registry"),
        base_size_bytes=len(b"base-model"),
        descriptor_digest=descriptor.descriptor_digest,
        descriptor_registry_key=descriptor.registry_key,
        challenger_size_bytes=descriptor.size_bytes,
    )


def _request(
    *,
    evaluation_set_sha256: str = _HELD_OUT_SHA256,
) -> ModelRequest:
    return ModelRequest(
        request_id="benchmark-request-1",
        messages=(ModelMessage(role="user", content="question"),),
        model="candidate-model",
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
        privacy=PrivacyClass.PRIVATE,
        metadata={
            "model_candidate_id": "models/candidate/job-1",
            "evaluation_set_sha256": evaluation_set_sha256,
        },
    )


def _success_script(
    tmp_path: Path,
    *,
    text_expression: str = '"answer"',
    digest_expression: str = "hashlib.sha256(candidate).hexdigest()",
    size_expression: str = "len(candidate)",
    descriptor_expression: str = 'request["binding"]["descriptor_digest"]',
    total_tokens_expression: str = "5",
    provider_manifest_expression: str | None = None,
    extra_prefix: str = "",
    name: str = "evaluator.py",
) -> Path:
    provider_manifest_line = (
        f'    "provider_manifest_sha256": {provider_manifest_expression},\n'
        if provider_manifest_expression is not None
        else ""
    )
    return _script(
        tmp_path,
        f"""
import hashlib
import json
import os
import sys
from pathlib import Path

{extra_prefix}
request = json.loads(sys.stdin.buffer.read())
candidate = Path(request["candidate"]["path"]).read_bytes()
response = {{
    "protocol_version": request["protocol_version"],
    "request_id": request["request"]["request_id"],
    "provider_id": request["request"]["provider_id"],
    "model": request["request"]["model"],
    "text": {text_expression},
    "loaded_artifact_sha256": {digest_expression},
    "loaded_artifact_size_bytes": {size_expression},
    "descriptor_digest": {descriptor_expression},
{provider_manifest_line}    "usage": {{
        "input_tokens": 2,
        "output_tokens": 3,
        "total_tokens": {total_tokens_expression},
    }},
}}
sys.stdout.write(json.dumps(response))
""".strip(),
        name=name,
    )


def _registry(
    tmp_path: Path,
    script: Path,
    *,
    script_kind: str = "model_evaluator_command_file",
) -> tuple[ArtifactRegistry, str, str, Path]:
    executable = Path(sys.executable).resolve()
    roots = tuple(dict.fromkeys((executable.parent, tmp_path.resolve())))
    registry = ArtifactRegistry.from_store(
        SQLiteStore(tmp_path / "evaluation-artifacts.sqlite3"),
        local_file_roots=roots,
    )
    executable_record = registry.register_file(
        workspace_id="evaluation-tests",
        idempotency_key="python-evaluator-executable",
        path=executable,
        kind="model_evaluator_executable",
    )
    script_record = registry.register_file(
        workspace_id="evaluation-tests",
        idempotency_key=f"script:{script.name}",
        path=script,
        kind=script_kind,
    )
    return registry, executable_record.artifact_id, script_record.artifact_id, executable


def _adapter(
    tmp_path: Path,
    script: Path,
    *,
    timeout_seconds: float = 5.0,
    max_response_bytes: int = 64 * 1024,
    script_kind: str = "model_evaluator_command_file",
    environment: dict[str, str] | None = None,
) -> tuple[
    RegistrySubprocessLoadedModelAttestor,
    ArtifactRegistry,
    str,
    ModelArtifactDescriptor,
]:
    candidate_path, descriptor = _candidate(tmp_path)
    registry, executable_id, script_id, executable = _registry(
        tmp_path,
        script,
        script_kind=script_kind,
    )
    adapter = RegistrySubprocessLoadedModelAttestor(
        (str(executable), str(script)),
        artifact_registry=registry,
        evaluator_artifact_id=executable_id,
        command_artifact_ids={1: script_id},
        candidate_path=str(candidate_path.resolve()),
        descriptor=descriptor,
        allowed_root=str(tmp_path.resolve()),
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
        environment=environment,
    )
    return adapter, registry, script_id, descriptor


@pytest.mark.asyncio
async def test_real_subprocess_hashes_candidate_in_same_effect_and_gateway_accepts(
    tmp_path: Path,
) -> None:
    script = _success_script(tmp_path)
    adapter, _, _, descriptor = _adapter(tmp_path, script)
    binding = _binding(descriptor)
    gateway = AttestedTrainingCandidateGateway(
        adapter,
        binding=binding,
        expected_attestor_id=adapter.attestor_id,
        expected_attestor_sha256=adapter.attestor_sha256,
    )

    response = await gateway.complete(_request())

    assert response.text == "answer"
    assert response.provider_id == "ollama"
    assert response.model == "candidate-model"
    assert response.usage.input_tokens == 2
    assert response.usage.output_tokens == 3
    assert response.usage.total_tokens == 5


@pytest.mark.asyncio
async def test_same_effect_provider_manifest_is_carried_separately(
    tmp_path: Path,
) -> None:
    script = _success_script(
        tmp_path,
        provider_manifest_expression=repr(_PROVIDER_MANIFEST_SHA256),
    )
    adapter, _, _, descriptor = _adapter(tmp_path, script)

    result = await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert result.attestation.artifact_sha256 == descriptor.sha256
    assert result.attestation.provider_manifest_sha256 == _PROVIDER_MANIFEST_SHA256
    assert result.attestation.provider_manifest_sha256 != descriptor.sha256


@pytest.mark.asyncio
async def test_legacy_evaluator_response_keeps_provider_manifest_absent(
    tmp_path: Path,
) -> None:
    script = _success_script(tmp_path)
    adapter, _, _, descriptor = _adapter(tmp_path, script)

    result = await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert result.attestation.provider_manifest_sha256 is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest_expression",
    (
        repr("not-a-sha256"),
        repr("A" * 64),
        "None",
    ),
)
async def test_invalid_provider_manifest_is_unknown_after_evaluator_effect(
    tmp_path: Path,
    manifest_expression: str,
) -> None:
    script = _success_script(
        tmp_path,
        provider_manifest_expression=manifest_expression,
    )
    adapter, _, _, descriptor = _adapter(tmp_path, script)

    with pytest.raises(ModelGatewayError) as exc_info:
        await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN


@pytest.mark.asyncio
async def test_parent_environment_is_not_inherited_by_evaluator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIKA_EVALUATION_SECRET", "must-not-leak")
    script = _success_script(
        tmp_path,
        text_expression=(
            '"secret-seen:" + '
            'str(os.getenv("NIKA_EVALUATION_SECRET") is not None).lower()'
        ),
    )
    adapter, _, _, descriptor = _adapter(tmp_path, script)

    result = await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert result.response.text == "secret-seen:false"


@pytest.mark.asyncio
async def test_wrong_evaluation_set_is_rejected_before_evaluator_effect(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "started.txt"
    script = _success_script(
        tmp_path,
        extra_prefix=f'Path({str(marker)!r}).write_text("started", encoding="utf-8")',
    )
    adapter, _, _, descriptor = _adapter(tmp_path, script)

    with pytest.raises(ModelGatewayError) as exc_info:
        await adapter.complete_attested(
            _request(evaluation_set_sha256=_sha(b"different-held-out")),
            binding=_binding(descriptor),
        )

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert not marker.exists()


@pytest.mark.asyncio
async def test_candidate_tamper_fails_before_evaluator_effect(tmp_path: Path) -> None:
    marker = tmp_path / "started.txt"
    script = _success_script(
        tmp_path,
        extra_prefix=f'Path({str(marker)!r}).write_text("started", encoding="utf-8")',
    )
    adapter, _, _, descriptor = _adapter(tmp_path, script)
    (tmp_path / "candidate model.bin").write_bytes(b"tampered-candidate-model-bytes\n")

    with pytest.raises(ModelGatewayError) as exc_info:
        await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert not marker.exists()


@pytest.mark.asyncio
async def test_evaluator_command_tamper_fails_before_effect(tmp_path: Path) -> None:
    marker = tmp_path / "started.txt"
    script = _success_script(
        tmp_path,
        extra_prefix=f'Path({str(marker)!r}).write_text("started", encoding="utf-8")',
    )
    adapter, _, _, descriptor = _adapter(tmp_path, script)
    script.write_text("raise SystemExit(0)", encoding="utf-8")

    with pytest.raises(ModelGatewayError) as exc_info:
        await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert not marker.exists()


@pytest.mark.asyncio
async def test_command_launch_guard_spans_final_verify_spawn_and_post_start_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _success_script(tmp_path)
    adapter, _, _, descriptor = _adapter(tmp_path, script)
    state = {"active": False}
    verify_states: list[bool] = []
    spawn_states: list[bool] = []

    class GuardProbe:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> GuardProbe:
            assert state["active"] is False
            state["active"] = True
            return self

        def __exit__(
            self,
            exc_type: object,
            exc_value: object,
            traceback: object,
        ) -> None:
            state["active"] = False

    original_verify = adapter._verify_command_records
    original_spawn = evaluation_subprocess.asyncio.create_subprocess_exec

    def tracked_verify(records: object) -> None:
        verify_states.append(state["active"])
        original_verify(records)  # type: ignore[arg-type]

    async def tracked_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        spawn_states.append(state["active"])
        return await original_spawn(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(evaluation_subprocess, "_CommandArtifactLaunchGuard", GuardProbe)
    monkeypatch.setattr(adapter, "_verify_command_records", tracked_verify)
    monkeypatch.setattr(
        evaluation_subprocess.asyncio,
        "create_subprocess_exec",
        tracked_spawn,
    )

    result = await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert result.response.text == "answer"
    assert verify_states == [False, True, True]
    assert spawn_states == [True]
    assert state["active"] is False


@pytest.mark.skipif(os.name != "nt", reason="Windows file-share semantics")
def test_command_launch_guard_refuses_writer_and_releases_all_handles(
    tmp_path: Path,
) -> None:
    script = _success_script(tmp_path)
    registry, executable_id, script_id, executable = _registry(tmp_path, script)
    records = {
        0: registry.get(executable_id),
        1: registry.get(script_id),
    }
    command = (str(executable), str(script.resolve()))

    with script.open("r+b"):
        with pytest.raises(ModelGatewayError) as exc_info:
            with evaluation_subprocess._CommandArtifactLaunchGuard(
                command,
                records,
                provider_id="ollama",
            ):
                pytest.fail("guard unexpectedly admitted a writable command artifact")

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT

    with evaluation_subprocess._CommandArtifactLaunchGuard(
        command,
        records,
        provider_id="ollama",
    ):
        pass

    script.write_text("replacement-after-release", encoding="utf-8")
    assert script.read_text(encoding="utf-8") == "replacement-after-release"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows file-share semantics")
async def test_windows_command_launch_guard_blocks_replace_at_spawn_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _success_script(tmp_path)
    replacement = tmp_path / "replacement-evaluator.py"
    replacement.write_text("raise SystemExit(97)\n", encoding="utf-8")
    adapter, _, _, descriptor = _adapter(tmp_path, script)
    original_spawn = evaluation_subprocess.asyncio.create_subprocess_exec
    attempts = 0

    async def racing_spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        nonlocal attempts
        attempts += 1
        try:
            os.replace(replacement, script)
        except OSError as exc:
            assert getattr(exc, "winerror", None) in {5, 32, 33}
        else:
            raise AssertionError(
                "Registry-bound evaluator command artifact was replaceable at spawn"
            )
        return await original_spawn(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        evaluation_subprocess.asyncio,
        "create_subprocess_exec",
        racing_spawn,
    )

    result = await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert attempts == 1
    assert result.response.text == "answer"

    os.replace(replacement, script)
    assert script.read_text(encoding="utf-8") == "raise SystemExit(97)\n"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows file-share semantics")
async def test_windows_command_launch_guard_reverifies_swap_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "unexpected-evaluator-start.txt"
    script = _success_script(tmp_path)
    replacement = tmp_path / "replacement-evaluator.py"
    replacement.write_text(
        "from pathlib import Path\n"
        + f"Path({str(marker)!r}).write_text('started', encoding='utf-8')\n",
        encoding="utf-8",
    )
    adapter, _, _, descriptor = _adapter(tmp_path, script)
    original_verify = adapter._verify_command_records
    verify_calls = 0

    def verify_then_swap(records: object) -> None:
        nonlocal verify_calls
        verify_calls += 1
        original_verify(records)  # type: ignore[arg-type]
        if verify_calls == 1:
            os.replace(replacement, script)

    async def process_must_not_start(*args: object, **kwargs: object) -> object:
        raise AssertionError("process effect reached after evaluator command replacement")

    monkeypatch.setattr(adapter, "_verify_command_records", verify_then_swap)
    monkeypatch.setattr(
        evaluation_subprocess.asyncio,
        "create_subprocess_exec",
        process_must_not_start,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert verify_calls == 2
    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows file-share semantics")
def test_command_launch_guard_rolls_back_earlier_handle_when_later_lock_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _success_script(tmp_path)
    registry, executable_id, script_id, executable = _registry(tmp_path, script)
    records = {
        0: registry.get(executable_id),
        1: registry.get(script_id),
    }
    command = (str(executable), str(script.resolve()))
    opened: list[str] = []
    closed: list[int] = []

    def fake_open(path: str) -> int:
        opened.append(path)
        if len(opened) == 2:
            raise OSError(32, "sharing violation")
        return 101

    def fake_close(handle: int) -> None:
        closed.append(handle)

    monkeypatch.setattr(
        evaluation_subprocess,
        "_open_windows_command_artifact_lock",
        fake_open,
    )
    monkeypatch.setattr(
        evaluation_subprocess,
        "_close_windows_command_artifact_lock",
        fake_close,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        with evaluation_subprocess._CommandArtifactLaunchGuard(
            command,
            records,
            provider_id="ollama",
        ):
            pytest.fail("guard unexpectedly admitted a partially locked command")

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert opened == [command[0], command[1]]
    assert closed == [101]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("digest", ModelErrorCode.PROVIDER_ERROR),
        ("size", ModelErrorCode.PROVIDER_ERROR),
        ("descriptor", ModelErrorCode.PROVIDER_ERROR),
        ("tokens", ModelErrorCode.PROVIDER_ERROR),
    ],
)
async def test_post_effect_identity_forgery_is_unknown(
    tmp_path: Path,
    mode: str,
    expected_code: ModelErrorCode,
) -> None:
    kwargs: dict[str, str] = {}
    if mode == "digest":
        kwargs["digest_expression"] = '"0" * 64'
    elif mode == "size":
        kwargs["size_expression"] = "len(candidate) + 1"
    elif mode == "descriptor":
        kwargs["descriptor_expression"] = '"f" * 64'
    else:
        kwargs["total_tokens_expression"] = "999"
    script = _success_script(tmp_path, **kwargs)
    adapter, _, _, descriptor = _adapter(tmp_path, script)

    with pytest.raises(ModelGatewayError) as exc_info:
        await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert exc_info.value.code is expected_code
    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN


@pytest.mark.asyncio
async def test_nonzero_evaluator_kills_descendant_before_inherited_stdout_eof(
    tmp_path: Path,
) -> None:
    spawned = tmp_path / "nonzero-evaluator-descendant-spawned.txt"
    survived = tmp_path / "nonzero-evaluator-descendant-survived.txt"
    child_code = (
        "import pathlib,sys,time; "
        "time.sleep(1.5); "
        "pathlib.Path(sys.argv[1]).write_text('survived', encoding='utf-8')"
    )
    script = _script(
        tmp_path,
        f"""
import pathlib
import subprocess
import sys

sys.stdin.buffer.read()
subprocess.Popen([sys.executable, "-c", {child_code!r}, {str(survived)!r}])
pathlib.Path({str(spawned)!r}).write_text("spawned", encoding="utf-8")
raise SystemExit(7)
""".strip(),
    )
    adapter, _, _, descriptor = _adapter(tmp_path, script)

    with pytest.raises(ModelGatewayError) as exc_info:
        await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert spawned.exists(), "test did not prove that an evaluator descendant was started"
    await asyncio.sleep(1.0)
    assert not survived.exists(), "evaluator descendant escaped nonzero containment"


@pytest.mark.asyncio
async def test_malformed_json_is_unknown_after_process_start(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        """
import sys
sys.stdin.buffer.read()
sys.stdout.write("{not-json")
""".strip(),
    )
    adapter, _, _, descriptor = _adapter(tmp_path, script)

    with pytest.raises(ModelGatewayError) as exc_info:
        await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN


@pytest.mark.asyncio
async def test_oversized_response_is_bounded_and_unknown(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        """
import sys
sys.stdin.buffer.read()
sys.stdout.write("x" * 65536)
sys.stdout.flush()
""".strip(),
    )
    adapter, _, _, descriptor = _adapter(
        tmp_path,
        script,
        max_response_bytes=1024,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN


@pytest.mark.asyncio
async def test_timeout_kills_evaluator_and_is_unknown(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist.txt"
    script = _script(
        tmp_path,
        f"""
import sys
import time
from pathlib import Path

sys.stdin.buffer.read()
time.sleep(2)
Path({str(marker)!r}).write_text("late-effect", encoding="utf-8")
""".strip(),
    )
    adapter, _, _, descriptor = _adapter(
        tmp_path,
        script,
        timeout_seconds=0.1,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert exc_info.value.code is ModelErrorCode.TIMEOUT
    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    await asyncio.sleep(0.15)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_timeout_kills_evaluator_descendant_process_tree(tmp_path: Path) -> None:
    spawned = tmp_path / "evaluator-descendant-spawned.txt"
    survived = tmp_path / "evaluator-descendant-survived.txt"
    child_code = (
        "import pathlib,sys,time; "
        "time.sleep(1.5); "
        "pathlib.Path(sys.argv[1]).write_text('survived', encoding='utf-8')"
    )
    script = _script(
        tmp_path,
        f"""
import pathlib
import subprocess
import sys
import time

sys.stdin.buffer.read()
time.sleep(0.2)
subprocess.Popen([sys.executable, "-c", {child_code!r}, {str(survived)!r}])
pathlib.Path({str(spawned)!r}).write_text("spawned", encoding="utf-8")
time.sleep(30)
""".strip(),
    )
    adapter, _, _, descriptor = _adapter(
        tmp_path,
        script,
        timeout_seconds=1.0,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert exc_info.value.code is ModelErrorCode.TIMEOUT
    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert spawned.exists(), "test did not prove that an evaluator descendant was started"
    await asyncio.sleep(1.0)
    assert not survived.exists(), "evaluator descendant escaped timeout containment"


@pytest.mark.asyncio
async def test_success_does_not_leave_evaluator_descendant_running(tmp_path: Path) -> None:
    spawned = tmp_path / "success-evaluator-descendant-spawned.txt"
    survived = tmp_path / "success-evaluator-descendant-survived.txt"
    child_code = (
        "import pathlib,sys,time; "
        "time.sleep(1.0); "
        "pathlib.Path(sys.argv[1]).write_text('survived', encoding='utf-8')"
    )
    script = _success_script(
        tmp_path,
        extra_prefix=f"""
import subprocess
import time

time.sleep(0.2)
subprocess.Popen([sys.executable, "-c", {child_code!r}, {str(survived)!r}])
Path({str(spawned)!r}).write_text("spawned", encoding="utf-8")
""".strip(),
    )
    adapter, _, _, descriptor = _adapter(tmp_path, script)

    result = await adapter.complete_attested(
        _request(),
        binding=_binding(descriptor),
    )

    assert result.response.text == "answer"
    assert spawned.exists(), "test did not prove that an evaluator descendant was started"
    await asyncio.sleep(1.2)
    assert not survived.exists(), "evaluator descendant escaped successful containment"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX cleanup-failure injection")
async def test_success_fails_closed_when_evaluator_group_cleanup_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _success_script(tmp_path)
    adapter, _, _, descriptor = _adapter(tmp_path, script)
    monkeypatch.setattr(
        "nika_core.training_evaluation_subprocess.terminate_process_group",
        lambda pid: False,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        await adapter.complete_attested(
            _request(),
            binding=_binding(descriptor),
        )

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN


@pytest.mark.asyncio
async def test_task_cancellation_kills_evaluator(tmp_path: Path) -> None:
    marker = tmp_path / "cancelled-process-survived.txt"
    script = _script(
        tmp_path,
        f"""
import sys
import time
from pathlib import Path

sys.stdin.buffer.read()
time.sleep(2)
Path({str(marker)!r}).write_text("late-effect", encoding="utf-8")
""".strip(),
    )
    adapter, _, _, descriptor = _adapter(tmp_path, script, timeout_seconds=5.0)
    task = asyncio.create_task(
        adapter.complete_attested(_request(), binding=_binding(descriptor))
    )

    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.15)

    assert not marker.exists()


def test_command_artifact_kind_is_part_of_evaluator_authority(tmp_path: Path) -> None:
    script = _success_script(tmp_path)

    with pytest.raises(ValueError, match="kind"):
        _adapter(
            tmp_path,
            script,
            script_kind="training_command_file",
        )


def test_attestor_digest_binds_exact_registered_command_files(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_script = _success_script(first_root, text_expression='"first"')
    second_script = _success_script(second_root, text_expression='"second"')
    first, _, _, _ = _adapter(first_root, first_script)
    second, _, _, _ = _adapter(second_root, second_script)

    assert first.attestor_id != ""
    assert second.attestor_id != ""
    assert first.attestor_sha256 != second.attestor_sha256


def test_absolute_evaluator_script_requires_registry_binding(tmp_path: Path) -> None:
    script = _success_script(tmp_path)
    candidate_path, descriptor = _candidate(tmp_path)
    registry, executable_id, _, executable = _registry(tmp_path, script)

    with pytest.raises(ValueError, match="bound through Artifact Registry"):
        RegistrySubprocessLoadedModelAttestor(
            (str(executable), str(script)),
            artifact_registry=registry,
            evaluator_artifact_id=executable_id,
            candidate_path=str(candidate_path.resolve()),
            descriptor=descriptor,
            allowed_root=str(tmp_path.resolve()),
        )


@pytest.mark.asyncio
async def test_explicit_environment_allowlist_exposes_only_admitted_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NIKA_EVAL_BLOCKED", "parent-secret")
    script = _success_script(
        tmp_path,
        text_expression=(
            'os.getenv("NIKA_EVAL_ALLOWED", "missing") + ":" + '
            'str(os.getenv("NIKA_EVAL_BLOCKED") is not None).lower()'
        ),
    )
    adapter, _, _, descriptor = _adapter(
        tmp_path,
        script,
        environment={"NIKA_EVAL_ALLOWED": "yes"},
    )

    result = await adapter.complete_attested(_request(), binding=_binding(descriptor))

    assert result.response.text == "yes:false"



def test_attestor_identity_binds_explicit_environment(tmp_path: Path) -> None:
    script = _success_script(tmp_path)
    left, _, _, _ = _adapter(
        tmp_path,
        script,
        environment={"NIKA_EVAL_MODE": "left"},
    )
    right, _, _, _ = _adapter(
        tmp_path,
        script,
        environment={"NIKA_EVAL_MODE": "right"},
    )

    assert left.attestor_sha256 != right.attestor_sha256


@pytest.mark.parametrize(
    "key",
    [
        "PYTHONPATH",
        "pythonhome",
        "LD_PRELOAD",
        "dyld_insert_libraries",
        "PATH",
        "Node_Options",
        "DOTNET_STARTUP_HOOKS",
    ],
)
def test_environment_rejects_runtime_loader_authority(
    tmp_path: Path,
    key: str,
) -> None:
    script = _success_script(tmp_path)

    with pytest.raises(ValueError, match="runtime or loader authority"):
        _adapter(tmp_path, script, environment={key: "untrusted"})


@pytest.mark.parametrize(
    "key",
    ["NIKA_API_KEY", "ACCESS_TOKEN", "DB_PASSWORD", "SERVICE_CREDENTIAL"],
)
def test_environment_rejects_credential_named_fields(
    tmp_path: Path,
    key: str,
) -> None:
    script = _success_script(tmp_path)

    with pytest.raises(ValueError, match="credential material"):
        _adapter(tmp_path, script, environment={key: "sensitive"})


@pytest.mark.asyncio
async def test_direct_non_request_carrier_is_typed_no_effect_failure(
    tmp_path: Path,
) -> None:
    script = _success_script(tmp_path)
    adapter, _, _, descriptor = _adapter(tmp_path, script)

    with pytest.raises(ModelGatewayError) as exc_info:
        await adapter.complete_attested(  # type: ignore[arg-type]
            object(),
            binding=_binding(descriptor),
        )

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT



@pytest.mark.asyncio
async def test_unknown_request_metadata_is_rejected_before_subprocess_effect(
    tmp_path: Path,
) -> None:
    script = _success_script(tmp_path)
    adapter, _, _, descriptor = _adapter(tmp_path, script)
    request = _request()
    request = ModelRequest(
        request_id=request.request_id,
        messages=request.messages,
        model=request.model,
        provider_id=request.provider_id,
        provider_kind=request.provider_kind,
        fallback_provider_ids=request.fallback_provider_ids,
        privacy=request.privacy,
        timeout_seconds=request.timeout_seconds,
        temperature=request.temperature,
        metadata={
            **dict(request.metadata),
            "unexpected_private_context": "must-not-cross-process-boundary",
        },
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        await adapter.complete_attested(request, binding=_binding(descriptor))

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT



@pytest.mark.parametrize(
    "key",
    ["NIKA_AUTHOR_MODE", "TOKENIZER_MODE", "AUTHORITY_MODE"],
)
def test_environment_allows_noncredential_substring_names(
    tmp_path: Path,
    key: str,
) -> None:
    script = _success_script(tmp_path)

    adapter, _, _, _ = _adapter(tmp_path, script, environment={key: "enabled"})

    assert len(adapter.attestor_sha256) == 64


def test_environment_rejects_case_insensitive_duplicate_keys(tmp_path: Path) -> None:
    script = _success_script(tmp_path)

    with pytest.raises(ValueError, match="unique ignoring case"):
        _adapter(
            tmp_path,
            script,
            environment={"NIKA_MODE": "one", "nika_mode": "two"},
        )


@pytest.mark.parametrize(
    ("field", "environment"),
    [
        ("key", {"NIKA_\ud800": "value"}),
        ("value", {"NIKA_MODE": "\ud800"}),
        ("key-control", {"NIKA\nMODE": "value"}),
        ("value-control", {"NIKA_MODE": "line\nbreak"}),
    ],
)
def test_environment_rejects_noncanonical_text(
    tmp_path: Path,
    field: str,
    environment: dict[str, str],
) -> None:
    del field
    script = _success_script(tmp_path)

    with pytest.raises(ValueError):
        _adapter(tmp_path, script, environment=environment)


def test_command_rejects_noncanonical_text_before_registry_access(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        RegistrySubprocessLoadedModelAttestor(
            (str(tmp_path / "evaluator"), "\ud800"),
            artifact_registry=object(),  # type: ignore[arg-type]
            evaluator_artifact_id="0" * 64,
            candidate_path=str(tmp_path / "candidate.bin"),
            descriptor=object(),  # type: ignore[arg-type]
        )
