from __future__ import annotations

import hashlib
import json
import re
import tempfile
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

SBOM_FILE = "THIRD_PARTY_SBOM.cdx.json"
SUPPLY_CHAIN_FILE = "THIRD_PARTY_SUPPLY_CHAIN.json"
_CYCLONEDX_SCHEMA = "https://cyclonedx.org/schema/bom-1.6.schema.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_VCS_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_IMMUTABLE_HEX_VCS = frozenset({"git", "hg"})


class SupplyChainEvidenceError(RuntimeError):
    pass


class _DuplicateJsonKey(ValueError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8-sig")
        value = json.loads(text, object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeError, json.JSONDecodeError, _DuplicateJsonKey) as exc:
        raise SupplyChainEvidenceError(f"Invalid JSON evidence: {path.name}") from exc
    if not isinstance(value, dict):
        raise SupplyChainEvidenceError(f"JSON evidence must be an object: {path.name}")
    return value


def _atomic_write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(_canonical_json(value))
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def _bounded_license_label(value: str) -> str | None:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 512
        or any(ord(character) < 32 for character in normalized)
    ):
        return None
    return normalized


def _metadata_license(dist: metadata.Distribution) -> str | None:
    expression = _bounded_license_label(dist.metadata.get("License-Expression") or "")
    if expression:
        return expression
    legacy = _bounded_license_label(dist.metadata.get("License") or "")
    if legacy and legacy.upper() != "UNKNOWN":
        return legacy
    classifiers = [
        item
        for item in dist.metadata.get_all("Classifier", [])
        if item.startswith("License ::")
    ]
    return _bounded_license_label("; ".join(classifiers))


def _license_evidence(dist: metadata.Distribution) -> tuple[dict[str, str], ...]:
    evidence: list[dict[str, str]] = []
    for item in dist.files or ():
        leaf = Path(str(item)).name.casefold()
        if not any(token in leaf for token in ("license", "licence", "copying", "notice")):
            continue
        try:
            path = Path(dist.locate_file(item))
            if not path.is_file():
                continue
            digest = _sha256_bytes(path.read_bytes())
        except OSError:
            continue
        evidence.append({"path": str(item).replace("\\", "/"), "sha256": digest})
    return tuple(sorted(evidence, key=lambda item: (item["path"], item["sha256"])))


def _installed_distribution_evidence(name: str, version: str) -> dict[str, object]:
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError as exc:
        raise SupplyChainEvidenceError(
            f"Resolved runtime distribution is not installed: {name}"
        ) from exc
    if dist.version != version:
        raise SupplyChainEvidenceError(
            "Installed runtime version mismatch for "
            f"{name}: report={version} installed={dist.version}"
        )
    declared_license = _metadata_license(dist)
    license_files = _license_evidence(dist)
    if not declared_license and not license_files:
        raise SupplyChainEvidenceError(f"Runtime distribution has no license evidence: {name}")
    record = dist.read_text("RECORD")
    return {
        "declared_license": declared_license,
        "license_evidence": list(license_files),
        "installed_record_sha256": _sha256_bytes(record.encode("utf-8")) if record else None,
    }


def _source_identity(item: dict[str, Any], name: str) -> dict[str, str]:
    download_info = item.get("download_info")
    if not isinstance(download_info, dict):
        raise SupplyChainEvidenceError(f"Runtime component lacks download provenance: {name}")

    archive_info = download_info.get("archive_info")
    if isinstance(archive_info, dict):
        hashes = archive_info.get("hashes")
        sha256 = hashes.get("sha256") if isinstance(hashes, dict) else None
        if isinstance(sha256, str):
            normalized = sha256.strip().casefold()
            if _SHA256_RE.fullmatch(normalized):
                return {"kind": "archive", "sha256": normalized}

    vcs_info = download_info.get("vcs_info")
    if isinstance(vcs_info, dict):
        vcs = vcs_info.get("vcs")
        commit_id = vcs_info.get("commit_id")
        normalized_vcs = vcs.casefold() if isinstance(vcs, str) else ""
        normalized_commit = commit_id.casefold() if isinstance(commit_id, str) else ""
        if (
            isinstance(vcs, str)
            and vcs == vcs.strip()
            and normalized_vcs in _IMMUTABLE_HEX_VCS
            and isinstance(commit_id, str)
            and commit_id == commit_id.strip()
            and _VCS_COMMIT_RE.fullmatch(normalized_commit)
        ):
            return {
                "kind": "vcs",
                "vcs": normalized_vcs,
                "commit_id": normalized_commit,
            }

    raise SupplyChainEvidenceError(
        f"Runtime component lacks immutable source artifact identity: {name}"
    )


def _safe_source_host(item: dict[str, Any]) -> str | None:
    download_info = item.get("download_info")
    if not isinstance(download_info, dict):
        return None
    url = download_info.get("url")
    if not isinstance(url, str):
        return None
    try:
        split = urlsplit(url)
    except ValueError:
        return None
    host = split.hostname
    if not host or len(host) > 253:
        return None
    return host.casefold()


def _report_environment(report: dict[str, Any]) -> dict[str, str]:
    raw_environment = report.get("environment")
    if not isinstance(raw_environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in raw_environment.items()
    ):
        raise SupplyChainEvidenceError("pip installation report environment is invalid")
    required_keys = set(default_environment())
    missing = sorted(required_keys - set(raw_environment))
    if missing:
        raise SupplyChainEvidenceError(
            f"pip installation report environment is incomplete: {missing}"
        )
    return dict(raw_environment)


def _read_runtime_report(report_path: Path, *, application_name: str) -> dict[str, object]:
    report = _load_json_object(report_path)
    if report.get("version") != "1":
        raise SupplyChainEvidenceError("Unsupported pip installation report version")
    pip_version = report.get("pip_version")
    if not isinstance(pip_version, str) or not pip_version:
        raise SupplyChainEvidenceError("pip installation report is missing pip_version")
    raw_install = report.get("install")
    if not isinstance(raw_install, list):
        raise SupplyChainEvidenceError("pip installation report is missing install inventory")

    environment = _report_environment(report)
    application_identity = canonicalize_name(application_name)
    application_version: str | None = None
    components: list[dict[str, object]] = []
    requirements_by_name: dict[str, object] = {}
    seen: set[str] = set()

    for raw_item in raw_install:
        if not isinstance(raw_item, dict):
            raise SupplyChainEvidenceError("pip installation report contains a non-object item")
        raw_metadata = raw_item.get("metadata")
        if not isinstance(raw_metadata, dict):
            raise SupplyChainEvidenceError("pip installation report item is missing metadata")
        raw_name = raw_metadata.get("name")
        raw_version = raw_metadata.get("version")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise SupplyChainEvidenceError("pip installation report item is missing package name")
        if not isinstance(raw_version, str) or not raw_version.strip():
            raise SupplyChainEvidenceError(
                f"pip installation report item is missing version: {raw_name}"
            )

        name = canonicalize_name(raw_name)
        version = raw_version.strip()
        if name == application_identity:
            if application_version is not None and application_version != version:
                raise SupplyChainEvidenceError("Application appears with conflicting versions")
            application_version = version
            continue
        if name in seen:
            raise SupplyChainEvidenceError(f"Duplicate runtime component identity: {name}")
        seen.add(name)
        if raw_item.get("is_yanked") is True:
            raise SupplyChainEvidenceError(f"Yanked runtime component is forbidden: {name}")

        installed = _installed_distribution_evidence(name, version)
        source = _source_identity(raw_item, name)
        component: dict[str, object] = {
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{name}@{version}",
            "requested": raw_item.get("requested") is True,
            "source": source,
            "license": installed["declared_license"],
            "license_evidence": installed["license_evidence"],
            "installed_record_sha256": installed["installed_record_sha256"],
        }
        source_host = _safe_source_host(raw_item)
        if source_host:
            component["source_host"] = source_host
        components.append(component)
        requirements_by_name[name] = raw_metadata.get("requires_dist")

    if application_version is None:
        raise SupplyChainEvidenceError(
            f"pip installation report does not contain application metadata: {application_identity}"
        )
    return {
        "pip_version": pip_version,
        "application_version": application_version,
        "environment": environment,
        "components": sorted(
            components,
            key=lambda item: (str(item["name"]), str(item["version"])),
        ),
        "requirements_by_name": requirements_by_name,
    }

def _project_identity(project_root: Path) -> tuple[str, str]:
    try:
        with (project_root / "pyproject.toml").open("rb") as handle:
            project_data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SupplyChainEvidenceError("Could not read pyproject.toml project identity") from exc
    project = project_data.get("project")
    if not isinstance(project, dict):
        raise SupplyChainEvidenceError("pyproject.toml is missing [project]")
    raw_name = project.get("name")
    raw_version = project.get("version")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise SupplyChainEvidenceError("pyproject.toml project name is missing")
    if not isinstance(raw_version, str) or not raw_version.strip():
        raise SupplyChainEvidenceError("pyproject.toml project version is missing")
    return canonicalize_name(raw_name), raw_version.strip()


def _runtime_requirements(
    project_root: Path,
    *,
    extras: tuple[str, ...],
    environment: dict[str, str],
) -> dict[str, tuple[str, ...]]:
    try:
        with (project_root / "pyproject.toml").open("rb") as handle:
            project_data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SupplyChainEvidenceError(
            "Could not read pyproject.toml runtime declarations"
        ) from exc

    project = project_data.get("project")
    if not isinstance(project, dict):
        raise SupplyChainEvidenceError("pyproject.toml is missing [project]")

    entries: list[tuple[str | None, object]] = [
        (None, raw) for raw in project.get("dependencies", [])
    ]
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise SupplyChainEvidenceError("pyproject optional-dependencies must be a table")
    for extra in extras:
        requirements = optional.get(extra)
        if not isinstance(requirements, list):
            raise SupplyChainEvidenceError(f"Required runtime extra is missing: {extra}")
        entries.extend((extra, raw) for raw in requirements)

    dependencies: dict[str, set[str]] = {}
    for active_extra, raw in entries:
        if not isinstance(raw, str):
            raise SupplyChainEvidenceError("Runtime dependency declaration must be a string")
        try:
            requirement = Requirement(raw)
        except InvalidRequirement as exc:
            raise SupplyChainEvidenceError(
                f"Invalid runtime dependency declaration: {raw}"
            ) from exc
        marker_environment = dict(environment)
        marker_environment["extra"] = active_extra or ""
        if requirement.marker is not None and not requirement.marker.evaluate(marker_environment):
            continue
        name = canonicalize_name(requirement.name)
        dependencies.setdefault(name, set()).update(requirement.extras)
    return {
        name: tuple(sorted(selected_extras))
        for name, selected_extras in sorted(dependencies.items())
    }


def _metadata_requirement_edges(
    raw_requirements: object,
    *,
    environment: dict[str, str],
    active_extras: set[str],
    component_name: str,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if raw_requirements is None:
        return ()
    if not isinstance(raw_requirements, list):
        raise SupplyChainEvidenceError(
            f"Runtime component dependency metadata is invalid: {component_name}"
        )

    dependencies: dict[str, set[str]] = {}
    marker_extras = ("", *sorted(active_extras))
    for raw in raw_requirements:
        if not isinstance(raw, str):
            raise SupplyChainEvidenceError(
                f"Runtime component dependency declaration is invalid: {component_name}"
            )
        try:
            requirement = Requirement(raw)
        except InvalidRequirement as exc:
            raise SupplyChainEvidenceError(
                f"Invalid resolved runtime dependency declaration: {component_name}"
            ) from exc

        enabled = requirement.marker is None
        if requirement.marker is not None:
            enabled = any(
                requirement.marker.evaluate({**environment, "extra": extra})
                for extra in marker_extras
            )
        if not enabled:
            continue
        name = canonicalize_name(requirement.name)
        dependencies.setdefault(name, set()).update(requirement.extras)
    return tuple(
        (name, tuple(sorted(selected_extras)))
        for name, selected_extras in sorted(dependencies.items())
    )


def _resolved_dependency_graph(
    components: list[dict[str, object]],
    *,
    requirements_by_name: dict[str, object],
    application_requirements: dict[str, tuple[str, ...]],
    environment: dict[str, str],
) -> dict[str, tuple[str, ...]]:
    component_names = {
        str(component["name"]) for component in components if isinstance(component, dict)
    }
    selected_extras: dict[str, set[str]] = {
        name: set(extras) for name, extras in application_requirements.items()
    }
    graph: dict[str, set[str]] = {name: set() for name in component_names}

    changed = True
    while changed:
        changed = False
        for name in sorted(component_names):
            edges = _metadata_requirement_edges(
                requirements_by_name.get(name),
                environment=environment,
                active_extras=selected_extras.get(name, set()),
                component_name=name,
            )
            for dependency_name, dependency_extras in edges:
                if dependency_name not in component_names:
                    raise SupplyChainEvidenceError(
                        "Resolved runtime dependency is missing from inventory: "
                        f"{name}->{dependency_name}"
                    )
                graph[name].add(dependency_name)
                target_extras = selected_extras.setdefault(dependency_name, set())
                before = len(target_extras)
                target_extras.update(dependency_extras)
                if len(target_extras) != before:
                    changed = True

    return {
        name: tuple(sorted(dependencies))
        for name, dependencies in sorted(graph.items())
    }

def build_supply_chain_evidence(
    report_path: Path,
    *,
    project_root: Path,
    application_name: str,
    application_version: str,
    source_sha: str,
    extras: tuple[str, ...] = ("gui",),
) -> dict[str, object]:
    normalized_sha = source_sha.strip().casefold()
    if not _SOURCE_SHA_RE.fullmatch(normalized_sha):
        raise SupplyChainEvidenceError("Application source SHA must be a lowercase 40-hex commit")
    if not application_version or application_version != application_version.strip():
        raise SupplyChainEvidenceError("Application version must be non-empty and normalized")

    project_name, project_version = _project_identity(project_root)
    if (
        project_name != canonicalize_name(application_name)
        or project_version != application_version
    ):
        raise SupplyChainEvidenceError(
            "Application identity differs between pyproject.toml and release arguments"
        )

    report = _read_runtime_report(report_path, application_name=application_name)
    if report["application_version"] != application_version:
        raise SupplyChainEvidenceError(
            "Application version differs between pyproject/release and pip installation report"
        )

    components = report["components"]
    environment = report["environment"]
    requirements_by_name = report["requirements_by_name"]
    if (
        not isinstance(components, list)
        or not isinstance(environment, dict)
        or not isinstance(requirements_by_name, dict)
    ):
        raise SupplyChainEvidenceError("Resolved runtime inventory is structurally invalid")

    application_requirements = _runtime_requirements(
        project_root,
        extras=extras,
        environment=environment,
    )
    component_names = {
        str(item["name"]) for item in components if isinstance(item, dict)
    }
    missing = sorted(set(application_requirements) - component_names)
    if missing:
        raise SupplyChainEvidenceError(
            f"Declared runtime dependencies are missing from resolved inventory: {missing}"
        )

    graph = _resolved_dependency_graph(
        components,
        requirements_by_name=requirements_by_name,
        application_requirements=application_requirements,
        environment=environment,
    )
    persistent_components: list[dict[str, object]] = []
    for component in components:
        if not isinstance(component, dict):
            raise SupplyChainEvidenceError("Resolved runtime component must be an object")
        name = component.get("name")
        if not isinstance(name, str):
            raise SupplyChainEvidenceError("Resolved runtime component name is invalid")
        persistent = dict(component)
        persistent["dependencies"] = list(graph[name])
        persistent_components.append(persistent)

    return {
        "schema_version": 3,
        "application": {
            "name": canonicalize_name(application_name),
            "version": application_version,
            "source_sha": normalized_sha,
        },
        "resolver": {
            "kind": "pip-install-report",
            "report_version": "1",
            "pip_version": report["pip_version"],
            "raw_report_persisted": False,
        },
        "runtime_extras": list(extras),
        "declared_runtime_dependencies": list(application_requirements),
        "components": persistent_components,
        "policy": {
            "immutable_source_identity_required": True,
            "yanked_components_forbidden": True,
            "license_evidence_required": True,
            "model_licenses_separate_from_runtime": True,
        },
    }

def build_cyclonedx_sbom(supply_chain: dict[str, object]) -> dict[str, object]:
    application = supply_chain.get("application")
    raw_components = supply_chain.get("components")
    direct_dependencies = supply_chain.get("declared_runtime_dependencies")
    if (
        not isinstance(application, dict)
        or not isinstance(raw_components, list)
        or not isinstance(direct_dependencies, list)
    ):
        raise SupplyChainEvidenceError("Supply-chain evidence is structurally incomplete")
    app_name = application.get("name")
    app_version = application.get("version")
    source_sha = application.get("source_sha")
    if not all(
        isinstance(value, str) and value
        for value in (app_name, app_version, source_sha)
    ):
        raise SupplyChainEvidenceError("Application identity is structurally incomplete")

    app_ref = f"pkg:generic/{app_name}@{app_version}"
    components: list[dict[str, object]] = []
    purl_by_name: dict[str, str] = {}
    dependency_names_by_ref: dict[str, tuple[str, ...]] = {}

    for raw in raw_components:
        if not isinstance(raw, dict):
            raise SupplyChainEvidenceError("Runtime component must be an object")
        name = raw.get("name")
        version = raw.get("version")
        purl = raw.get("purl")
        source = raw.get("source")
        raw_dependencies = raw.get("dependencies")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(purl, str)
            or not isinstance(source, dict)
            or not isinstance(raw_dependencies, list)
            or any(not isinstance(item, str) for item in raw_dependencies)
        ):
            raise SupplyChainEvidenceError("Runtime component identity is incomplete")
        if name in purl_by_name:
            raise SupplyChainEvidenceError(f"Duplicate SBOM component identity: {name}")
        purl_by_name[name] = purl
        dependency_names_by_ref[purl] = tuple(sorted(raw_dependencies))

        component: dict[str, object] = {
            "type": "library",
            "bom-ref": purl,
            "name": name,
            "version": version,
            "purl": purl,
        }
        declared_license = raw.get("license")
        if isinstance(declared_license, str) and declared_license:
            component["licenses"] = [{"license": {"name": declared_license}}]

        properties: list[dict[str, str]] = []
        if source.get("kind") == "archive":
            sha256 = source.get("sha256")
            if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
                raise SupplyChainEvidenceError(f"Invalid archive provenance for {name}")
            component["hashes"] = [{"alg": "SHA-256", "content": sha256}]
            properties.append({"name": "nika:source-kind", "value": "archive"})
        elif source.get("kind") == "vcs":
            vcs = source.get("vcs")
            commit_id = source.get("commit_id")
            if (
                not isinstance(vcs, str)
                or not isinstance(commit_id, str)
                or not _VCS_COMMIT_RE.fullmatch(commit_id)
            ):
                raise SupplyChainEvidenceError(f"Invalid VCS provenance for {name}")
            properties.extend(
                [
                    {"name": "nika:source-kind", "value": "vcs"},
                    {"name": "nika:vcs", "value": vcs},
                    {"name": "nika:vcs-commit", "value": commit_id},
                ]
            )
        else:
            raise SupplyChainEvidenceError(f"Unknown source provenance type for {name}")

        source_host = raw.get("source_host")
        if isinstance(source_host, str) and source_host:
            properties.append({"name": "nika:source-host", "value": source_host})
        record_sha256 = raw.get("installed_record_sha256")
        if isinstance(record_sha256, str) and _SHA256_RE.fullmatch(record_sha256):
            properties.append(
                {"name": "nika:installed-record-sha256", "value": record_sha256}
            )
        for item in raw.get("license_evidence", []):
            if (
                isinstance(item, dict)
                and isinstance(item.get("path"), str)
                and isinstance(item.get("sha256"), str)
            ):
                properties.append(
                    {
                        "name": "nika:license-evidence",
                        "value": f"{item['path']}#sha256:{item['sha256']}",
                    }
                )
        if properties:
            component["properties"] = sorted(
                properties, key=lambda item: (item["name"], item["value"])
            )
        components.append(component)

    if any(
        not isinstance(name, str) or name not in purl_by_name
        for name in direct_dependencies
    ):
        raise SupplyChainEvidenceError(
            "Application dependency graph references an unknown component"
        )

    dependencies: list[dict[str, object]] = [
        {
            "ref": app_ref,
            "dependsOn": sorted(purl_by_name[name] for name in direct_dependencies),
        }
    ]
    for ref, dependency_names in sorted(dependency_names_by_ref.items()):
        unknown = sorted(set(dependency_names) - set(purl_by_name))
        if unknown:
            raise SupplyChainEvidenceError(
                f"SBOM dependency graph references unknown components: {unknown}"
            )
        dependencies.append(
            {
                "ref": ref,
                "dependsOn": sorted(purl_by_name[name] for name in dependency_names),
            }
        )

    metadata_component = {
        "type": "application",
        "bom-ref": app_ref,
        "name": app_name,
        "version": app_version,
        "purl": app_ref,
        "properties": [{"name": "nika:source-sha", "value": source_sha}],
    }
    return {
        "$schema": _CYCLONEDX_SCHEMA,
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {"component": metadata_component},
        "components": sorted(
            components, key=lambda item: (str(item["name"]), str(item["version"]))
        ),
        "dependencies": dependencies,
    }

def write_supply_chain_evidence(
    bundle_dir: Path,
    report_path: Path,
    *,
    project_root: Path,
    application_name: str,
    application_version: str,
    source_sha: str,
    extras: tuple[str, ...] = ("gui",),
) -> tuple[Path, Path]:
    supply_chain = build_supply_chain_evidence(
        report_path,
        project_root=project_root,
        application_name=application_name,
        application_version=application_version,
        source_sha=source_sha,
        extras=extras,
    )
    sbom = build_cyclonedx_sbom(supply_chain)
    supply_path = _atomic_write_json(bundle_dir / SUPPLY_CHAIN_FILE, supply_chain)
    sbom_path = _atomic_write_json(bundle_dir / SBOM_FILE, sbom)
    findings = verify_supply_chain_evidence(
        bundle_dir,
        report_path,
        project_root=project_root,
        application_name=application_name,
        application_version=application_version,
        source_sha=source_sha,
        extras=extras,
    )
    if findings:
        raise SupplyChainEvidenceError(f"Supply-chain verification failed: {findings}")
    return supply_path, sbom_path


def verify_supply_chain_evidence(
    bundle_dir: Path,
    report_path: Path,
    *,
    project_root: Path,
    application_name: str,
    application_version: str,
    source_sha: str,
    extras: tuple[str, ...] = ("gui",),
) -> tuple[str, ...]:
    expected_supply = build_supply_chain_evidence(
        report_path,
        project_root=project_root,
        application_name=application_name,
        application_version=application_version,
        source_sha=source_sha,
        extras=extras,
    )
    expected_sbom = build_cyclonedx_sbom(expected_supply)

    findings: list[str] = []
    supply_path = bundle_dir / SUPPLY_CHAIN_FILE
    sbom_path = bundle_dir / SBOM_FILE
    if not supply_path.is_file():
        findings.append(f"missing:{SUPPLY_CHAIN_FILE}")
    else:
        try:
            actual_supply = _load_json_object(supply_path)
        except SupplyChainEvidenceError:
            findings.append("supply-chain:invalid")
        else:
            if actual_supply != expected_supply:
                findings.append("supply-chain:mismatch")

    if not sbom_path.is_file():
        findings.append(f"missing:{SBOM_FILE}")
    else:
        try:
            actual_sbom = _load_json_object(sbom_path)
        except SupplyChainEvidenceError:
            findings.append("sbom:invalid")
        else:
            if actual_sbom != expected_sbom:
                findings.append("sbom:mismatch")
    return tuple(findings)
