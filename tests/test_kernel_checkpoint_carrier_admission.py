from __future__ import annotations

import pytest

from nika_core.kernel import checkpoint as checkpoint_module


class _TextSubclass(str):
    pass


class _IntegerSubclass(int):
    pass


class _DeceptiveList(list):
    def __len__(self) -> int:
        pytest.fail("non-canonical list length was inspected")

    def __iter__(self):
        pytest.fail("non-canonical list iterator was invoked")


class _DeceptiveDict(dict):
    def __len__(self) -> int:
        pytest.fail("non-canonical dict length was inspected")

    def items(self):
        pytest.fail("non-canonical dict items were inspected")


def test_checkpoint_rejects_noncanonical_json_carriers_before_encoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: tuple[dict[str, object], ...] = (
        {"value": _TextSubclass("value")},
        {"value": _IntegerSubclass(7)},
        {"value": _DeceptiveList([1, 2, 3])},
        _DeceptiveDict({"value": 1}),
        {_TextSubclass("key"): 1},
    )

    def unexpected_encoder(*_args: object, **_kwargs: object) -> str:
        pytest.fail("non-canonical payload reached json.dumps")

    monkeypatch.setattr(checkpoint_module.json, "dumps", unexpected_encoder)

    for payload in payloads:
        with pytest.raises(ValueError, match="canonical built-in JSON types"):
            checkpoint_module._canonical_json(payload)


def test_checkpoint_keeps_exact_builtin_tuple_normalization() -> None:
    body = checkpoint_module._canonical_json(
        {"items": (1, "text", True, None), "nested": {"value": 2.5}}
    )

    assert body == '{"items":[1,"text",true,null],"nested":{"value":2.5}}'
