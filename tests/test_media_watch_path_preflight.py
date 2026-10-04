from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from nika_core.media.errors import MediaError, MediaErrorCode
from nika_core.media.process import SafeProcessRunner


def _require_symlink_support(tmp_path: Path) -> None:
    probe = tmp_path / "symlink-support-check"
    try:
        probe.symlink_to(tmp_path / "missing-probe-target")
    except (OSError, NotImplementedError):
        pytest.skip("creating symbolic links is unavailable on this host")
    finally:
        probe.unlink(missing_ok=True)


def _assert_rejected_before_spawn(
    tmp_path: Path, watched: Path, expected: MediaErrorCode
) -> None:
    started = tmp_path / "subprocess-started"
    code = "from pathlib import Path; Path('subprocess-started').touch()"
    with pytest.raises(MediaError) as caught:
        SafeProcessRunner().run(
            (sys.executable, "-c", code),
            cwd=tmp_path,
            timeout_seconds=5,
            watched_paths=(watched,),
            max_watched_file_bytes=16,
        )
    assert caught.value.code == expected
    assert not started.exists()


def test_dangling_watched_symlink_is_rejected_without_spawning(tmp_path: Path) -> None:
    _require_symlink_support(tmp_path)
    link = tmp_path / "download.partial.part"
    link.symlink_to(tmp_path / "nonexistent-target")
    assert link.is_symlink()
    assert not link.exists()

    _assert_rejected_before_spawn(tmp_path, link, MediaErrorCode.PATH_ESCAPE)
    assert link.is_symlink()


def test_existing_watched_symlink_is_rejected_without_spawning(tmp_path: Path) -> None:
    _require_symlink_support(tmp_path)
    target = tmp_path / "existing-target"
    target.write_bytes(b"safe-sized")
    link = tmp_path / "download.partial.part"
    link.symlink_to(target)

    _assert_rejected_before_spawn(tmp_path, link, MediaErrorCode.PATH_ESCAPE)
    assert target.read_bytes() == b"safe-sized"


def test_oversize_watched_file_is_rejected_before_spawning(tmp_path: Path) -> None:
    watched = tmp_path / "download.partial.part"
    watched.write_bytes(b"x" * 17)

    _assert_rejected_before_spawn(tmp_path, watched, MediaErrorCode.SOURCE_TOO_LARGE)
    assert watched.read_bytes() == b"x" * 17


def test_nonregular_watched_path_is_rejected_before_spawning(tmp_path: Path) -> None:
    watched = tmp_path / "download.partial.part"
    watched.mkdir()

    _assert_rejected_before_spawn(tmp_path, watched, MediaErrorCode.INVALID_SOURCE)
    assert watched.is_dir()


def test_subprocess_created_dangling_symlink_is_rejected_after_exit(
    tmp_path: Path,
) -> None:
    _require_symlink_support(tmp_path)
    watched = tmp_path / "download.partial.part"
    code = (
        "from pathlib import Path; "
        "Path('download.partial.part').symlink_to('missing-output')"
    )
    with pytest.raises(MediaError) as caught:
        SafeProcessRunner().run(
            (sys.executable, "-c", code),
            cwd=tmp_path,
            timeout_seconds=5,
            watched_paths=(watched,),
            max_watched_file_bytes=16,
        )
    assert caught.value.code == MediaErrorCode.PATH_ESCAPE
    assert watched.is_symlink()
    assert not watched.exists()


def test_absent_watched_output_does_not_block_successful_process(tmp_path: Path) -> None:
    result = SafeProcessRunner().run(
        (sys.executable, "-c", "print('ok')"),
        cwd=tmp_path,
        timeout_seconds=5,
        watched_paths=(tmp_path / "not-created.part",),
        max_watched_file_bytes=16,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == b"ok"


def test_already_cancelled_run_does_not_spawn_a_process(tmp_path: Path) -> None:
    cancel_event = threading.Event()
    cancel_event.set()
    started = tmp_path / "subprocess-started"
    code = "from pathlib import Path; Path('subprocess-started').touch()"
    with pytest.raises(MediaError) as caught:
        SafeProcessRunner().run(
            (sys.executable, "-c", code),
            cwd=tmp_path,
            timeout_seconds=5,
            cancel_event=cancel_event,
        )
    assert caught.value.code == MediaErrorCode.PROCESS_CANCELLED
    assert not started.exists()


def test_cancellation_during_watch_preflight_prevents_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel_event = threading.Event()
    watched = tmp_path / "output.part"
    original = SafeProcessRunner._watched_file_failure

    def cancel_during_preflight(paths: tuple[Path, ...], *, max_bytes: int | None):
        cancel_event.set()
        return original(paths, max_bytes=max_bytes)

    monkeypatch.setattr(
        SafeProcessRunner, "_watched_file_failure", staticmethod(cancel_during_preflight)
    )
    started = tmp_path / "subprocess-started"
    code = "from pathlib import Path; Path('subprocess-started').touch()"
    with pytest.raises(MediaError) as caught:
        SafeProcessRunner().run(
            (sys.executable, "-c", code),
            cwd=tmp_path,
            timeout_seconds=5,
            cancel_event=cancel_event,
            watched_paths=(watched,),
            max_watched_file_bytes=16,
        )
    assert caught.value.code == MediaErrorCode.PROCESS_CANCELLED
    assert not started.exists()


def test_uninspectable_watched_output_fails_closed_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watched = tmp_path / "output.part"
    watched.write_bytes(b"hello")
    original = Path.lstat

    def denied_lstat(path: Path):
        if path == watched:
            raise PermissionError("synthetic permission denial")
        return original(path)

    monkeypatch.setattr(Path, "lstat", denied_lstat)
    started = tmp_path / "subprocess-started"
    code = "from pathlib import Path; Path('subprocess-started').touch()"
    with pytest.raises(MediaError) as caught:
        SafeProcessRunner().run(
            (sys.executable, "-c", code),
            cwd=tmp_path,
            timeout_seconds=5,
            watched_paths=(watched,),
            max_watched_file_bytes=16,
        )
    assert caught.value.code == MediaErrorCode.PATH_ESCAPE
    assert not started.exists()


def _require_hardlink_support(tmp_path: Path) -> None:
    source = tmp_path / "hardlink-support-source"
    alias = tmp_path / "hardlink-support-alias"
    source.write_bytes(b"test")
    try:
        os.link(source, alias)
    except (OSError, NotImplementedError):
        pytest.skip("creating hard links is unavailable on this host")
    finally:
        alias.unlink(missing_ok=True)
        source.unlink(missing_ok=True)


def test_preexisting_hardlink_is_rejected_before_child_spawn(tmp_path: Path) -> None:
    _require_hardlink_support(tmp_path)
    original = tmp_path / "important-existing-file"
    original.write_bytes(b"unchanged")
    watched = tmp_path / "download.partial.part"
    os.link(original, watched)

    _assert_rejected_before_spawn(tmp_path, watched, MediaErrorCode.PATH_ESCAPE)
    assert original.read_bytes() == b"unchanged"
    assert watched.read_bytes() == b"unchanged"


def test_child_created_hardlink_is_rejected_after_exit(tmp_path: Path) -> None:
    _require_hardlink_support(tmp_path)
    original = tmp_path / "important-existing-file"
    original.write_bytes(b"unchanged")
    watched = tmp_path / "download.partial.part"
    code = "import os; os.link('important-existing-file', 'download.partial.part')"

    with pytest.raises(MediaError) as caught:
        SafeProcessRunner().run(
            (sys.executable, "-c", code),
            cwd=tmp_path,
            timeout_seconds=5,
            watched_paths=(watched,),
            max_watched_file_bytes=16,
        )
    assert caught.value.code == MediaErrorCode.PATH_ESCAPE
    assert original.read_bytes() == b"unchanged"
    assert watched.read_bytes() == b"unchanged"


def test_existing_single_link_regular_output_is_still_allowed(tmp_path: Path) -> None:
    watched = tmp_path / "download.partial.part"
    watched.write_bytes(b"ordinary")
    result = SafeProcessRunner().run(
        (sys.executable, "-c", "print('ok')"),
        cwd=tmp_path,
        timeout_seconds=5,
        watched_paths=(watched,),
        max_watched_file_bytes=16,
    )
    assert result.returncode == 0
    assert watched.read_bytes() == b"ordinary"
