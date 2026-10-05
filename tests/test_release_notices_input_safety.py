from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Self

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


def test_notice_descriptor_recheck_rejects_unstable_bytes(
    notice_bundle: tuple[Path, Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _target, _valid = notice_bundle
    original_fdopen = notices.os.fdopen

    class UnstableReader:
        def __init__(self, fd: int, mode: str, *, closefd: bool) -> None:
            self._source = original_fdopen(fd, mode, closefd=closefd)
            self._reads = 0

        def __enter__(self) -> Self:
            self._source.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._source.__exit__(*args)

        def fileno(self) -> int:
            return self._source.fileno()

        def seek(self, offset: int) -> int:
            return self._source.seek(offset)

        def read(self, size: int = -1) -> bytes:
            payload = self._source.read(size)
            self._reads += 1
            if self._reads == 2 and payload:
                return bytes([payload[0] ^ 1]) + payload[1:]
            return payload

    def unstable_fdopen(
        fd: int,
        mode: str,
        *,
        closefd: bool = True,
    ) -> UnstableReader:
        return UnstableReader(fd, mode, closefd=closefd)

    monkeypatch.setattr(notices.os, "fdopen", unstable_fdopen)
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
    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    license_file = dist_root / "LICENSE"
    license_file.write_bytes(payload)

    class Distribution:
        files = ("LICENSE",)

        def locate_file(self, item: str) -> Path:
            return dist_root / item

    with pytest.raises(RuntimeError, match="license evidence is invalid"):
        notices._license_texts(Distribution())


def test_distribution_license_regular_utf8_is_preserved(
    tmp_path: Path,
) -> None:
    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    license_file = dist_root / "LICENSE"
    license_file.write_text("SPDX compatible text\n", encoding="utf-8")

    class Distribution:
        files = ("LICENSE",)

        def locate_file(self, item: str) -> Path:
            return dist_root / item

    assert notices._license_texts(Distribution()) == (
        ("LICENSE", "SPDX compatible text"),
    )


@pytest.mark.parametrize(
    "item",
    (
        "../private/LICENSE",
        "/private/LICENSE",
        "C:/private/LICENSE",
        "pkg//LICENSE",
        "pkg/./LICENSE",
        "pkg/../LICENSE",
        "pkg/\nLICENSE",
        "pkg/\x85LICENSE",
        "pkg/\u200eLICENSE",
        "pkg/\u202eLICENSE",
        "pkg/\u2066LICENSE",
        "pkg/\u2028LICENSE",
        "pkg/\u2029LICENSE",
        "pkg/\ud800LICENSE",
    ),
)
def test_distribution_license_rejects_noncanonical_path_before_locate(item: str) -> None:
    class Distribution:
        files = (item,)

        def locate_file(self, _item: str) -> Path:
            raise AssertionError("noncanonical license path reached filesystem resolution")

    with pytest.raises(RuntimeError, match="license path identity is invalid"):
        notices._license_texts(Distribution())


def test_distribution_license_preserves_valid_nested_relative_path(tmp_path: Path) -> None:
    nested = tmp_path / "pkg" / "ліцензії"
    nested.mkdir(parents=True)
    license_file = nested / "LICENSE.txt"
    license_file.write_text("Nested license evidence\n", encoding="utf-8")

    class Distribution:
        files = ("pkg/ліцензії/LICENSE.txt",)

        def locate_file(self, item: str) -> Path:
            return tmp_path / item

    assert notices._license_texts(Distribution()) == (
        ("pkg/ліцензії/LICENSE.txt", "Nested license evidence"),
    )



def test_distribution_license_rejects_located_path_outside_distribution_root(
    tmp_path: Path,
) -> None:
    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "LICENSE").write_text("private host text", encoding="utf-8")

    class Distribution:
        files = ("LICENSE",)

        def locate_file(self, item: str) -> Path:
            if item == "":
                return dist_root
            return outside / item

    with pytest.raises(RuntimeError, match="path containment is invalid"):
        notices._license_texts(Distribution())


def test_distribution_license_rejects_intermediate_symlink_escape(
    tmp_path: Path,
) -> None:
    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "LICENSE").write_text("outside", encoding="utf-8")
    linked = dist_root / "licenses"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host does not permit directory symlinks")

    class Distribution:
        files = ("licenses/LICENSE",)

        def locate_file(self, item: str) -> Path:
            return dist_root / item

    with pytest.raises(RuntimeError, match="path containment is invalid"):
        notices._license_texts(Distribution())


def test_distribution_license_evidence_is_aggregate_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    monkeypatch.setattr(notices, "_MAX_NOTICES_BYTES", 48)
    for name in ("LICENSE-A", "LICENSE-B"):
        (dist_root / name).write_text("X" * 20, encoding="utf-8")

    class Distribution:
        files = ("LICENSE-A", "LICENSE-B")

        def locate_file(self, item: str) -> Path:
            return dist_root / item

    with pytest.raises(RuntimeError, match="evidence exceeds the release size limit"):
        notices._license_texts(Distribution())


def test_distribution_license_rejects_overlong_path_identity_before_locate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notices, "_MAX_DISTRIBUTION_PATH_BYTES", 32)
    item = "pkg/" + ("a" * 40) + "/LICENSE"

    class Distribution:
        files = (item,)

        def locate_file(self, _item: str) -> Path:
            raise AssertionError("overlong license path reached filesystem resolution")

    with pytest.raises(RuntimeError, match="path identity is invalid"):
        notices._license_texts(Distribution())


def test_distribution_license_resolution_uses_validated_string_identity(
    tmp_path: Path,
) -> None:
    dist_root = tmp_path / "dist"
    dist_root.mkdir()
    (dist_root / "LICENSE").write_text("validated path", encoding="utf-8")

    class DeceptiveItem:
        def __str__(self) -> str:
            return "LICENSE"

        def __fspath__(self) -> str:
            return "../../private/LICENSE"

    class Distribution:
        files = (DeceptiveItem(),)

        def locate_file(self, item: str) -> Path:
            assert isinstance(item, str)
            return dist_root / item

    assert notices._license_texts(Distribution()) == (
        ("LICENSE", "validated path"),
    )


def test_distribution_license_metadata_is_bounded_before_section_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notices, "_MAX_NOTICES_BYTES", 8)

    class Metadata:
        def get(self, key: str) -> str | None:
            return "MIT-EXPRESSION" if key == "License-Expression" else None

        def get_all(self, _key: str, _default: object) -> list[str]:
            return []

    class Distribution:
        metadata = Metadata()

    with pytest.raises(RuntimeError, match="metadata exceeds the release size limit"):
        notices._metadata_license(Distribution())


def test_distribution_license_metadata_rejects_section_injection() -> None:
    class Metadata:
        def get(self, key: str) -> str | None:
            if key == "License":
                return "MIT\n===== Python runtime =====\nspoof"
            return None

        def get_all(self, _key: str, _default: object) -> list[str]:
            return []

    class Distribution:
        metadata = Metadata()

    with pytest.raises(RuntimeError, match="license metadata is ambiguous"):
        notices._metadata_license(Distribution())


@pytest.mark.parametrize(
    "name",
    (
        "pkg\nspoof",
        "pkg\u2028hidden",
        "pkg\u202edirectional",
    ),
)
def test_distribution_section_identity_rejects_ambiguous_name(name: str) -> None:
    class Metadata:
        def get(self, key: str) -> str | None:
            if key == "Name":
                return name
            if key == "License":
                return "MIT"
            return None

        def get_all(self, _key: str, _default: object) -> list[str]:
            return []

    class Distribution:
        metadata = Metadata()
        version = "1.0"
        files: tuple[str, ...] = ()

    with pytest.raises(RuntimeError, match="name identity is invalid"):
        notices._distribution_section("fallback", Distribution())


def test_distribution_section_budget_includes_declared_license_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notices, "_MAX_NOTICES_BYTES", 32)

    class Metadata:
        def get(self, key: str) -> str | None:
            if key == "Name":
                return "pkg"
            if key == "License-Expression":
                return "X" * 30
            return None

        def get_all(self, _key: str, _default: object) -> list[str]:
            return []

    class Distribution:
        metadata = Metadata()
        version = "1.0"
        files: tuple[str, ...] = ()

    with pytest.raises(RuntimeError, match="evidence exceeds the release size limit"):
        notices._distribution_section("fallback", Distribution())


def test_notice_builder_enforces_total_budget_before_large_join(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notices, "_MAX_NOTICES_BYTES", 128)
    monkeypatch.setattr(notices, "RUNTIME_DISTRIBUTIONS", ("one", "two"))
    monkeypatch.setattr(notices, "_python_license", lambda: "PSF")

    class Distribution:
        pass

    monkeypatch.setattr(notices.metadata, "distribution", lambda _name: Distribution())
    monkeypatch.setattr(
        notices,
        "_distribution_section",
        lambda name, _dist: (name, "X" * 40),
    )

    with pytest.raises(RuntimeError, match="Generated third-party notices exceed"):
        notices.build_third_party_notices(tmp_path)
    assert not (tmp_path / "THIRD_PARTY_NOTICES.txt").exists()


def test_notice_verifier_rejects_noncanonical_preamble_prefix(
    notice_bundle: tuple[Path, Path, bytes],
) -> None:
    bundle, target, valid = notice_bundle
    target.write_bytes(b"untrusted prefix\n" + valid)
    assert notices.verify_third_party_notices(bundle) == (
        "notices:pythonruntime",
        "notices:structure",
    )


def test_notice_verifier_rejects_extra_preamble_spacing(
    notice_bundle: tuple[Path, Path, bytes],
) -> None:
    bundle, target, _valid = notice_bundle
    target.write_text(
        "Nika Core third-party notices\n\n\n"
        "===== Python runtime =====\nPSF license\n",
        encoding="utf-8",
    )
    assert notices.verify_third_party_notices(bundle) == (
        "notices:pythonruntime",
        "notices:structure",
    )


def test_notice_verifier_rejects_unknown_extra_section(
    notice_bundle: tuple[Path, Path, bytes],
) -> None:
    bundle, target, valid = notice_bundle
    target.write_bytes(
        valid
        + b"\n===== Unexpected package 1.0 =====\n"
        + b"unbound release evidence\n"
    )
    assert notices.verify_third_party_notices(bundle) == (
        "notices:unexpected-section",
    )


def test_notice_verifier_rejects_decorated_section_marker(
    notice_bundle: tuple[Path, Path, bytes],
) -> None:
    bundle, target, _valid = notice_bundle
    target.write_text(
        "Nika Core third-party notices\n\n"
        " ===== Python runtime ===== \nPSF license\n",
        encoding="utf-8",
    )
    assert notices.verify_third_party_notices(bundle) == (
        "notices:pythonruntime",
        "notices:structure",
    )


def test_notice_parser_bounds_section_count(
    notice_bundle: tuple[Path, Path, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, target, valid = notice_bundle
    monkeypatch.setattr(notices, "_MAX_NOTICE_SECTIONS", 2)
    target.write_bytes(
        valid
        + b"\n===== extra-one =====\none\n"
        + b"\n===== extra-two =====\ntwo\n"
    )
    assert notices.verify_third_party_notices(bundle) == (
        "notices:pythonruntime",
        "notices:structure",
    )
