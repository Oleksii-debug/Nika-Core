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
VALID_ACTIVATION = frozenset({"required", "optional", "test-only", "build-only"})

# Freeze the reviewed adoption *decision*, not merely the presence of a label.
# Changing an engine's canonical owner or REUSE/ADAPT mode requires an explicit
# reviewed code change, not a manifest-only edit that passes the drift guard.
EXPECTED_GROUP_AUTHORITIES = {
    "base": ("REUSE", "Nika-owned application DTOs, persistence, file/HTTP adapters",
        "Platform/data/document parsing"),
    "agent": ("ADAPT", "AgentRuntimePort, SchedulerPort and ModelGateway",
        "Replaceable orchestration, model and scheduling engines"),
    "embedded-ai": ("ADAPT", "ModelGateway provider adapter",
        "Local model inference"),
    "planning": ("ADAPT", "Deterministic Brain contracts and ToolExecutor",
        "Model-free planning engine"),
    "gui": ("ADAPT", "Desktop UI action bridge",
        "Windows shell"),
    "browser": ("ADAPT", "Semantic browser interaction port",
        "Browser DOM automation"),
    "windows-interaction": ("ADAPT", "Semantic UIA interaction port",
        "Windows object-model automation"),
    "learning": ("ADAPT", "Experiments/promotion authority",
        "Measured learning capability"),
    "media": ("ADAPT", "Media acquisition/transcription boundaries",
        "Optional media engines"),
    "credentials": ("REUSE", "Credential/Identity Broker reference boundary",
        "Protected OS credential access"),
    "deployment": ("ADAPT", "Authorized deployment/staging adapter",
        "Remote deployment worker"),
    "dev": ("REUSE", "Repository CI and source-verification harness",
        "Automated development verification"),
    "qa": ("REUSE", "Release/QA provenance gate",
        "Security and packaging verification"),
    "build-system": ("REUSE", "Nika package build backend and source distribution",
        "Build-system dependency and packaging authority"),
}
EXPECTED_EVIDENCE_POLICY = "This file inventories requested dependency constraints and authority boundaries; it is not a lockfile, resolved version assertion, upstream maintenance audit, license certification, installation proof or Section DONE."

EXPECTED_REJECTED_AUTHORITIES = {
    "Microsoft Agent Framework": "SECONDARY_ONLY",
    "CrewAI / Agno / Agent Zero": "REFERENCE_ONLY",
    "Qdrant as durable state": "REJECT_AS_AUTHORITY",
    "WebView2 / ASGI / HTTP DTO as domain core": "REJECT_AS_AUTHORITY",
    "Direct provider SDK use from domain contracts": "REJECT_AS_AUTHORITY",
}


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
        or manifest.get("build_backend") != project["build-system"]["build-backend"]
    ):
        raise ValueError("Wrong Plan 1 identity, schema or Python compatibility")

    if manifest.get("evidence_policy") != EXPECTED_EVIDENCE_POLICY:
        raise ValueError("Evidence policy drift cannot authorize unverified closure")

    # An unconstrained install from pyproject is not an exact resolved lock.
    if manifest.get("resolution_state") != "DECLARED_RANGES_ONLY_NOT_REPRODUCIBLE":
        raise ValueError("Resolved/locked dependency evidence cannot be invented")

    expected = {
        "base": metadata["dependencies"],
        "build-system": project["build-system"]["requires"],
    }
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
        expected_activation = (
            "build-only"
            if name == "build-system"
            else "required"
            if name == "base"
            else "test-only"
            if name in {"dev", "qa"}
            else "optional"
        )
        if group["activation"] != expected_activation:
            raise ValueError(f"Incorrect dependency activation boundary: {name}")
        for field in ("canonical_owner", "capability"):
            if type(group.get(field)) is not str or not group[field].strip():
                raise ValueError(f"Missing authority attribution: {name}/{field}")
        if group.get("upstream_maintenance") != "NOT_INDEPENDENTLY_VERIFIED":
            raise ValueError("Unsubstantiated upstream maintenance assertion")
        if group.get("license_provenance") != "REQUIRES_PER_PACKAGE_RELEASE_REVIEW":
            raise ValueError("Unsubstantiated package-license clearance")

    if seen != set(EXPECTED_GROUP_AUTHORITIES):
        raise ValueError("Canonical adoption group ownership drift")
    for group in actual:
        if (group["mode"], group["canonical_owner"], group["capability"]) != (
            EXPECTED_GROUP_AUTHORITIES[group["group"]]
        ):
            raise ValueError(f"Canonical adoption decision/owner drift: {group['group']}")

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

    if {item["technology"]: item["decision"] for item in exclusions} != EXPECTED_REJECTED_AUTHORITIES:
        raise ValueError("Competing runtime/policy authority rejection drift")


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


def test_build_system_requirements_and_backend_fail_closed_on_drift() -> None:
    manifest = read_manifest(MANIFEST.read_text(encoding="utf-8"))
    project = tomllib.loads(PROJECT.read_text(encoding="utf-8"))
    validate_manifest(manifest, project)

    bad_backend = copy.deepcopy(manifest)
    bad_backend["build_backend"] = "unreviewed.backend"
    with pytest.raises(ValueError, match="identity"):
        validate_manifest(bad_backend, project)

    missing_build_requirement = copy.deepcopy(manifest)
    build_groups = [
        group for group in missing_build_requirement["groups"]
        if group["group"] == "build-system"
    ]
    assert len(build_groups) == 1
    build_groups[0]["requirements"].pop()
    with pytest.raises(ValueError, match="constraints"):
        validate_manifest(missing_build_requirement, project)

    forged_build_activation = copy.deepcopy(manifest)
    build_group = next(
        group for group in forged_build_activation["groups"]
        if group["group"] == "build-system"
    )
    build_group["activation"] = "optional"
    with pytest.raises(ValueError, match="activation"):
        validate_manifest(forged_build_activation, project)

    changed_pyproject = copy.deepcopy(project)
    changed_pyproject["build-system"]["requires"].append("unreviewed-builder>=0")
    with pytest.raises(ValueError, match="constraints"):
        validate_manifest(manifest, changed_pyproject)


@pytest.mark.parametrize(
    ("group_name", "field", "replacement"),
    [
        ("base", "canonical_owner", "External SDK decides all app policy"),
        ("agent", "mode", "REUSE"),
        ("embedded-ai", "canonical_owner", "Unreviewed model provider"),
        ("build-system", "mode", "ADAPT"),
    ],
)
def test_adoption_manifest_cannot_silently_replace_approved_authority(
    group_name: str, field: str, replacement: str
) -> None:
    manifest = read_manifest(MANIFEST.read_text(encoding="utf-8"))
    project = tomllib.loads(PROJECT.read_text(encoding="utf-8"))
    changed = copy.deepcopy(manifest)
    group = next(item for item in changed["groups"] if item["group"] == group_name)
    group[field] = replacement
    with pytest.raises(ValueError, match="decision/owner drift"):
        validate_manifest(changed, project)



def test_adoption_guard_rejects_forged_capability_authority() -> None:
    manifest = read_manifest(MANIFEST.read_text(encoding="utf-8"))
    project = tomllib.loads(PROJECT.read_text(encoding="utf-8"))
    altered = copy.deepcopy(manifest)
    group = next(item for item in altered["groups"] if item["group"] == "agent")
    group["capability"] = "Unreviewed framework owns runtime policy and durable effects"
    with pytest.raises(ValueError, match="decision/owner drift"):
        validate_manifest(altered, project)


def test_adoption_guard_rejects_forged_terminal_or_lock_evidence() -> None:
    manifest = read_manifest(MANIFEST.read_text(encoding="utf-8"))
    project = tomllib.loads(PROJECT.read_text(encoding="utf-8"))
    altered = copy.deepcopy(manifest)
    altered["evidence_policy"] = "All versions locked and Section DONE"
    with pytest.raises(ValueError, match="Evidence policy drift"):
        validate_manifest(altered, project)


@pytest.mark.parametrize(
    ("technology", "replacement"),
    [
        ("Qdrant as durable state", "REUSE"),
        ("Microsoft Agent Framework", "PRIMARY"),
        ("Direct provider SDK use from domain contracts", "ADAPT"),
    ],
)
def test_manifest_cannot_reenable_rejected_competing_authority(
    technology: str, replacement: str
) -> None:
    manifest = read_manifest(MANIFEST.read_text(encoding="utf-8"))
    project = tomllib.loads(PROJECT.read_text(encoding="utf-8"))
    changed = copy.deepcopy(manifest)
    item = next(x for x in changed["rejected_competing_authorities"] if x["technology"] == technology)
    item["decision"] = replacement
    with pytest.raises(ValueError, match="rejection drift"):
        validate_manifest(changed, project)


def test_manifest_cannot_remove_all_competing_authority_rejections() -> None:
    manifest = read_manifest(MANIFEST.read_text(encoding="utf-8"))
    project = tomllib.loads(PROJECT.read_text(encoding="utf-8"))
    changed = copy.deepcopy(manifest)
    changed["rejected_competing_authorities"] = changed["rejected_competing_authorities"][:4]
    with pytest.raises(ValueError, match="rejection drift"):
        validate_manifest(changed, project)
