from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.training_ollama_manifest import (
    OllamaManifestAuthority,
    OllamaManifestAuthorityError,
    OllamaPromotionManifestStore,
    OllamaPromotionManifestStoreError,
    OllamaPreparedModelBinding,
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


ARTIFACT_SHA = _sha(b"candidate-gguf")
DESCRIPTOR_SHA = _sha(b"candidate-descriptor")
MANIFEST_SHA = _sha(b"ollama-manifest")


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


def test_manifest_authority_rejects_non_loopback_endpoint() -> None:
    with pytest.raises(ValueError, match="loopback"):
        OllamaManifestAuthority(base_url="https://example.test:11434")



def test_promotion_manifest_store_survives_restart_and_resolves_exact_task_pin(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "profile" / "nika.db")
    store.initialize()
    manifests = OllamaPromotionManifestStore(store)
    decision = _sha(b"promotion-decision")
    training_binding = _sha(b"training-binding")
    base = _binding(
        route_model_id="base:latest",
        manifest_model_id="base:latest",
        manifest_sha256=_sha(b"base-manifest"),
    )
    challenger = _binding()

    manifests.save_pair(
        decision_sha256=decision,
        binding_sha256=training_binding,
        base=base,
        challenger=challenger,
    )

    reopened = OllamaPromotionManifestStore(SQLiteStore(store.path))
    resolved_challenger = reopened.resolve(
        decision_sha256=decision,
        binding_sha256=training_binding,
        role="challenger",
        artifact_sha256=challenger.artifact_sha256,
        descriptor_digest=challenger.descriptor_digest,
        route_model_id=challenger.route_model_id,
        base_url="http://localhost:11434",
    )
    resolved_rollback = reopened.resolve(
        decision_sha256=decision,
        binding_sha256=training_binding,
        role="rollback",
        artifact_sha256=base.artifact_sha256,
        descriptor_digest=base.descriptor_digest,
        route_model_id=base.route_model_id,
        base_url="http://localhost:11434",
    )

    assert resolved_challenger == challenger
    assert resolved_rollback == base


def test_promotion_manifest_store_replay_is_idempotent_but_substitution_is_rejected(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    manifests = OllamaPromotionManifestStore(store)
    decision = _sha(b"promotion-decision")
    training_binding = _sha(b"training-binding")
    base = _binding(
        route_model_id="base:latest",
        manifest_model_id="base:latest",
        manifest_sha256=_sha(b"base-manifest"),
    )
    challenger = _binding()

    manifests.save_pair(
        decision_sha256=decision,
        binding_sha256=training_binding,
        base=base,
        challenger=challenger,
    )
    manifests.save_pair(
        decision_sha256=decision,
        binding_sha256=training_binding,
        base=base,
        challenger=challenger,
    )

    substituted = _binding(manifest_sha256=_sha(b"substituted-provider-manifest"))
    with pytest.raises(OllamaPromotionManifestStoreError, match="immutable"):
        manifests.save_pair(
            decision_sha256=decision,
            binding_sha256=training_binding,
            base=base,
            challenger=substituted,
        )

    assert manifests.resolve(
        decision_sha256=decision,
        binding_sha256=training_binding,
        role="challenger",
        artifact_sha256=challenger.artifact_sha256,
        descriptor_digest=challenger.descriptor_digest,
        route_model_id=challenger.route_model_id,
        base_url="http://localhost:11434",
    ) == challenger


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("artifact_sha256", _sha(b"other-artifact")),
        ("descriptor_digest", _sha(b"other-descriptor")),
        ("route_model_id", "other:latest"),
        ("base_url", "http://localhost:11435"),
    ),
)
def test_promotion_manifest_store_resolve_rejects_task_identity_drift(
    tmp_path,
    field: str,
    value: str,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    manifests = OllamaPromotionManifestStore(store)
    decision = _sha(b"promotion-decision")
    training_binding = _sha(b"training-binding")
    base = _binding(
        route_model_id="base:latest",
        manifest_model_id="base:latest",
        manifest_sha256=_sha(b"base-manifest"),
    )
    challenger = _binding()
    manifests.save_pair(
        decision_sha256=decision,
        binding_sha256=training_binding,
        base=base,
        challenger=challenger,
    )
    values = {
        "decision_sha256": decision,
        "binding_sha256": training_binding,
        "role": "challenger",
        "artifact_sha256": challenger.artifact_sha256,
        "descriptor_digest": challenger.descriptor_digest,
        "route_model_id": challenger.route_model_id,
        "base_url": "http://localhost:11434",
    }
    values[field] = value

    with pytest.raises(
        OllamaPromotionManifestStoreError,
        match="does not match the task pin",
    ):
        manifests.resolve(**values)


def test_promotion_manifest_store_rejects_corrupted_durable_json(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    manifests = OllamaPromotionManifestStore(store)
    decision = _sha(b"promotion-decision")
    training_binding = _sha(b"training-binding")
    base = _binding(
        route_model_id="base:latest",
        manifest_model_id="base:latest",
        manifest_sha256=_sha(b"base-manifest"),
    )
    challenger = _binding()
    manifests.save_pair(
        decision_sha256=decision,
        binding_sha256=training_binding,
        base=base,
        challenger=challenger,
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE training_ollama_promotion_manifests "
            "SET prepared_model_json = ? "
            "WHERE decision_sha256 = ? AND role = 'challenger'",
            ('{"schema":"broken"}', decision),
        )

    with pytest.raises(OllamaPromotionManifestStoreError, match="invalid"):
        manifests.resolve(
            decision_sha256=decision,
            binding_sha256=training_binding,
            role="challenger",
            artifact_sha256=challenger.artifact_sha256,
            descriptor_digest=challenger.descriptor_digest,
            route_model_id=challenger.route_model_id,
            base_url="http://localhost:11434",
        )
