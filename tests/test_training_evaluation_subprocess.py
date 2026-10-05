from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

import pytest

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
        challenger_candidate_id="models/candidate/job-1",
        challenger_provider_id=descriptor.provider_id,
        challenger_model_id=descriptor.model_id,
        base_sha256=_sha(b"base-model"),
        challenger_sha256=descriptor.sha256,
        candidate_artifact_ref="models/candidate/job-1",
        frozen_package_sha256=_sha(b"package"),
        evaluation_set_sha256=_sha(b"held-out"),
        descriptor_digest=descriptor.descriptor_digest,
        descriptor_registry_key=descriptor.registry_key,
        challenger_size_bytes=descriptor.size_bytes,
    )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="benchmark-request-1",
        messages=(ModelMessage(role="user", content="question"),),
        model="candidate-model",
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
        privacy=PrivacyClass.PRIVATE,
        metadata={"model_candidate_id": "models/candidate/job-1"},
    )


def _success_script(
    tmp_path: Path,
    *,
    text_expression: str = '"answer"',
    digest_expression: str = "hashlib.sha256(candidate).hexdigest()",
    size_expression: str = "len(candidate)",
    descriptor_expression: str = 'request["binding"]["descriptor_digest"]',
    total_tokens_expression: str = "5",
    extra_prefix: str = "",
    name: str = "evaluator.py",
) -> Path:
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
    "usage": {{
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


def test_parent_environment_allowlist_is_explicit_only(tmp_path: Path) -> None:
    script = _success_script(tmp_path)
    candidate_path, descriptor = _candidate(tmp_path)
    registry, executable_id, script_id, executable = _registry(tmp_path, script)

    adapter = RegistrySubprocessLoadedModelAttestor(
        (str(executable), str(script)),
        artifact_registry=registry,
        evaluator_artifact_id=executable_id,
        command_artifact_ids={1: script_id},
        candidate_path=str(candidate_path.resolve()),
        descriptor=descriptor,
        allowed_root=str(tmp_path.resolve()),
        environment={"NIKA_EVAL_ALLOWED": "1"},
    )

    assert len(adapter.attestor_sha256) == 64
    assert os.environ.get("NIKA_EVAL_ALLOWED") is None
