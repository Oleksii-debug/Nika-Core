from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

RUNTIME_DISTRIBUTIONS = (
    "annotated-types",
    "bottle",
    "cffi",
    "clr-loader",
    "packaging",
    "platformdirs",
    "proxy-tools",
    "pycparser",
    "pydantic",
    "pydantic-core",
    "pydantic-settings",
    "pygments",
    "python-dotenv",
    "pythonnet",
    "pywebview",
    "rich",
    "setuptools",
    "tomli",
    "typing-extensions",
    "typing-inspection",
)

SUPPLY_CHAIN_FILE = "THIRD_PARTY_SUPPLY_CHAIN.json"
_ALLOWED_BUNDLE_EXTRAS = frozenset({"gui"})
_BUILD_ONLY_EXTRAS = frozenset({"dev", "qa"})
_RELEASE_CRITICAL_TOOLS = frozenset({"setuptools", "wheel", "pip-audit", "pyinstaller"})
_NATIVE_SUFFIXES = frozenset({".dll", ".exe", ".pyd", ".so", ".dylib"})
_REVIEW_LICENSE_TOKENS = ("agpl", "gpl", "lgpl", "sspl", "bsl", "busl", "proprietary")
_SECTION_RE = re.compile(r"^===== (?P<title>.+?) =====$")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _python_license() -> str:
    license_file = Path(sys.base_prefix) / "LICENSE.txt"
    if not license_file.exists():
        raise RuntimeError(f"Python runtime license not found: {license_file}")
    return license_file.read_text(encoding="utf-8", errors="replace").strip()


def _metadata_license(dist: metadata.Distribution) -> str | None:
    expression = dist.metadata.get("License-Expression")
    if expression and expression.strip():
        return expression.strip()
    license_value = dist.metadata.get("License")
    if license_value and license_value.strip() and license_value.strip().upper() != "UNKNOWN":
        return license_value.strip()
    classifiers = [
        value
        for value in dist.metadata.get_all("Classifier", [])
        if value.startswith("License ::")
    ]
    return "; ".join(classifiers) or None


def _license_texts(dist: metadata.Distribution) -> tuple[tuple[str, str], ...]:
    collected: list[tuple[str, str]] = []
    for item in dist.files or ():
        leaf = Path(str(item)).name.casefold()
        if not any(marker in leaf for marker in ("license", "licence", "copying", "notice")):
            continue
        try:
            path = Path(dist.locate_file(item))
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    collected.append((str(item).replace("\\", "/"), text))
        except OSError:
            continue
    return tuple(sorted(collected))


def _distribution_section(
    distribution_name: str,
    dist: metadata.Distribution,
) -> tuple[str, str]:
    package_name = dist.metadata.get("Name") or distribution_name
    declared_license = _metadata_license(dist)
    license_texts = _license_texts(dist)
    if not declared_license and not license_texts:
        raise RuntimeError(
            f"No license evidence found for runtime distribution: {distribution_name}"
        )
    body: list[str] = []
    if declared_license:
        body.append(f"Declared license: {declared_license}")
    for relative_path, text in license_texts:
        if body:
            body.append("")
        body.extend([f"--- {relative_path} ---", text])
    return f"{package_name} {dist.version}", "\n".join(body).strip()


def _project_urls(dist: metadata.Distribution) -> tuple[str, ...]:
    urls: set[str] = set()
    homepage = (dist.metadata.get("Home-page") or "").strip()
    if homepage:
        urls.add(homepage)
    for item in dist.metadata.get_all("Project-URL", []):
        _, separator, url = item.partition(",")
        candidate = url.strip() if separator else item.strip()
        if candidate:
            urls.add(candidate)
    return tuple(sorted(urls))


def _record_sha256(dist: metadata.Distribution) -> str | None:
    record = dist.read_text("RECORD")
    if not record:
        return None
    return _sha256_bytes(record.encode("utf-8"))


def _native_distribution_files(dist: metadata.Distribution) -> tuple[dict[str, object], ...]:
    found: list[dict[str, object]] = []
    for item in dist.files or ():
        relative = str(item).replace("\\", "/")
        if Path(relative).suffix.casefold() not in _NATIVE_SUFFIXES:
            continue
        try:
            path = Path(dist.locate_file(item))
            if not path.is_file():
                continue
            found.append(
                {
                    "path": relative,
                    "sha256": _sha256_file(path),
                    "size": path.stat().st_size,
                }
            )
        except OSError:
            continue
    return tuple(sorted(found, key=lambda item: str(item["path"])))


def _license_risk(declared_license: str | None) -> str:
    if not declared_license:
        return "metadata-missing-review"
    folded = declared_license.casefold()
    if any(token in folded for token in _REVIEW_LICENSE_TOKENS):
        return "review-required"
    return "no-known-restrictive-token"


def _distribution_evidence(distribution_name: str) -> dict[str, object]:
    try:
        dist = metadata.distribution(distribution_name)
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            f"Required runtime distribution is missing: {distribution_name}"
        ) from exc
    declared_license = _metadata_license(dist)
    license_texts = _license_texts(dist)
    if not declared_license and not license_texts:
        raise RuntimeError(
            f"No license evidence found for runtime distribution: {distribution_name}"
        )
    urls = _project_urls(dist)
    if not urls:
        raise RuntimeError(
            f"No upstream/source URL metadata found for runtime distribution: {distribution_name}"
        )
    installer = (dist.read_text("INSTALLER") or "").strip() or None
    return {
        "name": canonicalize_name(dist.metadata.get("Name") or distribution_name),
        "resolved_version": dist.version,
        "license": declared_license,
        "license_evidence_files": [path for path, _ in license_texts],
        "license_risk": _license_risk(declared_license),
        "project_urls": list(urls),
        "installer": installer,
        "record_sha256": _record_sha256(dist),
        "native_files": list(_native_distribution_files(dist)),
    }


def _find_project_root(bundle_dir: Path) -> Path | None:
    for candidate in (Path.cwd(), *bundle_dir.resolve().parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def _declared_dependency_surface(
    bundle_runtime: set[str],
    project_root: Path | None,
) -> list[dict[str, object]]:
    if project_root is None:
        return []
    with (project_root / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    project = data.get("project", {})
    raw_entries: list[tuple[str, str]] = [
        ("base", str(raw)) for raw in project.get("dependencies", [])
    ]
    optional = project.get("optional-dependencies", {})
    for group, requirements in optional.items():
        raw_entries.extend((str(group), str(raw)) for raw in requirements)

    evidence: list[dict[str, object]] = []
    for group, raw in raw_entries:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement as exc:
            raise RuntimeError(f"Invalid dependency declaration in pyproject.toml: {raw}") from exc
        normalized = canonicalize_name(requirement.name)
        try:
            resolved = metadata.version(requirement.name)
        except metadata.PackageNotFoundError:
            resolved = None
        role = (
            "base-declared"
            if group == "base"
            else "base-bundle-extra"
            if group in _ALLOWED_BUNDLE_EXTRAS
            else "build-only-extra"
            if group in _BUILD_ONLY_EXTRAS
            else "optional-not-bundled"
        )
        evidence.append(
            {
                "group": group,
                "role": role,
                "name": normalized,
                "requirement": raw,
                "specifier": str(requirement.specifier),
                "marker": str(requirement.marker) if requirement.marker else None,
                "resolved_version": resolved,
                "present_in_build_environment": resolved is not None,
                "listed_in_bundle_runtime": normalized in bundle_runtime,
            }
        )
    return sorted(
        evidence,
        key=lambda item: (str(item["group"]), str(item["name"]), str(item["requirement"])),
    )


def _release_critical_declarations(project_root: Path | None) -> list[dict[str, object]]:
    if project_root is None:
        return []
    with (project_root / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    build_requires = [str(raw) for raw in data.get("build-system", {}).get("requires", [])]
    qa_requires = [
        str(raw)
        for raw in data.get("project", {}).get("optional-dependencies", {}).get("qa", [])
    ]
    result: list[dict[str, object]] = []
    for raw in build_requires + qa_requires:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement as exc:
            raise RuntimeError(f"Invalid release-critical dependency declaration: {raw}") from exc
        name = canonicalize_name(requirement.name)
        if name not in _RELEASE_CRITICAL_TOOLS:
            continue
        exact = any(
            spec.operator in {"==", "==="} and "*" not in spec.version
            for spec in requirement.specifier
        )
        try:
            resolved = metadata.version(requirement.name)
        except metadata.PackageNotFoundError:
            resolved = None
        result.append(
            {
                "name": name,
                "requirement": raw,
                "exact_pin": exact,
                "resolved_version": resolved,
            }
        )
    return sorted(result, key=lambda item: str(item["name"]))


def _bundle_native_artifacts(bundle_dir: Path) -> list[dict[str, object]]:
    artifacts: list[dict[str, object]] = []
    for path in sorted(bundle_dir.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in _NATIVE_SUFFIXES:
            continue
        relative = path.relative_to(bundle_dir).as_posix()
        artifacts.append(
            {
                "path": relative,
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
                "origin_class": (
                    "pyinstaller-application"
                    if relative.casefold() == "nikacore.exe"
                    else "python-runtime"
                    if Path(relative).name.casefold().startswith("python")
                    else "packaged-native-runtime"
                ),
            }
        )
    return artifacts


def build_supply_chain_evidence(bundle_dir: Path) -> dict[str, object]:
    bundle_runtime = {canonicalize_name(name) for name in RUNTIME_DISTRIBUTIONS}
    project_root = _find_project_root(bundle_dir)
    return {
        "schema_version": 1,
        "artifact": "NikaCore Windows base runtime",
        "python_runtime": {
            "version": sys.version.split()[0],
            "implementation": sys.implementation.name,
            "license_sha256": _sha256_bytes(_python_license().encode("utf-8")),
        },
        "release_critical_declarations": _release_critical_declarations(project_root),
        "declared_dependency_surface": _declared_dependency_surface(bundle_runtime, project_root),
        "bundle_runtime_distributions": [
            _distribution_evidence(name) for name in RUNTIME_DISTRIBUTIONS
        ],
        "bundle_native_artifacts": _bundle_native_artifacts(bundle_dir),
        "policy": {
            "allowed_bundle_extras": sorted(_ALLOWED_BUNDLE_EXTRAS),
            "build_only_extras": sorted(_BUILD_ONLY_EXTRAS),
            "model_licenses_separate_from_engine": True,
        },
    }


def supply_chain_findings(payload: dict[str, object]) -> tuple[str, ...]:
    findings: list[str] = []
    declarations = payload.get("release_critical_declarations", [])
    if not isinstance(declarations, list):
        return ("supply-chain:release-critical-declarations",)
    for item in declarations:
        if not isinstance(item, dict):
            findings.append("supply-chain:release-critical-declaration-type")
            continue
        if item.get("exact_pin") is not True:
            findings.append(f"supply-chain:unpinned-release-tool:{item.get('name')}")

    dependency_surface = payload.get("declared_dependency_surface", [])
    if not isinstance(dependency_surface, list):
        findings.append("supply-chain:declared-dependency-surface")
    else:
        for item in dependency_surface:
            if not isinstance(item, dict):
                findings.append("supply-chain:dependency-declaration-type")
                continue
            if item.get("role") == "optional-not-bundled" and item.get(
                "listed_in_bundle_runtime"
            ) is True:
                findings.append(
                    f"supply-chain:optional-bundled:{item.get('group')}:{item.get('name')}"
                )

    runtime = payload.get("bundle_runtime_distributions", [])
    if not isinstance(runtime, list):
        findings.append("supply-chain:runtime-distributions")
    else:
        for item in runtime:
            if not isinstance(item, dict):
                findings.append("supply-chain:runtime-distribution-type")
                continue
            name = item.get("name")
            if item.get("license_risk") != "no-known-restrictive-token":
                findings.append(f"supply-chain:license-review:{name}")
            if not item.get("project_urls"):
                findings.append(f"supply-chain:source-provenance:{name}")
            if not item.get("record_sha256"):
                findings.append(f"supply-chain:installed-record:{name}")
    return tuple(dict.fromkeys(findings))


def build_third_party_notices(bundle_dir: Path) -> Path:
    sections = [
        "Nika Core third-party notices",
        "",
        "===== Python runtime =====",
        _python_license(),
    ]
    for distribution_name in RUNTIME_DISTRIBUTIONS:
        try:
            dist = metadata.distribution(distribution_name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(
                f"Required runtime distribution is missing: {distribution_name}"
            ) from exc
        title, body = _distribution_section(distribution_name, dist)
        sections.extend(["", f"===== {title} =====", body])
    target = bundle_dir / "THIRD_PARTY_NOTICES.txt"
    target.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")

    supply_chain = build_supply_chain_evidence(bundle_dir)
    supply_findings = supply_chain_findings(supply_chain)
    if supply_findings:
        raise RuntimeError(f"supply-chain evidence policy failed: {supply_findings}")
    supply_target = bundle_dir / SUPPLY_CHAIN_FILE
    supply_target.write_text(
        json.dumps(supply_chain, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def _sections(text: str) -> tuple[dict[str, str], tuple[str, ...]]:
    parsed: dict[str, str] = {}
    duplicates: list[str] = []
    title: str | None = None
    body: list[str] = []

    def commit() -> None:
        nonlocal body
        if title is None:
            return
        if title in parsed:
            duplicates.append(title)
        else:
            parsed[title] = "\n".join(body).strip()
        body = []

    for line in text.splitlines():
        match = _SECTION_RE.fullmatch(line.strip())
        if match:
            commit()
            title = match.group("title").strip()
            body = []
            continue
        if title is not None:
            body.append(line)
    commit()
    return parsed, tuple(duplicates)


def verify_third_party_notices(bundle_dir: Path) -> tuple[str, ...]:
    target = bundle_dir / "THIRD_PARTY_NOTICES.txt"
    if not target.is_file():
        return ("missing:THIRD_PARTY_NOTICES.txt",)

    text = target.read_text(encoding="utf-8", errors="replace")
    sections, duplicates = _sections(text)
    findings: list[str] = []
    if "Python runtime" in duplicates:
        findings.extend(("notices:pythonruntime", "notices:pythonruntime:duplicate"))
    python_body = sections.get("Python runtime")
    try:
        expected_python_body = _python_license()
    except RuntimeError:
        findings.extend(("notices:pythonruntime", "notices:pythonruntime:metadata"))
    else:
        if python_body != expected_python_body:
            findings.append("notices:pythonruntime")

    for distribution_name in RUNTIME_DISTRIBUTIONS:
        base_finding = f"notices:{distribution_name}"
        try:
            dist = metadata.distribution(distribution_name)
            title, expected_body = _distribution_section(distribution_name, dist)
        except (metadata.PackageNotFoundError, RuntimeError):
            findings.extend((base_finding, f"{base_finding}:metadata"))
            continue
        if title in duplicates:
            findings.extend((base_finding, f"{base_finding}:duplicate"))
            continue
        if sections.get(title) != expected_body:
            findings.append(base_finding)

    supply_target = bundle_dir / SUPPLY_CHAIN_FILE
    if not supply_target.is_file():
        findings.append(f"missing:{SUPPLY_CHAIN_FILE}")
        return tuple(dict.fromkeys(findings))
    try:
        actual_supply = json.loads(supply_target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        findings.append("supply-chain:invalid-json")
        return tuple(dict.fromkeys(findings))
    if not isinstance(actual_supply, dict):
        findings.append("supply-chain:invalid-type")
        return tuple(dict.fromkeys(findings))

    expected_supply = build_supply_chain_evidence(bundle_dir)
    findings.extend(supply_chain_findings(expected_supply))
    if actual_supply != expected_supply:
        findings.append("supply-chain:mismatch")
    return tuple(dict.fromkeys(findings))
