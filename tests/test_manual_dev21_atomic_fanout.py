from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nika_core.builder.spec import ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import ChildRequest, MultiAgentStore, TeamQuota
from nika_core.multi_agent.contracts import AgentHandoff, HandoffKind


def _store(
    tmp_path: Path,
    *,
    max_children: int = 4,
    max_total: int = 8,
) -> tuple[Path, MultiAgentStore]:
    path = tmp_path / "nika.db"
    sqlite = SQLiteStore(path)
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    store.create_team(
        team_id="team-atomic",
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=(
            ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com",)),
        ),
        quota=TeamQuota(
            max_depth=2,
            max_children_per_parent=max_children,
            max_total_agents=max_total,
            max_parallel=4,
        ),
    )
    return path, store


def _request(member_id: str) -> ChildRequest:
    return ChildRequest(
        member_id=member_id,
        agent_id="worker",
        agent_version=1,
        thread_id=f"thread-{member_id}",
        requested_grants=(
            ToolGrant(tool_id="web.read", max_risk=0, scopes=("example.com",)),
        ),
        payload={"member_id": member_id},
    )


def _task(member_id: str, *, handoff_id: str | None = None) -> AgentHandoff:
    return AgentHandoff(
        team_id="team-atomic",
        sender_id="root",
        recipient_id=member_id,
        kind=HandoffKind.TASK,
        payload={"member_id": member_id},
        handoff_id=handoff_id or f"task:team-atomic:{member_id}",
        correlation_id=f"corr:{member_id}",
    )


def _member_ids(store: MultiAgentStore) -> list[str]:
    return [member.member_id for member in store.members("team-atomic")]


def test_remaining_parent_quota_rejects_whole_wave_across_restart(tmp_path: Path) -> None:
    path, store = _store(tmp_path, max_children=3, max_total=8)
    store.spawn_child(
        team_id="team-atomic",
        parent_id="root",
        child_id="existing-1",
        agent_id="worker",
        agent_version=1,
        thread_id="thread-existing-1",
        requested_grants=(),
    )
    store.spawn_child(
        team_id="team-atomic",
        parent_id="root",
        child_id="existing-2",
        agent_id="worker",
        agent_version=1,
        thread_id="thread-existing-2",
        requested_grants=(),
    )

    with pytest.raises(RuntimeError, match="remaining children-per-parent quota"):
        store.spawn_children(
            team_id="team-atomic",
            parent_id="root",
            requests=(_request("new-1"), _request("new-2")),
            task_handoffs=(_task("new-1"), _task("new-2")),
        )

    assert _member_ids(store) == ["root", "existing-1", "existing-2"]
    reloaded = MultiAgentStore(SQLiteStore(path))
    assert _member_ids(reloaded) == ["root", "existing-1", "existing-2"]
    with pytest.raises(KeyError, match="no persisted task handoff"):
        reloaded.task_payload("team-atomic", "new-1")


def test_remaining_total_quota_rejects_whole_wave(tmp_path: Path) -> None:
    _, store = _store(tmp_path, max_children=6, max_total=4)
    for member_id in ("existing-1", "existing-2"):
        store.spawn_child(
            team_id="team-atomic",
            parent_id="root",
            child_id=member_id,
            agent_id="worker",
            agent_version=1,
            thread_id=f"thread-{member_id}",
            requested_grants=(),
        )

    with pytest.raises(RuntimeError, match="remaining total-agent quota"):
        store.spawn_children(
            team_id="team-atomic",
            parent_id="root",
            requests=(_request("new-1"), _request("new-2")),
        )
    assert _member_ids(store) == ["root", "existing-1", "existing-2"]


def test_late_handoff_constraint_failure_rolls_back_every_child(tmp_path: Path) -> None:
    _, store = _store(tmp_path)
    store.record_handoff(
        AgentHandoff(
            team_id="team-atomic",
            sender_id="root",
            recipient_id="root",
            kind=HandoffKind.STATUS,
            payload={"seed": True},
            handoff_id="handoff-collision",
            correlation_id="seed",
        )
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.spawn_children(
            team_id="team-atomic",
            parent_id="root",
            requests=(_request("child-1"), _request("child-2")),
            task_handoffs=(
                _task("child-1"),
                _task("child-2", handoff_id="handoff-collision"),
            ),
        )

    assert _member_ids(store) == ["root"]
    for member_id in ("child-1", "child-2"):
        with pytest.raises(KeyError, match="unknown team member"):
            store.member("team-atomic", member_id)


def test_competing_batches_cannot_overbook_total_quota(tmp_path: Path) -> None:
    path, _ = _store(tmp_path, max_children=4, max_total=3)

    def admit(prefix: str) -> str:
        worker_store = MultiAgentStore(SQLiteStore(path))
        try:
            worker_store.spawn_children(
                team_id="team-atomic",
                parent_id="root",
                requests=(_request(f"{prefix}-1"), _request(f"{prefix}-2")),
            )
        except RuntimeError as exc:
            assert "remaining total-agent quota" in str(exc)
            return "rejected"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(admit, ("wave-a", "wave-b")))

    assert outcomes == ["accepted", "rejected"]
    reloaded = MultiAgentStore(SQLiteStore(path))
    member_ids = _member_ids(reloaded)
    assert len(member_ids) == 3
    assert member_ids[0] == "root"
    assert set(member_ids[1:]) in (
        {"wave-a-1", "wave-a-2"},
        {"wave-b-1", "wave-b-2"},
    )
