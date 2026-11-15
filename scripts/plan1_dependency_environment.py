"""Emit reproducible *observation* of installed Plan 1 dependencies.

This is not a lock, an SBOM attestation, an upstream-maintenance decision or
license clearance. It neither installs dependencies nor changes any runtime
or credential authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from nika_core.packaging.notices import _license_texts, _metadata_license


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def observe_transitive_dependencies(
    requirements: list[str], *, lookup: Callable[[str], Any] = distribution,
) -> dict[str, Any]:
    """Follow active installed metadata, including extras, without importing engines.

    License text collection reuses the release notice collector. No environment
    paths, direct_url.json, credentials or unrelated installed packages are read.
    """
    pending = [Requirement(raw) for raw in requirements]
    visited: set[tuple[str, frozenset[str]]] = set()
    records: dict[str, Any] = {}
    failures: set[str] = set()
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        key = (name, frozenset(requirement.extras))
        try:
            installed = lookup(name)
        except PackageNotFoundError:
            failures.add(f"missing:{name}")
            continue
        # Each incoming constraint matters even when the node was already visited.
        if not requirement.specifier.contains(installed.version, prereleases=True):
            failures.add(f"version:{name}:{requirement.specifier}")
        if key in visited:
            continue
        visited.add(key)
        children = []
        for raw in installed.requires or ():
            child = Requirement(raw)
            contexts = {"", *requirement.extras}
            if child.marker is None or any(
                child.marker.evaluate({"extra": extra}) for extra in contexts
            ):
                children.append(raw)
                pending.append(child)
        license_texts = _license_texts(installed)
        previous_edges = records.get(name, {}).get("active_requires", [])
        metadata_text = installed.read_text("METADATA")
        records[name] = {
            "name": name,
            "version": installed.version,
            "active_requires": sorted(set(previous_edges) | set(children)),
            "declared_license": _metadata_license(installed),
            "metadata_text_sha256": _sha256((metadata_text or "").encode("utf-8")),
            "license_texts": [
                {"path": path, "utf8_text_sha256": _sha256(text.encode("utf-8"))}
                for path, text in license_texts
            ],
        }
    return {
        "evidence_class": "INSTALLED_METADATA_AND_LICENSE_TEXT_NOT_CLEARANCE",
        "license_clearance": False,
        "failures": sorted(failures),
        "distributions": [records[name] for name in sorted(records)],
    }


def observe_dependencies(
    root: Path,
    selected_groups: tuple[str, ...],
    *,
    lookup: Callable[[str], Any] = distribution,
) -> dict[str, Any]:
    """Read exact declared inputs and observe the active interpreter environment."""
    project_bytes = (root / "pyproject.toml").read_bytes()
    authority_bytes = (root / "docs" / "PLAN1_DEPENDENCY_AUTHORITY.json").read_bytes()
    project = tomllib.loads(project_bytes.decode("utf-8"))
    authority = json.loads(authority_bytes)
    if (
        type(authority) is not dict
        or (authority.get("plan"), authority.get("section")) != (1, 1)
        or authority.get("project") != project["project"]["name"]
        or authority.get("resolution_state") != "DECLARED_RANGES_ONLY_NOT_REPRODUCIBLE"
    ):
        raise ValueError("Plan 1 declared dependency authority is not the reviewed inventory")
    expected = {
        "base": project["project"]["dependencies"],
        "build-system": project["build-system"]["requires"],
        **project["project"].get("optional-dependencies", {}),
    }
    groups = authority.get("groups")
    if type(groups) is not list or any(type(item) is not dict for item in groups):
        raise ValueError("Invalid dependency authority groups")
    indexed = {item.get("group"): item for item in groups}
    if len(indexed) != len(groups) or set(indexed) != set(expected):
        raise ValueError("Dependency authority groups differ from the project")
    for name, requirements in expected.items():
        if indexed[name].get("requirements") != requirements:
            raise ValueError(f"Declared requirements drift: {name}")
    if not selected_groups or len(set(selected_groups)) != len(selected_groups):
        raise ValueError("Select one or more unique dependency groups")
    if any(name not in expected for name in selected_groups):
        raise ValueError("Unknown dependency group")

    observed = []
    for group in selected_groups:
        for raw in expected[group]:
            requirement = Requirement(raw)
            applies = requirement.marker is None or requirement.marker.evaluate()
            record: dict[str, Any] = {
                "group": group,
                "requirement": raw,
                "distribution": requirement.name,
                "applies": applies,
                "installed_version": None,
                "specifier_satisfied": None,
                "license_expression": None,
                "license_metadata_present": False,
                "evidence_class": "ENVIRONMENT_OBSERVATION_NOT_LICENSE_CLEARANCE",
            }
            if applies:
                try:
                    installed = lookup(requirement.name)
                except PackageNotFoundError:
                    record["specifier_satisfied"] = False
                else:
                    record["installed_version"] = installed.version
                    record["specifier_satisfied"] = requirement.specifier.contains(
                        installed.version, prereleases=True
                    )
                    expression = installed.metadata.get("License-Expression")
                    if expression:
                        # Metadata is only an unreviewed upstream assertion.
                        record["license_expression"] = expression[:256]
                    classifiers = getattr(
                        installed.metadata, "get_all", lambda _: []
                    )("Classifier") or []
                    record["license_metadata_present"] = bool(
                        expression
                        or installed.metadata.get("License")
                        or any(item.startswith("License ::") for item in classifiers)
                    )
            observed.append(record)
    missing_or_mismatched = [
        f"{item['group']}:{item['distribution']}"
        for item in observed
        if item["applies"] and item["specifier_satisfied"] is not True
    ]
    return {
        "schema_version": 1,
        "plan": 1,
        "section": 1,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": sys.platform,
        "project_sha256": _sha256(project_bytes),
        "authority_sha256": _sha256(authority_bytes),
        "selected_groups": list(selected_groups),
        "status": "UNREVIEWED_RESOLUTION_OBSERVATION",
        "license_clearance": False,
        "reproducible_lock_proven": False,
        "missing_or_mismatched": missing_or_mismatched,
        "distributions": observed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--groups", nargs="+", default=["base", "agent", "planning", "dev"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-transitive", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on missing or out-of-range selected applicable distributions",
    )
    args = parser.parse_args(argv)
    report = observe_dependencies(args.root, tuple(args.groups))
    if args.include_transitive:
        report["transitive"] = observe_transitive_dependencies([
            item["requirement"] for item in report["distributions"] if item["applies"]
        ])
    report["source_sha"] = subprocess_source_sha(args.root)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    failures = report["missing_or_mismatched"] or report.get("transitive", {}).get("failures")
    return int(args.strict and bool(failures))


def subprocess_source_sha(root: Path) -> str | None:
    """Bind the observation to source without copying environment or Git credentials."""
    import subprocess

    try:
        value = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return value if len(value) == 40 and all(c in "0123456789abcdef" for c in value) else None


if __name__ == "__main__":
    raise SystemExit(main())
