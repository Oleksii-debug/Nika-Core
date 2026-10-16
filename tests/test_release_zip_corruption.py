from __future__ import annotations

import hashlib
import json
import struct
import zipfile
from pathlib import Path

import pytest

from nika_core.packaging.release import verify_release_archive

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
PAYLOAD = b"driver data" * 128


def _corrupt_member_deflate(artifact: Path, path: str) -> None:
    with zipfile.ZipFile(artifact) as archive:
        member = archive.getinfo(path)
        assert member.compress_type == zipfile.ZIP_DEFLATED
        assert member.compress_size >= 2
        start = member.header_offset

    contents = bytearray(artifact.read_bytes())
    assert contents[start : start + 4] == bytes((0x50, 0x4B, 0x03, 0x04))
    filename_length, extra_length = struct.unpack_from("<HH", contents, start + 26)
    compressed_start = start + 30 + filename_length + extra_length
    contents[compressed_start : compressed_start + 2] = bytes((0xFF, 0xFF))
    artifact.write_bytes(contents)


@pytest.mark.parametrize(
    ("corrupt_member", "finding"),
    (
        ("release-manifest.json", "archive:invalid-manifest"),
        ("bin/driver.dll", "archive:unreadable:bin/driver.dll"),
    ),
)
def test_archive_contains_corrupt_deflate_as_verification_finding(
    tmp_path: Path, corrupt_member: str, finding: str
) -> None:
    artifact = tmp_path / "broken-deflate.zip"
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
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("release-manifest.json", json.dumps(manifest))
        archive.writestr("bin/driver.dll", PAYLOAD)

    _corrupt_member_deflate(artifact, corrupt_member)
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (finding,)
