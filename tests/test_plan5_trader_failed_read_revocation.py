"""Plan 5 §1: denial must dominate corrupt PAPER evidence after revocation."""
from __future__ import annotations

import sqlite3

import pytest

from nika_core.trading_research.contracts import TradingResearchError
from nika_core.trading_research.workspace_query import (
    PaperWorkspaceQuery,
    paper_state_provider,
)


class BrokenPaperRepository:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def account_payload(self, workspace_id: str, run_id: str):
        self.calls.append((workspace_id, run_id))
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("PRIVATE_SQLITE_DETAIL"),
        ValueError("PRIVATE_DECODE_DETAIL"),
        KeyError("PRIVATE_POSITION_KEY"),
        sqlite3.OperationalError("PRIVATE_DATABASE_LOCATION"),
    ],
)
def test_revocation_on_failed_read_masks_evidence_health(error: Exception) -> None:
    repo = BrokenPaperRepository(error)
    checks: list[tuple[str, str]] = []

    def authorize(workspace: str, run: str) -> bool:
        checks.append((workspace, run))
        return len(checks) == 1

    query = PaperWorkspaceQuery(repo, authorize_read=authorize)
    with pytest.raises(PermissionError, match="^paper workspace read denied$") as caught:
        query.read_account("mine", "paper-run")
    assert "PRIVATE" not in str(caught.value)
    assert checks == [("mine", "paper-run"), ("mine", "paper-run")]
    assert repo.calls == [("mine", "paper-run")]


def test_still_authorized_failed_read_remains_unavailable_not_empty() -> None:
    repo = BrokenPaperRepository(RuntimeError("PRIVATE_SQLITE_DETAIL"))
    checks: list[tuple[str, str]] = []
    query = PaperWorkspaceQuery(
        repo, authorize_read=lambda w, r: checks.append((w, r)) or True
    )
    with pytest.raises(
        TradingResearchError, match="^paper account evidence unavailable$"
    ) as caught:
        query.read_account("mine", "paper-run")
    assert "PRIVATE" not in str(caught.value)
    assert checks == [("mine", "paper-run"), ("mine", "paper-run")]
    assert repo.calls == [("mine", "paper-run")]


def test_accessible_state_distinguishes_revoked_from_unavailable_on_storage_fault() -> None:
    repo = BrokenPaperRepository(RuntimeError("PRIVATE_SQLITE_DETAIL"))
    checks = 0

    def revoked_after_preflight(_workspace: str, _run: str) -> bool:
        nonlocal checks
        checks += 1
        return checks == 1

    query = PaperWorkspaceQuery(repo, authorize_read=revoked_after_preflight)
    state = paper_state_provider(query, host_scope=lambda: ("mine", "paper-run"))()
    assert state["mode"] == "PAPER_ONLY"
    assert state["state"] == "ACCESS_DENIED"
    assert "cash" not in state and "positions" not in state
    assert "PRIVATE" not in str(state)
    assert checks == 2

    allowed = PaperWorkspaceQuery(repo, authorize_read=lambda _w, _r: True)
    state = paper_state_provider(allowed, host_scope=lambda: ("mine", "paper-run"))()
    assert state["state"] == "EVIDENCE_UNAVAILABLE"
    assert "cash" not in state and "positions" not in state
    assert "PRIVATE" not in str(state)
