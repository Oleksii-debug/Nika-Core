from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from nika_core.product_factory_incident_contracts import (
    INCIDENT_LIFECYCLE_SCHEMA,
    IncidentKind,
    IncidentLifecycleSnapshot,
    IncidentSeverity,
    IncidentTrigger,
    ProductIncidentError,
)
from nika_core.product_factory_incident_persistence import (
    MAX_INCIDENT_SNAPSHOT_BYTES,
    dump_incident_snapshot,
    load_incident_snapshot,
)


class _BehavioralSnapshotText(str):
    events: list[str]

    def __len__(self) -> int:
        self.events.append("len")
        return super().__len__()


class _BehavioralDateTime(datetime):
    events: list[str]

    def utcoffset(self) -> object:
        self.events.append("utcoffset")
        return super().utcoffset()

    def astimezone(self, *args: object, **kwargs: object) -> datetime:
        self.events.append("astimezone")
        return super().astimezone(*args, **kwargs)



def _behavioral_datetime(events: list[str]) -> _BehavioralDateTime:
    value = _BehavioralDateTime(2026, 10, 5, 12, 0, tzinfo=UTC)
    value.events = events
    return value


def _empty_snapshot(project_id: str = "проєкт-Ніка") -> IncidentLifecycleSnapshot:
    return IncidentLifecycleSnapshot(INCIDENT_LIFECYCLE_SCHEMA, project_id, (), ())


def test_incident_json_unicode_round_trip_and_canonical_stability() -> None:
    snapshot = _empty_snapshot()
    payload = dump_incident_snapshot(snapshot)
    assert "проєкт-Ніка" in payload
    assert load_incident_snapshot(payload) == snapshot
    assert dump_incident_snapshot(load_incident_snapshot(payload)) == payload


@pytest.mark.parametrize(
    "payload",
    (
        '{"schema":"a","schema":"b","project_id":"p","incidents":[],"fingerprint_index":[]}',
        (
            '{"schema":"a","project_id":"p","incidents":'
            '[{"incident_id":"first","incident_id":"second"}],"fingerprint_index":[]}'
        ),
    ),
)
def test_duplicate_keys_fail_before_snapshot_or_incident_admission(payload: str) -> None:
    with pytest.raises(ProductIncidentError, match="duplicate JSON keys"):
        load_incident_snapshot(payload)


@pytest.mark.parametrize("number", ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"))
def test_nonfinite_numbers_fail_before_snapshot_admission(number: str) -> None:
    payload = dump_incident_snapshot(_empty_snapshot())
    corrupted = payload.replace('"incidents":[]', f'"incidents":[{number}]')
    with pytest.raises(ProductIncidentError, match="non-finite JSON numbers"):
        load_incident_snapshot(corrupted)


def test_invalid_json_and_deeply_nested_input_fail_with_domain_error() -> None:
    with pytest.raises(ProductIncidentError, match="invalid JSON"):
        load_incident_snapshot('{"schema":')
    with pytest.raises(ProductIncidentError, match="JSON depth"):
        load_incident_snapshot("[" * 1500 + "0" + "]" * 1500)


def test_snapshot_byte_limit_rejects_ascii_before_decoding() -> None:
    payload = dump_incident_snapshot(_empty_snapshot())
    assert load_incident_snapshot(
        " " * (MAX_INCIDENT_SNAPSHOT_BYTES - len(payload)) + payload
    ) == _empty_snapshot()
    with pytest.raises(ProductIncidentError, match="byte limit"):
        load_incident_snapshot(" " * (MAX_INCIDENT_SNAPSHOT_BYTES + 1) + payload)


def test_utf8_byte_budget_rejects_multibyte_text_under_character_limit() -> None:
    oversized = json.dumps(
        {
            "schema": INCIDENT_LIFECYCLE_SCHEMA,
            "project_id": "ї" * (MAX_INCIDENT_SNAPSHOT_BYTES // 2),
            "incidents": [],
            "fingerprint_index": [],
        },
        ensure_ascii=False,
    )
    assert len(oversized) < MAX_INCIDENT_SNAPSHOT_BYTES
    with pytest.raises(ProductIncidentError, match="byte limit"):
        load_incident_snapshot(oversized)


def test_dump_will_not_publish_a_snapshot_its_loader_cannot_admit() -> None:
    with pytest.raises(ProductIncidentError, match="byte limit"):
        dump_incident_snapshot(_empty_snapshot("ї" * (MAX_INCIDENT_SNAPSHOT_BYTES // 2)))


@pytest.mark.parametrize("identity", ("\\ud800", "\\udfff"))
def test_escaped_surrogates_rejected_with_domain_error(identity: str) -> None:
    payload = dump_incident_snapshot(_empty_snapshot("project-a"))
    payload = payload.replace('"project-a"', f'"{identity}"')
    with pytest.raises(ProductIncidentError, match="valid UTF-8"):
        load_incident_snapshot(payload)


def test_direct_surrogate_in_dump_and_load_is_a_controlled_error() -> None:
    malformed = "broken-" + chr(0xD800)
    with pytest.raises(ProductIncidentError, match="cannot be serialized"):
        dump_incident_snapshot(_empty_snapshot(malformed))
    payload = dump_incident_snapshot(_empty_snapshot("project-a"))
    with pytest.raises(ProductIncidentError, match="valid UTF-8"):
        load_incident_snapshot(payload.replace("project-a", malformed))


@pytest.mark.parametrize(
    "observed_at",
    ("0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"),
)
def test_unrepresentable_utc_incident_time_fails_with_domain_error(
    observed_at: str,
) -> None:
    payload = json.loads(dump_incident_snapshot(_empty_snapshot("project-a")))
    payload["incidents"] = [
        {
            "incident_id": "incident-1",
            "trigger": {
                "project_id": "project-a",
                "service_id": "api",
                "environment_id": "prod-eu",
                "release_sha": "1" * 40,
                "kind": "health",
                "severity": "high",
                "evidence_refs": ["health://degraded"],
                "approval_ref": "approval://incident",
                "observed_at": observed_at,
                "advisory": None,
            },
            "state": "open",
            "work_order": None,
            "candidates": [],
            "release_events": [],
        }
    ]
    with pytest.raises(ProductIncidentError, match="ISO-8601 datetime text"):
        load_incident_snapshot(json.dumps(payload))


def test_quoted_brackets_and_escaped_quotes_do_not_count_as_json_depth() -> None:
    project_id = 'проєкт [ { } ] з "лапками" та \\\\'
    snapshot = _empty_snapshot(project_id)
    assert load_incident_snapshot(dump_incident_snapshot(snapshot)) == snapshot


@pytest.mark.parametrize("payload", ('{"a":]', ']["a"', '{"a":"unclosed}'))
def test_invalid_structure_is_rejected_before_record_hydration(payload: str) -> None:
    with pytest.raises(ProductIncidentError, match="invalid JSON"):
        load_incident_snapshot(payload)


@pytest.mark.parametrize(
    "observed_at",
    (
        "0001-01-01T00:00:00+14:00",
        "9999-12-31T23:59:59-14:00",
    ),
)
def test_direct_incident_trigger_rejects_utc_overflow(observed_at: str) -> None:
    with pytest.raises(ProductIncidentError, match="representable in UTC"):
        IncidentTrigger(
            "project-a",
            "api",
            "prod-eu",
            "1" * 40,
            IncidentKind.HEALTH,
            IncidentSeverity.HIGH,
            ("health://degraded",),
            "approval://incident",
            datetime.fromisoformat(observed_at),
        )


def test_direct_incident_trigger_rejects_nondatetime_without_attribute_error() -> None:
    with pytest.raises(ProductIncidentError, match="timezone-aware datetime"):
        IncidentTrigger(
            "project-a",
            "api",
            "prod-eu",
            "1" * 40,
            IncidentKind.HEALTH,
            IncidentSeverity.HIGH,
            ("health://degraded",),
            "approval://incident",
            "2026-10-05T12:00:00+00:00",  # type: ignore[arg-type]
        )


def test_snapshot_loader_rejects_behavioral_text_before_string_operations() -> None:
    events: list[str] = []
    payload = _BehavioralSnapshotText(
        dump_incident_snapshot(_empty_snapshot("project-a"))
    )
    payload.events = events

    with pytest.raises(ProductIncidentError, match="non-empty JSON text"):
        load_incident_snapshot(payload)

    assert events == []


def test_direct_trigger_rejects_behavioral_datetime_before_timezone_hooks() -> None:
    events: list[str] = []

    with pytest.raises(ProductIncidentError, match="timezone-aware datetime"):
        IncidentTrigger(
            "project-a",
            "api",
            "prod-eu",
            "1" * 40,
            IncidentKind.HEALTH,
            IncidentSeverity.HIGH,
            ("health://degraded",),
            "approval://incident",
            _behavioral_datetime(events),
        )

    assert events == []
