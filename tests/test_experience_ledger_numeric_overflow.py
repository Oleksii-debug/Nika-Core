from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.experience_ledger import ContinuityKind, ContinuityOutcome, ExperienceLedger


def test_pathological_numeric_evidence_fails_closed_before_persistence(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Користувач Ніка" / "numeric overflow.db")
    store.initialize()
    ledger = ExperienceLedger(store)
    huge_integer = 10**10000

    with pytest.raises(ValueError, match="finite and non-negative"):
        ledger.record(
            event_key="task-overflow:delay",
            task_id="task-overflow",
            kind=ContinuityKind.RECOVERY,
            outcome=ContinuityOutcome.WAITING,
            reason_code="retry_pending",
            delay_seconds=huge_integer,
        )

    with pytest.raises(ValueError, match="finite and non-negative"):
        ledger.record(
            event_key="task-overflow:clock",
            task_id="task-overflow",
            kind=ContinuityKind.HIBERNATE,
            outcome=ContinuityOutcome.PRESERVED,
            reason_code="wake_reconciled",
            clock_jump_seconds=huge_integer,
        )

    assert ledger.get("task-overflow:delay") is None
    assert ledger.get("task-overflow:clock") is None
