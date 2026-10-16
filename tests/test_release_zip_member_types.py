from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path

import pytest

from nika_core.packaging.release import verify_release_archive

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
PAYLOAD = b"driver data"


def _member(
    path: str, *, system: int, unix_mode: int = 0, dos_attributes: int = 0
) -> zipfile.ZipInfo:
    member = zipfile.ZipInfo(path)
    member.create_system = system
    member.external_attr = (unix_mode << 16) | dos_attributes
    return member


@pytest.mark.parametrize(
    ("path", "system", "mode", "dos", "payload", "finding"),
    (
        ("driver.dll", 3, stat.S_IFDIR | 0o755, 0, PAYLOAD, "archive:member-type:1"),
        ("bin/", 3, stat.S_IFREG | 0o644, 0, b"", "archive:member-type:1"),
        ("pipe", 3, stat.S_IFIFO | 0o600, 0, PAYLOAD, "archive:member-type:1"),
        ("device", 3, stat.S_IFCHR | 0o600, 0, PAYLOAD, "archive:member-type:1"),
        ("block", 3, stat.S_IFBLK | 0o600, 0, PAYLOAD, "archive:member-type:1"),
        ("socket", 3, stat.S_IFSOCK | 0o600, 0, PAYLOAD, "archive:member-type:1"),
        ("driver.dll", 0, 0, 0x10, PAYLOAD, "archive:member-type:1"),
        ("bin/", 3, stat.S_IFDIR | 0o755, 0, PAYLOAD, "archive:directory-content:1"),
    ),
)
def test_rejects_ambiguous_archive_member_before_manifest(
    tmp_path: Path,
    path: str,
    system: int,
    mode: int,
    dos: int,
    payload: bytes,
    finding: str,
) -> None:
    artifact = tmp_path / "ambiguous.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("release-manifest.json", b"invalid")
        archive.writestr(
            _member(path, system=system, unix_mode=mode, dos_attributes=dos),
            payload,
        )

    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (finding,)


@pytest.mark.parametrize(
    ("directory_system", "directory_mode", "directory_dos", "file_mode"),
    (
        (3, stat.S_IFDIR | 0o755, 0x10, stat.S_IFREG | 0o644),
        (3, 0, 0, 0),
        (0, 0, 0x10, 0),
    ),
)
def test_accepts_consistent_directory_and_file_modes(
    tmp_path: Path,
    directory_system: int,
    directory_mode: int,
    directory_dos: int,
    file_mode: int,
) -> None:
    artifact = tmp_path / "valid.zip"
    manifest = {
        "manifest_version": 2,
        "product": "NikaCore",
        "version": "1.0.0",
        "source_sha": SOURCE_SHA,
        "files": [{
            "path": "bin/driver.dll",
            "size": len(PAYLOAD),
            "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
        }],
    }
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("release-manifest.json", json.dumps(manifest))
        archive.writestr(
            _member(
                "bin/",
                system=directory_system,
                unix_mode=directory_mode,
                dos_attributes=directory_dos,
            ),
            b"",
        )
        archive.writestr(
            _member("bin/driver.dll", system=3, unix_mode=file_mode),
            PAYLOAD,
        )

    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == ()
