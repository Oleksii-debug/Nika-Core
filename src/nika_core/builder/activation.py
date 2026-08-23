from __future__ import annotations

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository
from nika_core.builder.spec import AgentDefinition


class AgentActivationService:
    """Revalidates an immutable agent version against live catalogs immediately before activation."""

    def __init__(self, repository: AgentDefinitionRepository, compiler: AgentCompiler) -> None:
        self._repository = repository
        self._compiler = compiler

    def activate(
        self,
        definition: AgentDefinition,
        *,
        approved_tool_ids: frozenset[str] = frozenset(),
    ) -> None:
        compiled = self._compiler.compile(definition)
        stored = self._repository.get(definition.agent_id, definition.version)
        if stored is None:
            raise KeyError("agent definition draft does not exist")
        if stored.definition != definition:
            raise ValueError("activation definition differs from persisted immutable draft")
        if (
            stored.required_human_approvals != compiled.required_human_approvals
            or stored.highest_risk != int(compiled.highest_risk)
        ):
            raise PermissionError(
                "agent tool policy changed after draft review; save and review a new version"
            )
        self._repository.activate(definition, approved_tool_ids=approved_tool_ids)
