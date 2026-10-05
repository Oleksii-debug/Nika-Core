from __future__ import annotations

import json
from pathlib import Path

import pytest

import nika_core.training_physical_evaluation_driver as driver
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelIntegrityBasis,
)
from nika_core.model_engineering import EvaluationPurpose
from nika_core.training_physical_pilot import PhysicalTrainingPilotReport


def _payload(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workspace_id": "evaluation-workspace",
        "project_id": "evaluation-project",
        "owner_id": "evaluation-owner",
        "physical_pilot_output_root": str(tmp_path / "pilot output"),
        "frozen_package_path": str(tmp_path / "frozen-package.json"),
        "base_artifact_ref": "models/base",
        "base_model_path": str(tmp_path / "base model.gguf"),
        "candidate_model_path": str(
            tmp_path / "pilot output" / "candidate" / "adapter_model.safetensors"
        ),
        "base_model": {
            "provider_id": "local-base",
            "model_id": "base-model",
            "model_version": "v1",
            "source_reference": "https://example.com/models/base",
            "license_reference": "https://example.com/licenses/base",
            "capabilities": ["text"],
        },
        "candidate_model": {
            "model_id": "nika-pilot-adapter",
            "source_reference": "https://example.com/models/base",
            "license_reference": "https://example.com/licenses/base",
        },
        "evaluator": {
            "executable": str(tmp_path / "evaluator.exe"),
            "command_files": [str(tmp_path / "evaluate.py")],
            "switches": ["--strict"],
            "provenance_ref": "https://example.com/evaluator/source",
            "license_ref": "https://example.com/evaluator/license",
        },
        "evaluation_set_path": str(tmp_path / "held-out.json"),
        "experiment_id": "physical-old-new-job-1",
        "permission_fingerprint": "evaluation-read-only",
        "benchmark": {
            "timeout_seconds": 60.0,
            "temperature": 0.0,
            "scorer_id": "exact-match-nfc-v1",
        },
        "policy": {
            "primary_metric": "model_quality_score",
            "minimum_improvement": 0.0,
            "minimum_replays": 1,
            "primary_higher_is_better": True,
            "guardrails": [
                {
                    "metric": "model_task_pass",
                    "higher_is_better": True,
                    "max_regression": 0.0,
                }
            ],
        },
    }


def _config(tmp_path: Path) -> driver.PhysicalEvaluationConfig:
    raw = json.dumps(_payload(tmp_path), ensure_ascii=False, sort_keys=True)
    return driver.PhysicalEvaluationConfig.from_json(raw)


def _evaluation_payload(*, purpose: str = "held_out") -> dict[str, object]:
    return {
        "evaluation_set_id": "held-out-physical",
        "version": "v1",
        "provenance_ref": "dataset:held-out-physical",
        "license_ref": "license:held-out-physical",
        "purpose": purpose,
        "privacy": "private",
        "cases": [
            {
                "case_id": "case-one",
                "messages": [{"role": "user", "content": "секретне питання"}],
                "expected_text": "відповідь",
                "pass_score": 1.0,
                "weight": 1.0,
            }
        ],
    }


def _candidate_config() -> driver.CandidateModelConfig:
    return driver.CandidateModelConfig(
        model_id="nika-pilot-adapter",
        source_reference="https://example.com/models/base",
        license_reference="https://example.com/licenses/base",
    )


def _candidate_descriptor() -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EXTERNAL_LOCAL,
        provider_id="training-runtime",
        model_id="nika-pilot-adapter",
        model_version="9" * 64,
        source_reference="https://example.com/models/base",
        license_reference="https://example.com/licenses/base",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256="9" * 64,
        size_bytes=123,
        capabilities=("text",),
    )


def _pilot_report(
    *,
    descriptor: ModelArtifactDescriptor | None = None,
) -> PhysicalTrainingPilotReport:
    descriptor = descriptor or _candidate_descriptor()
    return PhysicalTrainingPilotReport(
        job_id="pilot-job",
        base_sha256="1" * 64,
        frozen_package_sha256="2" * 64,
        training_material_sha256="3" * 64,
        scale_authorization_sha256="4" * 64,
        execution_plan_sha256="5" * 64,
        job_fingerprint="6" * 64,
        paused_checkpoint_id="pause-checkpoint",
        restart_checkpoint_id="restart-checkpoint",
        completed_checkpoint_id="complete-checkpoint",
        candidate_artifact_ref="models/pilot-candidate",
        candidate_descriptor_sha256=descriptor.descriptor_digest,
        candidate_registry_key=descriptor.registry_key,
        candidate_sha256="9" * 64,
        candidate_byte_count=123,
        completed_steps=2,
    )


def test_config_parses_strict_local_authorities(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert config.workspace_id == "evaluation-workspace"
    assert config.base_model.provider_id == "local-base"
    assert config.candidate_model.model_id == "nika-pilot-adapter"
    assert config.evaluator.switches == ("--strict",)
    assert config.benchmark.scorer_id == "exact-match-nfc-v1"
    assert config.policy.primary_metric == "model_quality_score"


def test_config_rejects_unknown_top_level_field(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["unexpected"] = True

    with pytest.raises(driver.PhysicalEvaluationDriverError, match="fields are invalid"):
        driver.PhysicalEvaluationConfig.from_json(json.dumps(payload))


def test_config_rejects_duplicate_json_field(tmp_path: Path) -> None:
    body = json.dumps(_payload(tmp_path))
    duplicated = body[:-1] + ',"owner_id":"other"}'

    with pytest.raises(driver.PhysicalEvaluationDriverError, match="invalid JSON"):
        driver.PhysicalEvaluationConfig.from_json(duplicated)


@pytest.mark.parametrize(
    "switch",
    ("script.py", "../escape", "--bad=value", "/absolute", "модель"),
)
def test_config_rejects_unbound_evaluator_arguments(
    tmp_path: Path,
    switch: str,
) -> None:
    payload = _payload(tmp_path)
    evaluator = payload["evaluator"]
    assert isinstance(evaluator, dict)
    evaluator["switches"] = [switch]

    with pytest.raises(driver.PhysicalEvaluationDriverError, match="simple --option"):
        driver.PhysicalEvaluationConfig.from_json(json.dumps(payload))



@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("provenance_ref", r"C:\\private\\evaluator.exe"),
        ("license_ref", "https://example.com/license?token=secret"),
        ("provenance_ref", "env:EVALUATOR_SECRET"),
    ),
)
def test_config_rejects_private_or_secret_evaluator_provenance(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    payload = _payload(tmp_path)
    evaluator = payload["evaluator"]
    assert isinstance(evaluator, dict)
    evaluator[field] = value

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="public and secret-free",
    ):
        driver.PhysicalEvaluationConfig.from_json(json.dumps(payload))

def test_evaluation_set_parser_preserves_held_out_identity() -> None:
    evaluation = driver._evaluation_set_from_json(
        json.dumps(_evaluation_payload(), ensure_ascii=False)
    )

    assert evaluation.purpose is EvaluationPurpose.HELD_OUT
    assert evaluation.cases[0].messages[0].content == "секретне питання"
    assert len(evaluation.content_sha256) == 64


def test_evaluation_set_parser_rejects_development_data() -> None:
    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="purpose=held_out",
    ):
        driver._evaluation_set_from_json(
            json.dumps(_evaluation_payload(purpose="development"))
        )


def test_evaluation_set_parser_rejects_duplicate_case_field() -> None:
    raw = json.dumps(_evaluation_payload(), ensure_ascii=False)
    raw = raw.replace(
        '"case_id": "case-one"',
        '"case_id": "case-one", "case_id": "case-two"',
    )

    with pytest.raises(driver.PhysicalEvaluationDriverError, match="invalid JSON"):
        driver._evaluation_set_from_json(raw)


def test_non_windows_gate_precedes_filesystem_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(driver, "_is_windows", lambda: False)

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="must execute on Windows",
    ):
        driver.run_physical_evaluation_from_config(config)

    assert not config.physical_pilot_output_root.exists()


def test_find_pilot_task_requires_exact_unique_identity(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "pilot.sqlite3")
    store.initialize()
    expected = TaskQueue(store).create(
        workspace_id="evaluation-workspace",
        agent_id="physical-peft-pilot",
        payload={"job_id": "pilot-job", "kind": "physical_peft_pilot"},
    )
    TaskQueue(store).create(
        workspace_id="other-workspace",
        agent_id="physical-peft-pilot",
        payload={"job_id": "pilot-job", "kind": "physical_peft_pilot"},
    )

    actual = driver._find_pilot_task(
        store,
        workspace_id="evaluation-workspace",
        job_id="pilot-job",
    )

    assert actual.task_id == expected.task_id


def test_find_pilot_task_rejects_ambiguous_identity(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "pilot.sqlite3")
    store.initialize()
    queue = TaskQueue(store)
    for _ in range(2):
        queue.create(
            workspace_id="evaluation-workspace",
            agent_id="physical-peft-pilot",
            payload={"job_id": "pilot-job", "kind": "physical_peft_pilot"},
        )

    with pytest.raises(driver.PhysicalEvaluationDriverError, match="exactly one"):
        driver._find_pilot_task(
            store,
            workspace_id="evaluation-workspace",
            job_id="pilot-job",
        )


def test_candidate_descriptor_must_match_physical_pilot_digest() -> None:
    descriptor = driver._candidate_descriptor(
        _candidate_config(),
        report=_pilot_report(),
    )

    assert descriptor.descriptor_digest == _candidate_descriptor().descriptor_digest
    assert descriptor.registry_key == _candidate_descriptor().registry_key


def test_candidate_descriptor_metadata_substitution_is_rejected() -> None:
    substituted = driver.CandidateModelConfig(
        model_id="other-adapter",
        source_reference="https://example.com/models/base",
        license_reference="https://example.com/licenses/base",
    )

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="does not match physical pilot evidence",
    ):
        driver._candidate_descriptor(substituted, report=_pilot_report())


def test_model_size_preflight_does_not_read_model_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = (tmp_path / "large-model.gguf").resolve()
    model.write_bytes(b"x" * 8192)

    def fail_open(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("model preflight must not open/read the entire artifact")

    monkeypatch.setattr(Path, "open", fail_open)

    assert driver._model_size(model, name="model") == 8192


def test_report_writer_is_no_clobber(tmp_path: Path) -> None:
    path = tmp_path / "physical-old-new-evaluation-report.json"
    path.write_text("existing\n", encoding="utf-8")

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="already exists",
    ):
        driver._write_report(path, {"schema": "new"})

    assert path.read_text(encoding="utf-8") == "existing\n"


def test_report_writer_cleans_temporary_after_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "physical-old-new-evaluation-report.json"
    monkeypatch.setattr(driver.os, "name", "posix")

    def fail_link(source: Path, destination: Path) -> None:
        assert source.parent == tmp_path
        assert destination == path
        raise OSError("synthetic publish failure")

    monkeypatch.setattr(driver.os, "link", fail_link)

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="could not be persisted",
    ):
        driver._write_report(path, {"schema": "test"})

    assert not path.exists()
    assert not tuple(tmp_path.glob(".physical-evaluation-report.*.tmp"))


def test_report_writer_preserves_foreign_destination_on_publish_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "physical-old-new-evaluation-report.json"

    def substitute_destination(source: Path, destination: Path) -> None:
        assert source.parent == tmp_path
        assert destination == path
        destination.write_text("foreign-evidence\n", encoding="utf-8")
        raise FileExistsError("synthetic destination race")

    monkeypatch.setattr(driver.os, "link", substitute_destination)

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="already exists",
    ):
        driver._write_report(path, {"schema": "test"})

    assert path.read_text(encoding="utf-8") == "foreign-evidence\n"
    assert not tuple(tmp_path.glob(".physical-evaluation-report.*.tmp"))


def test_report_writer_rolls_back_only_owned_file_after_final_byte_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "physical-old-new-evaluation-report.json"
    real_reader = driver._read_regular_file

    def drifted_reader(
        target: Path,
        *,
        name: str,
        max_bytes: int,
    ) -> bytes:
        if name == "published physical evaluation report":
            return b'{"schema":"other"}\n'
        return real_reader(target, name=name, max_bytes=max_bytes)

    monkeypatch.setattr(driver, "_read_regular_file", drifted_reader)

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="bytes changed",
    ):
        driver._write_report(path, {"schema": "test"})

    assert not path.exists()
    assert not tuple(tmp_path.glob(".physical-evaluation-report.*.tmp"))


def test_report_writer_rejects_linked_parent_before_publication(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable on this test host")
    path = linked / "physical-old-new-evaluation-report.json"

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="parent must be a canonical non-linked directory",
    ):
        driver._write_report(path, {"schema": "test"})

    assert not path.exists()


def test_report_writer_strict_parse_back_rejects_duplicate_key() -> None:
    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="invalid JSON",
    ):
        driver._strict_report_parse_back(b'{"schema":"one","schema":"two"}\n')


def test_evaluation_set_allows_multiline_payload_text() -> None:
    payload = _evaluation_payload()
    cases = payload["cases"]
    assert isinstance(cases, list)
    case = cases[0]
    assert isinstance(case, dict)
    messages = case["messages"]
    assert isinstance(messages, list)
    message = messages[0]
    assert isinstance(message, dict)
    message["content"] = "line one\nline two"
    case["expected_text"] = "answer line one\nanswer line two"

    evaluation = driver._evaluation_set_from_json(
        json.dumps(payload, ensure_ascii=False)
    )

    assert evaluation.cases[0].messages[0].content == "line one\nline two"
    assert evaluation.cases[0].expected_text == "answer line one\nanswer line two"


def test_numeric_overflow_is_reported_as_driver_error(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    benchmark = payload["benchmark"]
    assert isinstance(benchmark, dict)
    benchmark["timeout_seconds"] = 10**10000

    with pytest.raises(
        driver.PhysicalEvaluationDriverError,
        match="must be a finite number",
    ):
        driver.PhysicalEvaluationConfig.from_json(json.dumps(payload))
