from __future__ import annotations

from pathlib import Path


def _source(path: str) -> str:
    root = Path(__file__).resolve().parents[2]
    return (root / path).read_text(encoding="utf-8")


def test_packaged_task_create_is_wired_to_real_three_agent_execution() -> None:
    windows_source = _source("scripts/nika_windows.py")
    backend_source = _source("src/nika_core/ui/desktop_backend.py")

    start = windows_source.index("    backend = DesktopBackend(")
    end = windows_source.index("    products = ProductProjectCommandService", start)
    packaged_backend_construction = windows_source[start:end]

    assert '"task.create": product_router.create' in windows_source
    assert "V01PackagedTeamStateProvider" in windows_source

    assert "runtime=" in packaged_backend_construction, (
        "packaged build_windows_bridge() constructs DesktopBackend without an explicit runtime/team "
        "adapter, so task.create falls through to DesktopBackend's default single ReferenceRuntime "
        "instead of the current durable three-agent execution path"
    )
    assert "self._runtime = runtime or ReferenceRuntime()" not in backend_source or (
        "MultiAgent" in packaged_backend_construction or "ThreeAgent" in packaged_backend_construction
    ), (
        "packaged task.create has no explicit multi-agent/team execution wiring; the read-only team "
        "state projection can display an existing team but cannot make a newly submitted packaged "
        "task execute through the real three-agent successor"
    )
