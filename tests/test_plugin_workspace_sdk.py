from pathlib import Path

import pytest
from pydantic import ValidationError

from nika_core.plugins import (
    CapabilityDeclaration,
    PluginCompatibilityError,
    PluginManifest,
    PluginRuntime,
)
from nika_core.tools import ToolRisk
from nika_core.workspaces import (
    ACCESSIBILITY_REPAIR_MANIFEST,
    SOFTWARE_FACTORY_MANIFEST,
    AccessibilityEvidence,
    CapabilityGap,
    CodingRequest,
    PluginRequirement,
    WorkspaceCatalog,
    WorkspaceCompatibilityError,
    WorkspaceManifest,
    WorkspaceResolver,
)
from nika_core.workspaces.accessibility_repair import EvidenceMethod
from nika_core.workspaces.catalog import WorkspaceCapabilityGrant


def _coding_manifest(*, write_risk: ToolRisk = ToolRisk.LOCAL_WRITE) -> PluginManifest:
    return PluginManifest(
        plugin_id="coding.worker",
        name="Coding worker",
        version="1.0.0",
        entrypoint_name="coding-worker",
        capabilities=(
            CapabilityDeclaration(
                capability_id="coding.repository.read",
                risk=ToolRisk.READ_ONLY,
                description="Read the isolated source workspace",
            ),
            CapabilityDeclaration(
                capability_id="coding.workspace.write",
                risk=write_risk,
                description="Write only inside the isolated workspace",
            ),
            CapabilityDeclaration(
                capability_id="coding.tests.run",
                risk=ToolRisk.LOCAL_WRITE,
                description="Run declared verification commands",
            ),
        ),
    )


def _interaction_manifest() -> PluginManifest:
    return PluginManifest(
        plugin_id="interaction.semantic",
        name="Semantic interaction",
        version="1.0.0",
        entrypoint_name="interaction-semantic",
        capabilities=(
            CapabilityDeclaration(
                capability_id="browser.dom.inspect",
                risk=ToolRisk.READ_ONLY,
                description="Inspect browser semantics",
            ),
            CapabilityDeclaration(
                capability_id="windows.uia.inspect",
                risk=ToolRisk.READ_ONLY,
                description="Inspect Windows UI Automation semantics",
            ),
        ),
    )


class _Adapter:
    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_plugin_runtime_checks_api_and_runtime_manifest() -> None:
    manifest = _coding_manifest()
    runtime = PluginRuntime()
    runtime.register(manifest, lambda: _Adapter(manifest))
    adapter = runtime.activate("coding.worker")
    assert adapter.manifest == manifest
    assert runtime.activate("coding.worker") is adapter
    runtime.deactivate("coding.worker")
    assert adapter.closed is True

    incompatible = manifest.model_copy(update={"plugin_api_min": 2, "plugin_api_max": 2})
    with pytest.raises(PluginCompatibilityError):
        PluginRuntime().register(incompatible, lambda: _Adapter(incompatible))


def test_plugin_manifest_rejects_duplicate_capabilities() -> None:
    capability = CapabilityDeclaration(
        capability_id="same.capability",
        description="same",
    )
    with pytest.raises(ValidationError):
        PluginManifest(
            plugin_id="duplicate.plugin",
            name="Duplicate",
            version="1",
            entrypoint_name="duplicate-plugin",
            capabilities=(capability, capability),
        )


def test_software_factory_workspace_accepts_bounded_worker() -> None:
    WorkspaceCatalog().validate(
        SOFTWARE_FACTORY_MANIFEST,
        {"coding.worker": _coding_manifest()},
    )


def test_workspace_fails_closed_on_missing_capability_or_excess_risk() -> None:
    catalog = WorkspaceCatalog()
    incomplete = _coding_manifest().model_copy(
        update={
            "capabilities": (
                CapabilityDeclaration(
                    capability_id="coding.repository.read",
                    description="Read",
                ),
            )
        }
    )
    with pytest.raises(WorkspaceCompatibilityError, match="lacks capabilities"):
        catalog.validate(SOFTWARE_FACTORY_MANIFEST, {"coding.worker": incomplete})

    high_risk = _coding_manifest(write_risk=ToolRisk.HIGH_IMPACT)
    with pytest.raises(WorkspaceCompatibilityError, match="risk exceeds"):
        catalog.validate(SOFTWARE_FACTORY_MANIFEST, {"coding.worker": high_risk})


def test_accessibility_repair_requires_semantic_browser_and_uia() -> None:
    WorkspaceCatalog().validate(
        ACCESSIBILITY_REPAIR_MANIFEST,
        {"interaction.semantic": _interaction_manifest()},
    )
    browser_only = _interaction_manifest().model_copy(
        update={"capabilities": (_interaction_manifest().capabilities[0],)}
    )
    with pytest.raises(WorkspaceCompatibilityError, match="windows.uia.inspect"):
        WorkspaceCatalog().validate(
            ACCESSIBILITY_REPAIR_MANIFEST,
            {"interaction.semantic": browser_only},
        )


def test_workspace_grant_cannot_reference_undeclared_plugin() -> None:
    workspace = WorkspaceManifest(
        workspace_id="bad.workspace",
        name="Bad",
        version="1",
        required_plugins=(PluginRequirement(plugin_id="coding.worker"),),
        capability_grants=(
            WorkspaceCapabilityGrant(
                plugin_id="other.plugin",
                capability_id="other.read",
            ),
        ),
    )
    with pytest.raises(WorkspaceCompatibilityError, match="undeclared plugin"):
        WorkspaceCatalog().validate(workspace, {"coding.worker": _coding_manifest()})


def test_workspace_resolver_blocks_traversal(tmp_path: Path) -> None:
    resolver = WorkspaceResolver(tmp_path / "workspace")
    assert resolver.resolve("artifacts/result.txt") == (
        tmp_path / "workspace" / "artifacts" / "result.txt"
    ).resolve()
    with pytest.raises(ValueError, match="escapes"):
        resolver.resolve("../secret.txt")


def test_coding_request_is_isolated_and_capability_gap_requires_evidence(tmp_path: Path) -> None:
    request = CodingRequest(
        repository_root=tmp_path,
        goal="Implement a bounded adapter",
        allowed_paths=("src/nika_core/workspaces", "tests"),
        test_commands=("python scripts/verify.py",),
    )
    assert request.network_allowed is False
    with pytest.raises(ValueError, match="repository-relative"):
        CodingRequest(tmp_path, "bad", (str(tmp_path.resolve()),))
    with pytest.raises(ValueError, match="traverse"):
        CodingRequest(tmp_path, "bad", ("../outside",))
    with pytest.raises(ValueError, match="attempted methods"):
        CapabilityGap("task-1", "browser.special", "DOM unavailable", ())


def test_visual_fallback_cannot_claim_perfect_semantic_confidence() -> None:
    with pytest.raises(ValueError, match="perfect semantic confidence"):
        AccessibilityEvidence(
            target="legacy-app",
            method=EvidenceMethod.VISION,
            summary="Vision-derived candidate control",
            confidence=1.0,
        )
    evidence = AccessibilityEvidence(
        target="web-page",
        method=EvidenceMethod.DOM,
        summary="Accessible button found by role and name",
        accessible_controls=("button:Submit",),
    )
    assert evidence.confidence == 1.0
