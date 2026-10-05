from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactRegistryError,
)
from nika_core.model_gateway.contracts import (
    ModelDownloadAuthorization,
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
)
from nika_core.model_gateway.providers import OllamaProvider
from nika_core.training_artifacts import (
    CandidateArtifactIntegrityError,
    verify_candidate_artifact,
)
from nika_core.training_evaluation_attestation import (
    AttestedModelCompletionResult,
    LoadedModelArtifactAttestation,
)
from nika_core.training_evaluation_binding import TrainingEvaluationBinding

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UPLOAD_CHUNK_BYTES = 1024 * 1024
_MAX_IDENTITY_BYTES = 512


class OllamaTrainingCandidatePreparationError(RuntimeError):
    """Safe failure while explicitly importing one verified Loop-C candidate."""


def _canonical_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty canonical text")
    if any(not character.isprintable() for character in value):
        raise ValueError(f"{name} must not contain control characters")
    encoded = value.encode("utf-8")
    if len(encoded) > _MAX_IDENTITY_BYTES:
        raise ValueError(f"{name} exceeds the configured byte limit")
    return value


def _sha256(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be an exact lowercase SHA-256 digest")
    return value


def _validated_base_url(value: str) -> str:
    _canonical_text(value, name="base_url")
    if "\\" in value:
        raise ValueError("Ollama base_url contains unsafe characters")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("Ollama base_url is invalid") from None
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise ValueError("Ollama base_url requires an HTTP(S) loopback host")
    if hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Ollama local route must use a loopback host")
    if "%" in parsed.netloc or port == 0 or parsed.netloc.endswith(":"):
        raise ValueError("Ollama base_url has an invalid authority")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Ollama base_url must not contain userinfo")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Ollama base_url must not contain path, query, or fragment")
    return value.rstrip("/")


def _model_name_matches(expected: str, observed: object) -> bool:
    if type(observed) is not str:
        return False
    if observed == expected:
        return True
    return ":" not in expected and observed == f"{expected}:latest"


def _canonical_descriptor(
    descriptor: ModelArtifactDescriptor,
) -> ModelArtifactDescriptor:
    if type(descriptor) is not ModelArtifactDescriptor:
        raise TypeError("descriptor must be an exact ModelArtifactDescriptor")
    try:
        return ModelArtifactDescriptor.from_json(descriptor.canonical_json())
    except (AttributeError, ModelArtifactRegistryError, TypeError, ValueError) as exc:
        raise OllamaTrainingCandidatePreparationError(
            "candidate descriptor is not canonical"
        ) from exc


def _canonical_binding(binding: TrainingEvaluationBinding) -> TrainingEvaluationBinding:
    if type(binding) is not TrainingEvaluationBinding:
        raise TypeError("binding must be an exact TrainingEvaluationBinding")
    try:
        return binding.revalidated()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("binding must be canonical") from exc


@dataclass(frozen=True, slots=True)
class OllamaPreparedTrainingCandidate:
    """Secret-free receipt binding exact candidate bytes to one Ollama manifest."""

    binding_sha256: str
    candidate_sha256: str
    descriptor_digest: str
    model_id: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.binding_sha256, name="binding_sha256")
        _sha256(self.candidate_sha256, name="candidate_sha256")
        _sha256(self.descriptor_digest, name="descriptor_digest")
        _canonical_text(self.model_id, name="model_id")
        _sha256(self.manifest_sha256, name="manifest_sha256")

    def revalidated(self) -> OllamaPreparedTrainingCandidate:
        if type(self) is not OllamaPreparedTrainingCandidate:
            raise TypeError(
                "prepared candidate must be an exact OllamaPreparedTrainingCandidate"
            )
        try:
            return OllamaPreparedTrainingCandidate(
                binding_sha256=self.binding_sha256,
                candidate_sha256=self.candidate_sha256,
                descriptor_digest=self.descriptor_digest,
                model_id=self.model_id,
                manifest_sha256=self.manifest_sha256,
            )
        except AttributeError as exc:
            raise ValueError("prepared candidate fields are incomplete") from exc

    @property
    def evidence_sha256(self) -> str:
        receipt = self.revalidated()
        payload = {
            "schema": "nika-ollama-prepared-training-candidate-v1",
            "binding_sha256": receipt.binding_sha256,
            "candidate_sha256": receipt.candidate_sha256,
            "descriptor_digest": receipt.descriptor_digest,
            "model_id": receipt.model_id,
            "manifest_sha256": receipt.manifest_sha256,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class _CandidateUploadStream(httpx.AsyncByteStream):
    def __init__(self, path: Path) -> None:
        self._path = path

    async def __aiter__(self):
        handle = await asyncio.to_thread(self._path.open, "rb")
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, _UPLOAD_CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)


def _require_authorized_import(
    authorization: ModelDownloadAuthorization,
    *,
    binding: TrainingEvaluationBinding,
    descriptor: ModelArtifactDescriptor,
) -> None:
    if type(authorization) is not ModelDownloadAuthorization:
        raise TypeError("authorization must be an exact ModelDownloadAuthorization")
    if authorization.provider_id != "ollama":
        raise OllamaTrainingCandidatePreparationError(
            "candidate import authorization must target Ollama"
        )
    if authorization.model != binding.challenger_model_id:
        raise OllamaTrainingCandidatePreparationError(
            "candidate import authorization model does not match evaluation binding"
        )
    if authorization.license_reference != descriptor.license_reference:
        raise OllamaTrainingCandidatePreparationError(
            "candidate import authorization license does not match provenance"
        )
    if (
        authorization.expected_model_id is not None
        and authorization.expected_model_id != binding.challenger_model_id
    ):
        raise OllamaTrainingCandidatePreparationError(
            "candidate import authorization expected model identity is inconsistent"
        )


def _require_descriptor_binding(
    descriptor: ModelArtifactDescriptor,
    *,
    binding: TrainingEvaluationBinding,
) -> None:
    if (
        descriptor.provider_id != binding.challenger_provider_id
        or descriptor.provider_id != "ollama"
        or descriptor.model_id != binding.challenger_model_id
        or descriptor.sha256 != binding.challenger_sha256
        or descriptor.descriptor_digest != binding.descriptor_digest
        or descriptor.size_bytes != binding.challenger_size_bytes
    ):
        raise OllamaTrainingCandidatePreparationError(
            "candidate descriptor does not match training evaluation binding"
        )


def _json_object(response: httpx.Response, *, label: str) -> dict[str, Any]:
    media_type = response.headers.get("content-type", "").partition(";")[0]
    if media_type.strip().casefold() != "application/json":
        raise ValueError(f"{label} must return application/json")
    payload = response.json()
    if type(payload) is not dict:
        raise TypeError(f"{label} must return a JSON object")
    return payload


def _manifest_digest_from_models(payload: dict[str, Any], *, model_id: str) -> str:
    raw_models = payload.get("models")
    if type(raw_models) is not list:
        raise ValueError("Ollama models response is invalid")
    matches: list[dict[str, Any]] = []
    for item in raw_models:
        if type(item) is not dict:
            raise ValueError("Ollama model entry is invalid")
        if _model_name_matches(model_id, item.get("name")) or _model_name_matches(
            model_id, item.get("model")
        ):
            matches.append(item)
    if len(matches) != 1:
        raise ValueError("Ollama model identity is missing or ambiguous")
    digest = matches[0].get("digest")
    _sha256(digest, name="Ollama model manifest digest")
    return digest


def _prepared_manifest_from_tags(payload: dict[str, Any], *, model_id: str) -> str:
    raw_models = payload.get("models")
    if type(raw_models) is not list:
        raise ValueError("Ollama model list is invalid")
    matches: list[dict[str, Any]] = []
    for item in raw_models:
        if type(item) is not dict:
            raise ValueError("Ollama model entry is invalid")
        if _model_name_matches(model_id, item.get("name")) or _model_name_matches(
            model_id, item.get("model")
        ):
            matches.append(item)
    if len(matches) != 1:
        raise ValueError("prepared Ollama model identity is missing or ambiguous")
    details = matches[0].get("details")
    if type(details) is not dict or details.get("format") != "gguf":
        raise ValueError("prepared Ollama model is not a GGUF model")
    digest = matches[0].get("digest")
    _sha256(digest, name="prepared Ollama manifest digest")
    return digest


async def prepare_ollama_training_candidate(
    *,
    authorization: ModelDownloadAuthorization,
    binding: TrainingEvaluationBinding,
    descriptor: ModelArtifactDescriptor,
    candidate_path: str | Path,
    allowed_root: str | Path | None = None,
    base_url: str = "http://localhost:11434",
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
) -> OllamaPreparedTrainingCandidate:
    """Explicitly import exact verified candidate bytes into Ollama as one GGUF model.

    Ordinary inference never calls this function. The caller must provide the existing
    explicit model-acquisition authorization and the candidate is physically reverified
    before the import effect. Ollama's content-addressed blob endpoint independently
    checks the candidate SHA-256, and a successful create is bound to the resulting
    model-manifest digest.
    """

    canonical_binding = _canonical_binding(binding)
    canonical_descriptor = _canonical_descriptor(descriptor)
    _require_authorized_import(
        authorization,
        binding=canonical_binding,
        descriptor=canonical_descriptor,
    )
    _require_descriptor_binding(canonical_descriptor, binding=canonical_binding)
    endpoint = _validated_base_url(base_url)
    path = Path(candidate_path)
    try:
        verification = await asyncio.to_thread(
            verify_candidate_artifact,
            path,
            canonical_descriptor,
            allowed_root=allowed_root,
        )
    except (CandidateArtifactIntegrityError, OSError, TypeError, ValueError) as exc:
        raise OllamaTrainingCandidatePreparationError(
            "candidate physical artifact verification failed"
        ) from exc
    if (
        verification.sha256 != canonical_binding.challenger_sha256
        or verification.descriptor_digest != canonical_binding.descriptor_digest
    ):
        raise OllamaTrainingCandidatePreparationError(
            "candidate physical verification evidence is inconsistent"
        )

    blob_digest = f"sha256:{canonical_binding.challenger_sha256}"
    try:
        async with client_factory(
            timeout=60.0,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            head = await client.head(f"{endpoint}/api/blobs/{blob_digest}")
            if head.status_code == 404:
                upload = await client.post(
                    f"{endpoint}/api/blobs/{blob_digest}",
                    content=_CandidateUploadStream(path),
                )
                if upload.status_code not in {200, 201}:
                    upload.raise_for_status()
            elif head.status_code != 200:
                head.raise_for_status()

            create = await client.post(
                f"{endpoint}/api/create",
                json={
                    "model": canonical_binding.challenger_model_id,
                    "files": {"candidate.gguf": blob_digest},
                    "stream": False,
                },
            )
            create.raise_for_status()
            create_payload = _json_object(create, label="Ollama create")
            if create_payload.get("status") != "success":
                raise ValueError("Ollama create did not report success")

            tags = await client.get(f"{endpoint}/api/tags")
            tags.raise_for_status()
            manifest_sha256 = _prepared_manifest_from_tags(
                _json_object(tags, label="Ollama tags"),
                model_id=canonical_binding.challenger_model_id,
            )
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        raise OllamaTrainingCandidatePreparationError(
            "Ollama candidate import could not be proven"
        ) from exc

    try:
        await asyncio.to_thread(
            verify_candidate_artifact,
            path,
            canonical_descriptor,
            allowed_root=allowed_root,
        )
    except (CandidateArtifactIntegrityError, OSError, TypeError, ValueError) as exc:
        raise OllamaTrainingCandidatePreparationError(
            "candidate artifact changed during Ollama import"
        ) from exc

    return OllamaPreparedTrainingCandidate(
        binding_sha256=canonical_binding.binding_sha256,
        candidate_sha256=canonical_binding.challenger_sha256,
        descriptor_digest=canonical_binding.descriptor_digest,
        model_id=canonical_binding.challenger_model_id,
        manifest_sha256=manifest_sha256,
    )


class OllamaLoadedModelAttestor:
    """Concrete #1298 same-effect adapter for one explicitly prepared candidate.

    The adapter proves the logical alias still points to the prepared manifest before
    chat, executes the existing native Ollama provider, then requires /api/ps to
    report that exact manifest as loaded and rechecks the alias after the effect.
    """

    def __init__(
        self,
        *,
        prepared: OllamaPreparedTrainingCandidate,
        attestor_id: str,
        attestor_sha256: str,
        base_url: str = "http://localhost:11434",
        think: bool | str = False,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self._prepared = prepared.revalidated()
        self._attestor_id = _canonical_text(attestor_id, name="attestor_id")
        self._attestor_sha256 = _sha256(attestor_sha256, name="attestor_sha256")
        self._base_url = _validated_base_url(base_url)
        self._client_factory = client_factory
        self._provider = OllamaProvider(
            default_model=self._prepared.model_id,
            base_url=self._base_url,
            think=think,
            client_factory=client_factory,
        )
        self._effect_lock = asyncio.Lock()

    async def _manifest_before_effect(self) -> str:
        try:
            async with self._client_factory(
                timeout=30.0,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                response.raise_for_status()
                return _prepared_manifest_from_tags(
                    _json_object(response, label="Ollama tags"),
                    model_id=self._prepared.model_id,
                )
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                "prepared Ollama model identity could not be verified before inference",
                provider_id="ollama",
                retryable=False,
                failure_effect=ModelFailureEffect.NO_EFFECT,
            ) from exc

    async def _manifest_after_effect(self) -> tuple[str, str]:
        try:
            async with self._client_factory(
                timeout=30.0,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                running = await client.get(f"{self._base_url}/api/ps")
                running.raise_for_status()
                loaded_digest = _manifest_digest_from_models(
                    _json_object(running, label="Ollama ps"),
                    model_id=self._prepared.model_id,
                )
                tags = await client.get(f"{self._base_url}/api/tags")
                tags.raise_for_status()
                alias_digest = _prepared_manifest_from_tags(
                    _json_object(tags, label="Ollama tags"),
                    model_id=self._prepared.model_id,
                )
                return loaded_digest, alias_digest
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "Ollama loaded-model evidence could not be verified after inference",
                provider_id="ollama",
                retryable=False,
                failure_effect=ModelFailureEffect.UNKNOWN,
            ) from exc

    async def complete_attested(
        self,
        request: ModelRequest,
        *,
        binding: TrainingEvaluationBinding,
    ) -> AttestedModelCompletionResult:
        canonical_binding = _canonical_binding(binding)
        if (
            canonical_binding.binding_sha256 != self._prepared.binding_sha256
            or canonical_binding.challenger_provider_id != "ollama"
            or canonical_binding.challenger_model_id != self._prepared.model_id
            or canonical_binding.challenger_sha256 != self._prepared.candidate_sha256
            or canonical_binding.descriptor_digest != self._prepared.descriptor_digest
        ):
            raise ModelGatewayError(
                ModelErrorCode.INVALID_REQUEST,
                "prepared Ollama candidate does not match evaluation binding",
                provider_id="ollama",
                retryable=False,
                failure_effect=ModelFailureEffect.NO_EFFECT,
            )

        async with self._effect_lock:
            before = await self._manifest_before_effect()
            if before != self._prepared.manifest_sha256:
                raise ModelGatewayError(
                    ModelErrorCode.INVALID_REQUEST,
                    "prepared Ollama model alias changed before inference",
                    provider_id="ollama",
                    retryable=False,
                    failure_effect=ModelFailureEffect.NO_EFFECT,
                )

            response: ModelResponse = await self._provider.complete(request)
            loaded_digest, alias_digest = await self._manifest_after_effect()
            if (
                loaded_digest != self._prepared.manifest_sha256
                or alias_digest != self._prepared.manifest_sha256
            ):
                raise ModelGatewayError(
                    ModelErrorCode.PROVIDER_ERROR,
                    "Ollama inference is not bound to the prepared candidate manifest",
                    provider_id="ollama",
                    retryable=False,
                    failure_effect=ModelFailureEffect.UNKNOWN,
                )

            return AttestedModelCompletionResult(
                response=response,
                attestation=LoadedModelArtifactAttestation(
                    request_id=request.request_id,
                    binding_sha256=canonical_binding.binding_sha256,
                    provider_id="ollama",
                    model_id=canonical_binding.challenger_model_id,
                    artifact_sha256=canonical_binding.challenger_sha256,
                    descriptor_digest=canonical_binding.descriptor_digest,
                    attestor_id=self._attestor_id,
                    attestor_sha256=self._attestor_sha256,
                ),
            )
