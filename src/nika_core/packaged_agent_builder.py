from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping
from typing import Any

from nika_core.builder.compiler import AgentCompiler
from nika_core.builder.repository import AgentDefinitionRepository, StoredAgentDefinition
from nika_core.builder.spec import AgentDefinition
from nika_core.ui.bridge_models import UIResult

_MODEL_PROFILE = "deterministic"
_MAX_COMMAND_CHARS = 4000
_AGENT_ID_DIGEST_CHARS = 24
_NAME_CHARS = 108
_INSTRUCTIONS = (
    "Work only toward the declared goal. This packaged draft grants no tools or external "
    "capabilities. Capabilities, permissions and activation must be reviewed separately."
)


_STATUS_LABELS = {
    "draft": "чернетка",
    "active": "активний",
    "retired": "архівна версія",
}


class PackagedAgentBuilderStateProjector:
    """Add bounded canonical Agent Builder definitions to packaged presentation state."""

    def __init__(self, repository: AgentDefinitionRepository) -> None:
        self._repository = repository

    def decorate(self, state: Mapping[str, Any]) -> dict[str, Any]:
        projected: list[dict[str, Any]] = []
        visible_agents: list[dict[str, Any]] = []
        for stored in self._repository.list_latest(limit=50):
            label = _STATUS_LABELS.get(stored.status)
            if label is None:
                raise PermissionError("stored Agent Builder status is invalid")
            definition = stored.definition
            item = {
                "agent_id": definition.agent_id,
                "version": definition.version,
                "name": definition.name,
                "goal": definition.goal,
                "status": stored.status,
                "highest_risk": stored.highest_risk,
                "requires_human_approval": bool(stored.required_human_approvals),
            }
            projected.append(item)
            visible_agents.append(
                {
                    "agent_id": definition.agent_id,
                    "version": definition.version,
                    "name": f"Agent Builder [{label}]: {definition.name}",
                    "goal": definition.goal,
                }
            )

        result = dict(state)
        existing_agents = result.get("agents", [])
        if not isinstance(existing_agents, list):
            raise TypeError("packaged agents state must be a list")
        result["agents"] = [*existing_agents, *visible_agents]
        result["agent_builder_definitions"] = projected
        return result


class PackagedAgentBuilderDraftHandler:
    """Create one conservative durable Agent Builder draft from explicit packaged intent.

    The command router has already established high-confidence Agent Builder intent. This adapter
    deliberately grants no tools, schedule or resource budget and never activates the draft.
    AgentCompiler and AgentDefinitionRepository remain the policy and persistence authorities.
    """

    def __init__(self, repository: AgentDefinitionRepository) -> None:
        self._repository = repository
        self._compiler = AgentCompiler(
            tools=(),
            model_profiles={_MODEL_PROFILE},
        )

    def __call__(self, payload: Mapping[str, Any]) -> UIResult:
        goal = _normalized_goal(payload.get("command"))
        agent_id = _agent_id(goal)
        next_version = self._repository.next_version(agent_id)
        candidate = _definition(
            agent_id=agent_id,
            version=next_version,
            goal=goal,
        )

        if next_version > 1:
            previous = self._repository.get(agent_id, next_version - 1)
            if previous is not None and _same_draft(previous, candidate):
                return _result(
                    previous.definition,
                    "Чернетка Agent Builder уже збережена без змін",
                )

        compilation = self._compiler.compile(candidate)
        if compilation.requires_human_approval:
            raise PermissionError(
                "packaged safe-draft composition cannot create approval-bearing agent grants"
            )
        try:
            self._repository.save_draft(compilation)
        except ValueError:
            latest_version = self._repository.next_version(agent_id) - 1
            latest = (
                self._repository.get(agent_id, latest_version)
                if latest_version >= 1
                else None
            )
            if latest is not None and _same_draft(latest, candidate):
                return _result(
                    latest.definition,
                    "Чернетка Agent Builder уже збережена без змін",
                )
            raise
        return _result(candidate, "Чернетку Agent Builder збережено для окремого перегляду")


def _normalized_goal(value: object) -> str:
    if type(value) is not str:
        raise ValueError("Команда Agent Builder має бути звичайним текстом.")
    normalized = unicodedata.normalize("NFC", " ".join(value.split()))
    if not normalized:
        raise ValueError("Введіть опис агента перед створенням чернетки.")
    if len(normalized) > _MAX_COMMAND_CHARS:
        raise ValueError("Опис агента перевищує безпечний ліміт.")
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Опис агента містить некоректний текст Unicode.") from exc
    if any(
        unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}
        for char in normalized
    ):
        raise ValueError("Опис агента містить недопустимі керувальні символи.")
    return normalized


def _agent_id(goal: str) -> str:
    digest = hashlib.sha256(goal.casefold().encode("utf-8")).hexdigest()
    return f"agent.{digest[:_AGENT_ID_DIGEST_CHARS]}"


def _definition(*, agent_id: str, version: int, goal: str) -> AgentDefinition:
    name_goal = goal if len(goal) <= _NAME_CHARS else goal[: _NAME_CHARS - 3] + "..."
    return AgentDefinition(
        agent_id=agent_id,
        version=version,
        name=f"Agent — {name_goal}",
        goal=goal,
        instructions=_INSTRUCTIONS,
        model_profile=_MODEL_PROFILE,
        tool_grants=(),
        max_steps=100,
        enabled=True,
    )


def _same_draft(previous: StoredAgentDefinition, candidate: AgentDefinition) -> bool:
    if previous.status != "draft":
        return False
    old = previous.definition.model_dump(mode="json", exclude={"version"})
    new = candidate.model_dump(mode="json", exclude={"version"})
    return old == new


def _result(definition: AgentDefinition, prefix: str) -> UIResult:
    return UIResult(
        request_id="desktop-handler",
        status="completed",
        message=(
            f"{prefix}: {definition.agent_id}, версія {definition.version}. "
            "Чернетка не активована і не має дозволених інструментів."
        ),
        focus_id="agents-heading",
    )
