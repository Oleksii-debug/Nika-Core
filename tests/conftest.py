from __future__ import annotations

import pytest


def pytest_make_parametrize_id(
    config: pytest.Config,
    val: object,
    argname: str,
) -> str | None:
    """Keep pathological integer validation cases collectable without weakening them."""
    del config
    if argname == "timeout_seconds" and isinstance(val, int) and val.bit_length() > 16_384:
        return "huge-int"
    return None
