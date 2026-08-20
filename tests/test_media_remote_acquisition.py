from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

from nika_core.media.acquisition import RemoteAcquisitionPolicy, YtDlpRemoteAcquirer
from nika_core.media.contracts import AssetKind
from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.process import ProcessResult


class WritingRunner:
    def __init__(self, payload: bytes = b"remote-media") -> None:
        self.payload = payload
        self.calls: list[tuple[tuple[str, ...], Path, float, threading.Event | None]] = []

    def run(
        self,
        argv,
        *,
        cwd: Path,
        timeout_seconds: float,
        env=None,
        cancel_event: threading.Event | None = None,
    ) -> ProcessResult:
        normalized = tuple(argv)
        self.calls.append((normalized, cwd, timeout_seconds, cancel_event))
        output = Path(normalized[normalized.index("-o") + 1])
        output.write_bytes(self.payload)
        return ProcessResult(normalized, 0, b"", b"", 0.01)


class FailingRunner:
    def __init__(self, error: MediaError) -> None:
        self.error = error
        self.calls = 0

    def run(self, argv, *, cwd, timeout_seconds, env=None, cancel_event=None):
        self.calls += 1
        output = Path(argv[argv.index("-o") + 1])
        Path(f"{output}.part").write_bytes(b"resume-me")
        raise self.error


def test_remote_acquisition_promotes_checksum_bound_immutable_asset(tmp_path: Path) -> None:
    runner = WritingRunner(b"abc123")
    result = YtDlpRemoteAcquirer(runner).acquire_media(
        "https://example.com/watch/42",
        version_id="remote-version:one",
        output_root=tmp_path,
        format_id="140",
        expected_sha256=hashlib.sha256(b"abc123").hexdigest(),
        policy=RemoteAcquisitionPolicy(max_bytes=64, timeout_seconds=12),
    )

    assert result.resumed_partial is False
    assert result.asset.kind == AssetKind.ORIGINAL
    assert result.asset.immutable_original is True
    assert result.asset.size_bytes == 6
    assert result.asset.sha256 == hashlib.sha256(b"abc123").hexdigest()
    assert result.asset.relative_path.endswith(".media")
    assert (tmp_path / result.asset.relative_path).read_bytes() == b"abc123"
    assert not list(tmp_path.glob("*.partial"))

    argv, cwd, timeout_seconds, _cancel = runner.calls[0]
    assert cwd == tmp_path.resolve()
    assert timeout_seconds == 12
    assert argv[-1] == "https://example.com/watch/42"
    assert "--continue" in argv
    assert "--max-filesize" in argv
    assert argv[argv.index("--max-filesize") + 1] == "64"
    assert argv[argv.index("-f") + 1] == "140"
    assert "--exec" not in argv
    assert "--cookies-from-browser" not in argv
    assert "--write-info-json" not in argv


def test_remote_acquisition_preserves_bounded_resume_part_after_cancel(tmp_path: Path) -> None:
    runner = FailingRunner(
        MediaError(MediaErrorCode.PROCESS_CANCELLED, "media subprocess was cancelled")
    )
    acquirer = YtDlpRemoteAcquirer(runner)

    with pytest.raises(MediaError) as raised:
        acquirer.acquire_media(
            "https://example.com/watch/42",
            version_id="remote-version:resume",
            output_root=tmp_path,
            policy=RemoteAcquisitionPolicy(max_bytes=64),
        )
    assert raised.value.code == MediaErrorCode.PROCESS_CANCELLED
    resume_parts = list(tmp_path.glob("*.partial.part"))
    assert len(resume_parts) == 1
    assert resume_parts[0].read_bytes() == b"resume-me"

    retry_runner = WritingRunner(b"finished")
    result = YtDlpRemoteAcquirer(retry_runner).acquire_media(
        "https://example.com/watch/42",
        version_id="remote-version:resume",
        output_root=tmp_path,
        policy=RemoteAcquisitionPolicy(max_bytes=64),
    )
    assert result.resumed_partial is True


def test_oversized_resume_part_fails_before_subprocess(tmp_path: Path) -> None:
    runner = WritingRunner()
    acquirer = YtDlpRemoteAcquirer(runner)
    version_id = "remote-version:oversized"
    stem = acquirer._validate_format_id(None)  # exercise validator without changing name basis
    assert stem is None

    from nika_core.media.hashing import sha256_json

    name = f"remote-{sha256_json({'version_id': version_id, 'format_id': 'best'})[:32]}.media.partial.part"
    (tmp_path / name).write_bytes(b"12345")

    with pytest.raises(MediaError) as raised:
        acquirer.acquire_media(
            "https://example.com/watch/42",
            version_id=version_id,
            output_root=tmp_path,
            policy=RemoteAcquisitionPolicy(max_bytes=4),
        )
    assert raised.value.code == MediaErrorCode.SOURCE_TOO_LARGE
    assert runner.calls == []


def test_remote_acquisition_rejects_credentials_and_private_targets_before_runner(
    tmp_path: Path,
) -> None:
    runner = WritingRunner()
    acquirer = YtDlpRemoteAcquirer(runner)

    for url in (
        "https://user:secret@example.com/watch",
        "https://example.com/watch?token=secret",
        "http://127.0.0.1/media",
        "http://localhost/media",
    ):
        with pytest.raises(MediaError):
            acquirer.acquire_media(url, version_id="v1", output_root=tmp_path)
    assert runner.calls == []


def test_remote_acquisition_rejects_format_argument_injection(tmp_path: Path) -> None:
    runner = WritingRunner()
    with pytest.raises(ValueError, match="format_id"):
        YtDlpRemoteAcquirer(runner).acquire_media(
            "https://example.com/watch/42",
            version_id="v1",
            output_root=tmp_path,
            format_id="best --exec calc.exe",
        )
    assert runner.calls == []


def test_checksum_mismatch_never_promotes_final_asset(tmp_path: Path) -> None:
    runner = WritingRunner(b"wrong")
    with pytest.raises(MediaError) as raised:
        YtDlpRemoteAcquirer(runner).acquire_media(
            "https://example.com/watch/42",
            version_id="v1",
            output_root=tmp_path,
            expected_sha256="0" * 64,
            policy=RemoteAcquisitionPolicy(max_bytes=64),
        )
    assert raised.value.code == MediaErrorCode.CHECKSUM_MISMATCH
    assert list(tmp_path.glob("*.media")) == []
    assert len(list(tmp_path.glob("*.partial"))) == 1


def test_missing_completed_partial_is_typed_failure(tmp_path: Path) -> None:
    class NoOutputRunner:
        def run(self, argv, *, cwd, timeout_seconds, env=None, cancel_event=None):
            return ProcessResult(tuple(argv), 0, b"", b"", 0.01)

    with pytest.raises(MediaError) as raised:
        YtDlpRemoteAcquirer(NoOutputRunner()).acquire_media(
            "https://example.com/watch/42",
            version_id="v1",
            output_root=tmp_path,
        )
    assert raised.value.code == MediaErrorCode.PROCESS_FAILED
