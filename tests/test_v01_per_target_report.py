from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from nika_core.batch_report import (
    BatchReportProjectionError,
    TargetReportFacts,
    TargetReportStatus,
    project_batch_report,
    sanitize_report_reference,
)
from nika_core.runtime.idempotency import IdempotencyRecord, IdempotencyStatus


@dataclass
class FakeTarget:
    target_id: str
    input_positions: list[int]
    input_fingerprint: str
    operation_key: str
    attempt_state: str
    attempts: int = 0
    confirmed_result: dict[str, Any] | None = None
    uncertain_result: dict[str, Any] | None = None


@dataclass
class FakeState:
    task_id: str
    cursor_id: str
    input_count: int
    targets: list[FakeTarget]
    next_scheduled_intent: Any = None


def _record(
    target: FakeTarget,
    status: IdempotencyStatus,
    *,
    updated_at: str = "2026-08-28T20:05:00+00:00",
    result: dict[str, Any] | None = None,
    task_id: str = "task-1",
) -> IdempotencyRecord:
    return IdempotencyRecord(
        operation_key=target.operation_key,
        task_id=task_id,
        operation_type="v01.batch_target_effect",
        input_fingerprint=target.input_fingerprint,
        status=status,
        result=result,
        created_at="2026-08-28T20:00:00+00:00",
        updated_at=updated_at,
    )


def _single_target(
    *,
    attempt_state: str = "PENDING",
    attempts: int = 0,
    target_id: str = "declared-target",
) -> tuple[FakeState, FakeTarget]:
    target = FakeTarget(
        target_id=target_id,
        input_positions=[0],
        input_fingerprint="fp-0",
        operation_key="op-0",
        attempt_state=attempt_state,
        attempts=attempts,
    )
    return FakeState("task-1", "cursor-1", 1, [target]), target


def test_all_seven_statuses_project_from_canonical_state_and_effect_truth() -> None:
    states = [
        "PENDING",
        "IN_FLIGHT",
        "CONFIRMED",
        "FAILED",
        "UNCERTAIN",
        "SKIPPED",
        "CANCELLED",
    ]
    targets = [
        FakeTarget(
            target_id=f"target-{index}",
            input_positions=[index],
            input_fingerprint=f"fp-{index}",
            operation_key=f"op-{index}",
            attempt_state=state,
            attempts=1 if state in {"IN_FLIGHT", "CONFIRMED", "UNCERTAIN"} else 0,
            confirmed_result={"evidence_ref": "evidence:verified"}
            if state == "CONFIRMED"
            else None,
            uncertain_result={"reason": "effect_not_proven", "evidence_ref": "evidence:check"}
            if state == "UNCERTAIN"
            else None,
        )
        for index, state in enumerate(states)
    ]
    state = FakeState("task-1", "cursor-1", len(targets), targets)
    records = {
        targets[1].operation_key: _record(targets[1], IdempotencyStatus.PENDING),
        targets[2].operation_key: _record(
            targets[2],
            IdempotencyStatus.COMPLETED,
            result={"evidence_ref": "evidence:verified"},
        ),
        targets[4].operation_key: _record(targets[4], IdempotencyStatus.UNCERTAIN),
    }

    report = project_batch_report(
        state,
        batch_updated_at="2026-08-28T20:00:00Z",
        effect_records=records,
    )

    assert [item.status for item in report] == list(TargetReportStatus)
    assert report[1].attempted is True
    assert report[1].opened is None
    assert report[2].reason == "verified_success"
    assert report[4].reason == "effect_not_proven"


def test_duplicate_declared_inputs_expand_to_stable_distinct_rows_in_input_order() -> None:
    repeated = FakeTarget(
        target_id="same-target",
        input_positions=[0, 2],
        input_fingerprint="fp-a",
        operation_key="op-a",
        attempt_state="PENDING",
    )
    middle = FakeTarget(
        target_id="middle-target",
        input_positions=[1],
        input_fingerprint="fp-b",
        operation_key="op-b",
        attempt_state="PENDING",
    )
    state = FakeState("task-1", "cursor-1", 3, [repeated, middle])

    first = project_batch_report(state, batch_updated_at="2026-08-28T20:00:00Z")
    second = project_batch_report(state, batch_updated_at="2026-08-28T20:00:00Z")

    assert [item.input_order for item in first] == [0, 1, 2]
    assert first == second
    assert first[0].target_identity != first[2].target_identity
    assert first[0].display_name == "Target 1"
    assert first[2].display_name == "Target 3"


def test_uncertain_effect_dominates_later_cancel_fact_and_blocks_future_times() -> None:
    state, target = _single_target(attempt_state="UNCERTAIN", attempts=1)
    target.uncertain_result = {"reason": "external_effect_unresolved"}
    fact = TargetReportFacts(
        target_id=target.target_id,
        input_order=0,
        terminal_status=TargetReportStatus.CANCELLED,
        opened=True,
        attempted=True,
        updated_at="2026-08-28T20:06:00Z",
    )

    report = project_batch_report(
        state,
        batch_updated_at="2026-08-28T20:00:00Z",
        effect_records={target.operation_key: _record(target, IdempotencyStatus.UNCERTAIN)},
        facts=[fact],
    )

    assert report[0].status is TargetReportStatus.UNCERTAIN
    assert report[0].reason == "external_effect_unresolved"
    assert report[0].next_retry_at is None
    assert report[0].next_wake_at is None


def test_pending_target_uses_only_authoritative_retry_and_batch_wake_times() -> None:
    state, target = _single_target()
    state.next_scheduled_intent = SimpleNamespace(
        target_id=target.target_id,
        not_before="2026-08-28T20:30:00Z",
    )
    fact = TargetReportFacts(
        target_id=target.target_id,
        input_order=0,
        next_retry_at="2026-08-28T20:20:00+00:00",
        next_wake_at="2026-08-28T20:30:00+00:00",
    )

    report = project_batch_report(
        state,
        batch_updated_at="2026-08-28T20:00:00Z",
        facts=[fact],
    )

    assert report[0].status is TargetReportStatus.PENDING
    assert report[0].next_retry_at == "2026-08-28T20:20:00+00:00"
    assert report[0].next_wake_at == "2026-08-28T20:30:00+00:00"


def test_report_uses_latest_authoritative_timestamp_without_calling_now() -> None:
    state, target = _single_target(attempt_state="CONFIRMED", attempts=1)
    target.confirmed_result = {"evidence_ref": "evidence:ok"}
    fact = TargetReportFacts(
        target_id=target.target_id,
        input_order=0,
        updated_at="2026-08-28T20:06:00Z",
    )
    report = project_batch_report(
        state,
        batch_updated_at=datetime(2026, 8, 28, 20, 1, tzinfo=UTC),
        effect_records={
            target.operation_key: _record(
                target,
                IdempotencyStatus.COMPLETED,
                updated_at="2026-08-28T20:05:00+00:00",
                result={"evidence_ref": "evidence:ok"},
            )
        },
        facts=[fact],
    )

    assert report[0].updated_at == "2026-08-28T20:06:00+00:00"


def test_security_canaries_never_escape_raw_html_credentials_headers_or_result_body() -> None:
    state, target = _single_target(
        attempt_state="CONFIRMED",
        attempts=1,
        target_id="https://alice:PW-CANARY@example.test/run?token=QUERY-CANARY#frag",
    )
    target.confirmed_result = {
        "html": "<html><script>HTML-CANARY</script></html>",
        "cookie": "SID=COOKIE-CANARY",
        "token": "TOKEN-CANARY",
        "evidence_ref": (
            "https://alice:PW-CANARY@example.test/run?token=QUERY-CANARY#frag"
        ),
    }
    fact = TargetReportFacts(
        target_id=target.target_id,
        input_order=0,
        display_name="<script>NAME-CANARY</script>",
        opened=True,
        attempted=True,
        reason_code="X-Custom-Header: HEADER-CANARY TOKEN-CANARY",
        evidence_ref="Cookie: SID=FACT-CANARY",
    )
    record = _record(
        target,
        IdempotencyStatus.COMPLETED,
        result=target.confirmed_result,
    )

    report = project_batch_report(
        state,
        batch_updated_at="2026-08-28T20:00:00Z",
        effect_records={target.operation_key: record},
        facts=[fact],
    )
    serialized = report[0].model_dump_json()

    assert report[0].display_name == "Target 1"
    assert report[0].evidence_ref == "https://example.test/run"
    for canary in (
        "PW-CANARY",
        "QUERY-CANARY",
        "HTML-CANARY",
        "COOKIE-CANARY",
        "TOKEN-CANARY",
        "NAME-CANARY",
        "HEADER-CANARY",
        "FACT-CANARY",
        "alice",
        "X-Custom-Header",
        "Authorization",
        "Bearer",
        "<script>",
    ):
        assert canary not in serialized


def test_sensitive_url_path_is_minimized_to_origin() -> None:
    assert (
        sanitize_report_reference(
            "https://user:pass@example.test/reset/access_token/abc123?signature=SECRET#frag"
        )
        == "https://example.test"
    )


def test_opaque_references_are_allowlisted_and_bounded() -> None:
    assert sanitize_report_reference("evidence:abc-123") == "evidence:abc-123"
    assert sanitize_report_reference("artifact:report/42") == "artifact:report/42"
    assert sanitize_report_reference("sk-proj-raw-secret-value") is None
    assert sanitize_report_reference("evidence:token=SECRET") is None


def test_effect_bearing_cursor_without_durable_effect_record_fails_closed() -> None:
    for cursor_state in ("IN_FLIGHT", "CONFIRMED", "UNCERTAIN"):
        state, target = _single_target(attempt_state=cursor_state, attempts=1)
        if cursor_state == "CONFIRMED":
            target.confirmed_result = {"evidence_ref": "evidence:ok"}
        if cursor_state == "UNCERTAIN":
            target.uncertain_result = {"reason": "unresolved"}
        with pytest.raises(BatchReportProjectionError, match="lacks durable effect evidence"):
            project_batch_report(state, batch_updated_at="2026-08-28T20:00:00Z")


def test_effect_record_must_bind_task_operation_and_input_fingerprint() -> None:
    state, target = _single_target(attempt_state="IN_FLIGHT", attempts=1)
    wrong = _record(target, IdempotencyStatus.PENDING, task_id="other-task")
    with pytest.raises(BatchReportProjectionError, match="task identity mismatch"):
        project_batch_report(
            state,
            batch_updated_at="2026-08-28T20:00:00Z",
            effect_records={target.operation_key: wrong},
        )

    wrong_type = IdempotencyRecord(
        operation_key=target.operation_key,
        task_id="task-1",
        operation_type="other.effect",
        input_fingerprint=target.input_fingerprint,
        status=IdempotencyStatus.PENDING,
        result=None,
        created_at="2026-08-28T20:00:00+00:00",
        updated_at="2026-08-28T20:05:00+00:00",
    )
    with pytest.raises(BatchReportProjectionError, match="operation type mismatch"):
        project_batch_report(
            state,
            batch_updated_at="2026-08-28T20:00:00Z",
            effect_records={target.operation_key: wrong_type},
        )


def test_supplemental_facts_cannot_become_effect_success_or_uncertain_authority() -> None:
    state, target = _single_target()
    for forbidden in (
        TargetReportStatus.RUNNING,
        TargetReportStatus.SUCCEEDED,
        TargetReportStatus.UNCERTAIN,
        TargetReportStatus.PENDING,
    ):
        fact = TargetReportFacts(
            target_id=target.target_id,
            input_order=0,
            terminal_status=forbidden,
        )
        with pytest.raises(BatchReportProjectionError, match="may only declare"):
            project_batch_report(
                state,
                batch_updated_at="2026-08-28T20:00:00Z",
                facts=[fact],
            )


def test_terminal_facts_cannot_schedule_retry_or_wake() -> None:
    with pytest.raises(BatchReportProjectionError, match="cannot schedule future work"):
        state, target = _single_target()
        project_batch_report(
            state,
            batch_updated_at="2026-08-28T20:00:00Z",
            facts=[
                TargetReportFacts(
                    target_id=target.target_id,
                    input_order=0,
                    terminal_status=TargetReportStatus.FAILED,
                    next_retry_at="2026-08-28T21:00:00Z",
                )
            ],
        )


def test_attempted_false_cannot_override_durable_attempt_truth() -> None:
    state, target = _single_target(attempt_state="IN_FLIGHT", attempts=1)
    fact = TargetReportFacts(
        target_id=target.target_id,
        input_order=0,
        attempted=False,
    )
    with pytest.raises(BatchReportProjectionError, match="contradicts durable effect state"):
        project_batch_report(
            state,
            batch_updated_at="2026-08-28T20:00:00Z",
            effect_records={target.operation_key: _record(target, IdempotencyStatus.PENDING)},
            facts=[fact],
        )


def test_opened_is_not_invented_after_attempt_when_no_canonical_open_fact_exists() -> None:
    state, target = _single_target(attempt_state="IN_FLIGHT", attempts=1)
    report = project_batch_report(
        state,
        batch_updated_at="2026-08-28T20:00:00Z",
        effect_records={target.operation_key: _record(target, IdempotencyStatus.PENDING)},
    )
    assert report[0].attempted is True
    assert report[0].opened is None


def test_undeclared_fact_duplicate_position_and_malformed_timestamps_fail_closed() -> None:
    state, target = _single_target()
    with pytest.raises(BatchReportProjectionError, match="undeclared target input"):
        project_batch_report(
            state,
            batch_updated_at="2026-08-28T20:00:00Z",
            facts=[TargetReportFacts(target_id="other", input_order=0)],
        )
    with pytest.raises(BatchReportProjectionError, match="timezone-aware"):
        project_batch_report(state, batch_updated_at="2026-08-28T20:00:00")
    with pytest.raises(ValidationError):
        TargetReportFacts(target_id=target.target_id, input_order=-1)


def test_projection_is_read_only_and_does_not_mutate_state_records_or_facts() -> None:
    state, target = _single_target(attempt_state="CONFIRMED", attempts=1)
    target.confirmed_result = {"evidence_ref": "evidence:ok", "raw": {"secret": "CANARY"}}
    records = {
        target.operation_key: _record(
            target,
            IdempotencyStatus.COMPLETED,
            result={"evidence_ref": "evidence:ok", "raw": {"secret": "CANARY"}},
        )
    }
    facts = [
        TargetReportFacts(
            target_id=target.target_id,
            input_order=0,
            opened=True,
            attempted=True,
        )
    ]
    state_before = deepcopy(state)
    records_before = deepcopy(records)
    facts_before = deepcopy(facts)

    report = project_batch_report(
        state,
        batch_updated_at="2026-08-28T20:00:00Z",
        effect_records=records,
        facts=facts,
    )

    assert report[0].evidence_ref == "evidence:ok"
    assert state == state_before
    assert records == records_before
    assert facts == facts_before
    assert "CANARY" not in report[0].model_dump_json()
