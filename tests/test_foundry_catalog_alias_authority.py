from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nika_core.model_gateway.contracts import (
    ModelDownloadAuthorization,
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    PrivacyClass,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


class _SubstitutedModel:
    def __init__(self, *, cached: bool) -> None:
        self.id = "substituted-model-id"
        self.alias = "substituted-model"
        self.is_cached = cached
        self.is_loaded = False
        self.context_length = None
        self.input_modalities = None
        self.output_modalities = None
        self.capabilities = None
        self.supports_tool_calling = None
        self.load_calls = 0
        self.download_calls = 0
        self.chat_calls = 0

    def get_path(self) -> str:
        return "C:/unexpected/substituted-model"

    def load(self) -> None:
        self.load_calls += 1
        self.is_loaded = True

    def unload(self) -> None:
        self.is_loaded = False

    def download(self, *, cancel_event: object | None = None) -> None:
        self.download_calls += 1
        self.is_cached = True

    def get_chat_client(self) -> object:
        model = self

        class _Client:
            def complete_chat(self, messages: object) -> object:
                model.chat_calls += 1
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="substituted response")
                        )
                    ],
                    usage=SimpleNamespace(
                        prompt_tokens=1,
                        completion_tokens=1,
                        total_tokens=2,
                    ),
                )

        return _Client()


class _LoadAliasDriftModel(_SubstitutedModel):
    def __init__(self) -> None:
        super().__init__(cached=True)
        self.alias = "authorized-model"

    def load(self) -> None:
        super().load()
        self.alias = "substituted-model"


class _DownloadAliasDriftModel(_SubstitutedModel):
    def __init__(self) -> None:
        super().__init__(cached=False)
        self.alias = "authorized-model"

    def download(self, *, cancel_event: object | None = None) -> None:
        super().download(cancel_event=cancel_event)
        self.alias = "substituted-model"


class _ChatClientAliasDriftModel(_SubstitutedModel):
    def __init__(self) -> None:
        super().__init__(cached=True)
        self.alias = "authorized-model"
        self.is_loaded = True

    def get_chat_client(self) -> object:
        client = super().get_chat_client()
        self.alias = "substituted-model"
        return client


class _ChatCompletionIdentityDriftModel(_SubstitutedModel):
    def __init__(self) -> None:
        super().__init__(cached=True)
        self.id = "authorized-model-id"
        self.alias = "authorized-model"
        self.is_loaded = True

    def get_chat_client(self) -> object:
        model = self

        class _Client:
            def complete_chat(self, messages: object) -> object:
                model.chat_calls += 1
                model.id = "substituted-model-id"
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="substituted response")
                        )
                    ],
                    usage=SimpleNamespace(
                        prompt_tokens=1,
                        completion_tokens=1,
                        total_tokens=2,
                    ),
                )

        return _Client()


class _EvidenceAliasDriftModel:
    def __init__(self) -> None:
        self.id = "authorized-model-id"
        self.alias = "authorized-model"
        self.is_loaded = False
        self.context_length = None
        self.input_modalities = None
        self.output_modalities = None
        self.capabilities = None
        self.supports_tool_calling = None

    @property
    def is_cached(self) -> bool:
        self.alias = "substituted-model"
        return False


class _EvidenceIdentityDriftModel:
    def __init__(self) -> None:
        self.id = "authorized-model-id"
        self.alias = "authorized-model"
        self.is_loaded = False
        self.context_length = None
        self.input_modalities = None
        self.output_modalities = None
        self.capabilities = None
        self.supports_tool_calling = None

    @property
    def is_cached(self) -> bool:
        self.id = "substituted-model-id"
        return False


class _SubstitutingCatalog:
    def __init__(self, model: _SubstitutedModel) -> None:
        self.model = model
        self.requested_aliases: list[str] = []

    def get_model(self, alias: str) -> _SubstitutedModel:
        self.requested_aliases.append(alias)
        return self.model


class _Manager:
    def __init__(self, model: _SubstitutedModel) -> None:
        self.catalog = _SubstitutingCatalog(model)


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="alias-authority",
        messages=(ModelMessage(role="user", content="hello"),),
        model="authorized-model",
        provider_id="foundry-local",
        privacy=PrivacyClass.SENSITIVE,
        timeout_seconds=1.0,
    )


def _authorization() -> ModelDownloadAuthorization:
    return ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="authorized-model",
        license_reference="MODEL-LICENSE-REVIEW-ALIAS-AUTHORITY",
    )


def _assert_alias_substitution_failure(exc: ModelGatewayError) -> None:
    assert exc.code is ModelErrorCode.PROVIDER_ERROR
    assert exc.provider_id == "foundry-local"
    assert exc.retryable is False


def test_foundry_complete_rejects_catalog_alias_substitution_before_native_effect() -> None:
    model = _SubstitutedModel(cached=True)
    manager = _Manager(model)
    provider = FoundryLocalProvider(
        default_model="authorized-model",
        manager_factory=lambda: manager,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(_request()))

    _assert_alias_substitution_failure(exc_info.value)
    assert manager.catalog.requested_aliases == ["authorized-model"]
    assert model.load_calls == 0
    assert model.chat_calls == 0


def test_foundry_download_rejects_catalog_alias_substitution_before_native_effect() -> None:
    model = _SubstitutedModel(cached=False)
    manager = _Manager(model)
    provider = FoundryLocalProvider(
        default_model="authorized-model",
        manager_factory=lambda: manager,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.download_model(_authorization(), timeout_seconds=1.0))

    _assert_alias_substitution_failure(exc_info.value)
    assert manager.catalog.requested_aliases == ["authorized-model"]
    assert model.download_calls == 0


def test_foundry_inspect_rejects_catalog_alias_substitution() -> None:
    model = _SubstitutedModel(cached=False)
    manager = _Manager(model)
    provider = FoundryLocalProvider(
        default_model="authorized-model",
        manager_factory=lambda: manager,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        provider.inspect_model("authorized-model")

    _assert_alias_substitution_failure(exc_info.value)
    assert manager.catalog.requested_aliases == ["authorized-model"]
    assert model.load_calls == 0
    assert model.download_calls == 0
    assert model.chat_calls == 0


def test_foundry_inspect_rejects_alias_drift_during_evidence_collection() -> None:
    model = _EvidenceAliasDriftModel()
    manager = _Manager(model)  # type: ignore[arg-type]
    provider = FoundryLocalProvider(
        default_model="authorized-model",
        manager_factory=lambda: manager,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        provider.inspect_model("authorized-model")

    _assert_alias_substitution_failure(exc_info.value)


def test_foundry_inspect_rejects_identity_drift_during_evidence_collection() -> None:
    model = _EvidenceIdentityDriftModel()
    manager = _Manager(model)  # type: ignore[arg-type]
    provider = FoundryLocalProvider(
        default_model="authorized-model",
        expected_model_id="authorized-model-id",
        manager_factory=lambda: manager,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        provider.inspect_model("authorized-model")

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST
    assert exc_info.value.provider_id == "foundry-local"
    assert exc_info.value.retryable is False


def test_foundry_complete_rejects_alias_drift_after_native_load_before_chat() -> None:
    model = _LoadAliasDriftModel()
    manager = _Manager(model)
    provider = FoundryLocalProvider(
        default_model="authorized-model",
        manager_factory=lambda: manager,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(_request()))

    _assert_alias_substitution_failure(exc_info.value)
    assert manager.catalog.requested_aliases == ["authorized-model"]
    assert model.load_calls == 1
    assert model.is_loaded is False
    assert model.chat_calls == 0


def test_foundry_download_rejects_alias_drift_after_native_download() -> None:
    model = _DownloadAliasDriftModel()
    manager = _Manager(model)
    provider = FoundryLocalProvider(
        default_model="authorized-model",
        manager_factory=lambda: manager,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.download_model(_authorization(), timeout_seconds=1.0))

    _assert_alias_substitution_failure(exc_info.value)
    assert manager.catalog.requested_aliases == ["authorized-model"]
    assert model.download_calls == 1


def test_foundry_complete_revalidates_alias_after_chat_client_acquisition() -> None:
    model = _ChatClientAliasDriftModel()
    manager = _Manager(model)
    provider = FoundryLocalProvider(
        default_model="authorized-model",
        manager_factory=lambda: manager,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(_request()))

    _assert_alias_substitution_failure(exc_info.value)
    assert manager.catalog.requested_aliases == ["authorized-model"]
    assert model.chat_calls == 0


def test_foundry_complete_revalidates_model_identity_after_native_chat() -> None:
    model = _ChatCompletionIdentityDriftModel()
    manager = _Manager(model)
    provider = FoundryLocalProvider(
        default_model="authorized-model",
        expected_model_id="authorized-model-id",
        manager_factory=lambda: manager,
    )

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(provider.complete(_request()))

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST
    assert exc_info.value.provider_id == "foundry-local"
    assert exc_info.value.retryable is False
    assert manager.catalog.requested_aliases == ["authorized-model"]
    assert model.chat_calls == 1
