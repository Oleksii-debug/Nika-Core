from __future__ import annotations

import pytest

from nika_core.product_command.contracts import CommandRouteKind
from nika_core.product_command.routing import route_command


@pytest.mark.parametrize(
    "command",
    (
        "develop issue #654 in repository Oleksii-debug/Nika-Core",
        "implement PR 711 in repo Oleksii-debug/Nika-Core",
        "repository Oleksii-debug/Nika-Core issue 654 fix",
        "розроби issue #654 у repository Oleksii-debug/Nika-Core",
    ),
)
def test_repository_development_intent_routes_to_product_factory(command: str) -> None:
    decision = route_command(command)

    assert decision.route is CommandRouteKind.PRODUCT_PROJECT
    assert decision.normalized_goal == command
    assert decision.requires_user_decision is False


@pytest.mark.parametrize(
    "command",
    (
        "develop issue #654",
        "develop issue #654 in repository",
        "develop repository Oleksii-debug/Nika-Core",
        "explain issue #654 in repository Oleksii-debug/Nika-Core",
        "fix issue someday in repo Oleksii-debug/Nika-Core",
    ),
)
def test_incomplete_or_read_only_repository_intent_stays_agent_task(command: str) -> None:
    decision = route_command(command)

    assert decision.route is CommandRouteKind.AGENT_TASK


@pytest.mark.parametrize(
    "command",
    (
        "do not develop issue #654 in repository Oleksii-debug/Nika-Core",
        "don't implement PR 711 in repo Oleksii-debug/Nika-Core",
        "never fix issue 654 in repository Oleksii-debug/Nika-Core",
        "не розроби issue #654 у repository Oleksii-debug/Nika-Core",
    ),
)
def test_negated_repository_development_intent_stays_agent_task(command: str) -> None:
    decision = route_command(command)

    assert decision.route is CommandRouteKind.AGENT_TASK
    assert decision.normalized_goal == command
    assert decision.requires_user_decision is False


def test_development_intent_with_toolsmith_request_fails_closed_as_ambiguous() -> None:
    decision = route_command(
        "develop issue #654 in repository Oleksii-debug/Nika-Core and add plugin tool"
    )

    assert decision.route is CommandRouteKind.AMBIGUOUS
    assert decision.requires_user_decision is True
