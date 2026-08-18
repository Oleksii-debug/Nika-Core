from pathlib import Path

import pytest

from nika_core.agents.spec import PermissionPolicy, RiskLevel, ToolGrant
from nika_core.packaging.windows import default_windows_plan
from nika_core.plugins.sdk import PluginManifest, PluginRegistry
from nika_core.qa.accessibility import audit_html_accessibility
from nika_core.security.downstream import ActionIntent, DownstreamGuard, HumanApproval
from nika_core.workspaces.registry import WorkspaceResolver, WorkspaceSpec


def test_plugin_registry_is_explicit_and_duplicate_safe() -> None:
    manifest = PluginManifest(
        plugin_id="workspace.notes",
        name="Notes",
        version="1.0.0",
        entrypoint="nika_notes:create_plugin",
        capabilities=("read", "read", "write"),
    )
    registry = PluginRegistry()
    registry.register(manifest, lambda: {"ok": True})
    assert manifest.capabilities == ("read", "write")
    assert registry.create("workspace.notes") == {"ok": True}
    with pytest.raises(ValueError, match="already registered"):
        registry.register(manifest, lambda: object())


def test_workspace_resolver_blocks_escape(tmp_path: Path) -> None:
    resolver = WorkspaceResolver(tmp_path)
    assert resolver.resolve("notes/today.md") == (tmp_path / "notes" / "today.md").resolve()
    with pytest.raises(ValueError, match="escapes"):
        resolver.resolve("../outside.txt")
    spec = WorkspaceSpec(
        workspace_id="research.main",
        name="Research",
        required_plugins=("workspace.notes", "browser.read"),
    )
    assert resolver.validate_plugins(spec, {"workspace.notes"}) == ("browser.read",)


def test_downstream_guard_is_fail_closed_and_r4_requires_human(tmp_path: Path) -> None:
    guard = DownstreamGuard(tmp_path)
    policy = PermissionPolicy(
        tool_grants=(ToolGrant(tool_id="files.write", max_risk=RiskLevel.R2_EXTERNAL_WRITE),),
        allow_filesystem_write=True,
    )
    guard.authorize(ActionIntent("files.write", RiskLevel.R2_EXTERNAL_WRITE, "notes/a.txt"), policy)
    with pytest.raises(PermissionError, match="workspace"):
        guard.authorize(ActionIntent("files.write", RiskLevel.R2_EXTERNAL_WRITE, "../a.txt"), policy)
    r4 = ActionIntent("legal.submit", RiskLevel.R4_EXPLICIT_HUMAN)
    with pytest.raises(PermissionError, match="explicit human"):
        guard.authorize(r4, policy)
    guard.authorize(r4, policy, HumanApproval("submit-1", True, "human-confirmed"))


def test_windows_build_plan_keeps_assets_and_uses_onedir(tmp_path: Path) -> None:
    args = default_windows_plan(tmp_path).pyinstaller_args()
    assert "--onedir" in args
    assert "--windowed" in args
    assert "--add-data" in args
    assert any("nika_core/ui/web" in value for value in args)


def test_accessibility_source_gate_accepts_semantic_shell() -> None:
    html = """
    <html><body><main><label for='q'>Command</label><textarea id='q'></textarea>
    <button>Run</button><p role='status'>Ready</p></main></body></html>
    """
    assert audit_html_accessibility(html) == ()


def test_accessibility_source_gate_reports_missing_semantics() -> None:
    findings = audit_html_accessibility("<html><body><textarea></textarea><button></button></body></html>")
    codes = {finding.code for finding in findings}
    assert {"form-label", "button-name", "main-landmark", "live-status"} <= codes
