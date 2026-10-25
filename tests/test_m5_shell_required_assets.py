from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.ui import shell


def _assets(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text("<main>Nika</main>", encoding="utf-8")
    (root / "app.js").write_text("console.log('Nika')", encoding="utf-8")
    (root / "styles.css").write_text("body {}", encoding="utf-8")


@pytest.mark.parametrize("name", ("index.html", "app.js", "styles.css"))
@pytest.mark.parametrize("damage", ("missing", "empty"))
def test_missing_or_empty_runtime_asset_fails_before_window_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, damage: str
) -> None:
    _assets(tmp_path)
    target = tmp_path / name
    if damage == "missing":
        target.unlink()
    else:
        target.write_bytes(b"")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("WebView2 must not start with an incomplete UI")

    monkeypatch.setattr(shell, "web_asset_root", lambda: tmp_path)
    monkeypatch.setitem(
        sys.modules,
        "webview",
        SimpleNamespace(create_window=forbidden, start=forbidden),
    )
    with pytest.raises(FileNotFoundError, match=name.replace(".", r"\.")):
        shell.launch_windows_shell(object())  # type: ignore[arg-type]


def test_valid_runtime_assets_preserve_local_webview2_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _assets(tmp_path)
    monkeypatch.setattr(shell, "web_asset_root", lambda: tmp_path)
    calls: dict[str, object] = {}
    window = object()

    def create_window(title: str, url: str, **kwargs: object) -> object:
        calls.update(title=title, url=url, kwargs=kwargs)
        return window

    def start(*, gui: str) -> None:
        calls["gui"] = gui

    monkeypatch.setitem(
        sys.modules,
        "webview",
        SimpleNamespace(create_window=create_window, start=start),
    )
    assert shell.launch_windows_shell(object()) is window  # type: ignore[arg-type]
    assert calls["url"] == str((tmp_path / "index.html").resolve())
    assert calls["gui"] == "edgechromium"
