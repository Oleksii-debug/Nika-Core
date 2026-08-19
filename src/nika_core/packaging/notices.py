from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path

RUNTIME_DISTRIBUTIONS = (
    "platformdirs",
    "pydantic",
    "pydantic-settings",
    "pywebview",
    "pythonnet",
    "clr-loader",
    "proxy-tools",
    "cffi",
    "pycparser",
)

_LICENSE_NAMES = ("license", "licence", "copying", "notice")


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


def build_third_party_notices(bundle_dir: Path) -> Path:
    """Create deterministic third-party notices for the shipped Windows runtime."""

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

        package_name = dist.metadata.get("Name") or requested_name
        version = dist.version
        declared_license = _metadata_license(dist)
        license_texts = _license_texts(dist)
        if not declared_license and not license_texts:
            raise RuntimeError(f"no license metadata/text found for runtime distribution: {package_name}")

        sections.append(f"===== {package_name} {version} =====")
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
    target = bundle_dir / "THIRD_PARTY_NOTICES.txt"
    if not target.is_file():
        return ("missing:THIRD_PARTY_NOTICES.txt",)
    text = target.read_text(encoding="utf-8", errors="replace")
    findings: list[str] = []
    if "Python runtime" not in text:
        findings.append("notices:python-runtime")
    for name in RUNTIME_DISTRIBUTIONS:
        if name.casefold() not in text.casefold():
            findings.append(f"notices:{name}")
    return tuple(findings)
