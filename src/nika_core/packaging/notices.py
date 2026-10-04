from __future__ import annotations

import os
import re
import stat
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
_MAX_NOTICES_BYTES = 16 * 1024 * 1024


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


def _read_notices(target: Path) -> str | None:
    """Admit only bounded, regular, stable UTF-8 license evidence."""
    try:
        before = target.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_NOTICES_BYTES:
            return None
        with target.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or (opened.st_size, opened.st_mtime_ns)
                != (before.st_size, before.st_mtime_ns)
            ):
                return None
            data = source.read(_MAX_NOTICES_BYTES + 1)
            after = os.fstat(source.fileno())
        current = target.lstat()
        if (
            len(data) > _MAX_NOTICES_BYTES
            or len(data) != after.st_size
            or (opened.st_dev, opened.st_ino, opened.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_mtime_ns)
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino, current.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_mtime_ns)
        ):
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeError):
        return None


def verify_third_party_notices(bundle_dir: Path) -> tuple[str, ...]:
    target = bundle_dir / "THIRD_PARTY_NOTICES.txt"
    try:
        target.lstat()
    except FileNotFoundError:
        return ("missing:THIRD_PARTY_NOTICES.txt",)
    except OSError:
        return ("notices:unreadable",)

    text = _read_notices(target)
    if text is None:
        return ("notices:unreadable",)
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
