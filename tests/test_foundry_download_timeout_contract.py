from __future__ import annotations

import asyncio
import math

import pytest

from nika_core.model_gateway.contracts import ModelDownloadAuthorization
from nika_core.model_gateway.foundry_local import FoundryLocalProvider


def _authorization() -> ModelDownloadAuthorization:
    return ModelDownloadAuthorization(
        provider_id="foundry-local",
        model="fixture-model",
        license_reference="MODEL-LICENSE-REVIEW-FIXTURE",
    )


def _provider_with_forbidden_manager() -> tuple[FoundryLocalProvider, list[str]]:
    calls: list[str] = []

    def manager_factory() -> object:
        calls.append("manager")
        raise AssertionError("manager must not be reached for an invalid timeout")

    return (
        FoundryLocalProvider(
            default_model="fixture-model",
            manager_factory=manager_factory,
        ),
        calls,
    )


@pytest.mark.parametrize("value", (True, False, "30", None))
def test_download_timeout_rejects_non_numeric_or_boolean_before_manager(
    value: object,
) -> None:
    provider, calls = _provider_with_forbidden_manager()

    with pytest.raises(TypeError, match="timeout_seconds must be numeric"):
        asyncio.run(
            provider.download_model(
                _authorization(),
                timeout_seconds=value,  # type: ignore[arg-type]
            )
        )

    assert calls == []


@pytest.mark.parametrize(
    "value",
    (
        0,
        -1,
        math.nan,
        math.inf,
        -math.inf,
        10**10000,
    ),
)
def test_download_timeout_rejects_non_finite_or_non_positive_before_manager(
    value: float | int,
) -> None:
    provider, calls = _provider_with_forbidden_manager()

    with pytest.raises(ValueError, match="finite and greater than zero"):
        asyncio.run(
            provider.download_model(
                _authorization(),
                timeout_seconds=value,
            )
        )

    assert calls == []
