from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.packaging import sbom
from nika_core.packaging.sbom import SupplyChainEvidenceError

SOURCE_SHA = "a" * 40


def _project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        """
[project]
name = "nika-core"
version = "0.0.2"
dependencies = ["httpx>=0.28,<1"]

[project.optional-dependencies]
gui = ["pywebview>=6.2.1,<7"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return root


def _archive_item(
    name: str,
    version: str,
    sha256: str,
    *,
    requested: bool = False,
    url: str | None = None,
    requires_dist: list[str] | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {"name": name, "version": version}
    if requires_dist is not None:
        metadata["requires_dist"] = requires_dist
    return {
        "download_info": {
            "url": url or f"https://packages.example.invalid/{name}-{version}.whl",
            "archive_info": {"hashes": {"sha256": sha256}},
        },
        "is_direct": False,
        "is_yanked": False,
        "requested": requested,
        "metadata": metadata,
    }


def _report(
    path: Path,
    *,
    include_httpx: bool = True,
    include_pywebview: bool = True,
) -> Path:
    install: list[dict[str, object]] = [
        {
            "download_info": {"url": "file:///workspace/Nika-Core"},
            "is_direct": True,
            "is_yanked": False,
            "requested": True,
            "metadata": {"name": "nika-core", "version": "0.0.2"},
        }
    ]
    if include_httpx:
        install.append(
            _archive_item(
                "httpx",
                "0.28.1",
                "1" * 64,
                requested=True,
                url=(
                    "https://resolver-user:resolver-secret@packages.example.invalid/"
                    "httpx.whl?token=resolver-canary"
                ),
                requires_dist=["httpcore>=1"],
            )
        )
        install.append(_archive_item("httpcore", "1.0.9", "5" * 64))
    if include_pywebview:
        install.append(_archive_item("pywebview", "6.2.1", "2" * 64, requested=True))
    path.write_text(
        json.dumps(
            {
                "version": "1",
                "pip_version": "26.0",
                "install": install,
                "environment": sbom.default_environment(),
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def installed_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_installed(name: str, version: str) -> dict[str, object]:
        return {
            "declared_license": "MIT",
            "license_evidence": [
                {
                    "path": f"{name}-{version}.dist-info/licenses/LICENSE",
                    "sha256": "3" * 64,
                }
            ],
            "installed_record_sha256": "4" * 64,
        }

    monkeypatch.setattr(sbom, "_installed_distribution_evidence", fake_installed)


def test_supply_chain_rejects_missing_resolver_environment(
    tmp_path: Path,
    installed_metadata: None,
) -> None:
    project_root = _project(tmp_path / "project")
    report = _report(tmp_path / "report.json")
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload.pop("environment")
    report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        SupplyChainEvidenceError,
        match="pip installation report environment is invalid",
    ):
        sbom.build_supply_chain_evidence(
            report,
            project_root=project_root,
            application_name="nika-core",
            application_version="0.0.2",
            source_sha=SOURCE_SHA,
        )


def test_supply_chain_requires_every_declared_runtime_dependency(
    tmp_path: Path,
    installed_metadata: None,
) -> None:
    project_root = _project(tmp_path / "project")
    report = _report(tmp_path / "report.json", include_httpx=False)

    with pytest.raises(
        SupplyChainEvidenceError,
        match="Declared runtime dependencies are missing",
    ):
        sbom.build_supply_chain_evidence(
            report,
            project_root=project_root,
            application_name="nika-core",
            application_version="0.0.2",
            source_sha=SOURCE_SHA,
        )


def test_supply_chain_requires_immutable_distribution_artifact_identity(
    tmp_path: Path,
    installed_metadata: None,
) -> None:
    project_root = _project(tmp_path / "project")
    report = _report(tmp_path / "report.json")
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["install"][1]["download_info"]["archive_info"] = {}
    report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        SupplyChainEvidenceError,
        match="immutable source artifact identity",
    ):
        sbom.build_supply_chain_evidence(
            report,
            project_root=project_root,
            application_name="nika-core",
            application_version="0.0.2",
            source_sha=SOURCE_SHA,
        )


def test_supply_chain_rejects_yanked_component(
    tmp_path: Path,
    installed_metadata: None,
) -> None:
    project_root = _project(tmp_path / "project")
    report = _report(tmp_path / "report.json")
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["install"][1]["is_yanked"] = True
    report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SupplyChainEvidenceError, match="Yanked runtime component"):
        sbom.build_supply_chain_evidence(
            report,
            project_root=project_root,
            application_name="nika-core",
            application_version="0.0.2",
            source_sha=SOURCE_SHA,
        )


def test_supply_chain_rejects_duplicate_component_identity(
    tmp_path: Path,
    installed_metadata: None,
) -> None:
    project_root = _project(tmp_path / "project")
    report = _report(tmp_path / "report.json")
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["install"].append(dict(payload["install"][1]))
    report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SupplyChainEvidenceError, match="Duplicate runtime component identity"):
        sbom.build_supply_chain_evidence(
            report,
            project_root=project_root,
            application_name="nika-core",
            application_version="0.0.2",
            source_sha=SOURCE_SHA,
        )


def test_persistent_evidence_never_copies_raw_resolver_url(
    tmp_path: Path,
    installed_metadata: None,
) -> None:
    project_root = _project(tmp_path / "project")
    report = _report(tmp_path / "report.json")

    evidence = sbom.build_supply_chain_evidence(
        report,
        project_root=project_root,
        application_name="nika-core",
        application_version="0.0.2",
        source_sha=SOURCE_SHA,
    )
    serialized = json.dumps(evidence, sort_keys=True)

    assert "resolver-user" not in serialized
    assert "resolver-secret" not in serialized
    assert "resolver-canary" not in serialized
    assert "/httpx.whl" not in serialized
    assert "packages.example.invalid" in serialized
    assert evidence["resolver"]["raw_report_persisted"] is False


def test_cyclonedx_is_deterministic_and_binds_source_identity(
    tmp_path: Path,
    installed_metadata: None,
) -> None:
    project_root = _project(tmp_path / "project")
    report = _report(tmp_path / "report.json")
    evidence = sbom.build_supply_chain_evidence(
        report,
        project_root=project_root,
        application_name="nika-core",
        application_version="0.0.2",
        source_sha=SOURCE_SHA,
    )

    first = sbom.build_cyclonedx_sbom(evidence)
    second = sbom.build_cyclonedx_sbom(evidence)

    assert first == second
    assert first["bomFormat"] == "CycloneDX"
    assert first["specVersion"] == "1.6"
    assert first["metadata"]["component"]["properties"] == [
        {"name": "nika:source-sha", "value": SOURCE_SHA}
    ]
    httpx = next(item for item in first["components"] if item["name"] == "httpx")
    assert httpx["hashes"] == [{"alg": "SHA-256", "content": "1" * 64}]

    dependency_map = {
        item["ref"]: item["dependsOn"] for item in first["dependencies"]
    }
    app_ref = "pkg:generic/nika-core@0.0.2"
    httpx_ref = "pkg:pypi/httpx@0.28.1"
    httpcore_ref = "pkg:pypi/httpcore@1.0.9"
    pywebview_ref = "pkg:pypi/pywebview@6.2.1"
    assert dependency_map[app_ref] == [httpx_ref, pywebview_ref]
    assert dependency_map[httpx_ref] == [httpcore_ref]
    assert dependency_map[httpcore_ref] == []


def test_vcs_commit_is_accepted_as_immutable_provenance(
    tmp_path: Path,
    installed_metadata: None,
) -> None:
    project_root = _project(tmp_path / "project")
    report = _report(tmp_path / "report.json")
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["install"][1]["download_info"] = {
        "url": "git+https://example.invalid/runtime.git",
        "vcs_info": {
            "vcs": "git",
            "commit_id": "0123456789abcdef0123456789abcdef01234567",
        },
    }
    report.write_text(json.dumps(payload), encoding="utf-8")

    evidence = sbom.build_supply_chain_evidence(
        report,
        project_root=project_root,
        application_name="nika-core",
        application_version="0.0.2",
        source_sha=SOURCE_SHA,
    )

    httpx = next(item for item in evidence["components"] if item["name"] == "httpx")
    assert httpx["source"] == {
        "kind": "vcs",
        "vcs": "git",
        "commit_id": "0123456789abcdef0123456789abcdef01234567",
    }


@pytest.mark.parametrize(
    ("vcs", "commit_id"),
    [
        ("git", "main"),
        ("git", "refs/heads/main"),
        ("git", "v1.2.3"),
        ("git", "deadbee"),
        ("svn", "0123456789abcdef0123456789abcdef01234567"),
        ("git ", "0123456789abcdef0123456789abcdef01234567"),
    ],
)
def test_vcs_ref_shaped_identity_is_not_immutable_provenance(
    tmp_path: Path,
    installed_metadata: None,
    vcs: str,
    commit_id: str,
) -> None:
    project_root = _project(tmp_path / "project")
    report = _report(tmp_path / "report.json")
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["install"][1]["download_info"] = {
        "url": "git+https://example.invalid/runtime.git",
        "vcs_info": {"vcs": vcs, "commit_id": commit_id},
    }
    report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        SupplyChainEvidenceError,
        match="immutable source artifact identity",
    ):
        sbom.build_supply_chain_evidence(
            report,
            project_root=project_root,
            application_name="nika-core",
            application_version="0.0.2",
            source_sha=SOURCE_SHA,
        )


def test_supply_chain_rejects_missing_active_transitive_dependency(
    tmp_path: Path,
    installed_metadata: None,
) -> None:
    project_root = _project(tmp_path / "project")
    report = _report(tmp_path / "report.json")
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["install"] = [
        item
        for item in payload["install"]
        if item["metadata"]["name"] != "httpcore"
    ]
    report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        SupplyChainEvidenceError,
        match="Resolved runtime dependency is missing from inventory: httpx->httpcore",
    ):
        sbom.build_supply_chain_evidence(
            report,
            project_root=project_root,
            application_name="nika-core",
            application_version="0.0.2",
            source_sha=SOURCE_SHA,
        )


def test_write_verify_and_tamper_detection(
    tmp_path: Path,
    installed_metadata: None,
) -> None:
    project_root = _project(tmp_path / "project")
    report = _report(tmp_path / "report.json")
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    supply_path, sbom_path = sbom.write_supply_chain_evidence(
        bundle,
        report,
        project_root=project_root,
        application_name="nika-core",
        application_version="0.0.2",
        source_sha=SOURCE_SHA,
    )

    assert sbom.verify_supply_chain_evidence(
        bundle,
        report,
        project_root=project_root,
        application_name="nika-core",
        application_version="0.0.2",
        source_sha=SOURCE_SHA,
    ) == ()

    supply_payload = json.loads(supply_path.read_text(encoding="utf-8"))
    supply_payload["application"]["source_sha"] = "b" * 40
    supply_path.write_text(json.dumps(supply_payload), encoding="utf-8")
    assert "supply-chain:mismatch" in sbom.verify_supply_chain_evidence(
        bundle,
        report,
        project_root=project_root,
        application_name="nika-core",
        application_version="0.0.2",
        source_sha=SOURCE_SHA,
    )

    supply_path.write_text(
        sbom._canonical_json(
            sbom.build_supply_chain_evidence(
                report,
                project_root=project_root,
                application_name="nika-core",
                application_version="0.0.2",
                source_sha=SOURCE_SHA,
            )
        ),
        encoding="utf-8",
    )
    sbom_payload = json.loads(sbom_path.read_text(encoding="utf-8"))
    sbom_payload["version"] = 2
    sbom_path.write_text(json.dumps(sbom_payload), encoding="utf-8")
    assert "sbom:mismatch" in sbom.verify_supply_chain_evidence(
        bundle,
        report,
        project_root=project_root,
        application_name="nika-core",
        application_version="0.0.2",
        source_sha=SOURCE_SHA,
    )
