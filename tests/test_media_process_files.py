from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from nika_core.media import Page
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.files import promote_partial_file
from nika_core.media.hashing import sha256_bytes
from nika_core.media.privacy import redact_text
from nika_core.media.process import SafeProcessRunner


def test_public_page_contract_is_available() -> None:
    page = Page(page_number=1, text="text", source_sha256="a" * 64)
    assert page.page_number == 1


def test_safe_process_runner_supports_explicit_cancellation(tmp_path: Path) -> None:
    cancel = threading.Event()

    def trigger() -> None:
        time.sleep(0.05)
        cancel.set()

    thread = threading.Thread(target=trigger, daemon=True)
    thread.start()
    with pytest.raises(MediaError) as caught:
        SafeProcessRunner().run(
            (sys.executable, "-c", "import time; time.sleep(10)"),
            cwd=tmp_path,
            timeout_seconds=5,
            cancel_event=cancel,
        )
    thread.join(timeout=1)
    assert caught.value.code == MediaErrorCode.PROCESS_CANCELLED


def test_safe_process_runner_terminates_when_watched_file_exceeds_limit(tmp_path: Path) -> None:
    watched = tmp_path / "download.partial.part"
    code = (
        "from pathlib import Path; import time; "
        "Path('download.partial.part').write_bytes(b'x' * 128); time.sleep(10)"
    )
    with pytest.raises(MediaError) as caught:
        SafeProcessRunner().run(
            (sys.executable, "-c", code),
            cwd=tmp_path,
            timeout_seconds=5,
            watched_paths=(watched,),
            max_watched_file_bytes=16,
        )
    assert caught.value.code == MediaErrorCode.SOURCE_TOO_LARGE
    assert watched.exists()
    assert watched.stat().st_size == 128


def test_safe_process_runner_rejects_watch_path_outside_cwd(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.partial"
    with pytest.raises(MediaError) as caught:
        SafeProcessRunner().run(
            (sys.executable, "-c", "print('never started')"),
            cwd=tmp_path,
            timeout_seconds=5,
            watched_paths=(outside,),
            max_watched_file_bytes=16,
        )
    assert caught.value.code == MediaErrorCode.PATH_ESCAPE


@pytest.mark.parametrize(
    ("stderr_text", "canary"),
    (
        ("api_key=QA53_CANARY_MEDIA_API_KEY_4C17", "QA53_CANARY_MEDIA_API_KEY_4C17"),
        ("Cookie: sessionid=QA53_CANARY_MEDIA_COOKIE_91A2", "QA53_CANARY_MEDIA_COOKIE_91A2"),
    ),
)
def test_safe_process_runner_redacts_secret_stderr_canaries(
    tmp_path: Path,
    stderr_text: str,
    canary: str,
) -> None:
    code = f"import sys; sys.stderr.write({stderr_text!r}); sys.exit(7)"
    with pytest.raises(MediaError) as caught:
        SafeProcessRunner(max_output_bytes=4096).run(
            (sys.executable, "-c", code),
            cwd=tmp_path,
            timeout_seconds=10,
        )
    assert canary not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)


def test_safe_process_runner_redacts_sensitive_public_argv_evidence(tmp_path: Path) -> None:
    password_canary = "QA53_CANARY_MEDIA_PASSWORD_8D63"
    token_canary = "QA53_CANARY_MEDIA_TOKEN_SPLIT_14B7"
    profile_canary = "QA53_CANARY_BROWSER_PROFILE_6E11"
    result = SafeProcessRunner(max_output_bytes=4096).run(
        (
            sys.executable,
            "-c",
            "pass",
            f"--password={password_canary}",
            "--token",
            token_canary,
            "--cookies-from-browser",
            profile_canary,
        ),
        cwd=tmp_path,
        timeout_seconds=10,
    )
    public_argv = " ".join(result.argv)
    assert password_canary not in public_argv
    assert token_canary not in public_argv
    assert profile_canary not in public_argv
    assert public_argv.count("[REDACTED]") == 3


def test_redact_text_preserves_nonsecret_token_count_metadata() -> None:
    assert redact_text("token_count=17 cookieCount=2") == "token_count=17 cookieCount=2"


def test_partial_promotion_validates_checksum_and_renames_atomically(tmp_path: Path) -> None:
    partial = tmp_path / "audio.wav.partial"
    final = tmp_path / "audio.wav"
    payload = b"validated media bytes"
    partial.write_bytes(payload)
    result = promote_partial_file(
        partial,
        final,
        allowed_root=tmp_path,
        expected_sha256=sha256_bytes(payload),
        max_bytes=1024,
    )
    assert result.path == final
    assert result.sha256 == sha256_bytes(payload)
    assert final.read_bytes() == payload
    assert not partial.exists()


def test_partial_promotion_keeps_evidence_on_checksum_failure(tmp_path: Path) -> None:
    partial = tmp_path / "audio.wav.partial"
    final = tmp_path / "audio.wav"
    partial.write_bytes(b"bytes")
    with pytest.raises(MediaError) as caught:
        promote_partial_file(
            partial,
            final,
            allowed_root=tmp_path,
            expected_sha256="0" * 64,
        )
    assert caught.value.code == MediaErrorCode.CHECKSUM_MISMATCH
    assert partial.exists()
    assert not final.exists()


def test_partial_promotion_never_overwrites_existing_output(tmp_path: Path) -> None:
    partial = tmp_path / "audio.wav.partial"
    final = tmp_path / "audio.wav"
    partial.write_bytes(b"new")
    final.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        promote_partial_file(partial, final, allowed_root=tmp_path)
    assert final.read_bytes() == b"original"
    assert partial.read_bytes() == b"new"
