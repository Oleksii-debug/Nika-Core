"""Prove frozen module ownership against the canonical packaged license notices."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from packaging.utils import canonicalize_name

from nika_core.packaging.notices import _sections, verify_third_party_notices

# Independently audited 32 third-party roots from exact M11 PYZ inventories.
# Click was an unlocked runner package: an isolated build must not need it.
ROOT_DISTRIBUTIONS = {
    "_distutils_hack": "setuptools",
    "_pytest": "pytest",
    "annotated_types": "annotated-types",
    "anyio": "anyio",
    "bottle": "bottle",
    "certifi": "certifi",
    "cffi": "cffi",
    "click": "click",
    "clr": "pythonnet",
    "clr_loader": "clr-loader",
    "dotenv": "python-dotenv",
    "h11": "h11",
    "httpcore": "httpcore",
    "httpx": "httpx",
    "idna": "idna",
    "markdown_it": "markdown-it-py",
    "mdurl": "mdurl",
    "packaging": "packaging",
    "platformdirs": "platformdirs",
    "proxy_tools": "proxy-tools",
    "pycparser": "pycparser",
    "pydantic": "pydantic",
    "pydantic_core": "pydantic-core",
    "pydantic_settings": "pydantic-settings",
    "pygments": "pygments",
    "pythonnet": "pythonnet",
    "rich": "rich",
    "setuptools": "setuptools",
    "typing_extensions": "typing-extensions",
    "typing_inspection": "typing-inspection",
    "webview": "pywebview",
    "wheel": "wheel",
}


def module_notice_findings(
    modules: set[str], notice_titles: set[str],
) -> tuple[str, ...]:
    roots = {name.partition(".")[0] for name in modules}
    third_party = roots - sys.stdlib_module_names - {"nika_core"}
    noticed = {canonicalize_name(title.rsplit(" ", 1)[0]) for title in notice_titles}
    findings = []
    for root in sorted(third_party):
        owner = ROOT_DISTRIBUTIONS.get(root)
        if owner is None:
            findings.append(f"unmapped-packaged-module:{root}")
        elif canonicalize_name(owner) not in noticed:
            findings.append(f"missing-packaged-distribution-notice:{owner}")
    return tuple(findings)


def verify_bundle(bundle: Path) -> tuple[str, ...]:
    # PyInstaller is QA tooling, not a new runtime dependency.
    from PyInstaller.archive.readers import CArchiveReader

    findings = list(verify_third_party_notices(bundle))
    reader = CArchiveReader(str(bundle / "NikaCore.exe"))
    pyz = reader.open_embedded_archive("PYZ.pyz")
    modules = set(pyz.toc)
    if "nika_core" not in modules:
        findings.append("missing-packaged-nika-core")
    text = (bundle / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    sections, duplicates = _sections(text)
    if duplicates:
        findings.append("duplicate-packaged-notice-sections")
    findings.extend(module_notice_findings(modules, set(sections)))
    if not findings:
        roots = {name.partition(".")[0] for name in modules}
        third_party = roots - sys.stdlib_module_names - {"nika_core"}
        print(
            f"Verified {len(third_party)} packaged third-party roots "
            f"against {len(sections)} canonical notice sections."
        )
    return tuple(findings)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    try:
        findings = verify_bundle(args.bundle)
    except (OSError, ValueError, KeyError, RuntimeError):
        print("Packaged notice inspection failed.", file=sys.stderr)
        return 1
    for finding in findings:
        print(finding, file=sys.stderr)
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
