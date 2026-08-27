from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from nika_core.packaging.release import (
    build_release_manifest,
    verify_release_archive,
    verify_release_manifest,
    write_release_manifest,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
CANARY = "NIKA_QA_W081_PACKAGE_CONTENT_CANARY_7ef390"


def _bundle(tmp_path: Path, relative_path: str, content: bytes) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "NikaCore.exe").write_bytes(b"binary")
    target = bundle / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return bundle


def _manifest(bundle: Path):
    return build_release_manifest(
        bundle,
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
    )


def _archive_bundle(tmp_path: Path, bundle: Path) -> Path:
    manifest = _manifest(bundle)
    write_release_manifest(bundle, manifest)
    artifact = tmp_path / "candidate.zip"
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle).as_posix())
    return artifact


def test_release_manifest_fails_closed_on_secret_content_under_benign_filename(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        tmp_path,
        "config.json",
        json.dumps({"api_key": CANARY}).encode(),
    )
    findings = verify_release_manifest(bundle, _manifest(bundle))
    assert findings == ("secret-content:config.json",)
    assert CANARY not in "\n".join(findings)


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("settings.toml", f'client_secret = "{CANARY}"\n'.encode()),
        ("app.yaml", f"access_token: {CANARY}\n".encode()),
        ("nika.ini", f"password={CANARY}\n".encode()),
        ("bom.properties", b"\xef\xbb\xbfapi_key=" + CANARY.encode() + b"\n"),
    ],
)
def test_release_manifest_rejects_high_confidence_secret_assignments(
    tmp_path: Path,
    relative_path: str,
    content: bytes,
) -> None:
    bundle = _bundle(tmp_path, relative_path, content)
    assert verify_release_manifest(bundle, _manifest(bundle)) == (
        f"secret-content:{relative_path}",
    )


def test_release_archive_rejects_secret_content_after_manifest_hash_binding(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        "config.json",
        json.dumps({"api_key": CANARY}).encode(),
    )
    artifact = _archive_bundle(tmp_path, bundle)
    findings = verify_release_archive(artifact, source_sha=SOURCE_SHA)
    assert findings == ("archive:secret-content:config.json",)
    assert CANARY not in "\n".join(findings)


@pytest.mark.parametrize(
    "content",
    [
        b'{"api_key":"${NIKA_API_KEY}"}',
        b'{"client_secret":"{{ credential_ref }}"}',
        b'{"password":"%NIKA_PASSWORD%"}',
        b'{"access_token":"keyring:nika/token"}',
        b'{"secret_key":"REDACTED"}',
    ],
)
def test_secret_content_policy_allows_non_secret_references(
    tmp_path: Path,
    content: bytes,
) -> None:
    bundle = _bundle(tmp_path, "config.json", content)
    assert verify_release_manifest(bundle, _manifest(bundle)) == ()


@pytest.mark.parametrize(
    "content",
    [
        b'{"not_api_key":"public"}',
        b'{"public_password":"demo"}',
        b'{"api_key_backup":"public"}',
        b'{"password_hint":"demo"}',
        b'not-api-key = "public"\n',
        '{"секретapi_key":"public"}'.encode(),
    ],
)
def test_secret_content_policy_requires_complete_credential_key_tokens(
    tmp_path: Path,
    content: bytes,
) -> None:
    bundle = _bundle(tmp_path, "config.json", content)
    assert verify_release_manifest(bundle, _manifest(bundle)) == ()


def test_secret_content_policy_ignores_assignment_text_inside_normal_json_value(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        tmp_path,
        "config.json",
        b'{"note":"public documentation says api_key=example"}',
    )
    assert verify_release_manifest(bundle, _manifest(bundle)) == ()


def test_secret_content_policy_detects_inline_structural_json_key(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        "config.json",
        b'{"safe":"ok","api_key":"' + CANARY.encode() + b'"}',
    )
    assert verify_release_manifest(bundle, _manifest(bundle)) == (
        "secret-content:config.json",
    )


def test_secret_content_policy_ignores_keyword_without_assignment(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        "tokenizer.json",
        json.dumps({"tokens": ["api_key", "password", "client_secret"]}).encode(),
    )
    assert verify_release_manifest(bundle, _manifest(bundle)) == ()


def test_secret_assignment_across_scan_chunk_boundary_is_detected(tmp_path: Path) -> None:
    prefix = b"x" * (64 * 1024 - 10)
    content = prefix + f'\napi_key = "{CANARY}"\n'.encode()
    bundle = _bundle(tmp_path, "settings.conf", content)
    assert verify_release_manifest(bundle, _manifest(bundle)) == (
        "secret-content:settings.conf",
    )


def test_stream_window_boundary_is_not_treated_as_a_new_key_boundary(tmp_path: Path) -> None:
    second_window_start = (64 * 1024) - (8 * 1024)
    content = b"x" * second_window_start + b"api_key=public\n"
    bundle = _bundle(tmp_path, "settings.conf", content)
    assert verify_release_manifest(bundle, _manifest(bundle)) == ()
