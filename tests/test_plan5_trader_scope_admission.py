"""Plan 5 §1: PAPER scope is inert before Core policy and SQLite evidence."""
from __future__ import annotations

import pytest

from nika_core.trading_research.contracts import TradingResearchError
from nika_core.trading_research.workspace_query import (
    PaperWorkspaceQuery,
    paper_state_provider,
)


class ObservedRepository:
    def __init__(self) -> None:
        self.reads: list[tuple[str, str]] = []

    def account_payload(self, workspace_id: str, run_id: str):
        self.reads.append((workspace_id, run_id))
        return None


class HostileString(str):
    def strip(self, *args, **kwargs):
        raise AssertionError("executed hostile strip hook")

    def isprintable(self):
        raise AssertionError("executed hostile printability hook")

    def encode(self, *args, **kwargs):
        raise AssertionError("executed hostile encoder")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        " ",
        " mine",
        "mine ",
        "mine\nspoof",
        "mine\tspoof",
        "mine\u202espoof",
        "mine\u200bspoof",
        "mine\u2028spoof",
        "x" * 513,
        "ж" * 257,  # bounded in UTF-8 bytes, not Python codepoints
        None,
        1,
        True,
        HostileString("mine"),
    ],
)
@pytest.mark.parametrize("slot", [0, 1])
def test_rejects_unsafe_scope_before_core_or_database(bad: object, slot: int) -> None:
    repo = ObservedRepository()
    authority_calls: list[tuple[str, str]] = []
    def authorize(workspace: str, run: str) -> bool:
        authority_calls.append((workspace, run))
        return True

    query = PaperWorkspaceQuery(repo, authorize_read=authorize)
    scope = ["mine", "run-1"]
    scope[slot] = bad
    with pytest.raises(TradingResearchError, match="^invalid paper workspace scope$"):
        query.read_account(*scope)
    assert authority_calls == []
    assert repo.reads == []


@pytest.mark.parametrize(
    "bad_scope",
    [
        ("mine\nspoof", "run-1"),
        ("mine", "run\u202espoof"),
        (" mine", "run-1"),
        ("mine", "x" * 513),
        ("mine", HostileString("run-1")),
        ("mine", None),
        ("mine",),
    ],
)
def test_invalid_host_scope_is_access_denied_not_storage_unavailable(bad_scope) -> None:
    repo = ObservedRepository()
    authority_calls: list[tuple[str, str]] = []
    def authorize(workspace: str, run: str) -> bool:
        authority_calls.append((workspace, run))
        return True

    query = PaperWorkspaceQuery(repo, authorize_read=authorize)
    state = paper_state_provider(query, host_scope=lambda: bad_scope)()
    assert state["mode"] == "PAPER_ONLY"
    assert state["state"] == "ACCESS_DENIED"
    assert "cash" not in state and "positions" not in state
    assert authority_calls == []
    assert repo.reads == []


def test_exact_utf8_boundary_and_normal_unicode_scope_remain_usable() -> None:
    repo = ObservedRepository()
    workspace = "ж" * 256  # exactly 512 UTF-8 bytes
    run = "дослідницький запуск"
    authority_calls: list[tuple[str, str]] = []
    def authorize(w: str, r: str) -> bool:
        authority_calls.append((w, r))
        return (w, r) == (workspace, run)

    query = PaperWorkspaceQuery(repo, authorize_read=authorize)
    state = paper_state_provider(query, host_scope=lambda: (workspace, run))()
    assert state["mode"] == "PAPER_ONLY"
    assert state["state"] == "NO_PAPER_DATA"
    assert state["equity"] is None
    assert repo.reads == [(workspace, run)]
    assert authority_calls == [(workspace, run), (workspace, run)]
