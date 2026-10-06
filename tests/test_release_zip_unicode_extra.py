from __future__ import annotations

import hashlib
import json
import struct
import zipfile
import zlib
from pathlib import Path

import pytest

from nika_core.packaging.release import verify_release_archive

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
PAYLOAD = b"verified Windows release asset"


def _unicode_path_extra(path: str, replacement: str) -> bytes:
    raw_name = path.encode("utf-8")
    alias = replacement.encode("utf-8")
    payload = struct.pack("<BI", 1, zlib.crc32(raw_name)) + alias
    return struct.pack("<HH", 0x7075, len(payload)) + payload


def _benign_extra_with_unicode_payload(path: str, replacement: str) -> bytes:
    # Same shape as the Unicode Path field, under an unrelated field ID.
    # This lets the test introduce the 0x7075 ID in the local header only.
    raw = _unicode_path_extra(path, replacement)
    return struct.pack("<H", 0x5455) + raw[2:]


def _valid_manifest(path: str = "NikaCore.exe") -> bytes:
    return json.dumps(
        {
            "manifest_version": 2,
            "product": "NikaCore",
            "version": "1.0.0",
            "source_sha": SOURCE_SHA,
            "files": [
                {
                    "path": path,
                    "size": len(PAYLOAD),
                    "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
                }
            ],
        }
    ).encode("utf-8")


def _zip_with_extra(
    artifact: Path,
    *,
    member: str,
    extra: bytes,
    extra_directory: bool = False,
) -> None:
    with zipfile.ZipFile(artifact, "w") as archive:
        manifest = zipfile.ZipInfo("release-manifest.json")
        if member == "release-manifest.json":
            manifest.extra = extra
        archive.writestr(manifest, _valid_manifest())
        if extra_directory:
            directory = zipfile.ZipInfo(member)
            directory.extra = extra
            archive.writestr(directory, b"")
            archive.writestr("NikaCore.exe", PAYLOAD)
        elif member != "release-manifest.json":
            asset = zipfile.ZipInfo(member)
            asset.extra = extra
            archive.writestr(asset, PAYLOAD)
        else:
            archive.writestr("NikaCore.exe", PAYLOAD)


@pytest.mark.parametrize(
    ("member", "alias", "extra_directory", "index"),
    (
        ("NikaCore.exe", "../escape.dll", False, 1),
        ("NikaCore.exe", "NikaCore.exe", False, 1),
        ("release-manifest.json", "other.json", False, 0),
        ("assets/", "../escape/", True, 1),
    ),
)
def test_rejects_central_unicode_path_alias_before_trusting_archive(
    tmp_path: Path, member: str, alias: str, extra_directory: bool, index: int
) -> None:
    artifact = tmp_path / "central-alias.zip"
    _zip_with_extra(
        artifact,
        member=member,
        extra=_unicode_path_extra(member, alias),
        extra_directory=extra_directory,
    )
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        f"archive:unicode-path-extra:{index}",
    )


def test_rejects_local_only_unicode_path_alias(tmp_path: Path) -> None:
    artifact = tmp_path / "local-alias.zip"
    name = "NikaCore.exe"
    extra = _benign_extra_with_unicode_payload(name, "../escape.dll")
    _zip_with_extra(artifact, member=name, extra=extra)

    with zipfile.ZipFile(artifact) as archive:
        member = archive.getinfo(name)
        offset = member.header_offset
    raw = bytearray(artifact.read_bytes())
    assert raw[offset : offset + 4] == b"PK\x03\x04"
    name_size = struct.unpack_from("<H", raw, offset + 26)[0]
    local_extra_offset = offset + 30 + name_size
    assert struct.unpack_from("<H", raw, local_extra_offset)[0] == 0x5455
    struct.pack_into("<H", raw, local_extra_offset, 0x7075)
    artifact.write_bytes(raw)

    # Central directory remains harmless; only the local entry is ambiguous.
    with zipfile.ZipFile(artifact) as archive:
        assert archive.getinfo(name).extra == extra
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        "archive:unicode-path-extra:1",
    )


def test_rejects_malformed_extra_field_framing(tmp_path: Path) -> None:
    artifact = tmp_path / "malformed-extra.zip"
    _zip_with_extra(artifact, member="NikaCore.exe", extra=b"\x00")
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        "archive:member-extra-format:1",
    )


def test_preserves_safe_extra_and_ukrainian_asset_path(tmp_path: Path) -> None:
    artifact = tmp_path / "benign-extra.zip"
    name = "ресурси/повідомлення.txt"
    manifest = _valid_manifest(name)
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("release-manifest.json", manifest)
        entry = zipfile.ZipInfo(name)
        entry.extra = struct.pack("<HHB", 0x5455, 1, 0)
        archive.writestr(entry, PAYLOAD)
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == ()


def test_rejects_mismatched_local_directory_name(tmp_path: Path) -> None:
    artifact = tmp_path / "directory-name-confusion.zip"
    _zip_with_extra(artifact, member="assets/", extra=b"", extra_directory=True)
    with zipfile.ZipFile(artifact) as archive:
        offset = archive.getinfo("assets/").header_offset
    raw = bytearray(artifact.read_bytes())
    filename_start = offset + 30
    assert raw[filename_start : filename_start + 7] == b"assets/"
    raw[filename_start : filename_start + 7] = b"../out/"
    artifact.write_bytes(raw)

    # Directory members are not read when matching manifest file hashes.
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        "archive:member-local-path:1",
    )


@pytest.mark.parametrize("field_offset", (6, 8))
def test_rejects_mismatched_local_header_flags_or_compression(
    tmp_path: Path, field_offset: int
) -> None:
    artifact = tmp_path / "header-confusion.zip"
    _zip_with_extra(artifact, member="NikaCore.exe", extra=b"")
    with zipfile.ZipFile(artifact) as archive:
        offset = archive.getinfo("NikaCore.exe").header_offset
    raw = bytearray(artifact.read_bytes())
    value = struct.unpack_from("<H", raw, offset + field_offset)[0]
    struct.pack_into("<H", raw, offset + field_offset, value ^ 8)
    artifact.write_bytes(raw)
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        "archive:member-header-mismatch:1",
    )
