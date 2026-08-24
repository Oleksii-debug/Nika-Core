from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nika_core.media.errors import MediaError
from nika_core.media.process import SafeProcessRunner


def test_qa53_media_subprocess_stderr_never_echoes_api_key_canary(tmp_path: Path) -> None:
    canary = "QA53_CANARY_MEDIA_API_KEY_4C17"
    runner = SafeProcessRunner(max_output_bytes=4096)

    with pytest.raises(MediaError) as caught:
        runner.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('api_key=" + canary + "'); sys.exit(7)",
            ],
            cwd=tmp_path,
            timeout_seconds=10,
        )

    assert canary not in str(caught.value)


def test_qa53_media_subprocess_stderr_never_echoes_cookie_canary(tmp_path: Path) -> None:
    canary = "QA53_CANARY_MEDIA_COOKIE_91A2"
    runner = SafeProcessRunner(max_output_bytes=4096)

    with pytest.raises(MediaError) as caught:
        runner.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('Cookie: sessionid=" + canary + "'); sys.exit(9)",
            ],
            cwd=tmp_path,
            timeout_seconds=10,
        )

    assert canary not in str(caught.value)


def test_qa53_media_process_result_argv_never_exposes_password_canary(tmp_path: Path) -> None:
    canary = "QA53_CANARY_MEDIA_PASSWORD_8D63"
    runner = SafeProcessRunner(max_output_bytes=4096)

    result = runner.run(
        [sys.executable, "-c", "pass", "--password=" + canary],
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert canary not in " ".join(result.argv)
