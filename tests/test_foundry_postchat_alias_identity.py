from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


class _Model:
    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.alias = "selected-alias"
        self.is_cached = True
        self.is_loaded = True
        self.usage_read = False
        self.chat_calls = 0

    @property
    def id(self) -> str:
        if self.stage == "final-id" and self.usage_read:
            self.alias = "substituted-alias"
        return "stable-artifact:1"

    def get_chat_client(self) -> object:
        model = self

        class _Usage:
            @property
            def prompt_tokens(self) -> int:
                model.usage_read = True
                if model.stage == "usage":
                    model.alias = "substituted-alias"
                return 1

            completion_tokens = 1
            total_tokens = 2

        class _Client:
            def complete_chat(self, messages: object) -> object:
                del messages
                model.chat_calls += 1
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(message=SimpleNamespace(content="Готово"))
                    ],
                    usage=_Usage(),
                )

        return _Client()


def _provider(model: _Model) -> FoundryLocalProvider:
    manager = SimpleNamespace(
        catalog=SimpleNamespace(get_model=lambda alias: model),
    )
    return FoundryLocalProvider(
        default_model="selected-alias",
        manager_factory=lambda: manager,
    )


def _request() -> ModelRequest:
    # No explicit model pin: the default SDK alias must remain authoritative.
    return ModelRequest(
        request_id="foundry-postchat-alias",
        messages=(ModelMessage(role="user", content="Привіт"),),
        timeout_seconds=2.0,
    )


@pytest.mark.parametrize("stage", ("usage", "final-id"))
def test_postchat_sdk_getters_cannot_retarget_selected_alias(stage: str) -> None:
    model = _Model(stage)
    provider = _provider(model)

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(provider.complete(_request()))

    assert caught.value.code is ModelErrorCode.PROVIDER_ERROR
    assert caught.value.retryable is False
    assert model.chat_calls == 1
    assert model.usage_read
    assert not provider._inference_lock.locked()


def test_stable_postchat_sdk_metadata_keeps_selected_default_alias() -> None:
    model = _Model("stable")
    provider = _provider(model)

    result = asyncio.run(provider.complete(_request()))

    assert result.request_id == "foundry-postchat-alias"
    assert result.text == "Готово"
    assert result.model == "selected-alias"
    assert result.usage.input_tokens == 1
    assert result.usage.output_tokens == 1
    assert result.usage.total_tokens == 2
    assert model.chat_calls == 1
    assert model.usage_read
    assert not provider._inference_lock.locked()
