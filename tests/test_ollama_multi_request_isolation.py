from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    PrivacyClass,
)
from nika_core.model_gateway.gateway import ModelGateway, model_identity_fingerprint
from nika_core.model_gateway.providers import OllamaProvider


class _Barrier:
    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._arrivals = 0
        self.all_arrived = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self) -> None:
        self._arrivals += 1
        if self._arrivals == self._parties:
            self.all_arrived.set()
        await self.release.wait()


class _AuditLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def append(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object] | None = None,
    ) -> int:
        assert entity_type == "model_request"
        self.events.append((event_type, entity_id, dict(payload or {})))
        return len(self.events)

    def for_request(self, request_id: str) -> list[tuple[str, dict[str, object]]]:
        return [
            (event_type, payload)
            for event_type, entity_id, payload in self.events
            if entity_id == request_id
        ]


def _request(request_id: str, *, model: str, prompt: str) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content=prompt),),
        model=model,
        provider_id="ollama",
        privacy=PrivacyClass.PRIVATE,
        timeout_seconds=10,
        temperature=0,
    )


def _client_factory(handler: Any) -> Any:
    transport = httpx.MockTransport(handler)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    return factory


def test_parallel_ollama_requests_keep_identity_model_usage_and_audit_isolated() -> None:
    async def scenario() -> None:
        barrier = _Barrier(2)
        audit = _AuditLog()
        outbound: list[tuple[str, str]] = []
        usage_by_model = {
            "model-a:1": (11, 3),
            "model-b:2": (23, 5),
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            model = body["model"]
            prompt = body["messages"][-1]["content"]
            outbound.append((model, prompt))
            await barrier.wait()
            prompt_tokens, output_tokens = usage_by_model[model]
            return httpx.Response(
                200,
                json={
                    "model": model,
                    "message": {"role": "assistant", "content": f"{model}:{prompt}"},
                    "done": True,
                    "prompt_eval_count": prompt_tokens,
                    "eval_count": output_tokens,
                },
            )

        gateway = ModelGateway(audit_log=audit)
        gateway.register(
            OllamaProvider(
                default_model="unused-default",
                client_factory=_client_factory(handler),
            )
        )

        first = asyncio.create_task(
            gateway.complete(_request("request-a", model="model-a:1", prompt="alpha"))
        )
        second = asyncio.create_task(
            gateway.complete(_request("request-b", model="model-b:2", prompt="beta"))
        )
        await asyncio.wait_for(barrier.all_arrived.wait(), timeout=2)
        barrier.release.set()
        first_response, second_response = await asyncio.gather(first, second)

        assert set(outbound) == {("model-a:1", "alpha"), ("model-b:2", "beta")}
        assert (
            first_response.request_id,
            first_response.model,
            first_response.text,
            first_response.usage.input_tokens,
            first_response.usage.output_tokens,
            first_response.usage.total_tokens,
        ) == ("request-a", "model-a:1", "model-a:1:alpha", 11, 3, 14)
        assert (
            second_response.request_id,
            second_response.model,
            second_response.text,
            second_response.usage.input_tokens,
            second_response.usage.output_tokens,
            second_response.usage.total_tokens,
        ) == ("request-b", "model-b:2", "model-b:2:beta", 23, 5, 28)

        for request_id, model, usage in (
            ("request-a", "model-a:1", (11, 3, 14)),
            ("request-b", "model-b:2", (23, 5, 28)),
        ):
            events = audit.for_request(request_id)
            assert [event_type for event_type, _payload in events] == [
                "model.requested",
                "model.completed",
            ]
            requested = events[0][1]
            completed = events[1][1]
            assert requested["model_fingerprint"] == model_identity_fingerprint(model)
            assert completed["model_fingerprint"] == model_identity_fingerprint(model)
            assert (
                completed["input_tokens"],
                completed["output_tokens"],
                completed["total_tokens"],
            ) == usage

    asyncio.run(scenario())


def test_cancelling_one_parallel_ollama_request_does_not_cancel_sibling() -> None:
    async def scenario() -> None:
        barrier = _Barrier(2)
        audit = _AuditLog()
        cancelled_in_transport = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            model = body["model"]
            try:
                await barrier.wait()
            except asyncio.CancelledError:
                if model == "cancel-model":
                    cancelled_in_transport.set()
                raise
            return httpx.Response(
                200,
                json={
                    "model": model,
                    "message": {"role": "assistant", "content": f"ok:{model}"},
                    "done": True,
                    "prompt_eval_count": 2,
                    "eval_count": 1,
                },
            )

        gateway = ModelGateway(audit_log=audit)
        gateway.register(
            OllamaProvider(
                default_model="unused-default",
                client_factory=_client_factory(handler),
            )
        )
        cancelled = asyncio.create_task(
            gateway.complete(
                _request("request-cancel", model="cancel-model", prompt="cancel me")
            )
        )
        sibling = asyncio.create_task(
            gateway.complete(_request("request-ok", model="ok-model", prompt="keep going"))
        )

        await asyncio.wait_for(barrier.all_arrived.wait(), timeout=2)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await asyncio.wait_for(cancelled_in_transport.wait(), timeout=2)

        assert sibling.done() is False
        barrier.release.set()
        response = await sibling
        assert (response.request_id, response.model, response.text) == (
            "request-ok",
            "ok-model",
            "ok:ok-model",
        )
        assert [event_type for event_type, _payload in audit.for_request("request-cancel")] == [
            "model.requested",
            "model.cancelled",
        ]
        assert [event_type for event_type, _payload in audit.for_request("request-ok")] == [
            "model.requested",
            "model.completed",
        ]

    asyncio.run(scenario())


def test_one_parallel_ollama_failure_does_not_poison_successful_sibling() -> None:
    async def scenario() -> None:
        barrier = _Barrier(2)
        audit = _AuditLog()

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            model = body["model"]
            await barrier.wait()
            if model == "broken-model":
                return httpx.Response(
                    200,
                    json={
                        "model": model,
                        "message": {"role": "assistant", "content": "broken"},
                        "done": True,
                        "prompt_eval_count": "invalid",
                        "eval_count": 1,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "model": model,
                    "message": {"role": "assistant", "content": "healthy"},
                    "done": True,
                    "prompt_eval_count": 7,
                    "eval_count": 4,
                },
            )

        gateway = ModelGateway(audit_log=audit)
        gateway.register(
            OllamaProvider(
                default_model="unused-default",
                client_factory=_client_factory(handler),
            )
        )
        failed = asyncio.create_task(
            gateway.complete(_request("request-fail", model="broken-model", prompt="bad"))
        )
        healthy = asyncio.create_task(
            gateway.complete(_request("request-healthy", model="healthy-model", prompt="good"))
        )

        await asyncio.wait_for(barrier.all_arrived.wait(), timeout=2)
        barrier.release.set()
        results = await asyncio.gather(failed, healthy, return_exceptions=True)

        assert isinstance(results[0], ModelGatewayError)
        assert results[0].code is ModelErrorCode.PROVIDER_ERROR
        healthy_response = results[1]
        assert not isinstance(healthy_response, BaseException)
        assert (healthy_response.request_id, healthy_response.model, healthy_response.text) == (
            "request-healthy",
            "healthy-model",
            "healthy",
        )
        assert (
            healthy_response.usage.input_tokens,
            healthy_response.usage.output_tokens,
            healthy_response.usage.total_tokens,
        ) == (7, 4, 11)

        assert [event_type for event_type, _payload in audit.for_request("request-fail")] == [
            "model.requested",
            "model.failed",
        ]
        assert [event_type for event_type, _payload in audit.for_request("request-healthy")] == [
            "model.requested",
            "model.completed",
        ]
        failure_payload = audit.for_request("request-fail")[-1][1]
        success_payload = audit.for_request("request-healthy")[-1][1]
        assert failure_payload["model_fingerprint"] == model_identity_fingerprint("broken-model")
        assert success_payload["model_fingerprint"] == model_identity_fingerprint("healthy-model")
        assert (
            success_payload["input_tokens"],
            success_payload["output_tokens"],
            success_payload["total_tokens"],
        ) == (7, 4, 11)

    asyncio.run(scenario())
