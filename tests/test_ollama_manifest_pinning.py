from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
)
from nika_core.model_gateway.providers import OllamaProvider

_MODEL = "nika-trained:candidate"
_EXPECTED = "a" * 64
_OTHER = "b" * 64


def _request(*, model: str = _MODEL) -> ModelRequest:
    return ModelRequest(
        request_id="ollama-manifest-pin",
        provider_id="ollama",
        model=model,
        messages=(ModelMessage(role="user", content="hello"),),
        timeout_seconds=5.0,
        temperature=0.0,
    )


def _catalog(*, digest: str, model: str = _MODEL) -> dict[str, object]:
    return {
        "models": [
            {
                "name": model,
                "model": model,
                "digest": digest,
            }
        ]
    }


def _chat(model: str = _MODEL) -> dict[str, object]:
    return {
        "model": model,
        "message": {"role": "assistant", "content": "answer"},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 2,
        "eval_count": 1,
    }


def _provider(handler: Any, *, model: str = _MODEL) -> OllamaProvider:
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    return OllamaProvider(
        default_model=model,
        expected_manifest_sha256=_EXPECTED,
        client_factory=client_factory,
    )


def test_pinned_ollama_verifies_catalog_and_loaded_manifest_around_chat() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=_catalog(digest=_EXPECTED))
        if request.url.path == "/api/ps":
            return httpx.Response(200, json=_catalog(digest=_EXPECTED))
        assert request.url.path == "/api/chat"
        assert json.loads(request.read())["model"] == _MODEL
        return httpx.Response(200, json=_chat())

    result = asyncio.run(_provider(handler).complete(_request()))

    assert result.text == "answer"
    assert seen == [
        ("GET", "/api/tags"),
        ("POST", "/api/chat"),
        ("GET", "/api/ps"),
    ]


def test_pinned_bare_model_accepts_latest_alias_in_manifest_catalog() -> None:
    model = "nika-trained"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path in {"/api/tags", "/api/ps"}:
            return httpx.Response(
                200,
                json=_catalog(digest=_EXPECTED, model=model + ":latest"),
            )
        assert request.url.path == "/api/chat"
        assert json.loads(request.read())["model"] == model
        return httpx.Response(200, json=_chat(model))

    result = asyncio.run(
        _provider(handler, model=model).complete(_request(model=model))
    )

    assert result.text == "answer"
    assert seen == ["/api/tags", "/api/chat", "/api/ps"]


def test_manifest_catalog_response_is_byte_bounded_before_chat() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b" " * (1024 * 1024 + 1),
        )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(_provider(handler).complete(_request()))

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert calls == 1


def test_manifest_catalog_rejects_duplicate_json_keys_before_chat() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=(
                b'{"models":[],"models":['
                + json.dumps(
                    {
                        "name": _MODEL,
                        "model": _MODEL,
                        "digest": _EXPECTED,
                    }
                ).encode("utf-8")
                + b"]}"
            ),
        )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(_provider(handler).complete(_request()))

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert calls == 1


def test_preflight_manifest_mismatch_blocks_chat_with_no_effect() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json=_catalog(digest=_OTHER))

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(_provider(handler).complete(_request()))

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert seen == ["/api/tags"]


def test_postflight_manifest_mismatch_discards_result_as_unknown_effect() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=_catalog(digest=_EXPECTED))
        if request.url.path == "/api/chat":
            return httpx.Response(200, json=_chat())
        assert request.url.path == "/api/ps"
        return httpx.Response(200, json=_catalog(digest=_OTHER))

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(_provider(handler).complete(_request()))

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.failure_effect is ModelFailureEffect.UNKNOWN
    assert seen == ["/api/tags", "/api/chat", "/api/ps"]


def test_missing_loaded_model_after_chat_is_unknown_effect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=_catalog(digest=_EXPECTED))
        if request.url.path == "/api/chat":
            return httpx.Response(200, json=_chat())
        return httpx.Response(200, json={"models": []})

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(_provider(handler).complete(_request()))

    assert caught.value.failure_effect is ModelFailureEffect.UNKNOWN


def test_postflight_timeout_after_chat_is_unknown_effect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=_catalog(digest=_EXPECTED))
        if request.url.path == "/api/chat":
            return httpx.Response(200, json=_chat())
        raise httpx.ReadTimeout("postflight timeout", request=request)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(_provider(handler).complete(_request()))

    assert caught.value.code is ModelErrorCode.TIMEOUT
    assert caught.value.retryable is False
    assert caught.value.failure_effect is ModelFailureEffect.UNKNOWN


def test_preflight_timeout_is_explicitly_no_effect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("preflight timeout", request=request)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(_provider(handler).complete(_request()))

    assert caught.value.code is ModelErrorCode.TIMEOUT
    assert caught.value.retryable is True
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT


def test_pinned_provider_rejects_request_model_override_before_http() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(_provider(handler).complete(_request(model="other:model")))

    assert caught.value.code is ModelErrorCode.INVALID_REQUEST
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert calls == 0


@pytest.mark.parametrize(
    "digest",
    (
        "a" * 63,
        "A" * 64,
        "sha256:" + "a" * 64,
        7,
    ),
)
def test_manifest_pin_requires_exact_lowercase_sha256(digest: object) -> None:
    with pytest.raises(ValueError, match="expected_manifest_sha256"):
        OllamaProvider(
            default_model=_MODEL,
            expected_manifest_sha256=digest,  # type: ignore[arg-type]
        )


def test_ambiguous_catalog_identity_fails_before_chat() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": _MODEL,
                        "model": _MODEL + ":latest",
                        "digest": _EXPECTED,
                    }
                ]
            },
        )

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(_provider(handler).complete(_request()))

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.failure_effect is ModelFailureEffect.NO_EFFECT
    assert seen == ["/api/tags"]
