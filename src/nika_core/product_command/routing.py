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


def route_command(text: str, *, active_project_id: str | None = None) -> CommandRouteDecision:
    """Classify command intent without an LLM or hidden project mutation.

    The router deliberately recognizes only high-confidence English and Ukrainian Product
    Factory or Toolsmith wording. Everything else remains an ordinary AgentTask. Mixed
    high-confidence intents require an explicit user decision instead of silently choosing
    a long-lived or capability-building path.
    """
    normalized = " ".join(text.split())
    if not normalized:
        raise ValueError("command must not be empty")
    if len(normalized) > 4000:
        raise ValueError("command exceeds 4000 characters")

    product = any(pattern.search(normalized) for pattern in _PRODUCT_PATTERNS)
    toolsmith = any(pattern.search(normalized) for pattern in _TOOLSMITH_PATTERNS)
    if product and toolsmith:
        return CommandRouteDecision(
            route=CommandRouteKind.AMBIGUOUS,
            reason="Command matches both long-lived product and capability-building intent.",
            requires_user_decision=True,
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
