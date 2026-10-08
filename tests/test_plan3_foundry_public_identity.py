from __future__ import annotations

import pytest

from nika_core.model_gateway.foundry_local import FoundryLocalProvider


@pytest.mark.parametrize(
    "invalid",
    ["", " model", "model ", "model\nforged", "model\u202elive",
     "e\u0301", "\ud800", "x" * 513, 19, ["model"]],
)
def test_default_model_identity_is_canonical_before_sdk(invalid: object) -> None:
    with pytest.raises(ValueError, match="default_model"):
        FoundryLocalProvider(default_model=invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "invalid",
    ["", " variant", "variant ", "v1\u2066spoof", "e\u0301",
     "\ud800", "x" * 513, 19],
)
def test_provider_model_pin_identity_is_canonical_before_sdk(invalid: object) -> None:
    with pytest.raises(ValueError, match="expected_model_id"):
        FoundryLocalProvider(
            default_model="safe-model",
            expected_model_id=invalid,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "invalid",
    ["", " forged", "model\nforged", "model\u202elive",
     "e\u0301", "\ud800", "x" * 513, 19],
)
def test_inspection_explicit_invalid_alias_cannot_fall_back_to_default(
    invalid: object,
) -> None:
    sdk_calls: list[str] = []

    def forbidden_manager() -> object:
        sdk_calls.append("manager")
        raise AssertionError("invalid model identity reached native SDK")

    provider = FoundryLocalProvider(
        default_model="safe-model", manager_factory=forbidden_manager,
    )
    with pytest.raises(ValueError, match="model_alias"):
        provider.inspect_model(invalid)  # type: ignore[arg-type]
    assert sdk_calls == []
