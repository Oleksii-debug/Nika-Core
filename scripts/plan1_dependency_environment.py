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
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any, Callable

from packaging.requirements import Requirement


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


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
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on missing or out-of-range selected applicable distributions",
    )
    args = parser.parse_args(argv)
    report = observe_dependencies(args.root, tuple(args.groups))
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return int(args.strict and bool(report["missing_or_mismatched"]))


if __name__ == "__main__":
    raise SystemExit(main())
