from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.interaction import (
    BrowserSession,
    DialogBroker,
    DialogRule,
    DownloadBroker,
    FrameScope,
    PlaywrightInteractionAdapter,
)


class _FakeDialog:
    def __init__(self, dialog_type: str, message: str) -> None:
        self.type = dialog_type
        self.message = message
        self.accepted: str | None = None
        self.dismissed = False

    def accept(self, prompt_text: str | None = None) -> None:
        self.accepted = "" if prompt_text is None else prompt_text

    def dismiss(self) -> None:
        self.dismissed = True


class _FakeDownload:
    def __init__(self, suggested_filename: str, payload: str = "evidence") -> None:
        self.suggested_filename = suggested_filename
        self.payload = payload
        self.destination: Path | None = None

    def save_as(self, destination: str) -> None:
        self.destination = Path(destination)
        self.destination.write_text(self.payload, encoding="utf-8")


def test_frame_scope_requires_exactly_one_identity() -> None:
    with pytest.raises(ValueError):
        FrameScope()
    with pytest.raises(ValueError):
        FrameScope(name="frame", url="https://example.test/frame")
    assert FrameScope(name="details").name == "details"
    assert FrameScope(url="https://example.test/frame").url == "https://example.test/frame"


def test_dialog_broker_dismisses_unexpected_dialog() -> None:
    broker = DialogBroker()
    dialog = _FakeDialog("confirm", "Delete everything?")
    broker.handle(dialog)
    assert dialog.dismissed is True
    assert dialog.accepted is None
    assert broker.events == [("confirm", "Delete everything?", "unexpected-dismiss")]


def test_dialog_broker_accepts_only_exact_expected_dialog() -> None:
    broker = DialogBroker()
    broker.expect(DialogRule("prompt", "Name", "accept", "Nika"))
    wrong = _FakeDialog("prompt", "Other")
    broker.handle(wrong)
    assert wrong.dismissed is True

    expected = _FakeDialog("prompt", "Name")
    broker.handle(expected)
    assert expected.accepted == "Nika"
    assert expected.dismissed is False


def test_download_broker_persists_under_approved_root(tmp_path: Path) -> None:
    root = tmp_path / "approved artifacts"
    broker = DownloadBroker(root)
    download = _FakeDownload("доказ.txt", "UTF-8 доказ")
    broker.handle(download)
    assert broker.saved == [(root / "доказ.txt").resolve()]
    assert broker.saved[0].read_text(encoding="utf-8") == "UTF-8 доказ"


def test_download_broker_never_uses_suggested_parent_path(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    broker = DownloadBroker(root)
    download = _FakeDownload("../outside.txt")
    broker.handle(download)
    assert broker.saved == [(root / "outside.txt").resolve()]
    assert not (tmp_path / "outside.txt").exists()


def test_aria_name_decoder_preserves_ukrainian_and_escapes() -> None:
    assert PlaywrightInteractionAdapter._decode_aria_name("Доступне керування") == "Доступне керування"
    assert PlaywrightInteractionAdapter._decode_aria_name(r'Кнопка \"Раз\"') == 'Кнопка "Раз"'
    assert PlaywrightInteractionAdapter._decode_aria_name(None) == ""


def test_aria_scalar_decoder_preserves_values_and_empty_children() -> None:
    assert PlaywrightInteractionAdapter._decode_scalar("Перевірка UTF-8") == "Перевірка UTF-8"
    assert PlaywrightInteractionAdapter._decode_scalar('"quoted value"') == "quoted value"
    assert PlaywrightInteractionAdapter._decode_scalar("") is None
    assert PlaywrightInteractionAdapter._decode_scalar(None) is None


def test_semantic_revision_tracks_accessibility_state_but_ignores_focus_marker() -> None:
    baseline = '- button "Save"\n- textbox "Name": Oleksii'
    changed = '- button "Save"\n- textbox "Name": Олексій'
    assert PlaywrightInteractionAdapter._semantic_revision(baseline, 1) != (
        PlaywrightInteractionAdapter._semantic_revision(changed, 1)
    )
    assert PlaywrightInteractionAdapter._semantic_revision('- button "Save" [focused]', 1) == (
        PlaywrightInteractionAdapter._semantic_revision('- button "Save" ', 1)
    )
    assert PlaywrightInteractionAdapter._semantic_revision(baseline, 1) != (
        PlaywrightInteractionAdapter._semantic_revision(baseline, 2)
    )


def test_adapter_exposes_no_direct_navigation_bypass(tmp_path: Path) -> None:
    adapter = PlaywrightInteractionAdapter(
        session=BrowserSession(download_root=tmp_path),
        page_id="not-started",
    )
    assert not hasattr(adapter, "navigate")


def test_browser_session_is_ephemeral_by_contract(tmp_path: Path) -> None:
    session = BrowserSession(download_root=tmp_path / "downloads")
    assert session.context is None
    assert session.registry is None
    assert session.page_ids() == ()
    assert session.downloads.approved_root == (tmp_path / "downloads").resolve()
