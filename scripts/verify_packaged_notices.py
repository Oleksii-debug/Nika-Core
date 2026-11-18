"""Prove frozen module ownership against the canonical packaged license notices."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from packaging.utils import canonicalize_name

from nika_core.packaging.notices import RUNTIME_DISTRIBUTIONS, verify_third_party_notices

# Independently audited third-party roots from exact M11 PYZ inventories.
# psutil was observed in the resource candidate's Windows frozen inventory.
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
    "psutil": "psutil",
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
    modules: set[str], noticed_distributions: set[str],
) -> tuple[str, ...]:
    roots = {name.partition(".")[0] for name in modules}
    third_party = roots - sys.stdlib_module_names - {"nika_core"}
    noticed = {canonicalize_name(name) for name in noticed_distributions}
    findings = []
    for root in sorted(third_party):
        owner = ROOT_DISTRIBUTIONS.get(root)
        if owner is None:
            findings.append(f"unmapped-packaged-module:{root}")
        elif canonicalize_name(owner) not in noticed:
            findings.append(f"missing-packaged-distribution-notice:{owner}")
    return tuple(findings)


def verify_bundle(bundle: Path) -> tuple[str, ...]:
    findings = list(verify_third_party_notices(bundle))
    if findings:
        return tuple(findings)
    # The canonical verifier has proved every inventory entry and its actual
    # license body using its bounded reader. Do not reread evidence unsafely.
    # PyInstaller is QA tooling, not a new runtime dependency.
    from PyInstaller.archive.readers import CArchiveReader

    reader = CArchiveReader(str(bundle / "NikaCore.exe"))
    pyz = reader.open_embedded_archive("PYZ.pyz")
    modules = set(pyz.toc)
    if "nika_core" not in modules:
        findings.append("missing-packaged-nika-core")
    findings.extend(module_notice_findings(modules, set(RUNTIME_DISTRIBUTIONS)))
    if not findings:
        roots = {name.partition(".")[0] for name in modules}
        third_party = roots - sys.stdlib_module_names - {"nika_core"}
        print(
            f"Verified {len(third_party)} packaged third-party roots "
            f"against {len(RUNTIME_DISTRIBUTIONS) + 1} canonical notice sections."
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
