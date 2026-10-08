"""Plan 1 / Section 1: keep Nika-owned contracts independent of replaceable engines.

This is an intentionally narrow, source-level drift gate. It does not assert
that plugins/providers are safe to execute or that a packaged UI is accessible.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# The provider/runtime/web/desktop engines may be imported in their adapters,
# not in these stable domain/port contracts.
OWNED_BOUNDARIES = (
    "src/nika_core/runtime/contracts.py",
    "src/nika_core/intelligence/contracts.py",
    "src/nika_core/model_gateway/contracts.py",
    "src/nika_core/scheduler/contracts.py",
    "src/nika_core/product_command/contracts.py",
    "src/nika_core/plugins/sdk.py",
)

FOREIGN_ENGINE_ROOTS = frozenset(
    {
        "langgraph",
        "langchain",
        "langchain_core",
        "foundry_local_sdk",
        "litellm",
        "openai",
        "httpx",
        "apscheduler",
        "playwright",
        "webview",
        "pywebview",
        "pywinauto",
        "fastapi",
        "starlette",
        "qdrant_client",
        "sqlalchemy",
        "mcp",
    }
)


def direct_engine_imports(source: str) -> tuple[str, ...]:
    """Return direct provider/host imports; malformed source fails rather than passing."""
    tree = ast.parse(source)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = (node.module,)
        else:
            continue
        for name in names:
            if name.split(".", 1)[0] in FOREIGN_ENGINE_ROOTS:
                imports.add(name)
    return tuple(sorted(imports))


@pytest.mark.parametrize("path", OWNED_BOUNDARIES)
def test_nika_owned_ports_remain_adapter_and_presentation_neutral(path: str) -> None:
    source = (REPOSITORY_ROOT / path).read_text(encoding="utf-8")
    assert direct_engine_imports(source) == (), path


def test_architecture_guard_rejects_aliased_and_nested_vendor_imports() -> None:
    source = (
        "import langgraph.graph as orchestration\n"
        "from webview import Window\n"
        "def leak():\n"
        "    from foundry_local_sdk import FoundryLocalManager\n"
    )
    assert direct_engine_imports(source) == (
        "foundry_local_sdk",
        "langgraph.graph",
        "webview",
    )


def test_architecture_guard_does_not_flag_documentation_or_internal_ports() -> None:
    source = (
        '"""import langgraph should not count as an actual import"""\n'
        "from nika_core.runtime.contracts import AgentRuntimePort\n"
        "from pydantic import BaseModel\n"
    )
    assert direct_engine_imports(source) == ()


def test_architecture_guard_fails_on_invalid_python_instead_of_silently_skipping() -> None:
    with pytest.raises(SyntaxError):
        direct_engine_imports("def broken(:\n")
