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
            _prove_ambiguity_and_scope(adapter)
            _prove_frame(adapter)
            _prove_dialog(adapter)
            _prove_download(adapter, root)
            _prove_navigation(adapter)
            _prove_popup(adapter)
        finally:
            session.close()


if __name__ == "__main__":
    main()
