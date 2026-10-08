from __future__ import annotations

import asyncio

import pytest

from nika_core.model_gateway.contracts import ModelDownloadAuthorization
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


@pytest.mark.parametrize(
    "invalid_timeout",
    [True, False, 0, -0.1, float("nan"), float("inf"), -float("inf"),
     86400.01, "30", 10**500],
)
def test_download_budget_rejected_before_sdk_or_lock(invalid_timeout: object) -> None:
    manager_calls = 0

    def forbidden_manager() -> object:
        nonlocal manager_calls
        manager_calls += 1
        raise AssertionError("unbounded download reached the Foundry SDK")

    provider = FoundryLocalProvider(
        default_model="test-model", manager_factory=forbidden_manager
    )
    permit = ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="test-model",
        license_reference="MODEL-LICENSE-REVIEW",
    )

    with pytest.raises(ValueError, match="timeout_seconds"):
        asyncio.run(
            provider.download_model(
                permit, timeout_seconds=invalid_timeout  # type: ignore[arg-type]
            )
        )

    assert manager_calls == 0
    assert provider._model_management_lock.locked() is False
    assert provider._inference_lock.locked() is False


@pytest.mark.parametrize(
    "field,malformed",
    [
        ("provider_id", object()),
        ("model", ["changed-model"]),
        ("license_reference", ""),
        ("expected_model_id", 37),
    ],
)
def test_mutated_authorization_rejected_before_sdk(
    field: str, malformed: object,
) -> None:
    manager_calls = 0

    def forbidden_manager() -> object:
        nonlocal manager_calls
        manager_calls += 1
        raise AssertionError("malformed authorization reached the Foundry SDK")

    provider = FoundryLocalProvider(
        default_model="safe-model", manager_factory=forbidden_manager,
    )
    permit = ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="safe-model",
        license_reference="REVIEWED-LICENSE",
    )
    object.__setattr__(permit, field, malformed)

    with pytest.raises(ValueError, match="authorization|must not be empty"):
        asyncio.run(provider.download_model(permit))
    assert manager_calls == 0
    assert not provider._model_management_lock.locked()
    assert not provider._inference_lock.locked()


def test_authorization_is_detached_across_lock_await(monkeypatch: pytest.MonkeyPatch) -> None:
    class CachedModel:
        id = "variant-v1"
        alias = "safe-model"
        is_cached = True
        is_loaded = False

        def get_path(self) -> str:
            return "fake-cache-path"

    selected: list[str] = []
    model = CachedModel()
    provider = FoundryLocalProvider(default_model="safe-model")
    def lookup(alias: str) -> CachedModel:
        selected.append(alias)
        return model

    monkeypatch.setattr(provider, "_get_model", lookup)
    permit = ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="safe-model",
        license_reference="REVIEWED-LICENSE",
    )

    async def exercise() -> object:
        await provider._model_management_lock.acquire()
        task = asyncio.create_task(provider.download_model(permit, timeout_seconds=3))
        try:
            await asyncio.sleep(0)
            object.__setattr__(permit, "model", "attacker-model")
        finally:
            provider._model_management_lock.release()
        return await task

    evidence = asyncio.run(exercise())
    assert selected == ["safe-model"]
    assert evidence.model_id == "variant-v1"
    assert evidence.alias == "safe-model"
    assert not provider._model_management_lock.locked()
    assert not provider._inference_lock.locked()


@pytest.mark.parametrize(
    "field,spoofed",
    [
        ("model", "model\nother"),
        ("model", "e\u0301"),
        ("provider_id", "foundry-\u202elocal"),
        ("license_reference", "LICENSE\u2066spoof"),
        ("license_reference", "L" * 4097),
        ("model", "x" * 513),
        ("expected_model_id", "\ud800"),
    ],
)
def test_download_acquisition_text_spoofing_fails_before_sdk_and_locks(
    field: str, spoofed: str,
) -> None:
    manager_calls = 0

    def forbidden_manager() -> object:
        nonlocal manager_calls
        manager_calls += 1
        raise AssertionError("untrusted acquisition evidence reached SDK")

    provider = FoundryLocalProvider(
        default_model="safe-model", manager_factory=forbidden_manager,
    )
    permit = ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="safe-model",
        license_reference="REVIEWED-LICENSE",
        expected_model_id="variant-v1",
    )
    object.__setattr__(permit, field, spoofed)

    with pytest.raises(ValueError, match="download authorization"):
        asyncio.run(provider.download_model(permit))
    assert manager_calls == 0
    assert not provider._model_management_lock.locked()
    assert not provider._inference_lock.locked()
