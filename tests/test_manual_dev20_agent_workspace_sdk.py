from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from nika_core.builder.activation import AgentActivationService
from nika_core.builder.compiler import AgentCompiler, RiskTier
from nika_core.builder.drafting import AgentDraftService
from nika_core.builder.proposal import AgentProposalService
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.model_gateway.contracts import ModelResponse, ModelUsage, ProviderKind
from nika_core.tools import ToolRisk, ToolSpec
from nika_core.workspace_sdk import (
    WorkspaceActivationService,
    WorkspaceEntrypointDescriptor,
    WorkspaceManifest,
    WorkspacePluginRepository,
    WorkspaceValidationCatalog,
    compile_workspace_activation,
)
from nika_core.workspace_sdk import discovery


def _agent(*, tool_id: str = "web.read", scopes: tuple[str, ...] = ()) -> AgentDefinition:
    return AgentDefinition(
        agent_id="dev20.agent",
        version=1,
        name="DEV20 agent",
        goal="Perform a bounded task",
        instructions="Use only explicitly declared capabilities.",
        model_profile="local-default",
        tool_grants=(ToolGrant(tool_id=tool_id, max_risk=0, scopes=scopes),),
    )


class _DraftGateway:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    async def complete(self, request):
        return ModelResponse(
            request_id=request.request_id,
            text=self.payload,
            provider_id="fixture",
            provider_kind=ProviderKind.NO_LLM,
            model="fixture",
            usage=ModelUsage(),
        )


def _agent_compiler(
    *tools: ToolSpec,
    permissions: dict[str, set[str]] | None = None,
) -> AgentCompiler:
    return AgentCompiler(
        tools=tools,
        model_profiles={"local-default"},
        permission_catalog=permissions,
    )


def test_natural_language_proposal_is_deterministically_registry_validated() -> None:
    drafted = _agent(tool_id="invented.tool").model_dump_json()
    service = AgentProposalService(
        AgentDraftService(_DraftGateway(drafted)),
        _agent_compiler(ToolSpec("web.read", "Read web", ToolRisk.READ_ONLY)),
    )
    with pytest.raises(ValueError, match="unknown tool"):
        asyncio.run(service.propose("Make an agent with whatever tool you need"))


def test_agent_permissions_fail_closed_and_known_scope_compiles() -> None:
    tool = ToolSpec("web.read", "Read web", ToolRisk.READ_ONLY)
    compiler = _agent_compiler(tool, permissions={"web.read": {"network.read"}})
    result = compiler.compile(_agent(scopes=("network.read",)))
    assert result.highest_risk is RiskTier.R0_READ_ONLY

    with pytest.raises(ValueError, match="unknown permission scope"):
        compiler.compile(_agent(scopes=("network.admin",)))
    with pytest.raises(ValueError, match="unknown permission scope"):
        _agent_compiler(tool).compile(_agent(scopes=("network.read",)))


def test_agent_activation_revalidates_live_catalog_and_r4_approval(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = AgentDefinitionRepository(store)
    dangerous = ToolSpec("release.publish", "Publish release", ToolRisk.HIGH_IMPACT)
    definition = _agent(tool_id="release.publish").model_copy(
        update={
            "tool_grants": (
                ToolGrant(
                    tool_id="release.publish",
                    max_risk=RiskTier.R4_HIGH_IMPACT,
                    scopes=("release.write",),
                ),
            )
        }
    )
    compiler = _agent_compiler(
        dangerous,
        permissions={"release.publish": {"release.write"}},
    )
    repository.save_draft(compiler.compile(definition))
    activation = AgentActivationService(repository, compiler)

    with pytest.raises(PermissionError, match="human approval"):
        activation.activate(definition)
    activation.activate(definition, approved_tool_ids=frozenset({"release.publish"}))
    assert repository.require_active(definition.agent_id, 1).definition == definition

    second = definition.model_copy(update={"version": 2, "goal": "Second reviewed version"})
    repository.save_draft(compiler.compile(second))
    drifted = AgentActivationService(
        repository,
        _agent_compiler(dangerous, permissions={"release.publish": set()}),
    )
    with pytest.raises(ValueError, match="unknown permission scope"):
        drifted.activate(second, approved_tool_ids=frozenset({"release.publish"}))


def _manifest(
    *,
    version: int = 1,
    permission_ids: tuple[str, ...] = ("workspace.read",),
    capability_ids: tuple[str, ...] = ("research.capture",),
    action_ids: tuple[str, ...] = ("workspace.open",),
    sdk_api_version: int = 1,
) -> WorkspaceManifest:
    return WorkspaceManifest(
        workspace_id="research.workspace",
        version=version,
        sdk_api_version=sdk_api_version,
        display_name="Research workspace",
        capability_ids=capability_ids,
        permission_ids=permission_ids,
        action_ids=action_ids,
    )


def _catalog() -> WorkspaceValidationCatalog:
    return WorkspaceValidationCatalog(
        capability_ids=frozenset({"research.capture"}),
        permission_ids=frozenset({"workspace.read", "workspace.write"}),
        action_ids=frozenset({"workspace.open"}),
    )


def _descriptor(name: str = "research") -> WorkspaceEntrypointDescriptor:
    return WorkspaceEntrypointDescriptor(
        name=name,
        value="research_plugin:plugin",
        distribution_name="research-plugin",
        distribution_version="1.0.0",
    )


def test_workspace_manifest_rejects_unknown_compatibility_capability_permission_and_action() -> None:
    catalog = _catalog()
    with pytest.raises(ValueError, match="unsupported workspace SDK API version"):
        catalog.validate(_manifest(sdk_api_version=2))
    with pytest.raises(ValueError, match="unknown capability"):
        catalog.validate(_manifest(capability_ids=("invented.capability",)))
    with pytest.raises(ValueError, match="unknown permission"):
        catalog.validate(_manifest(permission_ids=("workspace.admin",)))
    with pytest.raises(ValueError, match="unknown Action Registry"):
        catalog.validate(_manifest(action_ids=("workspace.delete",)))


def test_workspace_activation_never_expands_declared_permissions() -> None:
    proposal = compile_workspace_activation(
        _manifest(),
        _descriptor(),
        catalog=_catalog(),
        approved_permission_ids=frozenset({"workspace.read", "workspace.write"}),
    )
    assert proposal.effective_permission_ids == ("workspace.read",)

    with pytest.raises(PermissionError, match="require approval"):
        compile_workspace_activation(
            _manifest(permission_ids=("workspace.write",)),
            _descriptor(),
            catalog=_catalog(),
            approved_permission_ids=frozenset({"workspace.read"}),
        )


@dataclass
class _FakeDist:
    name: str = "fixture-dist"
    version: str = "1.2.3"


class _FakeEntryPoint:
    def __init__(self, name: str, value: str, loaded) -> None:
        self.name = name
        self.value = value
        self.dist = _FakeDist()
        self._loaded = loaded

    def load(self):
        if isinstance(self._loaded, Exception):
            raise self._loaded
        return self._loaded


class _Plugin:
    def __init__(self, manifest: WorkspaceManifest) -> None:
        self.manifest = manifest


def test_entrypoint_discovery_is_provider_neutral_and_invalid_plugins_are_isolated(monkeypatch) -> None:
    good = _FakeEntryPoint("good", "good:plugin", _Plugin(_manifest()))
    bad = _FakeEntryPoint("bad", "bad:plugin", RuntimeError("broken import"))
    monkeypatch.setattr(discovery, "_entry_points", lambda *, group: (bad, good))

    descriptors = discovery.discover_workspace_entrypoints()
    assert [item.name for item in descriptors] == ["bad", "good"]
    assert all(type(item) is WorkspaceEntrypointDescriptor for item in descriptors)

    report = discovery.load_workspace_plugins(_catalog())
    assert [item.descriptor.name for item in report.loaded] == ["good"]
    assert len(report.failures) == 1
    assert report.failures[0].descriptor.name == "bad"
    assert report.failures[0].error_type == "RuntimeError"


def test_duplicate_workspace_identity_version_isolated(monkeypatch) -> None:
    first = _FakeEntryPoint("first", "first:plugin", _Plugin(_manifest()))
    second = _FakeEntryPoint("second", "second:plugin", _Plugin(_manifest()))
    monkeypatch.setattr(discovery, "_entry_points", lambda *, group: (first, second))
    report = discovery.load_workspace_plugins(_catalog())
    assert report.loaded == ()
    assert {item.error_type for item in report.failures} == {"DuplicateWorkspaceVersion"}


def test_workspace_versions_activation_and_restart_persist_exact_permissions(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = WorkspacePluginRepository(store)
    manifest = _manifest()
    proposal = compile_workspace_activation(
        manifest,
        _descriptor(),
        catalog=_catalog(),
        approved_permission_ids=frozenset({"workspace.read", "workspace.write"}),
    )
    repository.save_candidate(proposal)
    WorkspaceActivationService(repository, _catalog()).activate(
        manifest,
        _descriptor(),
        approved_permission_ids=frozenset({"workspace.read", "workspace.write"}),
    )

    restarted = WorkspacePluginRepository(SQLiteStore(store.path))
    active = restarted.active(manifest.workspace_id)
    assert active is not None
    assert active.manifest == manifest
    assert active.effective_permission_ids == ("workspace.read",)
    assert restarted.next_version(manifest.workspace_id) == 2

    second_manifest = _manifest(version=2, permission_ids=("workspace.write",))
    second_proposal = compile_workspace_activation(
        second_manifest,
        _descriptor(),
        catalog=_catalog(),
        approved_permission_ids=frozenset({"workspace.write"}),
    )
    restarted.save_candidate(second_proposal)
    WorkspaceActivationService(restarted, _catalog()).activate(
        second_manifest,
        _descriptor(),
        approved_permission_ids=frozenset({"workspace.write"}),
    )
    current = restarted.active(manifest.workspace_id)
    assert current is not None
    assert current.manifest.version == 2
    retired = restarted.get(manifest.workspace_id, 1)
    assert retired is not None
    assert retired.status == "retired"

    with pytest.raises(ValueError, match="expected 3"):
        restarted.save_candidate(second_proposal)
