from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from nika_core.packaging.release import (
    build_release_manifest,
    verify_release_archive,
    write_release_manifest,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


class _ForwardOnly:
    """Exercise the ZIP data-descriptor form written without seeking."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, data: bytes) -> int:
        return self.buffer.write(data)

    def tell(self) -> int:
        return self.buffer.tell()

    def flush(self) -> None:
        pass


def _archive(tmp_path: Path, *, mode: str = "normal") -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"portable verification fixture")
    manifest = build_release_manifest(
        bundle, product="NikaCore", version="1.0", source_sha=SOURCE_SHA
    )
    write_release_manifest(bundle, manifest)
    artifact = tmp_path / "release.zip"
    if mode == "descriptor":
        destination = _ForwardOnly()
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for source in sorted(bundle.iterdir()):
                output.writestr(source.name, source.read_bytes())
        artifact.write_bytes(destination.buffer.getvalue())
    else:
        with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for source in sorted(bundle.iterdir()):
                if mode == "zip64":
                    with output.open(source.name, "w", force_zip64=True) as entry:
                        entry.write(source.read_bytes())
                else:
                    output.write(source, source.name)
    return artifact


@pytest.mark.parametrize("mode", ["normal", "zip64", "descriptor"])
def test_valid_local_header_forms_match_central_evidence(
    tmp_path: Path, mode: str
) -> None:
    artifact = _archive(tmp_path, mode=mode)
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == ()


@pytest.mark.parametrize("local_offset", [14, 18, 22])
def test_corrupt_local_crc_or_size_fails_before_manifest_trust(
    tmp_path: Path, local_offset: int
) -> None:
    artifact = _archive(tmp_path)
    with zipfile.ZipFile(artifact) as archive:
        member = archive.getinfo("NikaCore.exe")
        offset = member.header_offset + local_offset
    damaged = bytearray(artifact.read_bytes())
    damaged[offset] ^= 0x55
    artifact.write_bytes(damaged)
    assert "archive:member-header-mismatch:0" in verify_release_archive(
        artifact, source_sha=SOURCE_SHA
    )


def test_corrupt_zip64_local_size_fails_closed(tmp_path: Path) -> None:
    artifact = _archive(tmp_path, mode="zip64")
    with zipfile.ZipFile(artifact) as archive:
        member = archive.getinfo("NikaCore.exe")
        # Local ZIP64 extra begins immediately after the local filename.
        extra_offset = member.header_offset + 30 + len(member.filename.encode("utf-8"))
    damaged = bytearray(artifact.read_bytes())
    assert damaged[extra_offset : extra_offset + 2] == b"\x01\x00"
    damaged[extra_offset + 4] ^= 0x55
    artifact.write_bytes(damaged)
    assert "archive:member-header-mismatch:0" in verify_release_archive(
        artifact, source_sha=SOURCE_SHA
    )


@pytest.mark.parametrize("descriptor_offset", [0, 4, 8])
def test_corrupt_deferred_descriptor_fails_closed(
    tmp_path: Path, descriptor_offset: int
) -> None:
    artifact = _archive(tmp_path, mode="descriptor")
    with zipfile.ZipFile(artifact) as archive:
        member = archive.getinfo("NikaCore.exe")
        assert member.flag_bits & 0x0008
        raw = artifact.read_bytes()
        start = member.header_offset
        name_size = int.from_bytes(raw[start + 26 : start + 28], "little")
        extra_size = int.from_bytes(raw[start + 28 : start + 30], "little")
        descriptor = start + 30 + name_size + extra_size + member.compress_size
        if raw[descriptor : descriptor + 4] == b"PK\x07\x08":
            descriptor += 4
    damaged = bytearray(artifact.read_bytes())
    damaged[descriptor + descriptor_offset] ^= 0x55
    artifact.write_bytes(damaged)
    assert "archive:member-header-mismatch:0" in verify_release_archive(
        artifact, source_sha=SOURCE_SHA
    )
