from __future__ import annotations

import tempfile
from pathlib import Path

from nika_core.interaction import (
    AmbiguousTargetError,
    BrowserSession,
    ControlLocator,
    DialogRule,
    FrameScope,
    InteractionAction,
    PlaywrightInteractionAdapter,
    StaleSnapshotError,
    UnsupportedInteractionError,
    resolve_strict,
)


def _exercise_form_and_spa(adapter: PlaywrightInteractionAdapter) -> None:
    adapter.load_inline_fixture(
        "<main><h1>Доступне керування</h1>"
        "<label for='problem'>Опис проблеми</label>"
        "<input id='problem'>"
        "<label><input type='checkbox'> Увімкнути перевірку</label>"
        "<button onclick=\"this.textContent='Готово'\">Виконати</button></main>"
    )
    snapshot = adapter.observe()
    assert resolve_strict(snapshot, ControlLocator(role="heading", name="Доступне керування"))

    before = adapter.observe()
    textbox = resolve_strict(before, ControlLocator(role="textbox", name="Опис проблеми"))
    adapter.focus(textbox)
    assert adapter.capture_focus() == textbox.node_id
    adapter.act(textbox, InteractionAction.SET_VALUE, "Перевірка UTF-8")
    after = adapter.observe()
    assert adapter.verify(before, after, textbox, InteractionAction.SET_VALUE, "Перевірка UTF-8")
    assert resolve_strict(after, ControlLocator(role="textbox", label="Опис проблеми")).value == (
        "Перевірка UTF-8"
    )

    before = adapter.observe()
    checkbox = resolve_strict(
        before,
        ControlLocator(role="checkbox", name="Увімкнути перевірку"),
    )
    adapter.focus(checkbox)
    adapter.act(checkbox, InteractionAction.TOGGLE, None)
    after = adapter.observe()
    assert adapter.verify(before, after, checkbox, InteractionAction.TOGGLE, None)

    before = adapter.observe()
    button = resolve_strict(before, ControlLocator(role="button", name="Виконати"))
    adapter.focus(button)
    adapter.act(button, InteractionAction.INVOKE, None)
    after = adapter.observe()
    assert adapter.verify(before, after, button, InteractionAction.INVOKE, None)
    assert resolve_strict(after, ControlLocator(role="button", name="Готово"))


def _set_and_verify(
    adapter: PlaywrightInteractionAdapter,
    locator: ControlLocator,
    value: str,
) -> None:
    before = adapter.observe()
    node = resolve_strict(before, locator)
    adapter.focus(node)
    adapter.act(node, InteractionAction.SET_VALUE, value)
    after = adapter.observe()
    assert adapter.verify(before, after, node, InteractionAction.SET_VALUE, value)


def _prove_text_entry(adapter: PlaywrightInteractionAdapter) -> None:
    secret_canary = "NIKA_SECRET_CANARY_TEXT_ENTRY_71f04d"
    adapter.load_inline_fixture(
        "<main><h1>Semantic text entry</h1>"
        "<label for='short'>Коротке поле</label>"
        "<input id='short' value='Старий вміст'>"
        "<label for='notes'>Багаторядкові нотатки</label>"
        "<textarea id='notes'>Стара нотатка</textarea>"
        "<div role='textbox' aria-label='Редактор' contenteditable='true'>Стара чернетка</div>"
        "<label for='disabled'>Заблоковане поле</label>"
        "<input id='disabled' value='Не змінювати' disabled>"
        "<label for='readonly'>Лише читання</label>"
        "<textarea id='readonly' readonly>Не змінювати</textarea>"
        "</main>"
    )

    # SET_VALUE is explicit replacement/fill semantics, not append semantics.
    replacement = "Український текст: їжак, ґанок, єдність."
    _set_and_verify(adapter, ControlLocator(role="textbox", label="Коротке поле"), replacement)
    snapshot = adapter.observe()
    short = resolve_strict(snapshot, ControlLocator(role="textbox", name="Коротке поле"))
    assert short.value == replacement
    assert "Старий вміст" not in (short.value or "")

    # Empty text is a deliberate clear operation.
    _set_and_verify(adapter, ControlLocator(label="Коротке поле"), "")
    snapshot = adapter.observe()
    assert resolve_strict(snapshot, ControlLocator(role="textbox", name="Коротке поле")).value in {
        "",
        None,
    }

    multiline = "Перший рядок\nДругий рядок\nТретій рядок"
    _set_and_verify(
        adapter,
        ControlLocator(role="textbox", name="Багаторядкові нотатки"),
        multiline,
    )

    # Playwright fill supports contenteditable; verification must not call input_value() for it.
    editor_value = "Редактор: український Unicode — готово"
    _set_and_verify(adapter, ControlLocator(role="textbox", name="Редактор"), editor_value)

    # Non-editable failures are typed, bounded, and never include attempted secret content.
    for locator in (
        ControlLocator(role="textbox", name="Заблоковане поле"),
        ControlLocator(role="textbox", name="Лише читання"),
    ):
        before = adapter.observe()
        node = resolve_strict(before, locator)
        try:
            adapter.act(node, InteractionAction.SET_VALUE, secret_canary)
        except UnsupportedInteractionError as exc:
            assert "not editable" in str(exc)
            assert secret_canary not in str(exc)
        else:  # pragma: no cover - physical proof assertion
            raise AssertionError("non-editable semantic text target accepted SET_VALUE")
        after = adapter.observe()
        assert secret_canary not in str(resolve_strict(after, locator).value)


def _prove_ambiguity_and_scope(adapter: PlaywrightInteractionAdapter) -> None:
    adapter.load_inline_fixture(
        "<main>"
        "<section aria-label='Primary'><button>Однаково</button></section>"
        "<section aria-label='Secondary'>"
        "<button onclick=\"this.textContent='Secondary done'\">Однаково</button>"
        "</section></main>"
    )
    snapshot = adapter.observe()
    try:
        resolve_strict(snapshot, ControlLocator(role="button", name="Однаково"))
    except AmbiguousTargetError:
        pass
    else:  # pragma: no cover - physical proof assertion
        raise AssertionError("duplicate semantic targets did not fail closed")

    secondary = resolve_strict(snapshot, ControlLocator(role="region", name="Secondary"))
    button = resolve_strict(
        snapshot,
        ControlLocator(
            role="button",
            name="Однаково",
            ancestor_node_id=secondary.node_id,
        ),
    )
    adapter.focus(button)
    adapter.act(button, InteractionAction.INVOKE, None)
    after = adapter.observe()
    assert adapter.verify(snapshot, after, button, InteractionAction.INVOKE, None)
    assert resolve_strict(after, ControlLocator(role="button", name="Secondary done"))


def _prove_frame(adapter: PlaywrightInteractionAdapter) -> None:
    adapter.load_inline_fixture(
        "<main><iframe name='details' "
        "srcdoc=\"<main><button>Кнопка у фреймі</button></main>\"></iframe></main>"
    )
    assert adapter.session.registry is not None
    adapter.session.registry.get(adapter.page_id).page.wait_for_load_state("load")
    framed = PlaywrightInteractionAdapter(
        session=adapter.session,
        page_id=adapter.page_id,
        frame_scope=FrameScope(name="details"),
    )
    snapshot = framed.observe()
    assert resolve_strict(snapshot, ControlLocator(role="button", name="Кнопка у фреймі"))


def _prove_stale_frame_and_dom_identity(adapter: PlaywrightInteractionAdapter) -> None:
    """Physical Chromium proof for the #592/#593 stale-target families."""
    assert adapter.session.registry is not None
    page = adapter.session.registry.get(adapter.page_id).page

    # Same semantic role/name on a replacement DOM element must not inherit old authority.
    adapter.load_inline_fixture(
        "<main><button id='stable' aria-label='Стабільна дія' "
        "onclick=\"window.__nikaDomEffects=(window.__nikaDomEffects||0)+1\">"
        "Стабільна дія</button></main>"
    )
    page.evaluate("() => { window.__nikaDomEffects = 0; }")
    stale_snapshot = adapter.observe()
    stale_node = resolve_strict(
        stale_snapshot,
        ControlLocator(role="button", name="Стабільна дія"),
    )
    page.evaluate(
        """
        () => {
          const oldNode = document.getElementById('stable');
          const replacement = oldNode.cloneNode(true);
          oldNode.replaceWith(replacement);
        }
        """
    )
    try:
        adapter.act(stale_node, InteractionAction.INVOKE, None)
    except StaleSnapshotError:
        pass
    else:  # pragma: no cover - physical proof assertion
        raise AssertionError("same-semantics replacement reused stale DOM authority")
    assert page.evaluate("() => window.__nikaDomEffects") == 0

    fresh_node = resolve_strict(
        adapter.observe(),
        ControlLocator(role="button", name="Стабільна дія"),
    )
    adapter.act(fresh_node, InteractionAction.INVOKE, None)
    assert page.evaluate("() => window.__nikaDomEffects") == 1

    # Unscoped identical semantics across root and child frame must be ambiguous.
    adapter.load_inline_fixture(
        "<main><button>Спільна дія</button>"
        "<iframe name='duplicate' "
        "srcdoc=\"<main><button>Спільна дія</button></main>\"></iframe></main>"
    )
    duplicate_frame = page.frame(name="duplicate")
    assert duplicate_frame is not None
    duplicate_frame.get_by_role("button", name="Спільна дія", exact=True).wait_for()
    try:
        resolve_strict(adapter.observe(), ControlLocator(role="button", name="Спільна дія"))
    except AmbiguousTargetError:
        pass
    else:  # pragma: no cover - physical proof assertion
        raise AssertionError("root/child identical semantics did not fail ambiguous")

    # Replacing an iframe with the same semantic target invalidates the old child-frame identity.
    adapter.load_inline_fixture(
        "<main><iframe name='stale-frame' "
        "srcdoc=\"<main><button onclick='parent.__nikaFrameEffects=(parent.__nikaFrameEffects||0)+1'>"
        "Frame action</button></main>\"></iframe></main>"
    )
    page.evaluate("() => { window.__nikaFrameEffects = 0; }")
    first_frame = page.frame(name="stale-frame")
    assert first_frame is not None
    first_frame.get_by_role("button", name="Frame action", exact=True).wait_for()
    framed = PlaywrightInteractionAdapter(
        session=adapter.session,
        page_id=adapter.page_id,
        frame_scope=FrameScope(name="stale-frame"),
    )
    old_node = resolve_strict(
        framed.observe(),
        ControlLocator(role="button", name="Frame action"),
    )
    page.evaluate(
        """
        () => {
          const oldFrame = document.querySelector("iframe[name='stale-frame']");
          const replacement = document.createElement('iframe');
          replacement.name = 'stale-frame';
          replacement.srcdoc = "<main><button onclick='parent.__nikaFrameEffects=(parent.__nikaFrameEffects||0)+1'>Frame action</button></main>";
          oldFrame.replaceWith(replacement);
        }
        """
    )
    replacement_frame = page.frame(name="stale-frame")
    assert replacement_frame is not None
    replacement_frame.get_by_role("button", name="Frame action", exact=True).wait_for()
    try:
        framed.act(old_node, InteractionAction.INVOKE, None)
    except StaleSnapshotError:
        pass
    else:  # pragma: no cover - physical proof assertion
        raise AssertionError("replacement frame reused stale child-frame authority")
    assert page.evaluate("() => window.__nikaFrameEffects") == 0

    fresh_frame_node = resolve_strict(
        framed.observe(),
        ControlLocator(role="button", name="Frame action"),
    )
    framed.act(fresh_frame_node, InteractionAction.INVOKE, None)
    assert page.evaluate("() => window.__nikaFrameEffects") == 1


def _prove_dialog(adapter: PlaywrightInteractionAdapter) -> None:
    adapter.load_inline_fixture(
        "<main><button onclick=\"alert('Підтвердити')\">Діалог</button></main>"
    )
    adapter.session.dialogs.expect(DialogRule("alert", "Підтвердити", "dismiss"))
    snapshot = adapter.observe()
    button = resolve_strict(snapshot, ControlLocator(role="button", name="Діалог"))
    adapter.focus(button)
    adapter.act(button, InteractionAction.INVOKE, None)
    assert adapter.session.dialogs.events[-1] == ("alert", "Підтвердити", "dismiss")


def _prove_navigation(adapter: PlaywrightInteractionAdapter) -> None:
    adapter.load_inline_fixture("<main><a href='about:blank?nika-next'>Перейти</a></main>")
    before = adapter.observe()
    link = resolve_strict(before, ControlLocator(role="link", name="Перейти"))
    adapter.focus(link)
    adapter.act(link, InteractionAction.INVOKE, None)
    after = adapter.observe()
    assert adapter.verify(before, after, link, InteractionAction.INVOKE, None)
    assert after.generation != before.generation


def _prove_popup(adapter: PlaywrightInteractionAdapter) -> None:
    adapter.load_inline_fixture(
        "<main><button onclick=\"window.open('about:blank', '_blank')\">Нова вкладка</button></main>"
    )
    before = adapter.observe()
    button = resolve_strict(before, ControlLocator(role="button", name="Нова вкладка"))
    adapter.focus(button)
    adapter.act(button, InteractionAction.INVOKE, None)
    after = adapter.observe()
    assert adapter.verify(before, after, button, InteractionAction.INVOKE, None)
    assert len(adapter.session.page_ids()) == 2


def _prove_download(adapter: PlaywrightInteractionAdapter, root: Path) -> None:
    adapter.load_inline_fixture(
        "<main><a download='evidence.txt' href='data:text/plain,semantic-proof'>"
        "Завантажити доказ</a></main>"
    )
    before = adapter.observe()
    link = resolve_strict(before, ControlLocator(role="link", name="Завантажити доказ"))
    adapter.focus(link)
    adapter.act(link, InteractionAction.INVOKE, None)
    after = adapter.observe()
    assert adapter.verify(before, after, link, InteractionAction.INVOKE, None)
    saved = root / "evidence.txt"
    assert saved.read_text(encoding="utf-8") == "semantic-proof"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="nika-dev04-playwright-") as temp_dir:
        root = Path(temp_dir).resolve()
        session = BrowserSession(download_root=root).start()
        try:
            page_id = session.new_page()
            adapter = PlaywrightInteractionAdapter(session=session, page_id=page_id)
            _exercise_form_and_spa(adapter)
            _prove_text_entry(adapter)
            _prove_ambiguity_and_scope(adapter)
            _prove_frame(adapter)
            _prove_stale_frame_and_dom_identity(adapter)
            _prove_dialog(adapter)
            _prove_download(adapter, root)
            _prove_navigation(adapter)
            _prove_popup(adapter)
        finally:
            session.close()


if __name__ == "__main__":
    main()
