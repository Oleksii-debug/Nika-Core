from __future__ import annotations

import os
from pathlib import Path

from nika_core.packaging.release import (
    build_release_manifest,
    verify_release_manifest,
    write_release_manifest,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


def test_manifest_publish_does_not_write_through_existing_hardlink(tmp_path: Path) -> None:
    bundle = tmp_path / "Nika Core"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"binary")

    outside = tmp_path / "outside.json"
    outside.write_text("sentinel", encoding="utf-8")
    manifest_path = bundle / "release-manifest.json"
    os.link(outside, manifest_path)

    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
    )
    target = write_release_manifest(bundle, manifest)

    assert target == manifest_path
    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert target.read_text(encoding="utf-8") != "sentinel"
    assert verify_release_manifest(bundle, manifest) == ()
