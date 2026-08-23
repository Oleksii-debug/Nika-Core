from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from types import SimpleNamespace

from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    PrivacyClass,
)
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


class _RestartModel:
    def __init__(self) -> None:
        self.id = "restart-model-cpu:1"
        self.alias = "restart-model"
        self.is_cached = True
        self.is_loaded = False
        self.context_length = 4096
        self.input_modalities = "text"
        self.output_modalities = "text"
        self.capabilities = "chat"
        self.supports_tool_calling: object = False
        self.unload_count = 0
        self.completion_count = 0

    def get_path(self) -> str:
        return "C:/Nika Test Models/restart-model"

    def load(self) -> None:
        self.is_loaded = True

    def unload(self) -> None:
        self.unload_count += 1
        self.is_loaded = False

    def get_chat_client(self) -> object:
        model = self

        class Client:
            settings = SimpleNamespace(temperature=None)

            def complete_chat(self, messages: list[dict[str, str]]) -> object:
                del messages
                model.completion_count += 1
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="ready"))],
                    usage=SimpleNamespace(
                        prompt_tokens=2,
                        completion_tokens=1,
                        total_tokens=3,
                    ),
                )

        return Client()


class _Catalog:
    def __init__(self, model: _RestartModel) -> None:
        self.model = model

    def get_model(self, alias: str) -> _RestartModel | None:
        return self.model if alias == self.model.alias else None


class _Manager:
    def __init__(self, model: _RestartModel) -> None:
        self.catalog = _Catalog(model)


def _request(request_id: str) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        messages=(ModelMessage(role="user", content="hello"),),
        provider_id="foundry-local",
        privacy=PrivacyClass.SENSITIVE,
        timeout_seconds=1.0,
    )


def test_foundry_provider_lifecycle_restart_has_no_stale_runtime_ownership() -> None:
    model = _RestartModel()
    manager = _Manager(model)

    first_provider = FoundryLocalProvider(
        default_model=model.alias,
        expected_model_id=model.id,
        manager_factory=lambda: manager,
    )
    first = asyncio.run(first_provider.complete(_request("before-restart")))
    first_provider.close()

    assert first.request_id == "before-restart"
    assert model.is_loaded is False
    assert model.unload_count == 1

    second_provider = FoundryLocalProvider(
        default_model=model.alias,
        expected_model_id=model.id,
        manager_factory=lambda: manager,
    )
    second = asyncio.run(second_provider.complete(_request("after-restart")))
    second_provider.close()

    assert second.request_id == "after-restart"
    assert model.completion_count == 2
    assert model.is_loaded is False
    assert model.unload_count == 2


def test_malformed_tool_capability_metadata_cannot_become_positive_evidence() -> None:
    model = _RestartModel()
    model.supports_tool_calling = "true"
    provider = FoundryLocalProvider(
        default_model=model.alias,
        expected_model_id=model.id,
        manager_factory=lambda: _Manager(model),
    )

    evidence = provider.inspect_model()

    assert evidence.supports_tool_calling is None


def test_foundry_sdk_types_remain_adapter_local() -> None:
    root = Path(__file__).resolve().parents[1]
    model_gateway = root / "src" / "nika_core" / "model_gateway"

    for path in model_gateway.glob("*.py"):
        if path.name == "foundry_local.py":
            continue
        source = path.read_text(encoding="utf-8")
        assert "foundry_local_sdk" not in source, path
        assert "FoundryLocalManager" not in source, path


def test_base_windows_package_keeps_foundry_optional_and_m11_excludes_extra() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    base_dependencies = tuple(project["dependencies"])
    optional = project["optional-dependencies"]
    embedded_dependencies = tuple(optional["embedded-ai"])

    assert not any("foundry-local-sdk" in dependency for dependency in base_dependencies)
    assert any("foundry-local-sdk-winml" in dependency for dependency in embedded_dependencies)

    release_workflow = (root / ".github/workflows/m11-windows-release.yml").read_text(
        encoding="utf-8"
    )
    assert 'pip install -e ".[gui,qa,dev]"' in release_workflow
    assert "embedded-ai" not in release_workflow
