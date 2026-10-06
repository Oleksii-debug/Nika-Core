from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Any

from nika_core.ui.bridge import UIActionBridge


def web_asset_root() -> Path:
    return Path(__file__).with_name("web")


def index_path() -> Path:
    return web_asset_root() / "index.html"


def preflight_windows_shell() -> None:
    """Validate packaged UI inputs before startup recovery may produce effects."""

    for required in ("index.html", "app.js", "styles.css"):
        path = web_asset_root() / required
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"Відсутній або порожній ресурс інтерфейсу: {path}")
    import_module("webview")


def launch_windows_shell(
    bridge: UIActionBridge,
    *,
    title: str = "Nika Core",
    on_gui_started: Callable[[], None] | None = None,
) -> Any:
    """Launch the local HTML shell with EdgeChromium/WebView2.

    Import pywebview lazily so headless/core installations can import Nika without
    loading GUI dependencies. The renderer is explicit: M5 acceptance is WebView2,
    not an accidental legacy Windows web engine.

    Pass a local filesystem path rather than a ``file://`` URI. Current pywebview
    guidance discourages file URLs and resolves local paths through its supported
    local-content hosting path, which preserves the injected JS API bridge in the
    packaged WebView2 host.
    """

    preflight_windows_shell()
    webview = import_module("webview")

    asset = index_path().resolve()
    window = webview.create_window(
        title,
        str(asset),
        js_api=bridge,
        width=1180,
        height=760,
        min_size=(760, 520),
        hidden=on_gui_started is not None,
    )
    callback_failure: list[Exception] = []

    def finish_startup() -> None:
        try:
            if not window.events.loaded.wait(20):
                raise RuntimeError("packaged WebView did not reach loaded state")
            if on_gui_started is not None:
                on_gui_started()
            window.show()
        except Exception as exc:  # noqa: BLE001 - relay worker-thread startup failure
            callback_failure.append(exc)
            # The host stays hidden until recovery succeeds, so the shown
            # event cannot be a destruction precondition on this path.
            # Always close it so webview.start() can return the failure.
            window.destroy()

    if on_gui_started is None:
        webview.start(gui="edgechromium")
    else:
        webview.start(finish_startup, gui="edgechromium")
    if callback_failure:
        raise callback_failure[0]
    return window
