from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

import pytest

from scripts import m11_release

SOURCE_SHA = "1" * 40
PROJECT_ID = "product-" + "a" * 64


def _payload() -> dict[str, object]:
    return {
        "route": "product_project",
        "project_id": PROJECT_ID,
        "spec_version": 1,
        "state": "active",
        "command_center_state_proven": True,
        "current_command_proven": True,
        "current_command_focus_proven": True,
        "bridge_state_project_id": PROJECT_ID,
        "bridge_state_spec_version": 1,
        "bridge_state_status_count": 0,
        "bridge_state_decision_count": 0,
        "restart_selection_integrity_proven": True,
        "bounded_projection_proven": True,
        "human_tested": False,
        "nvda_verified": False,
        "production_release_ready": False,
    }


def _proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    writer: Callable[[Path], None],
) -> Path:
    bundle = tmp_path / "NikaCore"
    bundle.mkdir(exist_ok=True)
    (bundle / "NikaCore.exe").write_bytes(b"candidate")

    def fake_run(argv, *, check, env, timeout):
        del check, env, timeout
        arguments = tuple(str(item) for item in argv)
        output = Path(arguments[arguments.index("--pf11-proof-output") + 1])
        writer(output)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(m11_release.subprocess, "run", fake_run)
    return m11_release.prove_packaged_product_journey(bundle, source_sha=SOURCE_SHA)


def _json_writer(payload: dict[str, object]) -> Callable[[Path], None]:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")

    def write(path: Path) -> None:
        path.write_bytes(raw)

    return write


def test_valid_exact_pf11_evidence_remains_accepted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = _proof(monkeypatch, tmp_path, _json_writer(_payload()))
    evidence = json.loads(target.read_text(encoding="utf-8"))

    assert evidence["schema_version"] == 2
    assert evidence["product_project_id"] == PROJECT_ID
    assert evidence["restart_replay_proven"] is True
    assert evidence["human_tested"] is False
    assert evidence["nvda_verified"] is False


def test_duplicate_json_keys_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    raw = json.dumps(_payload(), ensure_ascii=False, sort_keys=True)
    duplicate = (raw[:-1] + ',"route":"product_project"}').encode("utf-8")

    with pytest.raises(RuntimeError, match="strict JSON evidence"):
        _proof(monkeypatch, tmp_path, lambda path: path.write_bytes(duplicate))


def test_nonfinite_json_constants_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = _payload()
    payload["bridge_state_status_count"] = float("nan")

    with pytest.raises(RuntimeError, match="strict JSON evidence"):
        _proof(monkeypatch, tmp_path, _json_writer(payload))


@pytest.mark.parametrize("field", ["spec_version", "bridge_state_spec_version"])
def test_boolean_cannot_alias_required_integer_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
) -> None:
    payload = _payload()
    payload[field] = True

    with pytest.raises(RuntimeError, match=f"invalid {field}"):
        _proof(monkeypatch, tmp_path, _json_writer(payload))


@pytest.mark.parametrize(
    "field",
    [
        "command_center_state_proven",
        "current_command_proven",
        "current_command_focus_proven",
        "restart_selection_integrity_proven",
        "bounded_projection_proven",
    ],
)
def test_every_emitted_positive_proof_flag_is_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
) -> None:
    payload = _payload()
    payload[field] = False

    with pytest.raises(RuntimeError, match=f"{field}=true"):
        _proof(monkeypatch, tmp_path, _json_writer(payload))


def test_raw_pf11_schema_rejects_missing_or_unexpected_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing = _payload()
    missing.pop("current_command_proven")
    with pytest.raises(RuntimeError, match="schema mismatch"):
        _proof(monkeypatch, tmp_path, _json_writer(missing))

    unexpected = _payload()
    unexpected["pf11_complete"] = True
    with pytest.raises(RuntimeError, match="schema mismatch"):
        _proof(monkeypatch, tmp_path, _json_writer(unexpected))


@pytest.mark.parametrize(
    "project_id",
    [
        "product-" + "A" * 64,
        "product-" + "a" * 63,
        "../product-" + "a" * 64,
        "not-a-product-id",
    ],
)
def test_product_project_identity_is_canonical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    project_id: str,
) -> None:
    payload = _payload()
    payload["project_id"] = project_id
    payload["bridge_state_project_id"] = project_id

    with pytest.raises(RuntimeError, match="non-canonical ProductProject id"):
        _proof(monkeypatch, tmp_path, _json_writer(payload))


def test_invalid_utf8_evidence_is_rejected_before_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="valid UTF-8"):
        _proof(monkeypatch, tmp_path, lambda path: path.write_bytes(b"\xff\xfe"))


def test_oversized_evidence_is_rejected_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    oversized = b"{" + (b" " * m11_release._PF11_MAX_EVIDENCE_BYTES) + b"}"
    with pytest.raises(RuntimeError, match="exceeds the size limit"):
        _proof(monkeypatch, tmp_path, lambda path: path.write_bytes(oversized))

def test_data_adoption_helper_reuses_strict_pf11_evidence_reader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "NikaCore.exe"
    executable.write_bytes(b"candidate")
    output = tmp_path / "data-adoption.json"
    cwd = tmp_path / "launch"
    cwd.mkdir()
    raw = json.dumps(_payload(), ensure_ascii=False, sort_keys=True)
    duplicate = (raw[:-1] + ',"route":"product_project"}').encode("utf-8")

    def fake_run(argv, *, check, env, cwd, timeout):
        del argv, check, env, cwd, timeout
        output.write_bytes(duplicate)
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(m11_release.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="strict JSON evidence"):
        m11_release._run_packaged_pf11(
            executable,
            output=output,
            environment={},
            cwd=cwd,
        )

@pytest.mark.parametrize("state", ["active\x00hidden", "act\u2028ive", "act\u2060ive"])
def test_control_text_is_rejected_from_pf11_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
) -> None:
    payload = _payload()
    payload["state"] = state

    with pytest.raises(RuntimeError, match="invalid state"):
        _proof(monkeypatch, tmp_path, _json_writer(payload))

