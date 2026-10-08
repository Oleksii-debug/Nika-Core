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
