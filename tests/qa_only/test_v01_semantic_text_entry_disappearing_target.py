"""QA_ONLY oracle for V0.1 semantic text entry disappearing-target safety.

This file is an exact-parent adversarial test for production PR #566. It must never
be merged as production. A failure routes to the current interaction owner.
"""

from __future__ import annotations

from typing import Any

import pytest

from nika_core.interaction import (
    BrowserSession,
    ControlNode,
    InteractionAction,
    PlaywrightInteractionAdapter,
)
from nika_core.interaction.domain import InteractionError


class _DisappearingTextLocator:
    """Model a semantic target that vanishes after editability but before fill."""

    def __init__(self, playwright_error: type[BaseException], secret: str) -> None:
        self._playwright_error = playwright_error
        self._secret = secret
        self.fill_calls = 0

    def evaluate(self, _expression: str) -> str:
        return "input"

    def is_editable(self) -> bool:
        return True

    def fill(self, value: str) -> None:
        self.fill_calls += 1
        assert value == self._secret
        raise self._playwright_error(
            "Locator.fill: target detached; "
            f"css=#raw-secret-selector <input value='{self._secret}'>"
        )

    def count(self) -> int:
        # Gives a repaired adapter a deterministic way to re-check the semantic target.
        return 0


def test_set_value_target_disappearing_mid_action_is_typed_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    secret = "NIKA_SECRET_CANARY_TEXT_ENTRY_DISAPPEAR_4c81"
    locator = _DisappearingTextLocator(sync_api.Error, secret)
    adapter = PlaywrightInteractionAdapter(
        session=BrowserSession(download_root=tmp_path),
        page_id="qa-only-page",
    )
    node = ControlNode(
        node_id="pw:qa-only-text-entry",
        role="textbox",
        name="Секретне поле",
    )

    monkeypatch.setattr(
        PlaywrightInteractionAdapter,
        "_locator_for_node",
        lambda _self, _node: locator,
    )

    caught: BaseException | None = None
    try:
        adapter.act(node, InteractionAction.SET_VALUE, secret)
    except BaseException as exc:  # noqa: BLE001 - QA oracle inspects the actual boundary type.
        caught = exc

    assert caught is not None, "disappearing target must fail closed"
    assert isinstance(caught, InteractionError), (
        "raw Playwright failure escaped the Nika interaction boundary: "
        f"{type(caught).__module__}.{type(caught).__name__}"
    )
    message = str(caught)
    assert secret not in message
    assert "<input" not in message
    assert "css=#raw-secret-selector" not in message
    assert locator.fill_calls == 1
