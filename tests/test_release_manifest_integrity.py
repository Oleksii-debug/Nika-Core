from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import nika_core.packaging.release as release_module
from nika_core.packaging.release import (
    ReleaseFile,
    ReleaseManifest,
    build_release_manifest,
    verify_release_manifest,
    write_release_manifest,
)

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
PRODUCT_VERSION = "1.0.0"
REQUIRED_TRUE_FIELDS = (
    "release_manifest_source_sha_bound",
    "exact_checkout_sha_verified",
    "core_ci_equivalent",
    "full_test_suite",
    "runtime_restart_recovery",
    "memory_scheduler_resource_regressions",
    "model_mock_nollm_regressions",
    "deterministic_brain_regressions",
    "foundry_local_adapter_regressions",
    "plugin_workspace_regressions",
    "security_sandbox_regressions",
    "integrated_ubuntu",
    "integrated_windows",
    "browser_semantic_proof",
    "windows_uia_semantic_proof",
    "windows_package_built",
    "manifest_verified",
    "third_party_notices_verified",
    "packaged_uia_keyboard_focus",
    "machine_readable_sbom_verified",
    "supply_chain_provenance_verified",
)
REQUIRED_FALSE_FIELDS = (
    "physical_windows_foundry_inference_proven",
    "human_tested",
    "nvda_verified",
    "production_release_ready",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle(tmp_path: Path) -> tuple[Path, ReleaseFile]:
    bundle = tmp_path / "Nika Core"
    bundle.mkdir()
    executable = bundle / "NikaCore.exe"
    executable.write_bytes(b"binary")
    entry = ReleaseFile(
        path="NikaCore.exe",
        size=executable.stat().st_size,
        sha256=_sha256(executable),
    )
    return bundle, entry


def test_verifier_rejects_duplicate_conflicting_path_identity(tmp_path: Path) -> None:
    bundle, valid = _bundle(tmp_path)
    forged = ReleaseFile(path=valid.path, size=999, sha256="0" * 64)
    manifest = ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(forged, valid),
    )

    assert verify_release_manifest(bundle, manifest) == (
        "manifest:duplicate-path:NikaCore.exe",
    )


@pytest.mark.parametrize(
    ("manifest", "finding"),
    [
        (
            ReleaseManifest(
                product="NikaCore",
                version="1.0.0",
                source_sha=SOURCE_SHA,
                files=(ReleaseFile("a", 1, "0" * 64),),
                manifest_version=True,
            ),
            "manifest:schema-version",
        ),
        (
            ReleaseManifest(
                product=" NikaCore",
                version="1.0.0",
                source_sha=SOURCE_SHA,
                files=(ReleaseFile("a", 1, "0" * 64),),
            ),
            "manifest:product",
        ),
        (
            ReleaseManifest(
                product="NikaCore",
                version="1.0.0 ",
                source_sha=SOURCE_SHA,
                files=(ReleaseFile("a", 1, "0" * 64),),
            ),
            "manifest:product-version",
        ),
        (
            ReleaseManifest(
                product="NikaCore",
                version="1.0.0",
                source_sha="deadbeef",
                files=(ReleaseFile("a", 1, "0" * 64),),
            ),
            "manifest:source-sha",
        ),
    ],
)
def test_verifier_rejects_invalid_manifest_metadata(
    tmp_path: Path,
    manifest: ReleaseManifest,
    finding: str,
) -> None:
    bundle, _ = _bundle(tmp_path)
    assert finding in verify_release_manifest(bundle, manifest)


@pytest.mark.parametrize(
    "path",
    [
        "../outside.bin",
        "/absolute.bin",
        r"dir\file.bin",
        "dir//file.bin",
        "release-manifest.json",
        "C:/outside.bin",
    ],
)
def test_verifier_rejects_noncanonical_manifest_paths(tmp_path: Path, path: str) -> None:
    bundle, _ = _bundle(tmp_path)
    manifest = ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(ReleaseFile(path=path, size=1, sha256="0" * 64),),
    )
    assert verify_release_manifest(bundle, manifest) == ("manifest:path:0",)


@pytest.mark.parametrize(
    "product",
    [
        "N" * 129,
        "NikaCore\x1f",
    ],
)
def test_manifest_rejects_product_name_outside_canonical_boundary(
    tmp_path: Path,
    product: str,
) -> None:
    bundle, valid = _bundle(tmp_path)
    manifest = ReleaseManifest(
        product=product,
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(valid,),
    )
    assert verify_release_manifest(bundle, manifest) == (
        "manifest:product",
    )
    with pytest.raises(ValueError, match="manifest:product"):
        write_release_manifest(bundle, manifest)


@pytest.mark.parametrize(
    "version",
    [
        "v" * 129,
        "1.0.0\x1f",
    ],
)
def test_manifest_rejects_product_version_outside_canonical_boundary(
    tmp_path: Path,
    version: str,
) -> None:
    bundle, valid = _bundle(tmp_path)
    manifest = ReleaseManifest(
        product="NikaCore",
        version=version,
        source_sha=SOURCE_SHA,
        files=(valid,),
    )
    assert verify_release_manifest(bundle, manifest) == (
        "manifest:product-version",
    )
    with pytest.raises(ValueError, match="product-version"):
        write_release_manifest(bundle, manifest)


@pytest.mark.parametrize(
    ("entry", "finding"),
    [
        (
            ReleaseFile(path="NikaCore.exe", size=True, sha256="0" * 64),
            "manifest:size-format:0",
        ),
        (
            ReleaseFile(path="NikaCore.exe", size=6, sha256="A" * 64),
            "manifest:sha256-format:0",
        ),
    ],
)
def test_verifier_rejects_invalid_file_evidence(
    tmp_path: Path,
    entry: ReleaseFile,
    finding: str,
) -> None:
    bundle, _ = _bundle(tmp_path)
    manifest = ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(entry,),
    )
    assert finding in verify_release_manifest(bundle, manifest)


def test_writer_refuses_malformed_manifest(tmp_path: Path) -> None:
    bundle, valid = _bundle(tmp_path)
    malformed = ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(valid, valid),
    )
    with pytest.raises(ValueError, match="duplicate-path"):
        write_release_manifest(bundle, malformed)
    assert not (bundle / "release-manifest.json").exists()


def test_builder_requires_exact_release_metadata(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path)
    with pytest.raises(ValueError, match="source-sha"):
        build_release_manifest(
            bundle,
            product="NikaCore",
            version="1.0.0",
            source_sha="deadbeef",
        )


def test_valid_manifest_still_verifies_and_writes(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path)
    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
    )
    assert verify_release_manifest(bundle, manifest) == ()
    target = write_release_manifest(bundle, manifest)
    assert target.is_file()


def test_snapshot_identity_rejects_same_size_path_replacement(tmp_path: Path) -> None:
    original = tmp_path / "original.bin"
    replacement = tmp_path / "replacement.bin"
    original.write_bytes(b"binary")
    replacement.write_bytes(b"binary")

    opened = original.stat()
    current = replacement.stat()

    assert not release_module._release_file_snapshot_is_stable(
        opened,
        opened,
        current,
        opened.st_size,
    )


def test_snapshot_rejects_path_outside_release_root(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")

    assert (
        release_module._stable_release_file_snapshot(
            outside,
            scan_secrets=False,
            root=root,
        )
        is None
    )


def test_snapshot_rejects_external_symlink_target_when_supported(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    link = root / "payload.bin"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("file symlink creation is unavailable on this host")

    assert (
        release_module._stable_release_file_snapshot(
            link,
            scan_secrets=False,
            root=root,
        )
        is None
    )


def test_builder_fails_closed_when_release_file_snapshot_is_unstable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _ = _bundle(tmp_path)
    original = release_module._stable_release_file_snapshot

    def unstable(
        path: Path,
        *,
        scan_secrets: bool,
        root: Path | None = None,
    ):
        if path.name == "NikaCore.exe":
            return None
        return original(path, scan_secrets=scan_secrets, root=root)

    monkeypatch.setattr(release_module, "_stable_release_file_snapshot", unstable)

    with pytest.raises(ValueError, match="release file changed while building manifest"):
        build_release_manifest(
            bundle,
            product="NikaCore",
            version="1.0.0",
            source_sha=SOURCE_SHA,
        )


def test_verifier_fails_closed_when_release_file_snapshot_is_unstable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, _ = _bundle(tmp_path)
    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
    )
    original = release_module._stable_release_file_snapshot

    def unstable(
        path: Path,
        *,
        scan_secrets: bool,
        root: Path | None = None,
    ):
        if path.name == "NikaCore.exe":
            return None
        return original(path, scan_secrets=scan_secrets, root=root)

    monkeypatch.setattr(release_module, "_stable_release_file_snapshot", unstable)

    assert verify_release_manifest(bundle, manifest) == ("unstable:NikaCore.exe",)


def _write_release_zip(bundle: Path, target: Path) -> None:
    import zipfile

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle).as_posix())


def _write_outer_evidence(evidence: Path, artifact: Path) -> None:
    import json

    evidence.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "product_version": PRODUCT_VERSION,
                "commit_sha": SOURCE_SHA,
                "distributable_zip_path": "./dist/NikaCore-1.0.0-windows-x64.zip",
                "distributable_zip_sha256": _sha256(artifact),
                "distributable_zip_size": artifact.stat().st_size,
                **{field: True for field in REQUIRED_TRUE_FIELDS},
                **{field: False for field in REQUIRED_FALSE_FIELDS},
            }
        ),
        encoding="utf-8",
    )


def test_outer_evidence_fails_closed_when_artifact_snapshot_is_unstable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "NikaCore-1.0.0-windows-x64.zip"
    artifact.write_bytes(b"candidate")
    evidence = tmp_path / "evidence.json"
    _write_outer_evidence(evidence, artifact)
    original = release_module._stable_release_file_snapshot

    def unstable(
        path: Path,
        *,
        scan_secrets: bool,
        root: Path | None = None,
    ):
        if path == artifact:
            return None
        return original(path, scan_secrets=scan_secrets, root=root)

    monkeypatch.setattr(release_module, "_stable_release_file_snapshot", unstable)

    assert release_module.verify_distributable_evidence(
        artifact,
        evidence,
        source_sha=SOURCE_SHA,
        artifact_reference="./dist/NikaCore-1.0.0-windows-x64.zip",
        expected_product_version=PRODUCT_VERSION,
    ) == ("distributable:unstable-artifact",)


def _valid_release_zip(tmp_path: Path) -> tuple[Path, Path]:
    bundle, _ = _bundle(tmp_path)
    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
    )
    write_release_manifest(bundle, manifest)
    artifact = tmp_path / "NikaCore-1.0.0-windows-x64.zip"
    _write_release_zip(bundle, artifact)
    return bundle, artifact


def test_release_archive_verifies_embedded_manifest(tmp_path: Path) -> None:
    from nika_core.packaging.release import verify_release_archive

    _, artifact = _valid_release_zip(tmp_path)
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == ()


def test_release_archive_rejects_post_manifest_payload_tamper(tmp_path: Path) -> None:
    from nika_core.packaging.release import verify_release_archive

    bundle, _ = _valid_release_zip(tmp_path)
    (bundle / "NikaCore.exe").write_bytes(b"tampered-after-manifest-verification")
    artifact = tmp_path / "tampered.zip"
    _write_release_zip(bundle, artifact)
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        "archive:size:NikaCore.exe",
    )


def test_release_archive_rejects_missing_manifest(tmp_path: Path) -> None:
    import zipfile

    artifact = tmp_path / "missing-manifest.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("NikaCore.exe", b"binary")
    from nika_core.packaging.release import verify_release_archive

    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        "archive:missing-manifest",
    )


def test_release_archive_rejects_duplicate_member_identity(tmp_path: Path) -> None:
    import warnings
    import zipfile

    bundle, _ = _valid_release_zip(tmp_path)
    manifest_content = (bundle / "release-manifest.json").read_bytes()
    artifact = tmp_path / "duplicate.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr("release-manifest.json", manifest_content)
            archive.writestr("NikaCore.exe", b"binary")
            archive.writestr("NikaCore.exe", b"binary")
    from nika_core.packaging.release import verify_release_archive

    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == (
        "archive:duplicate-path:NikaCore.exe",
    )


def test_release_archive_rejects_traversal_member(tmp_path: Path) -> None:
    import zipfile

    bundle, _ = _valid_release_zip(tmp_path)
    manifest_content = (bundle / "release-manifest.json").read_bytes()
    artifact = tmp_path / "traversal.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("release-manifest.json", manifest_content)
        archive.writestr("NikaCore.exe", b"binary")
        archive.writestr("../escape.dll", b"escape")
    from nika_core.packaging.release import verify_release_archive

    assert verify_release_archive(artifact, source_sha=SOURCE_SHA) == ("archive:path:2",)


def test_m12_cli_rejects_outer_bound_zip_with_inner_manifest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    from scripts import m12_release_evidence

    bundle, _ = _valid_release_zip(tmp_path)
    (bundle / "NikaCore.exe").write_bytes(b"tampered-after-manifest-verification")
    artifact = tmp_path / "NikaCore-1.0.0-windows-x64.zip"
    _write_release_zip(bundle, artifact)
    evidence = tmp_path / "evidence.json"
    _write_outer_evidence(evidence, artifact)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "m12_release_evidence.py",
            "--artifact",
            str(artifact),
            "--evidence",
            str(evidence),
            "--source-sha",
            SOURCE_SHA,
            "--artifact-reference",
            "./dist/NikaCore-1.0.0-windows-x64.zip",
            "--product-version",
            PRODUCT_VERSION,
        ],
    )
    with pytest.raises(SystemExit, match="archive:size:NikaCore.exe"):
        m12_release_evidence.main()


def test_outer_evidence_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    from nika_core.packaging.release import verify_distributable_evidence

    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"candidate")
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        '{"commit_sha":"ffffffffffffffffffffffffffffffffffffffff",'
        f'"commit_sha":"{SOURCE_SHA}",'
        '"distributable_zip_path":"ref",'
        f'"distributable_zip_size":{artifact.stat().st_size},'
        f'"distributable_zip_sha256":"{_sha256(artifact)}"}}',
        encoding="utf-8",
    )
    assert verify_distributable_evidence(
        artifact,
        evidence,
        source_sha=SOURCE_SHA,
        artifact_reference="ref",
        expected_product_version=PRODUCT_VERSION,
    ) == ("distributable:invalid-evidence",)

@pytest.mark.skipif(release_module.os.name == "nt", reason="POSIX descriptor flags")
def test_snapshot_open_uses_nonblocking_descriptor_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")
    original_open = release_module.os.open
    observed_flags: list[int] = []

    def capture_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        observed_flags.append(flags)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(release_module.os, "open", capture_open)

    snapshot = release_module._stable_release_file_snapshot(
        payload,
        scan_secrets=False,
    )

    assert snapshot is not None
    assert observed_flags
    nonblock = getattr(release_module.os, "O_NONBLOCK", 0)
    if nonblock:
        assert observed_flags[0] & nonblock

@pytest.mark.skipif(release_module.os.name != "nt", reason="Windows file-share semantics")
def test_snapshot_open_refuses_writer_and_holds_write_fence(tmp_path: Path) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload")

    with payload.open("r+b"):
        with pytest.raises(OSError):
            release_module._open_release_file_for_snapshot(payload)

    with release_module._open_release_file_for_snapshot(payload) as handle:
        assert handle.read() == b"payload"
        with pytest.raises(OSError):
            payload.write_bytes(b"replacement")

    payload.write_bytes(b"replacement")
    assert payload.read_bytes() == b"replacement"


@pytest.mark.skipif(release_module.os.name != "nt", reason="Windows file-share semantics")
def test_manifest_builder_refuses_preexisting_writer(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path)
    executable = bundle / "NikaCore.exe"

    with executable.open("r+b"):
        with pytest.raises(ValueError, match="release file changed while building manifest"):
            build_release_manifest(
                bundle,
                product="NikaCore",
                version=PRODUCT_VERSION,
                source_sha=SOURCE_SHA,
            )


@pytest.mark.skipif(release_module.os.name != "nt", reason="Windows file-share semantics")
def test_manifest_verifier_refuses_preexisting_writer(tmp_path: Path) -> None:
    bundle, _ = _bundle(tmp_path)
    manifest = build_release_manifest(
        bundle,
        product="NikaCore",
        version=PRODUCT_VERSION,
        source_sha=SOURCE_SHA,
    )
    executable = bundle / "NikaCore.exe"

    with executable.open("r+b"):
        assert verify_release_manifest(bundle, manifest) == ("unstable:NikaCore.exe",)


def test_snapshot_rejects_nonregular_descriptor_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = release_module.os.pipe()

    class NonRegularHandle:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            release_module.os.close(read_fd)
            release_module.os.close(write_fd)
            return False

        def fileno(self) -> int:
            return read_fd

        def read(self, _size: int = -1) -> bytes:
            raise AssertionError("non-regular descriptor must be rejected before read")

    monkeypatch.setattr(
        release_module,
        "_open_release_file_for_snapshot",
        lambda _path: NonRegularHandle(),
    )

    assert (
        release_module._stable_release_file_snapshot(
            tmp_path / "substituted-entry",
            scan_secrets=True,
        )
        is None
    )

class _BehavioralText(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("behavioral text method must not execute")

    def casefold(self) -> str:
        raise AssertionError("behavioral text method must not execute")

    def __eq__(self, other: object) -> bool:
        raise AssertionError("behavioral text comparison must not execute")

    def __ne__(self, other: object) -> bool:
        raise AssertionError("behavioral text comparison must not execute")


def test_manifest_verifier_rejects_noncanonical_runtime_carriers(
    tmp_path: Path,
) -> None:
    bundle, valid = _bundle(tmp_path)

    assert verify_release_manifest(bundle, object()) == (  # type: ignore[arg-type]
        "manifest:type",
    )

    product = ReleaseManifest(
        product=_BehavioralText("NikaCore"),
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(valid,),
    )
    assert verify_release_manifest(bundle, product) == ("manifest:product",)

    version = ReleaseManifest(
        product="NikaCore",
        version=_BehavioralText("1.0.0"),
        source_sha=SOURCE_SHA,
        files=(valid,),
    )
    assert verify_release_manifest(bundle, version) == ("manifest:product-version",)

    source = ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=_BehavioralText(SOURCE_SHA),
        files=(valid,),
    )
    assert verify_release_manifest(bundle, source) == ("manifest:source-sha",)

    path_manifest = ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(
            ReleaseFile(
                path=_BehavioralText(valid.path),
                size=valid.size,
                sha256=valid.sha256,
            ),
        ),
    )
    assert verify_release_manifest(bundle, path_manifest) == ("manifest:path:0",)

    digest = ReleaseManifest(
        product="NikaCore",
        version="1.0.0",
        source_sha=SOURCE_SHA,
        files=(
            ReleaseFile(
                path=valid.path,
                size=valid.size,
                sha256=_BehavioralText(valid.sha256),
            ),
        ),
    )
    assert verify_release_manifest(bundle, digest) == ("manifest:sha256-format:0",)


def test_manifest_builder_rejects_behavioral_identity_text_without_execution(
    tmp_path: Path,
) -> None:
    bundle, _ = _bundle(tmp_path)

    with pytest.raises(ValueError, match="manifest:product"):
        build_release_manifest(
            bundle,
            product=_BehavioralText("NikaCore"),
            version="1.0.0",
            source_sha=SOURCE_SHA,
        )

    with pytest.raises(ValueError, match="manifest:product-version"):
        build_release_manifest(
            bundle,
            product="NikaCore",
            version=_BehavioralText("1.0.0"),
            source_sha=SOURCE_SHA,
        )


def test_release_verifiers_require_exact_trusted_identity_text(
    tmp_path: Path,
) -> None:
    from nika_core.packaging.release import (
        verify_distributable_evidence,
        verify_release_archive,
    )

    _, artifact = _valid_release_zip(tmp_path)
    assert verify_release_archive(
        artifact,
        source_sha=_BehavioralText(SOURCE_SHA),
    ) == ("archive:source-sha-format",)
    assert verify_release_archive(
        artifact,
        source_sha=f" {SOURCE_SHA}",
    ) == ("archive:source-sha-format",)
    assert verify_release_archive(
        artifact,
        source_sha=SOURCE_SHA,
        expected_product=_BehavioralText("NikaCore"),
    ) == ("archive:expected-product-format",)
    assert verify_release_archive(
        artifact,
        source_sha=SOURCE_SHA,
        expected_product_version=_BehavioralText(PRODUCT_VERSION),
    ) == ("archive:expected-product-version-format",)

    evidence = tmp_path / "evidence.json"
    _write_outer_evidence(evidence, artifact)
    assert verify_distributable_evidence(
        artifact,
        evidence,
        source_sha=_BehavioralText(SOURCE_SHA),
        artifact_reference="./dist/NikaCore-1.0.0-windows-x64.zip",
        expected_product_version=PRODUCT_VERSION,
    ) == ("distributable:source-sha-format",)
    assert verify_distributable_evidence(
        artifact,
        evidence,
        source_sha=f"{SOURCE_SHA} ",
        artifact_reference="./dist/NikaCore-1.0.0-windows-x64.zip",
        expected_product_version=PRODUCT_VERSION,
    ) == ("distributable:source-sha-format",)
    assert verify_distributable_evidence(
        artifact,
        evidence,
        source_sha=SOURCE_SHA,
        artifact_reference=_BehavioralText(
            "./dist/NikaCore-1.0.0-windows-x64.zip"
        ),
        expected_product_version=PRODUCT_VERSION,
    ) == ("distributable:artifact-reference-format",)
    assert verify_distributable_evidence(
        artifact,
        evidence,
        source_sha=SOURCE_SHA,
        artifact_reference="./dist/NikaCore-1.0.0-windows-x64.zip",
        expected_product_version=_BehavioralText(PRODUCT_VERSION),
    ) == ("distributable:expected-product-version-format",)


def test_release_verifiers_keep_canonical_casefolded_source_sha(
    tmp_path: Path,
) -> None:
    from nika_core.packaging.release import (
        verify_distributable_evidence,
        verify_release_archive,
    )

    _, artifact = _valid_release_zip(tmp_path)
    assert verify_release_archive(artifact, source_sha=SOURCE_SHA.upper()) == ()

    evidence = tmp_path / "evidence.json"
    _write_outer_evidence(evidence, artifact)
    assert verify_distributable_evidence(
        artifact,
        evidence,
        source_sha=SOURCE_SHA.upper(),
        artifact_reference="./dist/NikaCore-1.0.0-windows-x64.zip",
        expected_product_version=PRODUCT_VERSION,
    ) == ()

