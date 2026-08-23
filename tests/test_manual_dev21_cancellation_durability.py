from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from nika_core.builder.spec import ToolGrant
from nika_core.data.sqlite import SQLiteStore
from nika_core.multi_agent import (
    CancellationProbeRequest,
    CancellationProbeState,
    CancellationReconciliationRequired,
    MemberState,
    MultiAgentStore,
    MultiAgentSupervisor,
    TeamQuota,
    TeamState,
)
from nika_core.multi_agent.cancellation import TeamCancellationJournal


class _UnusedDefinitions:
    pass


class _EffectThenErrorRuntime:
    capabilities = frozenset()

    def __init__(self) -> None:
        self.cancel_effects: list[str] = []

    async def cancel(self, *, task_id: str, thread_id: str) -> None:
        del thread_id
        self.cancel_effects.append(task_id)
        if len(self.cancel_effects) == 2:
            raise RuntimeError("uncertain cancellation result after external effect")


class _RecordingRuntime:
    capabilities = frozenset()

    def __init__(self) -> None:
        self.cancel_effects: list[str] = []

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del thread_id
        self.cancel_effects.append(task_id)
        return True


class _BlockingRuntime(_RecordingRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        self.cancel_effects.append(task_id)
        if len(self.cancel_effects) == 1:
            self.entered.set()
            await self.release.wait()
        del thread_id
        return True


class _Probe:
    def __init__(self, verdict: CancellationProbeState) -> None:
        self.verdict = verdict
        self.requests: list[CancellationProbeRequest] = []

    async def inspect_cancellation(
        self,
        request: CancellationProbeRequest,
    ) -> CancellationProbeState:
        self.requests.append(request)
        return self.verdict


def _make_store(tmp_path: Path) -> tuple[Path, MultiAgentStore]:
    path = tmp_path / "nika.db"
    sqlite = SQLiteStore(path)
    sqlite.initialize()
    store = MultiAgentStore(sqlite)
    store.create_team(
        team_id="team-cancel",
        root_member_id="root",
        root_agent_id="supervisor",
        root_agent_version=1,
        root_thread_id="thread-root",
        root_grants=(ToolGrant(tool_id="web.read", max_risk=0),),
        quota=TeamQuota(
            max_depth=2,
            max_children_per_parent=2,
            max_total_agents=3,
            max_parallel=2,
        ),
    )
    store.spawn_child(
        team_id="team-cancel",
        parent_id="root",
        child_id="child",
        agent_id="worker",
        agent_version=1,
        thread_id="thread-child",
        requested_grants=(),
    )
    return path, store


def _supervisor(
    store: MultiAgentStore,
    runtime: object,
    *,
    probe: _Probe | None = None,
) -> MultiAgentSupervisor:
    return MultiAgentSupervisor(
        runtime=runtime,
        store=store,
        definitions=_UnusedDefinitions(),
        cancellation_reconciliation=probe,
    )


def _assert_cancelled(store: MultiAgentStore) -> None:
    assert store.team_state("team-cancel") is TeamState.CANCELLED
    assert all(member.state is MemberState.CANCELLED for member in store.members("team-cancel"))


def test_uncertain_external_effect_is_durable_and_not_replayed_after_restart(
    tmp_path: Path,
) -> None:
    path, store = _make_store(tmp_path)
    runtime = _EffectThenErrorRuntime()
    supervisor = _supervisor(store, runtime)

    with pytest.raises(CancellationReconciliationRequired, match="uncertain cancellation result"):
        asyncio.run(supervisor.cancel_team("team-cancel"))

    assert runtime.cancel_effects == ["team:team-cancel:root", "team:team-cancel:child"]
    _assert_cancelled(store)

    restarted = MultiAgentStore(SQLiteStore(path))
    retry_runtime = _RecordingRuntime()
    restarted_supervisor = _supervisor(restarted, retry_runtime)
    with pytest.raises(CancellationReconciliationRequired, match="requires reconciliation"):
        asyncio.run(restarted_supervisor.cancel_team("team-cancel"))
    assert retry_runtime.cancel_effects == []
    with pytest.raises(CancellationReconciliationRequired, match="unfinished durable cancellation"):
        asyncio.run(restarted_supervisor.recover_team("team-cancel"))


def test_confirmed_reconciliation_does_not_repeat_already_effected_cancel(tmp_path: Path) -> None:
    path, store = _make_store(tmp_path)
    first_runtime = _EffectThenErrorRuntime()
    with pytest.raises(CancellationReconciliationRequired):
        asyncio.run(_supervisor(store, first_runtime).cancel_team("team-cancel"))

    restarted = MultiAgentStore(SQLiteStore(path))
    second_runtime = _RecordingRuntime()
    probe = _Probe(CancellationProbeState.CANCELLED)
    members = asyncio.run(
        _supervisor(restarted, second_runtime, probe=probe).reconcile_team_cancellation(
            "team-cancel"
        )
    )

    assert second_runtime.cancel_effects == []
    assert [request.member_id for request in probe.requests] == ["child"]
    assert all(member.state is MemberState.CANCELLED for member in members)
    with SQLiteStore(path).connection() as conn:
        row = conn.execute(
            "SELECT state FROM multi_agent_cancellations WHERE team_id = ?",
            ("team-cancel",),
        ).fetchone()
    assert row["state"] == "completed"


def test_not_cancelled_reconciliation_retries_only_the_exact_uncertain_member(
    tmp_path: Path,
) -> None:
    path, store = _make_store(tmp_path)
    with pytest.raises(CancellationReconciliationRequired):
        asyncio.run(_supervisor(store, _EffectThenErrorRuntime()).cancel_team("team-cancel"))

    restarted = MultiAgentStore(SQLiteStore(path))
    retry_runtime = _RecordingRuntime()
    probe = _Probe(CancellationProbeState.NOT_CANCELLED)
    asyncio.run(
        _supervisor(restarted, retry_runtime, probe=probe).reconcile_team_cancellation(
            "team-cancel"
        )
    )

    assert retry_runtime.cancel_effects == ["team:team-cancel:child"]
    assert [request.member_id for request in probe.requests] == ["child"]


def test_unknown_reconciliation_stays_blocked_without_an_external_retry(tmp_path: Path) -> None:
    path, store = _make_store(tmp_path)
    with pytest.raises(CancellationReconciliationRequired):
        asyncio.run(_supervisor(store, _EffectThenErrorRuntime()).cancel_team("team-cancel"))

    restarted = MultiAgentStore(SQLiteStore(path))
    retry_runtime = _RecordingRuntime()
    probe = _Probe(CancellationProbeState.UNKNOWN)
    with pytest.raises(CancellationReconciliationRequired, match="remains unresolved"):
        asyncio.run(
            _supervisor(restarted, retry_runtime, probe=probe).reconcile_team_cancellation(
                "team-cancel"
            )
        )
    assert retry_runtime.cancel_effects == []


def test_crash_after_dispatch_marker_requires_probe_before_retry(tmp_path: Path) -> None:
    path, store = _make_store(tmp_path)
    journal = TeamCancellationJournal(store)
    operation = journal.begin(team_id="team-cancel")
    first = operation.effects[0]
    journal.mark_dispatching(operation.operation_id, first.member_id)
    _assert_cancelled(store)

    restarted = MultiAgentStore(SQLiteStore(path))
    runtime = _RecordingRuntime()
    supervisor = _supervisor(restarted, runtime)
    with pytest.raises(CancellationReconciliationRequired, match="requires reconciliation"):
        asyncio.run(supervisor.cancel_team("team-cancel"))
    assert runtime.cancel_effects == []

    probe = _Probe(CancellationProbeState.NOT_CANCELLED)
    asyncio.run(
        _supervisor(restarted, runtime, probe=probe).reconcile_team_cancellation("team-cancel")
    )
    assert runtime.cancel_effects == ["team:team-cancel:root", "team:team-cancel:child"]


async def _run_competing_cancel_callers(store: MultiAgentStore) -> list[str]:
    runtime = _BlockingRuntime()
    first = _supervisor(store, runtime)
    second = _supervisor(store, runtime)
    first_task = asyncio.create_task(first.cancel_team("team-cancel"))
    await runtime.entered.wait()
    with pytest.raises(CancellationReconciliationRequired, match="requires reconciliation"):
        await second.cancel_team("team-cancel")
    assert runtime.cancel_effects == ["team:team-cancel:root"]
    runtime.release.set()
    await first_task
    return runtime.cancel_effects


def test_competing_cancel_callers_do_not_duplicate_external_effect(tmp_path: Path) -> None:
    _, store = _make_store(tmp_path)
    effects = asyncio.run(_run_competing_cancel_callers(store))
    assert effects == ["team:team-cancel:root", "team:team-cancel:child"]


def test_cancellation_intent_state_and_audit_roll_back_before_external_effect(
    tmp_path: Path,
) -> None:
    path, store = _make_store(tmp_path)
    runtime = _RecordingRuntime()
    supervisor = _supervisor(store, runtime)
    sqlite = SQLiteStore(path)
    with sqlite.connection() as conn:
        conn.execute(
            """CREATE TRIGGER fail_second_cancel_effect
            BEFORE INSERT ON multi_agent_cancellation_effects
            WHEN NEW.sequence = 1
            BEGIN
                SELECT RAISE(ABORT, 'injected cancellation intent failure');
            END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected cancellation intent failure"):
        asyncio.run(supervisor.cancel_team("team-cancel"))

    assert runtime.cancel_effects == []
    assert store.team_state("team-cancel") is TeamState.ACTIVE
    assert all(member.state is MemberState.SPAWNED for member in store.members("team-cancel"))
    with sqlite.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM multi_agent_cancellations").fetchone()[0] == 0
        events = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = ?",
            ("multi_agent.team_cancel_requested",),
        ).fetchone()[0]
    assert events == 0


def test_future_cancellation_extension_schema_fails_closed(tmp_path: Path) -> None:
    path, store = _make_store(tmp_path)
    _supervisor(store, _RecordingRuntime())
    sqlite = SQLiteStore(path)
    with sqlite.connection() as conn:
        conn.execute(
            "INSERT INTO multi_agent_cancellation_schema_migrations(version, applied_at) "
            "VALUES (?, ?)",
            (99, "2026-08-23T00:00:00+00:00"),
        )

    with pytest.raises(RuntimeError, match="newer than supported"):
        _supervisor(store, _RecordingRuntime())
