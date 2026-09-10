from __future__ import annotations

import asyncio
import traceback

import httpx
import pytest

from nika_core.model_gateway.api_route import (
    ApiModelRouteConfig,
    CredentialRefOpenAICompatibleProvider,
)
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    PrivacyClass,
)
from nika_core.model_gateway.gateway import ModelGateway

_API_KEY = "NIKA_OS36_SYNTHETIC_API_KEY_4d5e9f"
_PROMPT_SECRET = "NIKA_OS36_SYNTHETIC_REQUEST_BODY_582c1a"
_RESPONSE_HEADER_SECRET = "NIKA_OS36_SYNTHETIC_RESPONSE_HEADER_924bd0"
_RESPONSE_BODY_SECRET = "NIKA_OS36_SYNTHETIC_RESPONSE_BODY_a71c33"
_SIGNED_URL = "https://download.example.invalid/object?sig=NIKA_OS36_SIGNED_URL_13ab7e"
_ENV_SECRET = "NIKA_OS36_SYNTHETIC_ENV_SECRET_808c2f"
_FILESYSTEM_SECRET = r"C:\\Users\\synthetic\\nika-secret-5f6d2a.txt"
_CREDENTIAL_REF = "env:NIKA_OS36_SENSITIVE_CREDENTIAL_REF_76fe31"
_PROVIDER_ID = "os36-cloud"


class _StaticResolver:
    def resolve(self, credential_ref: str) -> str:
        assert credential_ref == _CREDENTIAL_REF
        return _API_KEY


class _ExplodingResolver:
    def resolve(self, credential_ref: str) -> str:
        raise RuntimeError(
            "resolver diagnostic "
            f"credential={credential_ref} env={_ENV_SECRET} path={_FILESYSTEM_SECRET} "
            f"signed={_SIGNED_URL}"
        )


class _Audit:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def append(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object] | None = None,
    ) -> int:
        self.events.append(
            {
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "payload": payload or {},
            }
        )
        return len(self.events)


def _config() -> ApiModelRouteConfig:
    return ApiModelRouteConfig(
        provider_id=_PROVIDER_ID,
        base_url="https://api.example.invalid/v1",
        default_model="model-a",
        credential_ref=_CREDENTIAL_REF,
    )


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="os36-cloud-error-minimization",
        messages=(ModelMessage(role="user", content=_PROMPT_SECRET),),
        model="model-a",
        provider_id=_PROVIDER_ID,
        privacy=PrivacyClass.PUBLIC,
        timeout_seconds=1.0,
    )


def _client_factory(transport: httpx.MockTransport):
    def factory(*, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=timeout)

    return factory


def _public_error_text(error: BaseException) -> str:
    return "\n".join(
        (
            str(error),
            repr(error),
            "".join(traceback.format_exception(type(error), error, error.__traceback__)),
            repr(getattr(error, "__dict__", {})),
        )
    )


def _assert_sensitive_material_absent(text: str) -> None:
    for value in (
        _API_KEY,
        _PROMPT_SECRET,
        _RESPONSE_HEADER_SECRET,
        _RESPONSE_BODY_SECRET,
        _SIGNED_URL,
        _ENV_SECRET,
        _FILESYSTEM_SECRET,
        _CREDENTIAL_REF,
    ):
        assert value not in text


@pytest.mark.parametrize(
    ("status", "expected_code"),
    (
        (400, ModelErrorCode.INVALID_REQUEST),
        (401, ModelErrorCode.AUTHENTICATION),
        (403, ModelErrorCode.POLICY_DENIED),
        (408, ModelErrorCode.TIMEOUT),
        (413, ModelErrorCode.RESOURCE_LIMIT),
        (429, ModelErrorCode.RATE_LIMITED),
        (500, ModelErrorCode.UNAVAILABLE),
    ),
)
def test_cloud_http_failure_keeps_taxonomy_without_public_or_durable_secrets(
    status: int,
    expected_code: ModelErrorCode,
) -> None:
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {_API_KEY}"
        assert _PROMPT_SECRET in request.content.decode("utf-8")
        return httpx.Response(
            status,
            headers={
                "X-Synthetic-Secret": _RESPONSE_HEADER_SECRET,
                "Location": _SIGNED_URL,
            },
            json={"error": _RESPONSE_BODY_SECRET},
        )

    audit = _Audit()
    provider = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=_StaticResolver(),
        client_factory=_client_factory(httpx.MockTransport(handler)),
    )
    gateway = ModelGateway(audit_log=audit)
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    error = caught.value
    assert error.code is expected_code
    assert error.provider_id == _PROVIDER_ID
    assert error.__cause__ is None
    assert error.__context__ is None
    assert len(seen_requests) == 1

    _assert_sensitive_material_absent(_public_error_text(error))
    _assert_sensitive_material_absent(repr(audit.events))
    assert any(
        event["event_type"] == "model.failed"
        and event["payload"].get("code") == expected_code.value  # type: ignore[union-attr]
        for event in audit.events
    )


def test_credential_resolver_diagnostic_is_minimized_without_losing_auth_classification() -> None:
    audit = _Audit()
    provider = CredentialRefOpenAICompatibleProvider(
        config=_config(),
        credential_resolver=_ExplodingResolver(),
    )
    gateway = ModelGateway(audit_log=audit)
    gateway.register(provider)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(gateway.complete(_request()))

    error = caught.value
    assert error.code is ModelErrorCode.AUTHENTICATION
    assert error.provider_id == _PROVIDER_ID
    assert error.retryable is False
    assert error.__cause__ is None
    assert error.__context__ is None

    _assert_sensitive_material_absent(_public_error_text(error))
    _assert_sensitive_material_absent(repr(audit.events))
