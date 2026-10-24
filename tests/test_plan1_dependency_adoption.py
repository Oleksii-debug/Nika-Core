"""Plan 1 Section 1: keep replaceable engines out of the mandatory Core install.

This is a manifest-level adoption regression gate, not a dependency audit,
license attestation, or proof that optional providers are installed.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest


PROJECT_TOML = Path(__file__).resolve().parents[1] / "pyproject.toml"

# Nika contracts may name adapter capabilities without requiring their hosts.
# HTTPX, Pydantic, SQLite and platformdirs are intentionally not listed here.
REPLACEABLE_ENGINE_DISTRIBUTIONS = frozenset(
    {
        "langgraph",
        "langgraph-checkpoint-sqlite",
        "deepagents",
        "litellm",
        "apscheduler",
        "mcp",
        "foundry-local-sdk",
        "foundry-local-sdk-winml",
        "pywebview",
        "playwright",
        "pywinauto",
        "unified-planning",
        "dspy",
        "ansible-runner",
    }
)


def _distribution(specifier: str) -> str:
    """Extract the PEP 508 distribution prefix, not an environment marker."""
    match = re.match(r"^\s*([a-zA-Z0-9][a-zA-Z0-9_.-]*)", specifier)
    if match is None:
        raise AssertionError(f"invalid dependency declaration: {specifier!r}")
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _declared_requirements() -> tuple[list[str], dict[str, list[str]]]:
    with PROJECT_TOML.open("rb") as source:
        project = tomllib.load(source)["project"]
    return project["dependencies"], project["optional-dependencies"]


@pytest.mark.parametrize("engine", sorted(REPLACEABLE_ENGINE_DISTRIBUTIONS))
def test_replaceable_engines_are_not_required_by_base_core(engine: str) -> None:
    base, extras = _declared_requirements()
    base_names = {_distribution(item) for item in base}
    optional_names = {
        _distribution(item)
        for declarations in extras.values()
        for item in declarations
    }
    assert engine not in base_names
    assert engine in optional_names, f"{engine}: adapter lacks an explicit optional extra"


def test_adopted_runtime_has_single_primary_orchestration_extra() -> None:
    base, extras = _declared_requirements()
    assert "langgraph" not in {_distribution(item) for item in base}
    agent_names = {_distribution(item) for item in extras["agent"]}
    assert "langgraph" in agent_names
    # A framework may be researched as an alternative, but importing its
    # competing kernel as a required package would fork runtime authority.
    assert "crewai" not in agent_names
    assert "autogen" not in agent_names
    assert "microsoft-agent-framework" not in agent_names


def test_adopted_dependencies_are_bounded_for_reproducible_qualification() -> None:
    base, extras = _declared_requirements()
    for dependency in (*base, *(item for group in extras.values() for item in group)):
        # One-sided version lower bounds silently adopt unreviewed new majors.
        # Exact pins or upper bounds preserve the adopted compatibility envelope.
        requirement = dependency.partition(";")[0]
        assert "==" in requirement or "<" in requirement, dependency


def test_distribution_parser_is_inert_and_marker_independent() -> None:
    assert _distribution(" Foundry_Local.SDK>=1,<2; sys_platform == 'win32'") == (
        "foundry-local-sdk"
    )
    assert _distribution("langgraph[sqlite]>=1,<2") == "langgraph"
