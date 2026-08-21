from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

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

_LICENSE_NAMES = ("license", "licence", "copying", "notice")


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _metadata_license(dist: metadata.Distribution) -> str:
    value = (dist.metadata.get("License-Expression") or dist.metadata.get("License") or "").strip()
    if value:
        return value
    classifiers = [
        item.removeprefix("License :: ").strip()
        for item in dist.metadata.get_all("Classifier", [])
        if item.startswith("License :: ")
    ]
    return "; ".join(classifiers)


def _license_texts(dist: metadata.Distribution) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    for relative in dist.files or ():
        name = Path(str(relative)).name.lower()
        if not any(token in name for token in _LICENSE_NAMES):
            continue
        path = Path(dist.locate_file(relative))
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")
        found.append((str(relative).replace("\\", "/"), text.strip()))
    return tuple(sorted(found))


def _python_license() -> tuple[str, str]:
    candidates = (
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
        Path(sys.prefix) / "LICENSE.txt",
        Path(sys.prefix) / "LICENSE",
    )
    for path in candidates:
        if path.is_file():
            return ("Python runtime", path.read_text(encoding="utf-8", errors="replace").strip())
    raise RuntimeError("Python runtime license file was not found")


def _distribution_evidence(dist: metadata.Distribution, requested_name: str) -> tuple[str, str, str]:
    package_name = dist.metadata.get("Name") or requested_name
    version = dist.version.strip()
    if not version:
        raise RuntimeError(f"runtime distribution has no version: {package_name}")
    return package_name, version, f"{package_name}=={version}"


def build_third_party_notices(bundle_dir: Path) -> Path:
    """Create deterministic third-party notices for the shipped Windows runtime closure."""

    sections: list[str] = [
        "Nika Core third-party notices",
        "Generated from the exact Windows release build environment.",
        "",
    ]
    python_label, python_text = _python_license()
    sections.extend((f"===== {python_label} =====", python_text, ""))

    for requested_name in sorted(RUNTIME_DISTRIBUTIONS, key=str.casefold):
        try:
            dist = metadata.distribution(requested_name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(
                f"required runtime distribution is missing from release environment: {requested_name}"
            ) from exc

        package_name, version, provenance = _distribution_evidence(dist, requested_name)
        declared_license = _metadata_license(dist)
        license_texts = _license_texts(dist)
        if not declared_license and not license_texts:
            raise RuntimeError(f"no license metadata/text found for runtime distribution: {package_name}")

        sections.append(f"===== {package_name} {version} =====")
        sections.append(f"Distribution provenance: {provenance}")
        if declared_license:
            sections.append(f"Declared license: {declared_license}")
        if license_texts:
            for relative_path, text in license_texts:
                sections.extend((f"--- {relative_path} ---", text))
        sections.append("")

    target = bundle_dir / "THIRD_PARTY_NOTICES.txt"
    target.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")
    return target


def verify_third_party_notices(bundle_dir: Path) -> tuple[str, ...]:
    """Validate exact version/license/provenance evidence, not package names alone."""

    target = bundle_dir / "THIRD_PARTY_NOTICES.txt"
    if not target.is_file():
        return ("missing:THIRD_PARTY_NOTICES.txt",)
    text = target.read_text(encoding="utf-8", errors="replace")
    findings: list[str] = []
    if "===== Python runtime =====" not in text:
        findings.append("notices:python-runtime")

    for requested_name in RUNTIME_DISTRIBUTIONS:
        try:
            dist = metadata.distribution(requested_name)
        except metadata.PackageNotFoundError:
            findings.append(f"notices-provenance-unverifiable:{requested_name}")
            continue

        package_name, version, provenance = _distribution_evidence(dist, requested_name)
        section_header = f"===== {package_name} {version} ====="
        if section_header not in text:
            findings.append(f"notices-version:{requested_name}")
            continue
        section_start = text.index(section_header) + len(section_header)
        next_section = text.find("\n===== ", section_start)
        section = text[section_start:] if next_section < 0 else text[section_start:next_section]

        if f"Distribution provenance: {provenance}" not in section:
            findings.append(f"notices-provenance:{requested_name}")

        declared_license = _metadata_license(dist)
        license_texts = _license_texts(dist)
        license_evidence = False
        if declared_license and f"Declared license: {declared_license}" in section:
            license_evidence = True
        if any(f"--- {relative_path} ---" in section and text_value for relative_path, text_value in license_texts):
            license_evidence = True
        if not license_evidence:
            findings.append(f"notices-license:{requested_name}")

    return tuple(findings)
