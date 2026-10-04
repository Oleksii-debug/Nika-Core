from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

from nika_core.packaging.release import (
    build_release_manifest,
    verify_release_archive,
    write_release_manifest,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _archive_with_extra(tmp_path: Path, extra: bytes) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"portable fixture")
    manifest = build_release_manifest(
        bundle, product="NikaCore", version="1.0", source_sha=SOURCE_SHA
    )
    write_release_manifest(bundle, manifest)
    artifact = tmp_path / "release.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        for source in sorted(bundle.iterdir()):
            info = zipfile.ZipInfo(source.name)
            if source.name == "NikaCore.exe":
                info.extra = extra
            archive.writestr(info, source.read_bytes())
    # Prove the test ZIP itself is readable by the standard library.
    with zipfile.ZipFile(artifact) as archive:
        assert archive.read("NikaCore.exe") == b"portable fixture"
    return artifact


@pytest.mark.parametrize("redundant_bytes", [0, 8, 16])
def test_rejects_unreferenced_local_zip64_size_fields(
    tmp_path: Path, redundant_bytes: int
) -> None:
    # Ordinary 32-bit header sizes are authoritative. ZIP64 may not supply a
    # second optional set of values that some extractors could prefer.
    alternate_sizes = b"\x00" * redundant_bytes
    extra = b"\x01\x00" + len(alternate_sizes).to_bytes(2, "little") + alternate_sizes
    artifact = _archive_with_extra(tmp_path, extra)
    assert "archive:member-header-mismatch:0" in verify_release_archive(
        artifact, source_sha=SOURCE_SHA
    )


def test_preserves_ordinary_benign_extra_fields(tmp_path: Path) -> None:
    artifact = _archive_with_extra(tmp_path, b"\xfe\xca\x02\x00ok")
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == ()


def test_rejects_trailing_values_in_required_local_zip64_field(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "trailing.zip"
    with zipfile.ZipFile(artifact, "w") as output:
        with output.open("NikaCore.exe", "w", force_zip64=True) as handle:
            handle.write(b"portable fixture")
    assert verify_release_archive(
        artifact, source_sha=SOURCE_SHA
    ) == ("archive:missing-manifest",)

    damaged = bytearray(artifact.read_bytes())
    name_size = int.from_bytes(damaged[26:28], "little")
    extra_size = int.from_bytes(damaged[28:30], "little")
    extra_start = 30 + name_size
    assert damaged[extra_start : extra_start + 4] == b"\x01\x00\x10\x00"
    # Preserve both genuine ZIP64 sizes but append an alternate third size.
    insertion = extra_start + 4 + 16
    damaged[insertion:insertion] = b"\x00" * 8
    struct.pack_into("<H", damaged, extra_start + 2, 24)
    struct.pack_into("<H", damaged, 28, extra_size + 8)
    eocd = damaged.rfind(b"PK\x05\x06")
    assert eocd != -1
    central_offset = struct.unpack_from("<I", damaged, eocd + 16)[0]
    struct.pack_into("<I", damaged, eocd + 16, central_offset + 8)
    artifact.write_bytes(damaged)
    with zipfile.ZipFile(artifact) as archive:
        assert archive.read("NikaCore.exe") == b"portable fixture"

    assert "archive:member-header-mismatch:0" in verify_release_archive(
        artifact, source_sha=SOURCE_SHA
    )
