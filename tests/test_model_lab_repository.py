from __future__ import annotations

import asyncio

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderKind,
)
from nika_core.model_lab import (
    BenchmarkCase,
    BenchmarkSuite,
    ExactMatchScorer,
    ModelBenchmarkRunner,
    ModelCandidate,
)
from nika_core.model_lab.repository import SQLiteModelLabRepository


CHECKSUM = "b" * 64


class FakeGateway:
    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            request_id=request.request_id,
            text=self.text,
            provider_id="ollama",
            provider_kind=ProviderKind.LOCAL,
            model="qwen3:8b",
            usage=ModelUsage(total_tokens=4),
            latency_ms=5.0,
        )


def candidate(*, version: str = "1") -> ModelCandidate:
    return ModelCandidate(
        candidate_id="qwen-local",
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
        model="qwen3:8b",
        model_version=version,
        license_reference="license://qwen3",
        provenance_reference="provenance://ollama/qwen3",
        permission_fingerprint="perm-v1",
        artifact_sha256=CHECKSUM,
    )


def evidence(*, run_id: str, response_text: str = "4"):
    benchmark_suite = BenchmarkSuite(
        suite_id="repo-suite",
        version="1",
        cases=(
            BenchmarkCase(
                case_id="case-1",
                messages=(ModelMessage(role="user", content="2+2?"),),
                dataset_ref="dataset://repo",
                dataset_version="1",
                scorer_id="exact_match",
                reference_text="4",
            ),
        ),
    )
    return asyncio.run(
        ModelBenchmarkRunner(FakeGateway(response_text)).run(
            run_id=run_id,
            candidate=candidate(),
            suite=benchmark_suite,
            scorers={"exact_match": ExactMatchScorer()},
        )
    )


def repository(tmp_path) -> SQLiteModelLabRepository:
    path = tmp_path / "дані з пробілами" / "model lab.sqlite"
    repo = SQLiteModelLabRepository(SQLiteStore(path))
    repo.initialize()
    return repo


def test_candidate_registry_is_idempotent_and_immutable(tmp_path) -> None:
    repo = repository(tmp_path)
    original = candidate()
    repo.register_candidate(original)
    repo.register_candidate(original)

    assert repo.get_candidate("qwen-local") == original
    assert repo.list_candidates() == (original,)

    with pytest.raises(ValueError, match="immutable"):
        repo.register_candidate(candidate(version="2"))


def test_benchmark_run_round_trips_and_is_idempotent(tmp_path) -> None:
    repo = repository(tmp_path)
    repo.register_candidate(candidate())
    run = evidence(run_id="run-1")

    repo.record_run(run)
    repo.record_run(run)

    assert repo.get_run("run-1") == run


def test_run_id_cannot_be_reused_for_different_evidence(tmp_path) -> None:
    repo = repository(tmp_path)
    repo.register_candidate(candidate())
    first = evidence(run_id="run-1", response_text="4")
    second = evidence(run_id="run-1", response_text="5")
    repo.record_run(first)

    with pytest.raises(ValueError, match="immutable"):
        repo.record_run(second)


def test_run_rejects_unregistered_or_changed_candidate_identity(tmp_path) -> None:
    repo = repository(tmp_path)
    run = evidence(run_id="run-1")
    with pytest.raises(KeyError, match="unknown model candidate"):
        repo.record_run(run)

    repo.register_candidate(candidate())
    changed = ModelCandidate(
        candidate_id="qwen-local",
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
        model="qwen3:8b",
        model_version="changed",
        license_reference="license://qwen3",
        provenance_reference="provenance://ollama/qwen3",
        permission_fingerprint="perm-v1",
        artifact_sha256=CHECKSUM,
    )
    changed_run = type(run)(
        run_id=run.run_id,
        candidate=changed,
        suite_id=run.suite_id,
        suite_version=run.suite_version,
        suite_sha256=run.suite_sha256,
        expected_attempts=run.expected_attempts,
        attempts=run.attempts,
    )
    with pytest.raises(ValueError, match="does not match"):
        repo.record_run(changed_run)


def test_get_run_detects_evidence_tampering(tmp_path) -> None:
    path = tmp_path / "model-lab.sqlite"
    store = SQLiteStore(path)
    repo = SQLiteModelLabRepository(store)
    repo.initialize()
    repo.register_candidate(candidate())
    repo.record_run(evidence(run_id="run-1"))

    with store.connection() as conn:
        conn.execute(
            "UPDATE model_lab_runs SET evidence_json = evidence_json || ' ' "
            "WHERE run_id = ?",
            ("run-1",),
        )

    with pytest.raises(RuntimeError, match="digest"):
        repo.get_run("run-1")
