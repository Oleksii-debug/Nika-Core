from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.packaging import notices


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
    original_open = Path.open

    def denied_open(path: Path, *args: object, **kwargs: object):
        if path == target:
            raise PermissionError("private host path")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)
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
    original_open = Path.open

    def swapped_open(path: Path, *args: object, **kwargs: object):
        if path == target:
            target.unlink()
            replacement.replace(target)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", swapped_open)
    assert notices.verify_third_party_notices(bundle) == ("notices:unreadable",)


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
