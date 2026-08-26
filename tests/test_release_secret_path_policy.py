from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from nika_core.packaging.release import (
    ReleaseFile,
    ReleaseManifest,
    build_release_manifest,
    verify_release_archive,
    verify_release_manifest,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    "relative_path",
    [
        ".env",
        "Config/.ENV",
        "state/.env.local",
        "auth/token.json",
        "Auth/TOKEN.JSON",
        "browser/cookies.txt",
        "Browser/Cookies.Txt",
    ],
)
def test_manifest_builder_rejects_secret_bearing_release_paths(
    tmp_path: Path,
    relative_path: str,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"binary")
    secret = bundle / Path(relative_path)
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("synthetic-canary", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest:secret-path"):
        build_release_manifest(
            bundle,
            product="NikaCore",
            version="1.0.0",
            source_sha=SOURCE_SHA,
        )


def test_manifest_verifier_rejects_forged_secret_path(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "token.json").write_text("synthetic-canary", encoding="utf-8")
    manifest = ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(ReleaseFile(path="token.json", size=16, sha256="0" * 64),),
    )

    assert verify_release_manifest(bundle, manifest) == ("manifest:secret-path:token.json",)


@pytest.mark.parametrize("relative_path", [".env", "nested/TOKEN.JSON", "cache/Cookies.Txt"])
def test_archive_verifier_rejects_secret_member_before_manifest_trust(
    tmp_path: Path,
    relative_path: str,
) -> None:
    artifact = tmp_path / "candidate.zip"
    manifest = {
        "manifest_version": 2,
        "product": "NikaCore",
        "version": "1.0.0",
        "source_sha": SOURCE_SHA,
        "files": [
            {
                "path": relative_path,
                "size": len(b"synthetic-canary"),
                "sha256": "0" * 64,
            }
        ],
    }
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("release-manifest.json", json.dumps(manifest))
        archive.writestr(relative_path, b"synthetic-canary")

    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        f"archive:secret-path:{relative_path}",
    )


@pytest.mark.parametrize("relative_path", ["tokenizer.json", "cookies_policy.txt", ".env.example"])
def test_secret_policy_does_not_block_ordinary_release_filenames(
    tmp_path: Path,
    relative_path: str,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"binary")
    ordinary = bundle / relative_path
    ordinary.write_text("public-data", encoding="utf-8")

    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
    )
    assert {entry.path for entry in manifest.files} == {"NikaCore.exe", relative_path}
    assert verify_release_manifest(bundle, manifest) == ()
