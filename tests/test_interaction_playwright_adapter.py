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
    UnsupportedInteractionError,
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


class _FakeTextEntryLocator:
    def __init__(self, kind: str, value: str | None) -> None:
        self.kind = kind
        self.value = value
        self.input_value_calls = 0
        self.text_content_calls = 0

    def evaluate(self, _expression: str) -> str:
        return self.kind

    def input_value(self) -> str:
        self.input_value_calls += 1
        return "" if self.value is None else self.value

    def text_content(self) -> str | None:
        self.text_content_calls += 1
        return self.value


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


def test_download_broker_rejects_suggested_parent_path_before_save(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    broker = DownloadBroker(root)
    download = _FakeDownload("../outside.txt")

    with pytest.raises(UnsupportedInteractionError, match="safe filename"):
        broker.handle(download)

    assert download.destination is None
    assert broker.saved == []
    assert list(broker.approved_root.iterdir()) == []
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


def test_text_entry_value_distinguishes_input_textarea_and_contenteditable() -> None:
    input_locator = _FakeTextEntryLocator("input", "Український текст")
    textarea_locator = _FakeTextEntryLocator("textarea", "Рядок 1\nРядок 2")
    editor_locator = _FakeTextEntryLocator("contenteditable", "Редактор")

    assert PlaywrightInteractionAdapter._text_entry_value(input_locator) == "Український текст"
    assert PlaywrightInteractionAdapter._text_entry_value(textarea_locator) == "Рядок 1\nРядок 2"
    assert PlaywrightInteractionAdapter._text_entry_value(editor_locator) == "Редактор"
    assert input_locator.input_value_calls == 1
    assert textarea_locator.input_value_calls == 1
    assert editor_locator.input_value_calls == 0
    assert editor_locator.text_content_calls == 1


def test_text_entry_contenteditable_none_normalizes_to_empty_string() -> None:
    locator = _FakeTextEntryLocator("contenteditable", None)
    assert PlaywrightInteractionAdapter._text_entry_value(locator) == ""


def test_text_entry_value_rejects_unsupported_element_without_echoing_secret() -> None:
    locator = _FakeTextEntryLocator("unsupported", "NIKA_SECRET_CANARY")
    with pytest.raises(UnsupportedInteractionError) as exc_info:
        PlaywrightInteractionAdapter._text_entry_value(locator)
    assert "input, textarea, or contenteditable" in str(exc_info.value)
    assert "NIKA_SECRET_CANARY" not in str(exc_info.value)


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


def test_download_broker_refuses_to_overwrite_prior_artifact(tmp_path: Path) -> None:
    broker = DownloadBroker(tmp_path / "downloads")
    broker.handle(_FakeDownload("result.txt", "first complete artifact"))
    with pytest.raises(UnsupportedInteractionError, match="already exists"):
        broker.handle(_FakeDownload("result.txt", "unapproved replacement"))
    assert (broker.approved_root / "result.txt").read_text(encoding="utf-8") == (
        "first complete artifact"
    )
    assert broker.saved == [broker.approved_root / "result.txt"]


def test_download_broker_rejects_racing_destination_without_overwriting(
    tmp_path: Path,
) -> None:
    broker = DownloadBroker(tmp_path / "downloads")
    destination = broker.approved_root / "race.txt"

    class RacingDownload(_FakeDownload):
        def save_as(self, staging: str) -> None:
            destination.write_text("concurrent artifact", encoding="utf-8")
            super().save_as(staging)

    with pytest.raises(UnsupportedInteractionError, match="already exists"):
        broker.handle(RacingDownload("race.txt", "new download"))
    assert destination.read_text(encoding="utf-8") == "concurrent artifact"
    assert broker.saved == []
    assert list(broker.approved_root.glob(".nika-download-*.part")) == []


def test_failed_download_removes_partial_staging_and_does_not_publish(
    tmp_path: Path,
) -> None:
    broker = DownloadBroker(tmp_path / "downloads")

    class PartialFailure(_FakeDownload):
        def save_as(self, staging: str) -> None:
            Path(staging).write_text("partial bytes", encoding="utf-8")
            raise OSError("synthetic save failure")

    with pytest.raises(OSError, match="synthetic save failure"):
        broker.handle(PartialFailure("partial.txt"))
    assert not (broker.approved_root / "partial.txt").exists()
    assert list(broker.approved_root.glob(".nika-download-*.part")) == []
    assert broker.saved == []


def test_download_broker_does_not_follow_linked_destination(tmp_path: Path) -> None:
    broker = DownloadBroker(tmp_path / "downloads")
    victim = broker.approved_root / "victim.txt"
    victim.write_text("protected", encoding="utf-8")
    alias = broker.approved_root / "alias.txt"
    try:
        alias.symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted by this test host")
    with pytest.raises(UnsupportedInteractionError, match="already exists"):
        broker.handle(_FakeDownload("alias.txt", "unapproved replacement"))
    assert victim.read_text(encoding="utf-8") == "protected"
    assert broker.saved == []

@pytest.mark.parametrize(
    "suggested_filename",
    [
        "NUL.txt",
        "con",
        "COM1.log",
        "lPt9.data",
        "CONIN$",
        "report.txt:private-stream",
        "bad?.txt",
        "bad*.txt",
        "bad|name.txt",
        "nested/file.txt",
        r"nested\file.txt",
        r"..\parent\доказ.txt",
        "trailing.",
        "trailing ",
        "control\x01.txt",
        "delete\x7f.txt",
        "surrogate-\ud800.txt",
        ("a" * 256) + ".txt",
    ],
)
def test_download_broker_rejects_nonordinary_windows_component_before_save(
    tmp_path: Path,
    suggested_filename: str,
) -> None:
    broker = DownloadBroker(tmp_path / "downloads")
    download = _FakeDownload(suggested_filename, "must not be written")

    with pytest.raises(UnsupportedInteractionError, match="safe filename"):
        broker.handle(download)

    assert download.destination is None
    assert broker.saved == []
    assert list(broker.approved_root.iterdir()) == []


def test_download_broker_accepts_255_utf16_unit_unicode_component(
    tmp_path: Path,
) -> None:
    broker = DownloadBroker(tmp_path / "downloads")
    filename = ("а" * 251) + ".txt"
    download = _FakeDownload(filename, "boundary")

    broker.handle(download)

    assert broker.saved == [(broker.approved_root / filename).resolve()]
    assert broker.saved[0].read_text(encoding="utf-8") == "boundary"


def test_download_filename_subclass_is_rejected_without_behavior(
    tmp_path: Path,
) -> None:
    class BehavioralFilename(str):
        def replace(self, *args: object, **kwargs: object) -> str:
            raise AssertionError("filename subclass behavior must not execute")

    broker = DownloadBroker(tmp_path / "downloads")
    download = _FakeDownload("placeholder.txt")
    download.suggested_filename = BehavioralFilename("evidence.txt")

    with pytest.raises(UnsupportedInteractionError, match="safe filename"):
        broker.handle(download)

    assert download.destination is None
    assert broker.saved == []

