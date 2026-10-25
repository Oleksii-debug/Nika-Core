from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.m12_release_evidence as release_evidence
from scripts.m12_release_evidence import (
    _read_runtime_evidence_json,
    _require_rollback_operation_marker,
    _run_installed_pf11,
)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"route":"product_project","route":"other","spec_version":1,"project_id":"p"}',
        b'{"outer":{"identity":"a","identity":"b"}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":1e400}',
        b'{"value":"\xff"}',
        b'\xff',
    ],
)
def test_runtime_evidence_rejects_ambiguous_or_invalid_json(
    tmp_path: Path,
    raw: bytes,
) -> None:
    path = tmp_path / "evidence.json"
    path.write_bytes(raw)

    with pytest.raises(RuntimeError, match="invalid or oversized JSON"):
        _read_runtime_evidence_json(path, label="runtime evidence")


def test_runtime_evidence_rejects_oversized_input(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_bytes(b" " * (1024 * 1024 + 1))

    with pytest.raises(RuntimeError, match="invalid or oversized JSON"):
        _read_runtime_evidence_json(path, label="runtime evidence")


def test_runtime_evidence_rejects_integer_above_digit_limit(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_bytes(b'{"value":' + b"9" * 1235 + b"}")

    with pytest.raises(RuntimeError, match="invalid or oversized JSON"):
        _read_runtime_evidence_json(path, label="runtime evidence")


def test_runtime_evidence_enforces_integer_bit_boundary(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    accepted = 1 << 4095
    path.write_text(json.dumps({"value": accepted}), encoding="utf-8")
    assert _read_runtime_evidence_json(path, label="runtime evidence") == {"value": accepted}

    path.write_text('{"value":' + str(1 << 4096) + "}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid or oversized JSON"):
        _read_runtime_evidence_json(path, label="runtime evidence")


def test_runtime_evidence_rejects_excess_depth(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_text("[" * 65 + "0" + "]" * 65, encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid or oversized JSON"):
        _read_runtime_evidence_json(path, label="runtime evidence")


def test_runtime_evidence_accepts_bom_and_depth_boundary(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    nested: object = 0
    for _ in range(63):
        nested = [nested]
    payload = {"nested": nested}
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))

    assert _read_runtime_evidence_json(path, label="runtime evidence") == payload


def test_pf11_rejects_boolean_spec_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "pf11.json"
    output.write_text(
        '{"route":"product_project","spec_version":true,"project_id":"p"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(release_evidence, "_run_checked", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="invalid PF11 route evidence"):
        _run_installed_pf11(Path("NikaCore.exe"), output, env={})


def test_pf11_rejects_duplicate_identity_before_route_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "pf11.json"
    output.write_text(
        '{"route":"other","route":"product_project","spec_version":1,"project_id":"p"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(release_evidence, "_run_checked", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="invalid or oversized JSON"):
        _run_installed_pf11(Path("NikaCore.exe"), output, env={})


def test_pf11_accepts_exact_typed_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "pf11.json"
    payload = {
        "route": "product_project",
        "spec_version": 1,
        "project_id": "product-project",
    }
    output.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(release_evidence, "_run_checked", lambda *args, **kwargs: None)

    assert _run_installed_pf11(Path("NikaCore.exe"), output, env={}) == payload


def test_rollback_marker_rejects_boolean_version(tmp_path: Path) -> None:
    marker = tmp_path / "rollback-operation.json"
    marker.write_text(
        json.dumps(
            {
                "marker_version": True,
                "operation_id": "a" * 32,
                "source_digest": "b" * 64,
                "target_digest": "c" * 64,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="does not match exact image authority"):
        _require_rollback_operation_marker(
            marker,
            operation_id="a" * 32,
            source_digest="b" * 64,
            target_digest="c" * 64,
        )


def test_rollback_marker_rejects_duplicate_version(tmp_path: Path) -> None:
    marker = tmp_path / "rollback-operation.json"
    marker.write_text(
        '{"marker_version":true,"marker_version":1,'
        '"operation_id":"' + "a" * 32 + '",'
        '"source_digest":"' + "b" * 64 + '",'
        '"target_digest":"' + "c" * 64 + '"}',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="invalid or oversized JSON"):
        _require_rollback_operation_marker(
            marker,
            operation_id="a" * 32,
            source_digest="b" * 64,
            target_digest="c" * 64,
        )


def test_rollback_marker_accepts_exact_identity(tmp_path: Path) -> None:
    marker = tmp_path / "rollback-operation.json"
    payload = {
        "marker_version": 1,
        "operation_id": "a" * 32,
        "source_digest": "b" * 64,
        "target_digest": "c" * 64,
    }
    marker.write_text(json.dumps(payload), encoding="utf-8")

    _require_rollback_operation_marker(
        marker,
        operation_id="a" * 32,
        source_digest="b" * 64,
        target_digest="c" * 64,
    )
