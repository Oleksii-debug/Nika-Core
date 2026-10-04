from __future__ import annotations

from pathlib import Path
from typing import Any

from nika_core.ui.bridge import UIActionBridge


def web_asset_root() -> Path:
    return Path(__file__).with_name("web")


def index_path() -> Path:
    return web_asset_root() / "index.html"


def launch_windows_shell(bridge: UIActionBridge, *, title: str = "Nika Core") -> Any:
    """Launch the local HTML shell with EdgeChromium/WebView2.

    Import pywebview lazily so headless/core installations can import Nika without
    loading GUI dependencies. The renderer is explicit: M5 acceptance is WebView2,
    not an accidental legacy Windows web engine.

    Pass a local filesystem path rather than a ``file://`` URI. Current pywebview
    guidance discourages file URLs and resolves local paths through its supported
    local-content hosting path, which preserves the injected JS API bridge in the
    packaged WebView2 host.
    """

    # index.html can exist even when the JavaScript bridge or stylesheet was
    # lost after packaging. Fail before opening a blank/unusable NVDA window.
    for required in ("index.html", "app.js", "styles.css"):
        path = web_asset_root() / required
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"Відсутній або порожній ресурс інтерфейсу: {path}")

    import webview

    asset = index_path().resolve()
    window = webview.create_window(
        title,
        str(asset),
        js_api=bridge,
        width=1180,
        height=760,
        min_size=(760, 520),
    )
    webview.start(gui="edgechromium")
    return window
