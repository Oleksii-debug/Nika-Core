from __future__ import annotations

import re

from nika_core.product_command.contracts import CommandRouteDecision, CommandRouteKind

_PRODUCT_PATTERNS = (
    re.compile(
        r"\b(create|build|develop|launch|maintain)\b.*\b(product|application|app|service|website|platform)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(product|application|app|service|website|platform)\b.*\b(research|design|implement|test|package|deploy|maintain)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(створи|створити|розроби|розробити|побудуй|побудувати|запусти|запустити|підтримуй|підтримувати)\b"
        r".*\b(продукт|продукту|застосунок|застосунку|додаток|додатку|сервіс|сервісу|сайт|сайту|"
        r"платформа|платформу)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(продукт|продукту|застосунок|застосунку|додаток|додатку|сервіс|сервісу|сайт|сайту|"
        r"платформа|платформу)\b.*\b(досліди|дослідити|спроєктуй|спроєктувати|реалізуй|реалізувати|"
        r"протестуй|протестувати|запакуй|запакувати|розгорни|розгорнути|підтримуй|підтримувати)\b",
        re.IGNORECASE,
    ),
)
_DEVELOPMENT_REQUEST_PATTERNS = (
    re.compile(
        r"\b(develop|implement|fix)\b.*\b(issue|pr|pull request)\s*#?\d+\b"
        r".*\b(repository|repo)\s+(?:https?://github\.com/)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(repository|repo)\s+(?:https?://github\.com/)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\b"
        r".*\b(issue|pr|pull request)\s*#?\d+\b.*\b(develop|implement|fix)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(розроби|розробити|реалізуй|реалізувати|виправ|виправити)\b"
        r".*\b(issue|pr|задачу|задача)\s*#?\d+\b"
        r".*\b(repository|repo|репозиторій|репозиторію)\s+"
        r"(?:https?://github\.com/)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\b",
        re.IGNORECASE,
    ),
)
_DEVELOPMENT_ACTION_AUTHORITY_PATTERNS = (
    re.compile(
        r"^(?:please\s+)?(?:develop|implement|fix)\s+"
        r"(?:issue|pr|pull request)\s*#?\d+\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:please\s+)?(?:can|could|would|will)\s+you\s+(?:please\s+)?"
        r"(?:develop|implement|fix)\s+(?:issue|pr|pull request)\s*#?\d+\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:repository|repo)\s+(?:https?://github\.com/)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\b"
        r".*\b(?:issue|pr|pull request)\s*#?\d+\b"
        r"\s*(?::|;|,|—|-)\s*(?:please\s+)?(?:develop|implement|fix)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:будь\s+ласка,?\s+)?(?:розроби|реалізуй|виправ)\s+"
        r"(?:issue|pr|задачу|задача)\s*#?\d+\b",
        re.IGNORECASE,
    ),
)
_NEGATED_DEVELOPMENT_PATTERNS = (
    re.compile(
        r"\b(?:do\s+not|don't|never)\b.{0,80}\b(?:develop|implement|fix)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:not|don't)\s+(?:want|need|ask|tell|allow)\b.{0,80}\b"
        r"(?:develop|implement|fix)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bне\b.{0,80}\b(?:розробляй|розроби|розробити|реалізуй|реалізувати|виправляй|виправ|виправити)\b",
        re.IGNORECASE,
    ),
)
_TOOLSMITH_PATTERNS = (
    re.compile(
        r"\b(missing|need|add|build)\b.*\b(tool|capability|plugin|connector)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(бракує|потрібен|потрібна|потрібно|додай|створи|побудуй)\b.*\b(інструмент|інструменту|"
        r"можливість|можливості|плагін|плагіну|конектор|конектора)\b",
        re.IGNORECASE,
    ),
)


def _is_authoritative_development_request(normalized: str) -> bool:
    if not any(pattern.search(normalized) for pattern in _DEVELOPMENT_REQUEST_PATTERNS):
        return False
    if any(pattern.search(normalized) for pattern in _NEGATED_DEVELOPMENT_PATTERNS):
        return False
    return any(
        pattern.search(normalized) for pattern in _DEVELOPMENT_ACTION_AUTHORITY_PATTERNS
    )


def route_command(text: str, *, active_project_id: str | None = None) -> CommandRouteDecision:
    """Classify command intent without an LLM or hidden project mutation.

    The router deliberately recognizes only high-confidence English and Ukrainian Product
    Factory, repository-development, or Toolsmith wording. Everything else remains an ordinary
    AgentTask. Mixed high-confidence product/tool intents require an explicit user decision
    instead of silently choosing a long-lived or capability-building path.
    """
    normalized = " ".join(text.split())
    if not normalized:
        raise ValueError("command must not be empty")
    if len(normalized) > 4000:
        raise ValueError("command exceeds 4000 characters")

    product = any(pattern.search(normalized) for pattern in _PRODUCT_PATTERNS)
    development_request = _is_authoritative_development_request(normalized)
    toolsmith = any(pattern.search(normalized) for pattern in _TOOLSMITH_PATTERNS)
    if (product or development_request) and toolsmith:
        return CommandRouteDecision(
            route=CommandRouteKind.AMBIGUOUS,
            reason="Command matches both long-lived product/development and capability-building intent.",
            requires_user_decision=True,
            normalized_goal=normalized,
        )
    if development_request:
        return CommandRouteDecision(
            route=CommandRouteKind.PRODUCT_PROJECT,
            reason=(
                "Command explicitly requests durable repository issue development through "
                "Product Factory."
            ),
            project_id=active_project_id,
            normalized_goal=normalized,
        )
    if product:
        return CommandRouteDecision(
            route=CommandRouteKind.PRODUCT_PROJECT,
            reason="Command explicitly describes a long-lived product lifecycle.",
            project_id=active_project_id,
            normalized_goal=normalized,
        )
    if toolsmith:
        return CommandRouteDecision(
            route=CommandRouteKind.TOOLSMITH,
            reason="Command explicitly requests a reusable tool or missing capability.",
            normalized_goal=normalized,
        )
    return CommandRouteDecision(
        route=CommandRouteKind.AGENT_TASK,
        reason="No high-confidence long-lived product or capability-building intent was found.",
        normalized_goal=normalized,
    )
