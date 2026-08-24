from __future__ import annotations

import hashlib
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

_SECTION_RE = re.compile(r"^===== (?P<title>.+?) =====$")
_PACKAGE_SEPARATORS_RE = re.compile(r"[-_.]+")


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
    return tuple(dict.fromkeys(findings))


def verified_third_party_notice_reference(
    bundle_dir: Path,
    *,
    package_name: str,
    version: str,
) -> str | None:
    """Return the content-bound evidence ref for one verified packaged notice section."""

    if not isinstance(package_name, str) or not package_name.strip():
        return None
    if not isinstance(version, str) or not version.strip():
        return None
    directory = Path(bundle_dir)
    if verify_third_party_notices(directory):
        return None
    target = directory / "THIRD_PARTY_NOTICES.txt"
    text = target.read_text(encoding="utf-8", errors="replace")
    sections, duplicates = _sections(text)
    if duplicates:
        return None

    requested_name = _canonical_package_name(package_name)
    requested_version = version.strip()
    for distribution_name in RUNTIME_DISTRIBUTIONS:
        try:
            dist = metadata.distribution(distribution_name)
            installed_name = dist.metadata.get("Name") or distribution_name
            if _canonical_package_name(installed_name) != requested_name:
                continue
            if dist.version != requested_version:
                return None
            title, expected_body = _distribution_section(distribution_name, dist)
        except (metadata.PackageNotFoundError, RuntimeError):
            return None
        actual_body = sections.get(title)
        if actual_body != expected_body:
            return None
        payload = f"{title}\n{actual_body}".encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        return f"artifact:THIRD_PARTY_NOTICES.txt#sha256:{digest}"
    return None


def _canonical_package_name(value: str) -> str:
    return _PACKAGE_SEPARATORS_RE.sub("-", value.strip().casefold())
