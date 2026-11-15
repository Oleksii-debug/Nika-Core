"""Independent inventory from the inspected packaged HTTP transport, not producer output."""

from __future__ import annotations

import re
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.packaging import notices

# Confirmed in the PYZ of the M11 final ZIP, SHA256
# bcf482d8a1df35baa00c46078250b9e7d0e006f6498e6382b3911b2b9233be1c.
# This set deliberately does not derive from RUNTIME_DISTRIBUTIONS: doing so
# would reproduce the original mutually agreeing producer/verifier blind spot.
PACKAGED_HTTP_DISTRIBUTIONS = {"httpx", "httpcore", "anyio", "h11", "idna", "certifi"}
PACKAGED_ADDITIONAL_DISTRIBUTIONS = {"markdown-it-py", "mdurl", "pytest", "wheel"}
PACKAGED_MISSING_DISTRIBUTIONS = PACKAGED_HTTP_DISTRIBUTIONS | PACKAGED_ADDITIONAL_DISTRIBUTIONS


def test_actual_packaged_http_transport_has_release_notice_coverage() -> None:
    assert PACKAGED_MISSING_DISTRIBUTIONS <= set(notices.RUNTIME_DISTRIBUTIONS)


@pytest.mark.parametrize("omitted", sorted(PACKAGED_MISSING_DISTRIBUTIONS))
def test_missing_bundled_http_notice_fails_and_regeneration_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, omitted: str,
) -> None:
    monkeypatch.setattr(notices, "_python_license", lambda: "Python license")

    def distribution(name: str) -> SimpleNamespace:
        metadata = Message()
        metadata["Name"] = name
        metadata["License-Expression"] = "MPL-2.0" if name == "certifi" else "MIT"
        return SimpleNamespace(version="1.0", metadata=metadata, files=())

    monkeypatch.setattr(notices.metadata, "distribution", distribution)
    path = notices.build_third_party_notices(tmp_path)
    original = path.read_text(encoding="utf-8")
    assert notices.verify_third_party_notices(tmp_path) == ()
    assert "https://pypi.org/project/certifi/1.0/#files" in original
    altered, count = re.subn(
        rf"(?ms)^===== {re.escape(omitted)} 1\.0 =====\n.*?(?=^===== |\Z)",
        "", original,
    )
    assert count == 1
    path.write_text(altered, encoding="utf-8")
    assert f"notices:{omitted}" in notices.verify_third_party_notices(tmp_path)
    notices.build_third_party_notices(tmp_path)
    assert notices.verify_third_party_notices(tmp_path) == ()


def test_certifi_source_reference_binds_and_escapes_the_resolved_version() -> None:
    metadata = Message()
    metadata["Name"] = "certifi"
    metadata["License-Expression"] = "MPL-2.0"
    installed = SimpleNamespace(version="2026.10.23?redirect=other", metadata=metadata, files=())
    _title, body = notices._distribution_section("certifi", installed)
    assert "certifi/2026.10.23%3Fredirect%3Dother/#files" in body


def test_windows_path_and_descriptor_compare_creation_time_without_losing_change_time() -> None:
    shared = {"st_dev": 7, "st_ino": 9, "st_size": 100, "st_mtime_ns": 20}
    path = SimpleNamespace(**shared, st_ctime_ns=10, st_birthtime_ns=10)
    descriptor = SimpleNamespace(**shared, st_ctime_ns=30, st_birthtime_ns=10)
    assert notices._path_descriptor_identity(path, windows=True) == (
        notices._path_descriptor_identity(descriptor, windows=True)
    )
    # Unix retains the full metadata-change comparison.
    assert notices._path_descriptor_identity(path, windows=False) != (
        notices._path_descriptor_identity(descriptor, windows=False)
    )
    # A descriptor mutation remains different even when birthtime is unchanged.
    changed = SimpleNamespace(**shared, st_ctime_ns=31, st_birthtime_ns=10)
    assert notices._snapshot_identity(changed) != notices._snapshot_identity(descriptor)
    changed.st_birthtime_ns = 11
    assert notices._path_descriptor_identity(changed, windows=True) != (
        notices._path_descriptor_identity(descriptor, windows=True)
    )


def test_descriptor_metadata_change_still_denies_a_notice_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "LICENSE.txt"
    target.write_text("MIT License\n", encoding="utf-8")
    original = notices.os.fstat
    calls = 0

    def changed_fstat(fd: int) -> SimpleNamespace:
        nonlocal calls
        observed = original(fd)
        calls += 1
        return SimpleNamespace(
            st_mode=observed.st_mode, st_dev=observed.st_dev, st_ino=observed.st_ino,
            st_size=observed.st_size, st_mtime_ns=observed.st_mtime_ns,
            st_ctime_ns=observed.st_ctime_ns + (1 if calls > 1 else 0),
            st_birthtime_ns=getattr(observed, "st_birthtime_ns", 0),
        )

    monkeypatch.setattr(notices.os, "fstat", changed_fstat)
    diagnostic: list[str] = []
    assert notices._read_notices(target, diagnostic=diagnostic) is None
    assert diagnostic == ["changed_during_first_read"]


@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_upstream_lf_and_crlf_licenses_have_one_verified_canonical_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: bytes,
) -> None:
    (tmp_path / "LICENSE.txt").write_bytes(ending.join([b"Python license", b"terms", b""]))
    dist_root = tmp_path / "installed"
    dist_root.mkdir()
    (dist_root / "LICENSE").write_bytes(ending.join([b"Example license", b"terms", b""]))
    metadata = Message()
    metadata["Name"] = "example"
    metadata["License-Expression"] = "MIT"
    installed = SimpleNamespace(
        version="1.0", metadata=metadata, files=("LICENSE",),
        locate_file=lambda item: dist_root / item,
    )
    monkeypatch.setattr(notices.sys, "base_prefix", str(tmp_path))
    monkeypatch.setattr(notices, "RUNTIME_DISTRIBUTIONS", ("example",))
    monkeypatch.setattr(notices.metadata, "distribution", lambda _: installed)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    path = notices.build_third_party_notices(bundle)
    assert b"\r" not in path.read_bytes()
    assert notices.verify_third_party_notices(bundle) == ()
    path.write_bytes(path.read_bytes().replace(b"Example license", b"Altered license"))
    assert "notices:example" in notices.verify_third_party_notices(bundle)
