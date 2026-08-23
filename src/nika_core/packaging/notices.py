from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class ThirdPartyNoticeRecord:
    distribution_name: str
    package_name: str
    version: str
    section_title: str
    body: str
    notice_ref: str


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
    for relative_path, license_text in license_texts:
        if body:
            body.append("")
        body.extend([f"--- {relative_path} ---", license_text])
    return f"{package_name} {dist.version}", "\n".join(body).strip()


def _notice_ref(section_title: str, body: str) -> str:
    payload = f"{section_title}\n{body}\n".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return f"artifact:THIRD_PARTY_NOTICES.txt#sha256:{digest}"


def distribution_notice_record(distribution_name: str) -> ThirdPartyNoticeRecord:
    """Resolve the exact installed distribution notice using the canonical notice renderer."""
    if not isinstance(distribution_name, str) or not distribution_name.strip():
        raise ValueError("distribution_name must be non-empty text")
    try:
        dist = metadata.distribution(distribution_name)
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            f"Required runtime distribution is missing: {distribution_name}"
        ) from exc
    title, body = _distribution_section(distribution_name, dist)
    package_name = dist.metadata.get("Name") or distribution_name
    return ThirdPartyNoticeRecord(
        distribution_name=distribution_name,
        package_name=package_name,
        version=dist.version,
        section_title=title,
        body=body,
        notice_ref=_notice_ref(title, body),
    )


def runtime_notice_records() -> tuple[ThirdPartyNoticeRecord, ...]:
    return tuple(distribution_notice_record(name) for name in RUNTIME_DISTRIBUTIONS)


def build_third_party_notices(bundle_dir: Path) -> Path:
    sections = [
        "Nika Core third-party notices",
        "",
        "===== Python runtime =====",
        _python_license(),
    ]
    for record in runtime_notice_records():
        sections.extend(["", f"===== {record.section_title} =====", record.body])
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
            record = distribution_notice_record(distribution_name)
        except RuntimeError:
            findings.extend((base_finding, f"{base_finding}:metadata"))
            continue
        if record.section_title in duplicates:
            findings.extend((base_finding, f"{base_finding}:duplicate"))
            continue
        if sections.get(record.section_title) != record.body:
            findings.append(base_finding)
    return tuple(dict.fromkeys(findings))
