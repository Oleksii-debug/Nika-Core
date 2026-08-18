import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from nika_core.plugins import (
    CapabilityDeclaration,
    PluginCompatibilityError,
    PluginManifest,
    PluginRegistration,
    PluginRuntime,
)
from nika_core.tools import ToolRisk
from nika_core.workspaces import (
    ACCESSIBILITY_REPAIR_MANIFEST,
    SOFTWARE_FACTORY_MANIFEST,
    AccessibilityEvidence,
    AccessibilityRepairService,
    CapabilityGap,
    CodingRequest,
    CodingResult,
    PluginRequirement,
    SoftwareFactoryService,
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


class _EntryPoint:
    def __init__(self, name: str, loaded: object) -> None:
        self.name = name
        self._loaded = loaded

    def load(self) -> object:
        return self._loaded


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


def test_entrypoint_registration_is_lazy_until_activation() -> None:
    manifest = _coding_manifest()
    calls = 0

    def factory() -> _Adapter:
        nonlocal calls
        calls += 1
        return _Adapter(manifest)

    registration = PluginRegistration(manifest=manifest, factory=factory)
    runtime = PluginRuntime()
    runtime.register_entrypoint(_EntryPoint("coding-worker", registration))  # type: ignore[arg-type]
    assert calls == 0
    runtime.activate("coding.worker")
    assert calls == 1

    with pytest.raises(TypeError, match="PluginRegistration"):
        PluginRuntime().register_entrypoint(  # type: ignore[arg-type]
            _EntryPoint("coding-worker", factory)
        )


def test_plugin_activation_closes_manifest_mismatch() -> None:
    manifest = _coding_manifest()
    wrong = manifest.model_copy(update={"version": "2.0.0"})
    adapter = _Adapter(wrong)
    runtime = PluginRuntime()
    runtime.register(manifest, lambda: adapter)
    with pytest.raises(PluginCompatibilityError, match="differs"):
        runtime.activate("coding.worker")
    assert adapter.closed is True


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
    with pytest.raises(ValueError, match="inside the repository"):
        CodingRequest(tmp_path, "bad", (str(tmp_path.resolve()),))
    with pytest.raises(ValueError, match="inside the repository"):
        CodingRequest(tmp_path, "bad", ("../outside",))
    with pytest.raises(ValueError, match="attempted methods"):
        CapabilityGap("task-1", "browser.special", "DOM unavailable", ())


def test_software_factory_rejects_worker_scope_escape_or_missing_evidence(tmp_path: Path) -> None:
    class Worker:
        def __init__(self, result: CodingResult) -> None:
            self.result = result

        async def execute(self, request: CodingRequest) -> CodingResult:
            return self.result

        async def cancel(self, task_id: str) -> None:
            return None

    request = CodingRequest(
        repository_root=tmp_path,
        goal="Implement safely",
        allowed_paths=("src",),
        test_commands=("pytest",),
    )
    valid = CodingResult(
        changed_paths=("src/nika_core/new.py",),
        test_evidence=("pytest passed",),
        patch_ref="artifact://patch-1",
    )
    assert asyncio.run(SoftwareFactoryService(Worker(valid)).execute(request)) == valid

    escaped = valid.__class__(
        changed_paths=("tests/escape.py",),
        test_evidence=("pytest passed",),
        patch_ref="artifact://patch-2",
    )
    with pytest.raises(ValueError, match="outside allowed scope"):
        asyncio.run(SoftwareFactoryService(Worker(escaped)).execute(request))

    missing_tests = valid.__class__(
        changed_paths=("src/nika_core/new.py",),
        test_evidence=(),
        patch_ref="artifact://patch-3",
    )
    with pytest.raises(ValueError, match="no test evidence"):
        asyncio.run(SoftwareFactoryService(Worker(missing_tests)).execute(request))


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
                confidence=0.7,
            )

    service = AccessibilityRepairService(Semantic(), Fallback())
    browser = asyncio.run(service.inspect_browser("page"))
    assert browser.method is EvidenceMethod.VISION
    assert events == ["dom", "vision"]

    events.clear()
    windows = asyncio.run(service.inspect_windows("window"))
    assert windows.method is EvidenceMethod.UIA
    assert events == ["uia"]
