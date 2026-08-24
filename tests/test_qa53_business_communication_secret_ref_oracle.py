from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.business_communication import (
    COMMUNICATION_SCHEMA,
    BusinessCommunication,
    BusinessCommunicationRepository,
    CommunicationAuditEvent,
)
from nika_core.data.sqlite import SQLiteStore


@pytest.mark.parametrize(
    ("field", "value", "canary"),
    [
        (
            "payload_ref",
            "api_key=QA53_CANARY_BUSINESS_PAYLOAD_5D21",
            "QA53_CANARY_BUSINESS_PAYLOAD_5D21",
        ),
        (
            "thread_ref",
            "https://user:QA53_CANARY_BUSINESS_THREAD_6A40@provider.invalid/thread/1",
            "QA53_CANARY_BUSINESS_THREAD_6A40",
        ),
        (
            "counterparty_ref",
            "Bearer QA53_CANARY_BUSINESS_COUNTERPARTY_7B53",
            "QA53_CANARY_BUSINESS_COUNTERPARTY_7B53",
        ),
    ],
)
def test_qa53_business_refs_must_not_persist_raw_secret_canaries(
    tmp_path: Path,
    field: str,
    value: str,
    canary: str,
) -> None:
    store = SQLiteStore(tmp_path / "qa53-business.db")
    store.initialize()
    repository = BusinessCommunicationRepository(store)
    repository.initialize()

    event = CommunicationAuditEvent(
        sequence=1,
        event_type="communication.drafted",
        evidence_ref="payload:sha256:synthetic",
        recorded_at="2026-08-24T00:00:00+00:00",
    )
    record = BusinessCommunication(
        schema=COMMUNICATION_SCHEMA,
        message_id="qa53-message",
        objective_id="qa53-objective",
        lead_id="qa53-lead",
        counterparty_ref="counterparty:synthetic",
        thread_ref="thread:synthetic",
        channel_id="qa53-channel",
        payload_ref="payload:sha256:synthetic",
        policy_id="qa53-policy",
        audit=(event,),
        row_version=1,
    )
    record = replace(record, **{field: value})

    repository.save(record, expected_row_version=0)

    with store.connection() as conn:
        raw = str(
            conn.execute(
                "SELECT payload_json FROM business_communications WHERE message_id = ?",
                (record.message_id,),
            ).fetchone()[0]
        )

    assert canary not in raw
