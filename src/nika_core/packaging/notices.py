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

_SECTION_RE = re.compile(r"^===== (?P<title>.+?) =====$")


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


def distribution_notice_identity(distribution_name: str) -> tuple[str, str]:
    """Return the exact installed distribution version and generated notice title."""
    if not isinstance(distribution_name, str) or not distribution_name.strip():
        raise ValueError("distribution_name must be non-empty text")
    try:
        dist = metadata.distribution(distribution_name)
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            f"Required runtime distribution is missing: {distribution_name}"
        ) from exc
    title, _body = _distribution_section(distribution_name, dist)
    return str(dist.version), title


def build_distribution_notices(
    bundle_dir: Path,
    distribution_names: tuple[str, ...],
    *,
    include_python_runtime: bool = False,
    filename: str = "THIRD_PARTY_NOTICES.txt",
    heading: str = "Nika Core third-party notices",
) -> Path:
    """Build deterministic notices for an explicit distribution inventory."""
    _validate_distribution_names(distribution_names)
    _validate_notice_filename(filename)
    sections = [heading]
    if include_python_runtime:
        sections.extend(["", "===== Python runtime =====", _python_license()])
    for distribution_name in distribution_names:
        try:
            dist = metadata.distribution(distribution_name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(
                f"Required runtime distribution is missing: {distribution_name}"
            ) from exc
        title, body = _distribution_section(distribution_name, dist)
        sections.extend(["", f"===== {title} =====", body])
    target = bundle_dir / filename
    target.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")
    return target


def build_third_party_notices(bundle_dir: Path) -> Path:
    return build_distribution_notices(
        bundle_dir,
        RUNTIME_DISTRIBUTIONS,
        include_python_runtime=True,
    )


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


def verify_distribution_notices(
    bundle_dir: Path,
    distribution_names: tuple[str, ...],
    *,
    include_python_runtime: bool = False,
    filename: str = "THIRD_PARTY_NOTICES.txt",
) -> tuple[str, ...]:
    _validate_distribution_names(distribution_names)
    _validate_notice_filename(filename)
    target = bundle_dir / filename
    if not target.is_file():
        return (f"missing:{filename}",)

    text = target.read_text(encoding="utf-8", errors="replace")
    sections, duplicates = _sections(text)
    findings: list[str] = []

    if include_python_runtime:
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
    elif "Python runtime" in sections:
        findings.append("notices:pythonruntime:unexpected")

    expected_titles: set[str] = set()
    for distribution_name in distribution_names:
        base_finding = f"notices:{distribution_name}"
        try:
            dist = metadata.distribution(distribution_name)
            title, expected_body = _distribution_section(distribution_name, dist)
        except (metadata.PackageNotFoundError, RuntimeError):
            findings.extend((base_finding, f"{base_finding}:metadata"))
            continue
        expected_titles.add(title)
        if title in duplicates:
            findings.extend((base_finding, f"{base_finding}:duplicate"))
            continue
        if sections.get(title) != expected_body:
            findings.append(base_finding)

    allowed_titles = expected_titles | ({"Python runtime"} if include_python_runtime else set())
    for unexpected in sorted(set(sections) - allowed_titles):
        findings.append(f"notices:orphan-section:{unexpected}")
    return tuple(dict.fromkeys(findings))


def verify_third_party_notices(bundle_dir: Path) -> tuple[str, ...]:
    return verify_distribution_notices(
        bundle_dir,
        RUNTIME_DISTRIBUTIONS,
        include_python_runtime=True,
    )


def _validate_distribution_names(distribution_names: tuple[str, ...]) -> None:
    if not isinstance(distribution_names, tuple):
        raise ValueError("distribution_names must be a tuple")
    normalized: list[str] = []
    for name in distribution_names:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("distribution name must be non-empty text")
        normalized.append(re.sub(r"[-_.]+", "-", name.strip().casefold()))
    if len(set(normalized)) != len(normalized):
        raise ValueError("duplicate distribution identity")


def _validate_notice_filename(filename: str) -> None:
    if (
        not isinstance(filename, str)
        or not filename
        or "/" in filename
        or "\\" in filename
        or filename in {".", ".."}
    ):
        raise ValueError("notice filename must be a simple file name")
