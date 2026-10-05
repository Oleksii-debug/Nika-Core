from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    PrivacyClass,
)
from nika_core.training_ollama_manifest import (
    ManifestPinnedOllamaProvider,
    OllamaManifestAuthority,
    OllamaManifestAuthorityError,
    OllamaPreparedModelBinding,
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


ARTIFACT_SHA = _sha(b"candidate-gguf")
DESCRIPTOR_SHA = _sha(b"candidate-descriptor")
MANIFEST_SHA = _sha(b"ollama-manifest")


def _request(model: str = "candidate:latest") -> ModelRequest:
    return ModelRequest(
        request_id="manifest-pinned-test",
        messages=(ModelMessage(role="user", content="hello"),),
        model=model,
        provider_id="ollama",
        privacy=PrivacyClass.PRIVATE,
        temperature=0,
    )


def _client_factory(handler):
    transport = httpx.MockTransport(handler)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    return factory


def _inventory(model: str, digest: str) -> dict[str, object]:
    return {
        "models": [
            {
                "name": model,
                "model": model,
                "digest": digest,
                "size": 123,
                "details": {"format": "gguf"},
            }
        ]
    }


def _binding(
    *,
    route_model_id: str = "candidate:latest",
    manifest_model_id: str = "candidate:latest",
    manifest_sha256: str = MANIFEST_SHA,
    endpoint_sha256: str | None = None,
) -> OllamaPreparedModelBinding:
    endpoint = endpoint_sha256 or _sha(b"http://localhost:11434")
    create_request = {
        "model": route_model_id,
        "files": {"model.gguf": "sha256:" + ARTIFACT_SHA},
        "stream": False,
    }
    create_request_sha = _sha(
        json.dumps(
            create_request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    payload = {
        "schema": "nika.training.ollama-prepared-model.v1",
        "provider_id": "ollama",
        "route_model_id": route_model_id,
        "manifest_model_id": manifest_model_id,
        "artifact_sha256": ARTIFACT_SHA,
        "descriptor_digest": DESCRIPTOR_SHA,
        "provider_manifest_sha256": manifest_sha256,
        "endpoint_sha256": endpoint,
        "create_request_sha256": create_request_sha,
    }
    preparation_sha = _sha(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return OllamaPreparedModelBinding(
        route_model_id=route_model_id,
        manifest_model_id=manifest_model_id,
        artifact_sha256=ARTIFACT_SHA,
        descriptor_digest=DESCRIPTOR_SHA,
        provider_manifest_sha256=manifest_sha256,
        endpoint_sha256=endpoint,
        create_request_sha256=create_request_sha,
        preparation_sha256=preparation_sha,
    )


def test_binding_revalidates_digest_only_provider_identity() -> None:
    binding = _binding()

    assert binding.revalidated() == binding
    assert binding.artifact_sha256 != binding.provider_manifest_sha256
    assert binding.to_payload()["provider_id"] == "ollama"
    assert "path" not in binding.to_payload()


def test_binding_rejects_mutated_preparation_digest() -> None:
    binding = _binding()

    with pytest.raises(ValueError, match="preparation_sha256"):
        replace(binding, preparation_sha256="0" * 64)


@pytest.mark.asyncio
async def test_prepare_existing_blob_binds_exact_blob_create_and_manifest() -> None:
    seen: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: object = None
        if request.content:
            body = json.loads(request.content)
        seen.append((request.method, request.url.path, body))
        if request.method == "HEAD":
            assert request.url.path == "/api/blobs/sha256:" + ARTIFACT_SHA
            return httpx.Response(200)
        if request.url.path == "/api/create":
            return httpx.Response(200, json={"status": "success"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=_inventory("candidate:latest", MANIFEST_SHA))
        raise AssertionError(request.url)

    authority = OllamaManifestAuthority(client_factory=_client_factory(handler))
    binding = await authority.prepare_existing_gguf_blob(
        model_id="candidate:latest",
        artifact_sha256=ARTIFACT_SHA,
        descriptor_digest=DESCRIPTOR_SHA,
    )

    assert binding.artifact_sha256 == ARTIFACT_SHA
    assert binding.provider_manifest_sha256 == MANIFEST_SHA
    assert binding.artifact_sha256 != binding.provider_manifest_sha256
    assert seen[1] == (
        "POST",
        "/api/create",
        {
            "model": "candidate:latest",
            "files": {"model.gguf": "sha256:" + ARTIFACT_SHA},
            "stream": False,
        },
    )
    assert all(path != "/api/pull" for _, path, _ in seen)


@pytest.mark.asyncio
async def test_prepare_supports_ollama_latest_alias_canonicalization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200)
        if request.url.path == "/api/create":
            return httpx.Response(200, json={"status": "success"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=_inventory("candidate:latest", MANIFEST_SHA))
        raise AssertionError(request.url)

    authority = OllamaManifestAuthority(client_factory=_client_factory(handler))
    binding = await authority.prepare_existing_gguf_blob(
        model_id="candidate",
        artifact_sha256=ARTIFACT_SHA,
        descriptor_digest=DESCRIPTOR_SHA,
    )

    assert binding.route_model_id == "candidate"
    assert binding.manifest_model_id == "candidate:latest"


@pytest.mark.asyncio
async def test_missing_blob_fails_before_create_effect() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(404)

    authority = OllamaManifestAuthority(client_factory=_client_factory(handler))

    with pytest.raises(OllamaManifestAuthorityError, match="not prepared"):
        await authority.prepare_existing_gguf_blob(
            model_id="candidate:latest",
            artifact_sha256=ARTIFACT_SHA,
            descriptor_digest=DESCRIPTOR_SHA,
        )

    assert paths == ["/api/blobs/sha256:" + ARTIFACT_SHA]


@pytest.mark.asyncio
async def test_control_response_byte_limit_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200)
        if request.url.path == "/api/create":
            return httpx.Response(200, content=b'{"status":"success","padding":"' + b"x" * 400 + b'"}')
        raise AssertionError(request.url)

    authority = OllamaManifestAuthority(
        client_factory=_client_factory(handler),
        max_control_response_bytes=256,
    )

    with pytest.raises(OllamaManifestAuthorityError, match="byte limit"):
        await authority.prepare_existing_gguf_blob(
            model_id="candidate:latest",
            artifact_sha256=ARTIFACT_SHA,
            descriptor_digest=DESCRIPTOR_SHA,
        )


@pytest.mark.asyncio
async def test_manifest_pin_blocks_inference_before_effect_when_available_digest_changed() -> None:
    chat_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json=_inventory("candidate:latest", _sha(b"replaced-manifest")),
            )
        if request.url.path == "/api/chat":
            chat_calls += 1
            return httpx.Response(
                200,
                json={
                    "model": "candidate:latest",
                    "message": {"role": "assistant", "content": "answer"},
                    "done": True,
                },
            )
        raise AssertionError(request.url)

    factory = _client_factory(handler)
    authority = OllamaManifestAuthority(client_factory=factory)
    provider = ManifestPinnedOllamaProvider(
        binding=_binding(),
        authority=authority,
        client_factory=factory,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        await provider.complete(_request())

    assert exc_info.value.code is ModelErrorCode.UNAVAILABLE
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert chat_calls == 0


@pytest.mark.asyncio
async def test_loaded_manifest_mismatch_after_chat_is_unknown_effect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=_inventory("candidate:latest", MANIFEST_SHA))
        if request.url.path == "/api/chat":
            return httpx.Response(
                200,
                json={
                    "model": "candidate:latest",
                    "message": {"role": "assistant", "content": "answer"},
                    "done": True,
                },
            )
        if request.url.path == "/api/ps":
            return httpx.Response(
                200,
                json=_inventory("candidate:latest", _sha(b"wrong-loaded-manifest")),
            )
        raise AssertionError(request.url)

    factory = _client_factory(handler)
    authority = OllamaManifestAuthority(client_factory=factory)
    provider = ManifestPinnedOllamaProvider(
        binding=_binding(),
        authority=authority,
        client_factory=factory,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        await provider.complete(_request())

    assert exc_info.value.code is ModelErrorCode.PROVIDER_ERROR
    assert exc_info.value.failure_effect is ModelFailureEffect.UNKNOWN


@pytest.mark.asyncio
async def test_manifest_pinned_provider_requires_same_digest_available_and_loaded() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path in {"/api/tags", "/api/ps"}:
            return httpx.Response(200, json=_inventory("candidate:latest", MANIFEST_SHA))
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            assert body["model"] == "candidate:latest"
            assert body["stream"] is False
            return httpx.Response(
                200,
                json={
                    "model": "candidate:latest",
                    "message": {"role": "assistant", "content": "answer"},
                    "done": True,
                    "prompt_eval_count": 2,
                    "eval_count": 1,
                },
            )
        raise AssertionError(request.url)

    factory = _client_factory(handler)
    authority = OllamaManifestAuthority(client_factory=factory)
    provider = ManifestPinnedOllamaProvider(
        binding=_binding(),
        authority=authority,
        client_factory=factory,
    )

    response = await provider.complete(_request())

    assert response.text == "answer"
    assert paths == ["/api/tags", "/api/chat", "/api/ps"]


@pytest.mark.asyncio
async def test_manifest_pinned_provider_rejects_model_substitution_without_effect() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    factory = _client_factory(handler)
    authority = OllamaManifestAuthority(client_factory=factory)
    provider = ManifestPinnedOllamaProvider(
        binding=_binding(),
        authority=authority,
        client_factory=factory,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        await provider.complete(_request(model="other:latest"))

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST
    assert exc_info.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert calls == 0


def test_manifest_authority_rejects_non_loopback_endpoint() -> None:
    with pytest.raises(ValueError, match="loopback"):
        OllamaManifestAuthority(base_url="https://example.test:11434")
