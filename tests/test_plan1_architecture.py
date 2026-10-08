"""Plan 1 / Section 1: keep Nika-owned contracts independent of replaceable engines.

This guards direct and common dynamic imports; it does not assert
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
    # Core trust, storage, chronology and action authorities must likewise
    # never import replaceable runtime/host SDKs directly.
    "src/nika_core/security/policy.py",
    "src/nika_core/kernel/audit.py",
    "src/nika_core/data/schema.py",
    "src/nika_core/kernel/action_registry.py",
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
    importlib_names = {"importlib"}
    builtins_names = {"builtins"}
    getattr_names = {"getattr"}
    dynamic_function_names = {"__import__"}
    dynamic_source_names = {"exec", "eval", "compile"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = (alias.name for alias in node.names)
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_names.add(alias.asname or "importlib")
                if alias.name == "builtins":
                    builtins_names.add(alias.asname or "builtins")
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = (node.module,)
            if node.module == "importlib":
                dynamic_function_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "import_module"
                )
            if node.module == "builtins":
                getattr_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "getattr"
                )
                dynamic_function_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "__import__"
                )
                dynamic_source_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name in {"exec", "eval", "compile"}
                )
        else:
            continue
        for name in names:
            if name.split(".", 1)[0] in FOREIGN_ENGINE_ROOTS:
                imports.add(name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Refuse acquisition of dangerous builtin source evaluators, including
        # when the retrieved callable is stored and invoked in a later statement.
        if (
            isinstance(func, ast.Name)
            and func.id in getattr_names
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in builtins_names
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in {"exec", "eval", "compile"}
        ):
            imports.add("<dynamic-source-execution>")
        # Core ports have no reason to evaluate dynamically supplied Python code.
        # Otherwise a vendor import can be hidden inside a string and evade the
        # import AST walk. This is a drift fence, not a Python sandbox.
        if (
            isinstance(func, ast.Name)
            and func.id in dynamic_source_names
        ) or (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in builtins_names
            and func.attr in {"exec", "eval", "compile"}
        ):
            imports.add("<dynamic-source-execution>")
        direct = isinstance(func, ast.Name) and func.id in dynamic_function_names
        via_importlib = (
            isinstance(func, ast.Attribute)
            and func.attr == "import_module"
            and isinstance(func.value, ast.Name)
            and func.value.id in importlib_names
        )
        via_builtins = (
            isinstance(func, ast.Attribute)
            and func.attr == "__import__"
            and isinstance(func.value, ast.Name)
            and func.value.id in builtins_names
        )
        # Resolve builtin getattr wrappers around source evaluation. Dynamic
        # string execution is prohibited in stable Core contracts, even when
        # the builtin is accessed indirectly rather than as builtins.exec.
        via_builtin_source_getattr = (
            isinstance(func, ast.Call)
            and isinstance(func.func, ast.Name)
            and func.func.id in getattr_names
            and len(func.args) == 2
            and isinstance(func.args[0], ast.Name)
            and func.args[0].id in builtins_names
            and isinstance(func.args[1], ast.Constant)
            and func.args[1].value in {"exec", "eval", "compile"}
        )
        if via_builtin_source_getattr:
            imports.add("<dynamic-source-execution>")
            continue
        via_getattr = (
            isinstance(func, ast.Call)
            and isinstance(func.func, ast.Name)
            and func.func.id in getattr_names
            and len(func.args) == 2
            and isinstance(func.args[0], ast.Name)
            and isinstance(func.args[1], ast.Constant)
            and (
                (
                    func.args[0].id in importlib_names
                    and func.args[1].value == "import_module"
                )
                or (
                    func.args[0].id in builtins_names
                    and func.args[1].value == "__import__"
                )
            )
        )
        if not (direct or via_importlib or via_builtins or via_getattr):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(
            node.args[0].value, str
        ):
            imports.add("<nonliteral-dynamic-import>")
            continue
        module = node.args[0].value
        if module.split(".", 1)[0] in FOREIGN_ENGINE_ROOTS:
            imports.add(module)
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


def test_architecture_guard_rejects_direct_and_aliased_dynamic_engine_imports() -> None:
    source = (
        "import importlib as importer\n"
        "from importlib import import_module as load\n"
        "__import__('langgraph.graph')\n"
        "importer.import_module('mcp')\n"
        "load('httpx')\n"
        "load('nika_core.runtime.contracts')\n"
    )
    assert direct_engine_imports(source) == ("httpx", "langgraph.graph", "mcp")


def test_architecture_guard_fails_closed_on_nonliteral_dynamic_import() -> None:
    source = "from importlib import import_module as load\nload(provider_name)\n"
    assert direct_engine_imports(source) == ("<nonliteral-dynamic-import>",)


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


def test_architecture_guard_blocks_builtins_import_alias_escape() -> None:
    source = (
        "import builtins as engine_loader\n"
        "from builtins import __import__ as import_engine\n"
        "engine_loader.__import__('langgraph.graph')\n"
        "import_engine('litellm')\n"
        "import_engine('nika_core.runtime.contracts')\n"
    )
    assert direct_engine_imports(source) == ("langgraph.graph", "litellm")


def test_architecture_guard_rejects_nonliteral_builtins_import() -> None:
    source = "from builtins import __import__ as load\nload(unknown_engine)\n"
    assert direct_engine_imports(source) == ("<nonliteral-dynamic-import>",)


def test_architecture_guard_rejects_getattr_dynamic_engine_imports() -> None:
    source = (
        "import importlib as loader\n"
        "import builtins as standard\n"
        "getattr(loader, 'import_module')('langgraph.graph')\n"
        "getattr(standard, '__import__')('mcp')\n"
    )
    assert direct_engine_imports(source) == ("langgraph.graph", "mcp")


def test_architecture_guard_rejects_getattr_nonliteral_provider_name() -> None:
    source = (
        "import importlib\n"
        "getattr(importlib, 'import_module')(user_supplied_module)\n"
    )
    assert direct_engine_imports(source) == ("<nonliteral-dynamic-import>",)


def test_architecture_guard_rejects_dynamic_python_code_escape() -> None:
    source = (
        "import builtins as host\n"
        "from builtins import exec as run_source\n"
        "exec('import langgraph')\n"
        "host.eval('1 + 1')\n"
        "run_source(untrusted_code)\n"
    )
    assert direct_engine_imports(source) == ("<dynamic-source-execution>",)


def test_architecture_guard_rejects_compilation_with_unknown_source() -> None:
    source = (
        "from builtins import compile as compile_code\n"
        "compile_code(source, '<port>', 'exec')\n"
    )
    assert direct_engine_imports(source) == ("<dynamic-source-execution>",)


def test_architecture_guard_rejects_getattr_wrapped_builtin_execution() -> None:
    source = (
        "import builtins as host\n"
        "from builtins import getattr as resolve\n"
        "resolve(host, 'exec')('import langgraph')\n"
        "getattr(host, 'eval')('1 + 1')\n"
        "resolve(host, 'compile')(source, '<core>', 'exec')\n"
    )
    assert direct_engine_imports(source) == ("<dynamic-source-execution>",)


def test_architecture_guard_rejects_aliased_getattr_dynamic_import() -> None:
    source = (
        "import builtins as host\n"
        "from builtins import getattr as resolve\n"
        "resolve(host, '__import__')('mcp')\n"
        "resolve(host, 'repr')('safe')\n"
    )
    assert direct_engine_imports(source) == ("mcp",)


def test_architecture_guard_rejects_hoisted_builtin_source_loader() -> None:
    source = (
        "import builtins as host\n"
        "from builtins import getattr as resolve\n"
        "runner = resolve(host, 'exec')\n"
        "runner('import langgraph')\n"
    )
    assert direct_engine_imports(source) == ("<dynamic-source-execution>",)


def test_architecture_guard_allows_hoisted_safe_builtin_getattr() -> None:
    source = (
        "import builtins as host\n"
        "from builtins import getattr as resolve\n"
        "runner = resolve(host, 'repr')\n"
        "runner('safe')\n"
    )
    assert direct_engine_imports(source) == ()
