"""Plan 2 / Section 1: reuse canonical Agent Builder with fail-closed authority."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from pydantic import ValidationError

from nika_core.builder.compiler import AgentCompiler, RiskTier
from nika_core.builder.drafting import AgentDraftService
from nika_core.builder.spec import AgentDefinition, ToolGrant
from nika_core.tools import ToolRisk, ToolSpec


def _definition() -> AgentDefinition:
    return AgentDefinition(
        agent_id="fixture.agent",
        name="Fixture",
        goal="Safe research",
        instructions="Use approved grants only.",
        model_profile="local",
        tool_grants=(ToolGrant(tool_id="web.read", max_risk=0),),
    )


def _compiler(*tools: ToolSpec) -> AgentCompiler:
    return AgentCompiler(tools=tools, model_profiles={"local"})


@pytest.mark.parametrize("second", (ToolRisk.READ_ONLY, ToolRisk.HIGH_IMPACT))
def test_registry_duplicate_id_fails_closed(second: ToolRisk) -> None:
    with pytest.raises(ValueError, match="duplicate registered tool identity"):
        _compiler(
            ToolSpec("web.read", "First", ToolRisk.READ_ONLY),
            ToolSpec("web.read", "Second", second),
        )


def test_compiler_risk_and_nested_definition_are_snapshot_at_review() -> None:
    dangerous = ToolSpec("release.publish", "Publish", ToolRisk.HIGH_IMPACT)
    compiler = _compiler(dangerous)
    object.__setattr__(dangerous, "risk", ToolRisk.READ_ONLY)
    requested = _definition().model_copy(update={
        "tool_grants": (ToolGrant(tool_id="release.publish", max_risk=4),)
    })
    compiled = compiler.compile(requested)
    assert compiled.highest_risk is RiskTier.R4_HIGH_IMPACT
    assert compiled.required_human_approvals == ("release.publish",)
    object.__setattr__(requested.tool_grants[0], "max_risk", 0)
    assert compiled.definition.tool_grants[0].max_risk == 4
    with pytest.raises(ValueError, match="requires R4_HIGH_IMPACT"):
        compiler.compile(_definition().model_copy(update={
            "tool_grants": (ToolGrant(tool_id="release.publish", max_risk=0),)
        }))


@pytest.mark.parametrize("untrusted", (True, False, 1.0, "1"))
def test_grant_risk_cannot_be_coerced(untrusted: object) -> None:
    with pytest.raises(ValidationError):
        ToolGrant(tool_id="web.read", max_risk=untrusted)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("format_version", True),
        ("format_version", 1.0),
        ("version", True),
        ("version", "1"),
        ("max_steps", 1.0),
        ("max_steps", "2"),
        ("enabled", 1),
        ("enabled", "true"),
    ),
)
def test_agent_identity_budget_and_enabled_are_strict(field: str, value: object) -> None:
    payload = _definition().model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        AgentDefinition.model_validate(payload)


def test_import_rejects_ambiguous_keys_and_oversize_without_authorization() -> None:
    raw = _definition().export_json()
    ambiguous = raw.replace('"max_risk": 0', '"max_risk": 0, "max_risk": 4')
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        AgentDefinition.import_json(ambiguous)
    with pytest.raises(ValueError, match="size limit"):
        AgentDefinition.import_json(raw + " " * (1024 * 1024))
    assert AgentDefinition.import_json(raw) == _definition()


class _UnsafeGateway:
    async def complete(self, request):
        class _Result:
            text = _definition().export_json().replace(
                '"max_risk": 0', '"max_risk": 0, "max_risk": 4'
            )
        return _Result()


def test_model_draft_cannot_launder_duplicate_permission_keys() -> None:
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        asyncio.run(AgentDraftService(_UnsafeGateway()).draft("Create safe agent"))

@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("version", True),
        ("max_steps", 1.0),
        ("max_steps", "10"),
        ("enabled", "true"),
    ),
)
def test_compiler_readmits_unvalidated_copy_before_authorizing(
    field: str, value: object,
) -> None:
    # model_copy(update=...) deliberately skips Pydantic validation.
    document = _definition().model_copy(update={field: value})
    with pytest.raises(ValidationError):
        _compiler(ToolSpec("web.read", "Read", ToolRisk.READ_ONLY)).compile(document)


def test_compiler_rejects_mutated_nested_grant_before_authorizing() -> None:
    grant = ToolGrant(tool_id="web.read", max_risk=0)
    object.__setattr__(grant, "max_risk", 0.0)
    document = _definition().model_copy(update={"tool_grants": (grant,)})
    with pytest.raises(ValidationError):
        _compiler(ToolSpec("web.read", "Read", ToolRisk.READ_ONLY)).compile(document)


def test_durable_duplicate_json_key_fails_closed_at_activation_and_restart(
    tmp_path,
) -> None:
    from nika_core.builder.repository import AgentDefinitionRepository
    from nika_core.data.sqlite import SQLiteStore

    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = AgentDefinitionRepository(store)
    definition = _definition()
    repository.save_draft(
        _compiler(ToolSpec("web.read", "Read", ToolRisk.READ_ONLY)).compile(definition)
    )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT definition_json FROM agent_definitions WHERE agent_id = ? AND version = ?",
            (definition.agent_id, definition.version),
        ).fetchone()
        assert row is not None
        original = str(row["definition_json"])
        tampered = original.replace(
            '"max_steps":100', '"max_steps":100,"max_steps":100000'
        )
        assert tampered != original
        conn.execute(
            "UPDATE agent_definitions SET definition_json = ? "
            "WHERE agent_id = ? AND version = ?",
            (tampered, definition.agent_id, definition.version),
        )

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        repository.activate(definition)
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        repository.get(definition.agent_id, definition.version)

    # Restoring exact validated durable evidence recovers activation without new authority.
    with store.connection() as conn:
        conn.execute(
            "UPDATE agent_definitions SET definition_json = ? "
            "WHERE agent_id = ? AND version = ?",
            (original, definition.agent_id, definition.version),
        )
    repository.activate(definition)
    assert (
        repository.require_active(definition.agent_id, definition.version).definition
        == definition
    )

    # A previously active version must also fail closed after on-disk tampering.
    with store.connection() as conn:
        conn.execute(
            "UPDATE agent_definitions SET definition_json = ? "
            "WHERE agent_id = ? AND version = ?",
            (tampered, definition.agent_id, definition.version),
        )
    restarted = AgentDefinitionRepository(SQLiteStore(tmp_path / "nika.db"))
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        restarted.require_active(definition.agent_id, definition.version)


@pytest.mark.parametrize(
    ("risk", "approvals"),
    (
        (RiskTier.R0_READ_ONLY, ("release.publish",)),
        (RiskTier.R4_HIGH_IMPACT, ()),
        (RiskTier.R4_HIGH_IMPACT, ("unregistered.tool",)),
        ("4", ("release.publish",)),
    ),
)
def test_durable_draft_rejects_forged_compilation_evidence(
    tmp_path, risk: object, approvals: tuple[str, ...],
) -> None:
    from nika_core.builder.repository import AgentDefinitionRepository
    from nika_core.data.sqlite import SQLiteStore

    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = AgentDefinitionRepository(store)
    definition = _definition().model_copy(
        update={"tool_grants": (ToolGrant(tool_id="release.publish", max_risk=4),)}
    )
    compiled = _compiler(
        ToolSpec("release.publish", "Publish", ToolRisk.HIGH_IMPACT)
    ).compile(definition)
    forged = replace(
        compiled,
        highest_risk=risk,
        required_human_approvals=approvals,
    )
    with pytest.raises(ValueError, match="compiled agent risk/approval"):
        repository.save_draft(forged)
    assert repository.get(definition.agent_id, definition.version) is None
    assert repository.next_version(definition.agent_id) == 1

    # The actual compiler output remains admissible and still needs human approval.
    repository.save_draft(compiled)
    with pytest.raises(PermissionError, match="explicit human approval"):
        repository.activate(definition)
    assert repository.active(definition.agent_id) is None


@pytest.mark.parametrize(
    ("column", "replacement"),
    (
        ("highest_risk", 0),
        ("required_approvals_json", "[]"),
        ("required_approvals_json", '["wrong.tool"]'),
        ("required_approvals_json", '{"forged":true}'),
    ),
)
def test_sqlite_restart_rejects_tampered_risk_evidence_before_activation(
    tmp_path, column: str, replacement: object,
) -> None:
    from nika_core.builder.repository import AgentDefinitionRepository
    from nika_core.data.sqlite import SQLiteStore

    path = tmp_path / "nika.db"
    sqlite = SQLiteStore(path)
    sqlite.initialize()
    repository = AgentDefinitionRepository(sqlite)
    definition = _definition().model_copy(
        update={"tool_grants": (ToolGrant(tool_id="release.publish", max_risk=4),)}
    )
    compiler = _compiler(ToolSpec("release.publish", "Publish", ToolRisk.HIGH_IMPACT))
    repository.save_draft(compiler.compile(definition))
    with sqlite.connection() as conn:
        row = conn.execute(
            f"SELECT {column} FROM agent_definitions WHERE agent_id = ?",
            (definition.agent_id,),
        ).fetchone()
        assert row is not None
        original = row[column]
        conn.execute(
            f"UPDATE agent_definitions SET {column} = ? WHERE agent_id = ?",
            (replacement, definition.agent_id),
        )

    restarted = AgentDefinitionRepository(SQLiteStore(path))
    with pytest.raises(ValueError, match="persisted agent risk/approval"):
        restarted.activate(
            definition, approved_tool_ids=frozenset({"release.publish"})
        )
    with pytest.raises(ValueError, match="persisted agent risk/approval"):
        restarted.get(definition.agent_id, definition.version)
    with sqlite.connection() as conn:
        row = conn.execute(
            "SELECT status FROM agent_definitions WHERE agent_id = ?",
            (definition.agent_id,),
        ).fetchone()
        assert row["status"] == "draft"
        conn.execute(
            f"UPDATE agent_definitions SET {column} = ? WHERE agent_id = ?",
            (original, definition.agent_id),
        )
    restarted.activate(definition, approved_tool_ids=frozenset({"release.publish"}))
    assert restarted.require_active(definition.agent_id, definition.version).status == "active"

def test_concurrent_draft_admission_has_one_version_after_restart(tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from nika_core.builder.repository import AgentDefinitionRepository
    from nika_core.data.sqlite import SQLiteStore

    path = tmp_path / "nika.db"
    SQLiteStore(path).initialize()
    definition = _definition()
    compiled = _compiler(
        ToolSpec("web.read", "Read", ToolRisk.READ_ONLY)
    ).compile(definition)
    ready = Barrier(2)

    def save(_: int) -> str:
        # Two independent connections race the same draft identity/version.
        ready.wait(timeout=10)
        writer = AgentDefinitionRepository(SQLiteStore(path))
        try:
            writer.save_draft(compiled)
        except ValueError as exc:
            assert "next immutable version" in str(exc)
            return "conflict"
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(save, range(2)))

    assert sorted(outcomes) == ["conflict", "saved"]
    restarted = AgentDefinitionRepository(SQLiteStore(path))
    stored = restarted.get(definition.agent_id, definition.version)
    assert stored is not None and stored.status == "draft"
    assert stored.definition == definition
    assert restarted.next_version(definition.agent_id) == 2


def test_concurrent_activation_preserves_single_durable_active_version(tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from nika_core.builder.repository import AgentDefinitionRepository
    from nika_core.data.sqlite import SQLiteStore

    path = tmp_path / "nika.db"
    SQLiteStore(path).initialize()
    repository = AgentDefinitionRepository(SQLiteStore(path))
    compiler = _compiler(ToolSpec("web.read", "Read", ToolRisk.READ_ONLY))
    first = _definition()
    second = first.model_copy(update={"version": 2})
    repository.save_draft(compiler.compile(first))
    repository.save_draft(compiler.compile(second))
    ready = Barrier(2)

    def activate(definition: AgentDefinition) -> None:
        ready.wait(timeout=10)
        AgentDefinitionRepository(SQLiteStore(path)).activate(definition)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(activate, (first, second)))

    restarted = AgentDefinitionRepository(SQLiteStore(path))
    statuses = {
        version: restarted.get(first.agent_id, version).status
        for version in (1, 2)
    }
    assert sorted(statuses.values()) == ["active", "retired"]
    active = restarted.active(first.agent_id)
    assert active is not None and active.status == "active"
    assert active.definition.version in (1, 2)

def test_compiled_draft_cannot_outgrow_its_durable_restart_reader(tmp_path) -> None:
    from nika_core.builder.repository import AgentDefinitionRepository
    from nika_core.data.sqlite import SQLiteStore

    path = tmp_path / "nika.db"
    SQLiteStore(path).initialize()
    repository = AgentDefinitionRepository(SQLiteStore(path))
    compiler = _compiler(ToolSpec("web.read", "Read", ToolRisk.READ_ONLY))
    scopes = tuple(f"scope-{index:05d}-" + "x" * 140 for index in range(7500))
    oversized = _definition().model_copy(
        update={
            "tool_grants": (
                ToolGrant(tool_id="web.read", max_risk=0, scopes=scopes),
            ),
        }
    )
    compilation = compiler.compile(oversized)
    assert len(compilation.definition.model_dump_json().encode("utf-8")) > 1024 * 1024

    with pytest.raises(ValueError, match="size limit"):
        repository.save_draft(compilation)

    assert repository.get(oversized.agent_id, oversized.version) is None
    assert repository.next_version(oversized.agent_id) == 1
    repository.save_draft(compiler.compile(_definition()))
    restarted = AgentDefinitionRepository(SQLiteStore(path))
    stored = restarted.get(oversized.agent_id, oversized.version)
    assert stored is not None and stored.definition == _definition()
