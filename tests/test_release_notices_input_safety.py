from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from nika_core.packaging import notices


_FIFO_SWAP_SCRIPT = r"""
import os
import sys
from pathlib import Path

from nika_core.packaging import notices

target = Path(sys.argv[1])
fifo = Path(sys.argv[2])
link_swap = sys.argv[3] == "link"
original_lstat = Path.lstat
swapped = False

def swapping_lstat(path, *args, **kwargs):
    global swapped
    result = original_lstat(path, *args, **kwargs)
    if path == target and not swapped:
        swapped = True
        target.unlink()
        if link_swap:
            target.symlink_to(fifo)
        else:
            os.replace(fifo, target)
    return result

Path.lstat = swapping_lstat
result = notices._read_notices(target)
raise SystemExit(0 if result is None else 2)
"""


@pytest.fixture
def notice_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, bytes]:
    monkeypatch.setattr(notices, "RUNTIME_DISTRIBUTIONS", ())
    monkeypatch.setattr(notices, "_python_license", lambda: "PSF license")
    bundle = tmp_path / "NikaCore"
    bundle.mkdir()
    target = bundle / "THIRD_PARTY_NOTICES.txt"
    valid = (
        b"Nika Core third-party notices\n\n"
        b"===== Python runtime =====\nPSF license\n"
    )
    target.write_bytes(valid)
    return bundle, target, valid


def test_valid_bounded_utf8_notices_are_accepted(
    notice_bundle: tuple[Path, Path, bytes],
) -> None:
    bundle, _target, _valid = notice_bundle
    assert notices.verify_third_party_notices(bundle) == ()


@pytest.mark.parametrize("invalid", [b"\xff", b"\xc3\x28", b"\x00\xff"])
def test_malformed_utf8_notices_fail_closed_without_replacement(
    notice_bundle: tuple[Path, Path, bytes],
    invalid: bytes,
) -> None:
    bundle, target, valid = notice_bundle
    target.write_bytes(valid + invalid)
    assert notices.verify_third_party_notices(bundle) == ("notices:unreadable",)


def test_oversized_notices_fail_before_full_file_read(
    notice_bundle: tuple[Path, Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, target, valid = notice_bundle
    monkeypatch.setattr(notices, "_MAX_NOTICES_BYTES", len(valid))
    assert notices.verify_third_party_notices(bundle) == ()
    target.write_bytes(valid + b"X")
    assert notices.verify_third_party_notices(bundle) == ("notices:unreadable",)


def test_notice_directory_is_not_accepted(
    notice_bundle: tuple[Path, Path, bytes],
) -> None:
    bundle, target, _valid = notice_bundle
    target.unlink()
    target.mkdir()
    assert notices.verify_third_party_notices(bundle) == ("notices:unreadable",)


def test_notice_symlink_is_not_followed(
    notice_bundle: tuple[Path, Path, bytes],
    tmp_path: Path,
) -> None:
    bundle, target, _valid = notice_bundle
    outside = tmp_path / "outside-licenses.txt"
    outside.write_text("outside", encoding="utf-8")
    target.unlink()
    try:
        target.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("this host does not permit symlinks")
    assert notices.verify_third_party_notices(bundle) == ("notices:unreadable",)


def test_notice_open_failure_returns_sanitized_finding(
    notice_bundle: tuple[Path, Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, target, _valid = notice_bundle
    original_open = notices._open_notices_descriptor

    def denied_open(path: Path) -> int:
        if path == target:
            raise PermissionError("private host path")
        return original_open(path)

    monkeypatch.setattr(notices, "_open_notices_descriptor", denied_open)
    assert notices.verify_third_party_notices(bundle) == ("notices:unreadable",)


def test_notice_path_swap_between_check_and_open_is_rejected(
    notice_bundle: tuple[Path, Path, bytes],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, target, valid = notice_bundle
    if not target.lstat().st_ino:
        pytest.skip("host does not expose file identity")
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(valid)
    original_open = notices._open_notices_descriptor

    def swapped_open(path: Path) -> int:
        if path == target:
            target.unlink()
            replacement.replace(target)
        return original_open(path)

    monkeypatch.setattr(notices, "_open_notices_descriptor", swapped_open)
    assert notices.verify_third_party_notices(bundle) == ("notices:unreadable",)


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "mkfifo"),
    reason="FIFO race regression requires a POSIX host",
)
@pytest.mark.parametrize("link_swap", [False, True], ids=["fifo", "symlink-to-fifo"])
def test_notice_fifo_swap_is_process_bounded(
    notice_bundle: tuple[Path, Path, bytes],
    tmp_path: Path,
    link_swap: bool,
) -> None:
    _bundle, target, _valid = notice_bundle
    fifo = tmp_path / "blocked-notices.fifo"
    os.mkfifo(fifo)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _FIFO_SWAP_SCRIPT,
                str(target),
                str(fifo),
                "link" if link_swap else "fifo",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("notice admission blocked while opening a swapped FIFO")
    assert result.returncode == 0, result.stderr


def test_generated_notices_reject_oversize_before_replacing_existing_file(
    notice_bundle: tuple[Path, Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, target, valid = notice_bundle
    monkeypatch.setattr(notices, "_MAX_NOTICES_BYTES", 32)
    with pytest.raises(RuntimeError, match="release size limit"):
        notices.build_third_party_notices(bundle)
    assert target.read_bytes() == valid
    assert list(bundle.glob(".THIRD_PARTY_NOTICES-*.tmp")) == []


def test_notice_publication_preserves_previous_file_when_replace_fails(
    notice_bundle: tuple[Path, Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, target, valid = notice_bundle

    def deny_replace(*_args: object) -> None:
        raise PermissionError("private publication path")

    monkeypatch.setattr(notices.os, "replace", deny_replace)
    with pytest.raises(PermissionError):
        notices.build_third_party_notices(bundle)
    assert target.read_bytes() == valid
    assert list(bundle.glob(".THIRD_PARTY_NOTICES-*.tmp")) == []


def test_notice_publication_replaces_existing_link_without_following_it(
    notice_bundle: tuple[Path, Path, bytes],
    tmp_path: Path,
) -> None:
    bundle, target, _valid = notice_bundle
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged external content", encoding="utf-8")
    target.unlink()
    try:
        target.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("this host does not permit symlinks")
    assert notices.build_third_party_notices(bundle) == target
    assert target.is_file() and not target.is_symlink()
    assert outside.read_text(encoding="utf-8") == "unchanged external content"
    assert notices.verify_third_party_notices(bundle) == ()


def test_python_runtime_license_is_bounded_before_notice_assembly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notices.sys, "base_prefix", str(tmp_path))
    monkeypatch.setattr(notices, "_MAX_NOTICES_BYTES", 64)
    license_file = tmp_path / "LICENSE.txt"
    license_file.write_text("PSF license", encoding="utf-8")
    assert notices._python_license() == "PSF license"
    license_file.write_bytes(b"A" * 65)
    with pytest.raises(RuntimeError, match="license evidence is invalid"):
        notices._python_license()


@pytest.mark.parametrize("payload", [b"A" * 65, b"PSF\xff"])
def test_distribution_license_is_bounded_and_utf8_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    monkeypatch.setattr(notices, "_MAX_NOTICES_BYTES", 64)
    license_file = tmp_path / "LICENSE"
    license_file.write_bytes(payload)

    class Distribution:
        files = ("LICENSE",)

        def locate_file(self, _item: str) -> Path:
            return license_file

    with pytest.raises(RuntimeError, match="license evidence is invalid"):
        notices._license_texts(Distribution())


def test_distribution_license_regular_utf8_is_preserved(
    tmp_path: Path,
) -> None:
    license_file = tmp_path / "LICENSE"
    license_file.write_text("SPDX compatible text\n", encoding="utf-8")

    class Distribution:
        files = ("LICENSE",)

        def locate_file(self, _item: str) -> Path:
            return license_file

    assert notices._license_texts(Distribution()) == (
        ("LICENSE", "SPDX compatible text"),
    )
