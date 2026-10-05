from __future__ import annotations

import json

import pytest

import nika_core.product_project_history_archive as archive_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
)
from nika_core.product_project_history_archive import ProductProjectHistoryArchiveService


def _service(tmp_path) -> tuple[ProductProjectHistoryArchiveService, SQLiteStore]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ProductProjectRepository(store).create(
        project_id="project-1",
        name="Archive admission",
        spec=ProductProjectSpec(goal="goal", desired_outcome="outcome"),
        idempotency_key="create:project-1",
    )
    return ProductProjectHistoryArchiveService(store), store


def test_valid_archive_remains_round_trip_compatible(tmp_path) -> None:
    service, _ = _service(tmp_path)

    archive = service.build("project-1")
    verified = service.verify(archive.bytes)

    assert verified == archive.summary


@pytest.mark.parametrize(
    "raw",
    [
        b'{"digest_sha256":"0","digest_sha256":"1","payload":{}}',
        b'{"digest_sha256":"0","payload":{"score":NaN}}',
        b'{"digest_sha256":"0","payload":{"score":Infinity}}',
    ],
)
def test_archive_rejects_ambiguous_or_nonfinite_json_before_semantics(
    tmp_path,
    raw: bytes,
) -> None:
    service, _ = _service(tmp_path)

    with pytest.raises(ProductProjectError, match="invalid ProductProject history archive"):
        service.verify(raw)


def test_archive_rejects_excessive_json_nesting_with_controlled_error(tmp_path) -> None:
    service, _ = _service(tmp_path)
    nested = (
        b'{"digest_sha256":"'
        + (b"0" * 64)
        + b'","payload":'
        + (b"[" * (archive_module._MAX_JSON_DEPTH + 2))
        + b"0"
        + (b"]" * (archive_module._MAX_JSON_DEPTH + 2))
        + b"}"
    )

    with pytest.raises(ProductProjectError, match="JSON nesting limit"):
        service.verify(nested)


def test_archive_byte_limit_is_checked_before_decode(tmp_path, monkeypatch) -> None:
    service, _ = _service(tmp_path)
    monkeypatch.setattr(archive_module, "_MAX_ARCHIVE_BYTES", 16)

    with pytest.raises(ProductProjectError, match="exceeds byte limit"):
        service.verify(b"{" + (b"x" * 32) + b"}")


def test_archive_rejects_oversized_numeric_token_before_conversion(
    tmp_path,
    monkeypatch,
) -> None:
    service, _ = _service(tmp_path)
    monkeypatch.setattr(archive_module, "_MAX_JSON_NUMBER_CHARS", 4)

    with pytest.raises(ProductProjectError, match="invalid ProductProject history archive"):
        service.verify(b'{"digest_sha256":"0000","payload":{"score":12345}}')


def test_build_never_emits_archive_that_its_verifier_size_gate_rejects(
    tmp_path,
    monkeypatch,
) -> None:
    service, _ = _service(tmp_path)
    monkeypatch.setattr(archive_module, "_MAX_ARCHIVE_BYTES", 128)

    with pytest.raises(ProductProjectError, match="exceeds byte limit"):
        service.build("project-1")


def test_archive_requires_exact_bytes_carrier(tmp_path) -> None:
    service, _ = _service(tmp_path)
    archive = service.build("project-1")

    with pytest.raises(ProductProjectError, match="must be exact bytes"):
        service.verify(bytearray(archive.bytes))  # type: ignore[arg-type]


def test_archive_rejects_unauthenticated_envelope_extensions(tmp_path) -> None:
    service, _ = _service(tmp_path)
    archive = service.build("project-1")
    envelope = json.loads(archive.bytes)
    envelope["ignored_extension"] = {"would": "not be digest-bound"}

    with pytest.raises(
        ProductProjectError,
        match="invalid ProductProject history archive envelope",
    ):
        service.verify(
            json.dumps(
                envelope,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )


def test_build_rejects_duplicate_keys_in_durable_json_columns(tmp_path) -> None:
    service, store = _service(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "UPDATE product_project_specs SET spec_json=? "
            "WHERE project_id='project-1' AND spec_version=1",
            (
                '{"goal":"first","goal":"second","desired_outcome":"outcome",'
                '"requirements":[],"milestones":[],"repository_refs":[],'
                '"build_refs":[],"release_refs":[],"deployment_refs":[],'
                '"incident_refs":[],"supersedes_spec_version":null}',
            ),
        )

    with pytest.raises(ProductProjectError):
        service.build("project-1")


def test_build_rejects_nonfinite_unknown_audit_payload(tmp_path) -> None:
    service, store = _service(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO audit_events(event_type,entity_type,entity_id,payload_json,created_at) "
            "VALUES (?,?,?,?,?)",
            (
                "product_project.custom_observation",
                "product_project",
                "project-1",
                '{"score":NaN}',
                "2026-10-05T00:00:00+00:00",
            ),
        )

    with pytest.raises(ProductProjectError, match="durable JSON column: payload_json"):
        service.build("project-1")

@pytest.mark.parametrize(
    "value",
    (
        float("nan"),
        float("inf"),
        float("-inf"),
        b"not-json",
        ("tuple",),
        {"set"},
    ),
)
def test_build_shape_rejects_values_strict_verifier_cannot_admit(value) -> None:
    with pytest.raises(
        ProductProjectError,
        match="(?:non-finite JSON number|unsupported JSON value)",
    ):
        archive_module._validate_json_shape(
            {"value": value},
            label="ProductProject history archive payload",
        )


class _BehavioralString(str):
    pass


def test_build_shape_rejects_behavioral_json_object_keys() -> None:
    with pytest.raises(ProductProjectError, match="non-text JSON object key"):
        archive_module._validate_json_shape(
            {_BehavioralString("key"): "value"},
            label="ProductProject history archive payload",
        )
