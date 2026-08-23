from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from nika_core.agent_builder import (
    AgentBuilderCompiler,
    AgentBuilderError,
    AgentDraft,
    AgentTemplate,
    DeterministicDraftInterpreter,
)
from nika_core.domain import RiskLevel, ToolDefinition, ToolRisk, WorkspaceDefinition
from nika_core.kernel import AgentRegistry, WorkspaceRegistry
from nika_core.kernel.workspaces import discover_workspace_plugins
from nika_core.security import PermissionScope
from nika_core.workspaces import (
    AccessibilityEvidence,
    AccessibilityRepairService,
    EvidenceMethod,
    WorkspaceCapability,
    WorkspaceCompatibility,
    WorkspaceDependency,
    WorkspaceManifest,
    WorkspacePermissionRequest,
    WorkspacePluginDescriptor,
    WorkspaceSdkError,
    register_workspace_plugin,
    validate_manifest_against_capabilities,
)


def test_workspace_manifest_roundtrip() -> None:
    manifest = WorkspaceManifest(
        workspace_id="research",
        name="Research",
        version="1.0.0",
        description="Research workspace",
        capabilities=(WorkspaceCapability("research.query", "1"),),
        dependencies=(WorkspaceDependency("core", ">=0.0.2"),),
        requested_permissions=(WorkspacePermissionRequest("network:research"),),
        compatibility=WorkspaceCompatibility(min_core_version="0.0.2"),
    )
    assert WorkspaceManifest.from_json(manifest.to_json()) == manifest


def test_workspace_manifest_rejects_duplicates() -> None:
    with pytest.raises(WorkspaceSdkError, match="duplicate workspace capability"):
        WorkspaceManifest(
            workspace_id="research",
            name="Research",
            version="1.0.0",
            description="Research workspace",
            capabilities=(
                WorkspaceCapability("research.query", "1"),
                WorkspaceCapability("research.query", "2"),
            ),
        )


def test_manifest_validation_against_capabilities() -> None:
    manifest = WorkspaceManifest(
        workspace_id="research",
        name="Research",
        version="1.0.0",
        description="Research workspace",
        capabilities=(WorkspaceCapability("research.query", "1"),),
    )
    validate_manifest_against_capabilities(manifest, {"research.query": "1"})
    with pytest.raises(WorkspaceSdkError, match="workspace capability version mismatch"):
        validate_manifest_against_capabilities(manifest, {"research.query": "2"})


def test_workspace_plugin_registration_rejects_duplicate_ids() -> None:
    registry: dict[str, WorkspacePluginDescriptor] = {}
    descriptor = WorkspacePluginDescriptor(
        manifest=WorkspaceManifest(
            workspace_id="research",
            name="Research",
            version="1.0.0",
            description="Research workspace",
        ),
        source="test",
    )
    register_workspace_plugin(registry, descriptor)
    with pytest.raises(WorkspaceSdkError, match="workspace plugin already registered"):
        register_workspace_plugin(registry, descriptor)


def test_workspace_plugin_discovery_isolated_failures(monkeypatch) -> None:
    @dataclass
    class EntryPoint:
        name: str
        value: str
        group: str = "nika.workspaces"

        def load(self):
            if self.name == "broken":
                raise RuntimeError("boom")
            return WorkspacePluginDescriptor(
                manifest=WorkspaceManifest(
                    workspace_id=self.name,
                    name=self.name.title(),
                    version="1.0.0",
                    description="plugin",
                ),
                source=self.value,
            )

    monkeypatch.setattr(
        "nika_core.kernel.workspaces.entry_points",
        lambda group=None: [
            EntryPoint("good", "pkg:good"),
            EntryPoint("broken", "pkg:broken"),
        ],
    )
    discovered, failures = discover_workspace_plugins()
    assert [item.manifest.workspace_id for item in discovered] == ["good"]
    assert failures == {"broken": "RuntimeError"}


def test_agent_builder_compiles_registry_validated_template(tmp_path) -> None:
    agents = AgentRegistry(tmp_path / "nika.db")
    workspaces = WorkspaceRegistry(tmp_path / "nika.db")
    workspaces.register(
        WorkspaceDefinition(
            workspace_id="research",
            name="Research",
            version="1.0.0",
            enabled=True,
        )
    )
    tools = {
        "research.query": ToolDefinition(
            tool_id="research.query",
            description="Query research sources",
            risk=ToolRisk(level=RiskLevel.LOW),
        )
    }
    compiler = AgentBuilderCompiler(
        agents=agents,
        workspaces=workspaces,
        tools=tools,
        interpreter=DeterministicDraftInterpreter(),
    )
    template = compiler.compile(
        AgentDraft(
            name="Researcher",
            description="Use research.query in research",
            workspace_id="research",
            model_provider="local",
            model_name="qwen3:8b",
        )
    )
    assert isinstance(template, AgentTemplate)
    assert template.workspace_id == "research"
    assert template.tool_ids == ("research.query",)


def test_agent_builder_rejects_unknown_workspace(tmp_path) -> None:
    compiler = AgentBuilderCompiler(
        agents=AgentRegistry(tmp_path / "nika.db"),
        workspaces=WorkspaceRegistry(tmp_path / "nika.db"),
        tools={},
        interpreter=DeterministicDraftInterpreter(),
    )
    with pytest.raises(AgentBuilderError, match="unknown workspace"):
        compiler.compile(
            AgentDraft(
                name="Researcher",
                description="Research",
                workspace_id="missing",
                model_provider="local",
                model_name="qwen3:8b",
            )
        )


def test_agent_builder_derives_permission_ceiling(tmp_path) -> None:
    agents = AgentRegistry(tmp_path / "nika.db")
    workspaces = WorkspaceRegistry(tmp_path / "nika.db")
    workspaces.register(
        WorkspaceDefinition(
            workspace_id="research",
            name="Research",
            version="1.0.0",
            enabled=True,
        )
    )
    tools = {
        "research.query": ToolDefinition(
            tool_id="research.query",
            description="Query research sources",
            risk=ToolRisk(level=RiskLevel.LOW),
        )
    }
    compiler = AgentBuilderCompiler(
        agents=agents,
        workspaces=workspaces,
        tools=tools,
        interpreter=DeterministicDraftInterpreter(),
    )
    template = compiler.compile(
        AgentDraft(
            name="Researcher",
            description="Use research.query in research",
            workspace_id="research",
            model_provider="local",
            model_name="qwen3:8b",
        )
    )
    assert template.permission_ceiling == PermissionScope.WORKSPACE


def test_agent_builder_rejects_unavailable_tool(tmp_path) -> None:
    agents = AgentRegistry(tmp_path / "nika.db")
    workspaces = WorkspaceRegistry(tmp_path / "nika.db")
    workspaces.register(
        WorkspaceDefinition(
            workspace_id="research",
            name="Research",
            version="1.0.0",
            enabled=True,
        )
    )
    compiler = AgentBuilderCompiler(
        agents=agents,
        workspaces=workspaces,
        tools={},
        interpreter=DeterministicDraftInterpreter(),
    )
    with pytest.raises(AgentBuilderError, match="unknown tool"):
        compiler.compile(
            AgentDraft(
                name="Researcher",
                description="Use research.query in research",
                workspace_id="research",
                model_provider="local",
                model_name="qwen3:8b",
            )
        )


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


def test_accessibility_repair_calls_semantics_before_visual_fallback() -> None:
    events: list[str] = []

    class Semantic:
        async def inspect_browser(self, target: str) -> AccessibilityEvidence:
            events.append("dom")
            return AccessibilityEvidence(
                target=target,
                method=EvidenceMethod.DOM,
                summary="No semantic controls exposed",
                accessible_controls=(),
            )

        async def inspect_windows(self, target: str) -> AccessibilityEvidence:
            events.append("uia")
            return AccessibilityEvidence(
                target=target,
                method=EvidenceMethod.UIA,
                summary="Named control exposed",
                accessible_controls=("button:Open",),
            )

    class Fallback:
        async def inspect_visual(self, target: str) -> AccessibilityEvidence:
            events.append("vision")
            return AccessibilityEvidence(
                target=target,
                method=EvidenceMethod.VISION,
                summary="Visual fallback candidate",
                accessible_controls=("button:Open",),
                confidence=0.7,
            )

    service = AccessibilityRepairService(Semantic(), Fallback())
    browser = asyncio.run(service.inspect_browser("page"))
    assert browser.method is EvidenceMethod.VISION
    assert browser.accessible_controls == ("button:Open",)
    assert events == ["dom", "vision"]

    events.clear()
    windows = asyncio.run(service.inspect_windows("window"))
    assert windows.method is EvidenceMethod.UIA
    assert events == ["uia"]
