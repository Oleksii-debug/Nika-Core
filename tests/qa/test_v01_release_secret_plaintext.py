from __future__ import annotations

import zipfile
from pathlib import Path

from nika_core.packaging.release import (
    build_release_manifest,
    verify_release_archive,
    verify_release_manifest,
    write_release_manifest,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
CANARY = "NIKA_QA_PLAINTEXT_SECRET_CANARY_9f3c2d"


def _bundle(tmp_path: Path) -> tuple[Path, object]:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"binary")
    (bundle / "debug.txt").write_text(
        f'api_key = "{CANARY}"\n',
        encoding="utf-8",
    )
    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
    )
    return bundle, manifest


def test_release_bundle_rejects_secret_assignment_in_ordinary_text_file(tmp_path: Path) -> None:
    bundle, manifest = _bundle(tmp_path)

    findings = verify_release_manifest(bundle, manifest)

    assert findings == ("secret-content:debug.txt",)
    assert CANARY not in "\n".join(findings)


def test_release_archive_rejects_secret_assignment_in_ordinary_text_file(tmp_path: Path) -> None:
    bundle, manifest = _bundle(tmp_path)
    write_release_manifest(bundle, manifest)
    artifact = tmp_path / "candidate.zip"
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle).as_posix())

    findings = verify_release_archive(artifact, source_sha=SOURCE_SHA)

    assert findings == ("archive:secret-content:debug.txt",)
    assert CANARY not in "\n".join(findings)
