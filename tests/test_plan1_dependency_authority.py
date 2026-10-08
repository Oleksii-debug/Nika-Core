"""Plan 1 Section 1: dependency/adoption decisions must track declared project inputs.

This is a *constraint inventory* guard, not a substitute for a resolved lock,
license clearance or actual dependency restore on Windows and Linux.
"""

from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "PLAN1_DEPENDENCY_AUTHORITY.json"
PROJECT = ROOT / "pyproject.toml"
VALID_MODES = frozenset({"REUSE", "ADAPT"})
VALID_ACTIVATION = frozenset({"required", "optional", "test-only"})


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate manifest key: {key}")
        result[key] = value
    return result


def read_manifest(raw: str) -> dict[str, object]:
    value = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    if type(value) is not dict:
        raise ValueError("Manifest must be an object")
    return value


def validate_manifest(manifest: dict[str, object], project: dict[str, object]) -> None:
    metadata = project["project"]
    assert isinstance(metadata, dict)
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or (manifest.get("plan"), manifest.get("section")) != (1, 1)
        or manifest.get("project") != metadata["name"]
        or manifest.get("source_path") != "pyproject.toml"
        or manifest.get("python_compatibility") != metadata["requires-python"]
    ):
        raise ValueError("Wrong Plan 1 identity, schema or Python compatibility")

    # An unconstrained install from pyproject is not an exact resolved lock.
    if manifest.get("resolution_state") != "DECLARED_RANGES_ONLY_NOT_REPRODUCIBLE":
        raise ValueError("Resolved/locked dependency evidence cannot be invented")

    expected = {"base": metadata["dependencies"]}
    expected.update(project["project"]["optional-dependencies"])
    actual = manifest.get("groups")
    if type(actual) is not list or len(actual) != len(expected):
        raise ValueError("Missing/extra adoption group")

    seen: set[str] = set()
    for group in actual:
        if type(group) is not dict:
            raise ValueError("Bad adoption group")
        name = group.get("group")
        if type(name) is not str or name in seen or name not in expected:
            raise ValueError("Unknown or duplicate adoption group")
        seen.add(name)
        declared = group.get("requirements")
        if type(declared) is not list or not declared or not all(
            type(item) is str and item.strip() == item and item for item in declared
        ):
            raise ValueError("Bad dependency constraints")
        if declared != expected[name] or len(set(declared)) != len(declared):
            raise ValueError(f"Untracked, reordered or conflicting constraints: {name}")
        if group.get("mode") not in VALID_MODES:
            raise ValueError("Unknown reuse/adapt decision")
        if group.get("activation") not in VALID_ACTIVATION:
            raise ValueError("Unknown dependency activation boundary")
        for field in ("canonical_owner", "capability"):
            if type(group.get(field)) is not str or not group[field].strip():
                raise ValueError(f"Missing authority attribution: {name}/{field}")
        if group.get("upstream_maintenance") != "NOT_INDEPENDENTLY_VERIFIED":
            raise ValueError("Unsubstantiated upstream maintenance assertion")
        if group.get("license_provenance") != "REQUIRES_PER_PACKAGE_RELEASE_REVIEW":
            raise ValueError("Unsubstantiated package-license clearance")

    exclusions = manifest.get("rejected_competing_authorities")
    if type(exclusions) is not list or len(exclusions) < 4:
        raise ValueError("Missing competing-authority decisions")
    technologies: set[str] = set()
    for item in exclusions:
        if type(item) is not dict or any(
            type(item.get(k)) is not str or not item[k].strip()
            for k in ("technology", "decision", "reason")
        ):
            raise ValueError("Invalid competing-authority decision")
        if item["technology"] in technologies:
            raise ValueError("Duplicate competing-authority decision")
        technologies.add(item["technology"])


def test_current_plan1_dependency_inventory_matches_project_without_unsupported_claims() -> None:
    manifest = read_manifest(MANIFEST.read_text(encoding="utf-8"))
    project = tomllib.loads(PROJECT.read_text(encoding="utf-8"))
    validate_manifest(manifest, project)


@pytest.mark.parametrize(
    ("change", "value"),
    [
        (("schema_version",), True),
        (("plan",), 2),
        (("python_compatibility",), ">=3.9"),
        (("resolution_state",), "DETERMINISTIC_LOCKED"),
        (("groups", 0, "mode"), "CUSTOM_RUNTIME"),
        (("groups", 0, "upstream_maintenance"), "CURRENT"),
        (("groups", 0, "license_provenance"), "VERIFIED"),
        (("groups", 0, "requirements"), ["unreviewed-engine>=0"]),
        (("groups", 0, "canonical_owner"), ""),
    ],
)
def test_adoption_guard_rejects_drift_and_unsupported_authority(
    change: tuple[object, ...], value: object
) -> None:
    manifest = read_manifest(MANIFEST.read_text(encoding="utf-8"))
    project = tomllib.loads(PROJECT.read_text(encoding="utf-8"))
    changed = copy.deepcopy(manifest)
    location = changed
    for key in change[:-1]:
        location = location[key]
    location[change[-1]] = value
    with pytest.raises(ValueError):
        validate_manifest(changed, project)


def test_adoption_guard_rejects_duplicate_group_and_missing_package() -> None:
    manifest = read_manifest(MANIFEST.read_text(encoding="utf-8"))
    project = tomllib.loads(PROJECT.read_text(encoding="utf-8"))
    changed = copy.deepcopy(manifest)
    changed["groups"][1]["group"] = changed["groups"][0]["group"]
    with pytest.raises(ValueError):
        validate_manifest(changed, project)

    changed = copy.deepcopy(manifest)
    changed["groups"][0]["requirements"].pop()
    with pytest.raises(ValueError):
        validate_manifest(changed, project)


def test_adoption_guard_rejects_duplicate_json_keys() -> None:
    with pytest.raises(ValueError, match="Duplicate manifest key"):
        read_manifest('{"schema_version":1,"schema_version":2}')
