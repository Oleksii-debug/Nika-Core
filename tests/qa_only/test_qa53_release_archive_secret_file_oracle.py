from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from nika_core.packaging import release

_SOURCE_SHA = "a" * 40
_CANARY = b"QA53_SYNTHETIC_PACKAGE_SECRET_9a4e71c2"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_release_archive(path: Path, forbidden_path: str) -> None:
    payloads = {
        "NikaCore.exe": b"MZ-QA53-placeholder",
        forbidden_path: _CANARY,
    }
    manifest = {
        "files": [
            {
                "path": member_path,
                "sha256": _sha256(payload),
                "size": len(payload),
            }
            for member_path, payload in sorted(payloads.items())
        ],
        "manifest_version": 2,
        "product": "NikaCore",
        "source_sha": _SOURCE_SHA,
        "version": "0.0.2",
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member_path, payload in payloads.items():
            archive.writestr(member_path, payload)
        archive.writestr(
            "release-manifest.json",
            json.dumps(manifest, sort_keys=True).encode("utf-8"),
        )


@pytest.mark.parametrize("forbidden_path", [".env", "token.json", "cookies.txt"])
def test_release_archive_rejects_known_secret_bearing_files(
    tmp_path: Path,
    forbidden_path: str,
) -> None:
    """QA_ONLY: M11/M12 archive verification must fail closed on secret files."""

    artifact = tmp_path / "NikaCore-windows-x64.zip"
    _write_release_archive(artifact, forbidden_path)

    findings = release.verify_release_archive(artifact, source_sha=_SOURCE_SHA)

    assert findings, "release verifier accepted a forbidden secret-bearing package file"
