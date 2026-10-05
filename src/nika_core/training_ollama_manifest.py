from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
)
from nika_core.model_gateway.providers import OllamaProvider

_PROVIDER_ID = "ollama"
_BINDING_SCHEMA = "nika.training.ollama-prepared-model.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_CONTROL_RESPONSE_BYTES = 1024 * 1024
_MAX_MODELS = 4096
_DEFAULT_TIMEOUT_SECONDS = 30.0


class OllamaManifestAuthorityError(RuntimeError):
    """Safe failure while binding or revalidating Ollama provider manifest identity."""


def _require_sha256(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be an exact lowercase SHA-256 digest")
    return value


def _require_model_id(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("model_id must be canonical non-empty text")
    if any(not char.isprintable() for char in value):
        raise ValueError("model_id must not contain control characters")
    return value


def _canonical_base_url(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("base_url must be canonical non-empty text")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base_url must contain a valid explicit port") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an HTTP(S) endpoint")
    host = parsed.hostname.lower()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Ollama manifest authority requires a loopback endpoint")
    if port is None or port == 0:
        raise ValueError("base_url must contain an explicit non-zero port")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain userinfo")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("base_url must not contain path, query, or fragment")
    host_text = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme.lower()}://{host_text}:{port}"


def _canonical_json_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _model_aliases(model_id: str) -> frozenset[str]:
    final_segment = model_id.rsplit("/", 1)[-1]
    if ":" in final_segment:
        return frozenset((model_id,))
    return frozenset((model_id, f"{model_id}:latest"))


@dataclass(frozen=True, slots=True)
class OllamaPreparedModelBinding:
    """Secret-free mapping from verified training bytes to an Ollama manifest."""

    route_model_id: str
    manifest_model_id: str
    artifact_sha256: str
    descriptor_digest: str
    provider_manifest_sha256: str
    endpoint_sha256: str
    create_request_sha256: str
    preparation_sha256: str

    def __post_init__(self) -> None:
        _require_model_id(self.route_model_id)
        _require_model_id(self.manifest_model_id)
        if self.manifest_model_id not in _model_aliases(self.route_model_id):
            raise ValueError("manifest_model_id does not match the route model alias")
        for value, name in (
            (self.artifact_sha256, "artifact_sha256"),
            (self.descriptor_digest, "descriptor_digest"),
            (self.provider_manifest_sha256, "provider_manifest_sha256"),
            (self.endpoint_sha256, "endpoint_sha256"),
            (self.create_request_sha256, "create_request_sha256"),
            (self.preparation_sha256, "preparation_sha256"),
        ):
            _require_sha256(value, name=name)
        expected = _canonical_json_sha256(self._payload_without_digest())
        if self.preparation_sha256 != expected:
            raise ValueError("preparation_sha256 does not match the binding payload")

    def _payload_without_digest(self) -> dict[str, object]:
        return {
            "schema": _BINDING_SCHEMA,
            "provider_id": _PROVIDER_ID,
            "route_model_id": self.route_model_id,
            "manifest_model_id": self.manifest_model_id,
            "artifact_sha256": self.artifact_sha256,
            "descriptor_digest": self.descriptor_digest,
            "provider_manifest_sha256": self.provider_manifest_sha256,
            "endpoint_sha256": self.endpoint_sha256,
            "create_request_sha256": self.create_request_sha256,
        }

    def revalidated(self) -> OllamaPreparedModelBinding:
        if type(self) is not OllamaPreparedModelBinding:
            raise TypeError("binding must be an exact OllamaPreparedModelBinding")
        return OllamaPreparedModelBinding(
            route_model_id=self.route_model_id,
            manifest_model_id=self.manifest_model_id,
            artifact_sha256=self.artifact_sha256,
            descriptor_digest=self.descriptor_digest,
            provider_manifest_sha256=self.provider_manifest_sha256,
            endpoint_sha256=self.endpoint_sha256,
            create_request_sha256=self.create_request_sha256,
            preparation_sha256=self.preparation_sha256,
        )

    def to_payload(self) -> dict[str, object]:
        payload = self._payload_without_digest()
        payload["preparation_sha256"] = self.preparation_sha256
        return payload


class OllamaManifestAuthority:
    """Bind exact prepared GGUF blob identity to Ollama's distinct manifest digest.

    This authority intentionally does not upload model files. The exact SHA-addressed
    blob must already exist in Ollama. Re-running prepare_existing_gguf_blob() is safe:
    /api/create is reasserted with the same model alias and exact blob digest before
    the resulting manifest identity is observed through /api/tags.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
        max_control_response_bytes: int = _MAX_CONTROL_RESPONSE_BYTES,
    ) -> None:
        self._base_url = _canonical_base_url(base_url)
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if type(max_control_response_bytes) is not int or not (
            256 <= max_control_response_bytes <= _MAX_CONTROL_RESPONSE_BYTES
        ):
            raise ValueError("max_control_response_bytes is outside the allowed range")
        self._timeout_seconds = float(timeout_seconds)
        self._client_factory = client_factory
        self._max_control_response_bytes = max_control_response_bytes

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def endpoint_sha256(self) -> str:
        return hashlib.sha256(self._base_url.encode("utf-8")).hexdigest()

    def _client(self) -> httpx.AsyncClient:
        return self._client_factory(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    async def _bounded_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
    ) -> object:
        try:
            async with client.stream(method, path, json=json_body) as response:
                if response.status_code != 200:
                    raise OllamaManifestAuthorityError(
                        "Ollama manifest control request failed"
                    )
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > self._max_control_response_bytes:
                        raise OllamaManifestAuthorityError(
                            "Ollama manifest control response exceeds the byte limit"
                        )
        except OllamaManifestAuthorityError:
            raise
        except (httpx.HTTPError, TimeoutError) as exc:
            raise OllamaManifestAuthorityError(
                "Ollama manifest control request failed"
            ) from exc
        try:
            return json.loads(
                bytes(raw).decode("utf-8", errors="strict"),
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise OllamaManifestAuthorityError(
                "Ollama manifest control response is invalid"
            ) from exc

    async def _require_blob(self, artifact_sha256: str) -> None:
        digest = "sha256:" + artifact_sha256
        try:
            async with self._client() as client:
                response = await client.head("/api/blobs/" + digest)
        except httpx.HTTPError as exc:
            raise OllamaManifestAuthorityError(
                "Ollama candidate blob could not be verified"
            ) from exc
        if response.status_code == 404:
            raise OllamaManifestAuthorityError(
                "Ollama candidate blob is not prepared"
            )
        if response.status_code != 200:
            raise OllamaManifestAuthorityError(
                "Ollama candidate blob could not be verified"
            )

    async def _observed_model(
        self,
        *,
        path: str,
        route_model_id: str,
    ) -> tuple[str, str]:
        aliases = _model_aliases(route_model_id)
        async with self._client() as client:
            payload = await self._bounded_json(client, "GET", path)
        if type(payload) is not dict or type(payload.get("models")) is not list:
            raise OllamaManifestAuthorityError(
                "Ollama model inventory response is invalid"
            )
        models = payload["models"]
        if len(models) > _MAX_MODELS:
            raise OllamaManifestAuthorityError(
                "Ollama model inventory exceeds the item limit"
            )
        matches: list[tuple[str, str]] = []
        for item in models:
            if type(item) is not dict:
                raise OllamaManifestAuthorityError(
                    "Ollama model inventory contains an invalid item"
                )
            names = []
            for key in ("model", "name"):
                value = item.get(key)
                if type(value) is str:
                    names.append(value)
            matching_names = sorted(set(names).intersection(aliases))
            if not matching_names:
                continue
            digest = item.get("digest")
            try:
                manifest_sha256 = _require_sha256(
                    digest,
                    name="provider manifest digest",
                )
            except ValueError as exc:
                raise OllamaManifestAuthorityError(
                    "Ollama model inventory has an invalid manifest digest"
                ) from exc
            manifest_name = matching_names[0]
            matches.append((manifest_name, manifest_sha256))
        if not matches:
            raise OllamaManifestAuthorityError(
                "Ollama model manifest is not available"
            )
        unique_digests = {digest for _, digest in matches}
        if len(unique_digests) != 1:
            raise OllamaManifestAuthorityError(
                "Ollama model alias resolves to ambiguous manifest identity"
            )
        manifest_sha256 = next(iter(unique_digests))
        manifest_names = sorted(name for name, _ in matches)
        return manifest_names[0], manifest_sha256

    async def prepare_existing_gguf_blob(
        self,
        *,
        model_id: str,
        artifact_sha256: str,
        descriptor_digest: str,
    ) -> OllamaPreparedModelBinding:
        route_model_id = _require_model_id(model_id)
        artifact_digest = _require_sha256(
            artifact_sha256,
            name="artifact_sha256",
        )
        descriptor = _require_sha256(
            descriptor_digest,
            name="descriptor_digest",
        )
        await self._require_blob(artifact_digest)

        create_request: dict[str, object] = {
            "model": route_model_id,
            "files": {"model.gguf": "sha256:" + artifact_digest},
            "stream": False,
        }
        async with self._client() as client:
            result = await self._bounded_json(
                client,
                "POST",
                "/api/create",
                json_body=create_request,
            )
        if type(result) is not dict or result.get("status") != "success":
            raise OllamaManifestAuthorityError(
                "Ollama model preparation did not report success"
            )

        manifest_model_id, manifest_sha256 = await self._observed_model(
            path="/api/tags",
            route_model_id=route_model_id,
        )
        create_request_sha256 = _canonical_json_sha256(create_request)
        payload = {
            "schema": _BINDING_SCHEMA,
            "provider_id": _PROVIDER_ID,
            "route_model_id": route_model_id,
            "manifest_model_id": manifest_model_id,
            "artifact_sha256": artifact_digest,
            "descriptor_digest": descriptor,
            "provider_manifest_sha256": manifest_sha256,
            "endpoint_sha256": self.endpoint_sha256,
            "create_request_sha256": create_request_sha256,
        }
        return OllamaPreparedModelBinding(
            route_model_id=route_model_id,
            manifest_model_id=manifest_model_id,
            artifact_sha256=artifact_digest,
            descriptor_digest=descriptor,
            provider_manifest_sha256=manifest_sha256,
            endpoint_sha256=self.endpoint_sha256,
            create_request_sha256=create_request_sha256,
            preparation_sha256=_canonical_json_sha256(payload),
        )

    def _require_endpoint(self, binding: OllamaPreparedModelBinding) -> None:
        if binding.endpoint_sha256 != self.endpoint_sha256:
            raise OllamaManifestAuthorityError(
                "Ollama prepared-model binding belongs to another endpoint"
            )

    async def assert_available(
        self,
        binding: OllamaPreparedModelBinding,
    ) -> OllamaPreparedModelBinding:
        canonical = binding.revalidated()
        self._require_endpoint(canonical)
        manifest_model_id, manifest_sha256 = await self._observed_model(
            path="/api/tags",
            route_model_id=canonical.route_model_id,
        )
        if (
            manifest_model_id not in _model_aliases(canonical.route_model_id)
            or manifest_sha256 != canonical.provider_manifest_sha256
        ):
            raise OllamaManifestAuthorityError(
                "Ollama available model manifest no longer matches the prepared binding"
            )
        return canonical

    async def assert_loaded(
        self,
        binding: OllamaPreparedModelBinding,
    ) -> OllamaPreparedModelBinding:
        canonical = binding.revalidated()
        self._require_endpoint(canonical)
        manifest_model_id, manifest_sha256 = await self._observed_model(
            path="/api/ps",
            route_model_id=canonical.route_model_id,
        )
        if (
            manifest_model_id not in _model_aliases(canonical.route_model_id)
            or manifest_sha256 != canonical.provider_manifest_sha256
        ):
            raise OllamaManifestAuthorityError(
                "Ollama loaded model manifest does not match the prepared binding"
            )
        return canonical


class ManifestPinnedOllamaProvider:
    """Ollama provider that enforces prepared manifest identity around inference."""

    def __init__(
        self,
        *,
        binding: OllamaPreparedModelBinding,
        authority: OllamaManifestAuthority,
        think: bool | str = False,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        canonical = binding.revalidated()
        if canonical.endpoint_sha256 != authority.endpoint_sha256:
            raise ValueError("binding and authority endpoints do not match")
        self._binding = canonical
        self._authority = authority
        self._provider = OllamaProvider(
            default_model=canonical.route_model_id,
            base_url=authority.base_url,
            think=think,
            client_factory=client_factory,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._provider.capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        requested_model = request.model or self._binding.route_model_id
        if (
            request.provider_id not in {None, _PROVIDER_ID}
            or requested_model != self._binding.route_model_id
        ):
            raise ModelGatewayError(
                ModelErrorCode.INVALID_REQUEST,
                "request does not match the manifest-pinned Ollama route",
                provider_id=_PROVIDER_ID,
                failure_effect=ModelFailureEffect.NO_EFFECT,
            )
        try:
            await self._authority.assert_available(self._binding)
        except OllamaManifestAuthorityError as exc:
            raise ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                "prepared Ollama model manifest is not available",
                provider_id=_PROVIDER_ID,
                failure_effect=ModelFailureEffect.NO_EFFECT,
            ) from exc

        response = await self._provider.complete(request)

        try:
            await self._authority.assert_loaded(self._binding)
        except OllamaManifestAuthorityError as exc:
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "loaded Ollama model manifest could not be verified",
                provider_id=_PROVIDER_ID,
                failure_effect=ModelFailureEffect.UNKNOWN,
            ) from exc
        return response


__all__ = [
    "ManifestPinnedOllamaProvider",
    "OllamaManifestAuthority",
    "OllamaManifestAuthorityError",
    "OllamaPreparedModelBinding",
]
