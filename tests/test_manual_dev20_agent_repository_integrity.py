from pathlib import Path

import pytest

from nika_core.activation_authority import ActivationSubject
from nika_core.builder.compiler import AgentCompiler, RiskTier
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.tools import ToolRisk, ToolSpec


class _Authority:
    def verify(self, subject: ActivationSubject, approval_refs: tuple[str, ...]) -> None:
        assert subject.kind == "agent"
        if approval_refs != ("approval://trusted",):
            raise PermissionError("untrusted approval evidence")


def _store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    return store


def _dangerous() -> tuple[AgentDefinition, AgentCompiler]:
    definition = AgentDefinition(
        agent_id="release.agent",
        version=1,
        name="Release agent",
        goal="Publish a reviewed release",
        instructions="Use only the reviewed release capability.",
        model_profile="test",
        tool_grants=(
            ToolGrant(
                tool_id="release.publish",
                max_risk=RiskTier.R4_HIGH_IMPACT,
            ),
        ),
    )
    compiler = AgentCompiler(
        tools=(
            ToolSpec(
                "release.publish",
                "Publish a release",
                ToolRisk.HIGH_IMPACT,
            ),
        ),
        model_profiles={"test"},
    )
    return definition, compiler


def _safe(*, version: int = 1) -> tuple[AgentDefinition, AgentCompiler]:
    definition = AgentDefinition(
        agent_id="reader.agent",
        version=version,
        name="Reader agent",
        goal="Read reviewed evidence",
        instructions="Use only the read capability.",
        model_profile="test",
        tool_grants=(
            ToolGrant(
                tool_id="web.read",
                max_risk=RiskTier.R0_READ_ONLY,
            ),
        ),
    )
    compiler = AgentCompiler(
        tools=(ToolSpec("web.read", "Read evidence", ToolRisk.READ_ONLY),),
        model_profiles={"test"},
    )
    return definition, compiler


def test_same_active_version_revalidates_persisted_r4_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path / "same-active.db")
    definition, compiler = _dangerous()
    repository = AgentDefinitionRepository(
        store,
        activation_authority=_Authority(),
    )
    repository.save_draft(compiler.compile(definition))
    repository.activate(
        definition,
        approval_refs=("approval://trusted",),
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE agent_definitions SET required_approvals_json = '[]' "
            "WHERE agent_id = ? AND version = ?",
            (definition.agent_id, definition.version),
        )

    with pytest.raises(PermissionError, match="approval metadata"):
        repository.activate(
            definition,
            approval_refs=("approval://trusted",),
        )


def test_active_read_rejects_definition_row_version_substitution(tmp_path: Path) -> None:
    store = _store(tmp_path / "row-version.db")
    definition, compiler = _safe()
    repository = AgentDefinitionRepository(store)
    repository.save_draft(compiler.compile(definition))
    repository.activate(definition)

    with store.connection() as conn:
        conn.execute(
            "UPDATE agent_definitions SET version = 2 "
            "WHERE agent_id = ? AND version = ?",
            (definition.agent_id, definition.version),
        )

    with pytest.raises(PermissionError, match="durable row identity"):
        repository.active(definition.agent_id)


def test_next_version_rejects_real_valued_durable_version(tmp_path: Path) -> None:
    store = _store(tmp_path / "real-version.db")
    definition, compiler = _safe()
    repository = AgentDefinitionRepository(store)
    repository.save_draft(compiler.compile(definition))

    with store.connection() as conn:
        conn.execute(
            "UPDATE agent_definitions SET version = 1.5 "
            "WHERE agent_id = ? AND version = ?",
            (definition.agent_id, definition.version),
        )

    with pytest.raises(PermissionError, match="exact positive integer"):
        repository.next_version(definition.agent_id)

    next_definition, _ = _safe(version=2)
    with pytest.raises(PermissionError, match="exact positive integer"):
        repository.save_draft(compiler.compile(next_definition))
