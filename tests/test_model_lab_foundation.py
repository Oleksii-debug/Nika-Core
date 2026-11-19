from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments.contracts import ExperimentStatus
from nika_core.experiments.repository import InMemoryExperimentRepository, SQLiteExperimentRepository
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelResponse,
    PrivacyClass,
    ProviderKind,
)
from nika_core.model_lab import (
    EvaluationCase,
    EvaluationSplit,
    EvaluationSuite,
    ExactMatchScorer,
    ModelBenchmarkPolicy,
    ModelCandidate,
    ModelEngineeringLab,
)
from nika_core.resources.contracts import ResourceSnapshot


class FakeGateway:
    def __init__(
        self,
        outputs: dict[tuple[str, str, str], str],
        *,
        fail_after: int | None = None,
        wrong_identity: bool = False,
    ) -> None:
        self.outputs = outputs
        self.calls = 0
        self.fail_after = fail_after
        self.wrong_identity = wrong_identity
        self.requests = []

    async def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("simulated crash")
        text = self.outputs[(request.provider_id, request.model, request.messages[-1].content)]
        return ModelResponse(
            request_id="wrong" if self.wrong_identity else request.request_id,
            text=text,
            provider_id=request.provider_id,
            provider_kind=ProviderKind.LOCAL,
            model=request.model,
        )


class StepClock:
    def __init__(self, deltas_ms: list[float]) -> None:
        self._now = 0
        self._deltas = iter(deltas_ms)
        self._start = True

    def __call__(self) -> int:
        if self._start:
            self._start = False
            return self._now
        self._start = True
        self._now += int(next(self._deltas) * 1_000_000)
        return self._now


class SequenceObserver:
    def __init__(self, values: list[tuple[float, float]]) -> None:
        self._values = iter(values)

    def snapshot(self) -> ResourceSnapshot:
        cpu, memory = next(self._values)
        return ResourceSnapshot(
            cpu_percent=cpu,
            memory_percent=memory,
            available_memory_bytes=1_000_000,
        )


def _candidate(candidate_id: str, model: str, *, permission: str = "inference-only-v1"):
    return ModelCandidate(
        candidate_id=candidate_id,
        version="1",
        provider_id="local",
        model=model,
        permission_fingerprint=permission,
    )


def _suite() -> EvaluationSuite:
    return EvaluationSuite(
        dataset_ref="eval://core",
        dataset_version="2026.08",
        split=EvaluationSplit.HELD_OUT,
        cases=(
            EvaluationCase(
                case_id="math",
                messages=(ModelMessage(role="user", content="2+2"),),
                expected_text="4",
            ),
            EvaluationCase(
                case_id="capital",
                messages=(ModelMessage(role="user", content="capital of France"),),
                expected_text="Paris",
            ),
        ),
        privacy=PrivacyClass.PRIVATE,
    )


def _outputs() -> dict[tuple[str, str, str], str]:
    return {
        ("local", "old", "2+2"): "5",
        ("local", "old", "capital of France"): "Paris",
        ("local", "new", "2+2"): "4",
        ("local", "new", "capital of France"): "Paris",
    }


def test_suite_digest_binds_messages_expected_text_and_split() -> None:
    original = _suite()
    changed = replace(
        original,
        cases=(replace(original.cases[0], expected_text="four"), original.cases[1]),
    )
    replay = replace(original, split=EvaluationSplit.REPLAY)

    assert original.content_sha256 != changed.content_sha256
    assert original.evidence_version != replay.evidence_version
    assert "sha256=" in original.evidence_version


def test_promotes_better_challenger_and_persists_only_numeric_evidence() -> None:
    repository = InMemoryExperimentRepository()
    gateway = FakeGateway(_outputs())
    lab = ModelEngineeringLab(
        gateway=gateway,
        repository=repository,
        clock_ns=StepClock([10, 10, 9, 9]),
    )

    report = asyncio.run(
        lab.compare(
            experiment_id="exp-1",
            champion=_candidate("champion", "old"),
            challengers=(_candidate("challenger", "new"),),
            suite=_suite(),
            policy=ModelBenchmarkPolicy(
                minimum_quality_improvement=0.1,
                max_latency_regression_ms=0,
            ),
            scorer=ExactMatchScorer(),
        )
    )

    assert report.status is ExperimentStatus.PROMOTED
    assert report.selected_candidate_id == "challenger"
    assert report.promoted is True
    assert [summary.quality_mean for summary in report.summaries] == [0.5, 1.0]
    snapshot = repository.get("exp-1")
    assert all(isinstance(item.value, float) for item in snapshot.observations)
    assert all("Paris" not in repr(item) for item in snapshot.observations)
    assert all(request.temperature == 0.0 for request in gateway.requests)


def test_latency_guardrail_blocks_better_but_slower_challenger() -> None:
    repository = InMemoryExperimentRepository()
    lab = ModelEngineeringLab(
        gateway=FakeGateway(_outputs()),
        repository=repository,
        clock_ns=StepClock([10, 10, 30, 30]),
    )

    report = asyncio.run(
        lab.compare(
            experiment_id="exp-2",
            champion=_candidate("champion", "old"),
            challengers=(_candidate("challenger", "new"),),
            suite=_suite(),
            policy=ModelBenchmarkPolicy(
                minimum_quality_improvement=0.1,
                max_latency_regression_ms=5,
            ),
            scorer=ExactMatchScorer(),
        )
    )

    assert report.status is ExperimentStatus.COMPLETED
    assert report.selected_candidate_id == "champion"


def test_crash_resume_skips_already_committed_case_groups() -> None:
    repository = InMemoryExperimentRepository()
    first_gateway = FakeGateway(_outputs(), fail_after=2)
    first = ModelEngineeringLab(
        gateway=first_gateway,
        repository=repository,
        clock_ns=StepClock([1, 1, 1, 1]),
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(
            first.compare(
                experiment_id="resume",
                champion=_candidate("champion", "old"),
                challengers=(_candidate("challenger", "new"),),
                suite=_suite(),
                policy=ModelBenchmarkPolicy(),
                scorer=ExactMatchScorer(),
            )
        )

    assert len(repository.get("resume").observations) == 4
    second_gateway = FakeGateway(_outputs())
    second = ModelEngineeringLab(
        gateway=second_gateway,
        repository=repository,
        clock_ns=StepClock([1, 1]),
    )
    report = asyncio.run(
        second.compare(
            experiment_id="resume",
            champion=_candidate("champion", "old"),
            challengers=(_candidate("challenger", "new"),),
            suite=_suite(),
            policy=ModelBenchmarkPolicy(),
            scorer=ExactMatchScorer(),
        )
    )

    assert second_gateway.calls == 2
    assert report.status in {ExperimentStatus.PROMOTED, ExperimentStatus.COMPLETED}
    assert len(repository.get("resume").observations) == 8


def test_sqlite_restart_resumes_without_replaying_committed_cases(tmp_path) -> None:
    database = tmp_path / "Nika Lab Δ" / "nika core.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    first_repository = SQLiteExperimentRepository(store)
    first_gateway = FakeGateway(_outputs(), fail_after=2)
    first = ModelEngineeringLab(
        gateway=first_gateway,
        repository=first_repository,
        clock_ns=StepClock([2, 2, 2, 2]),
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(
            first.compare(
                experiment_id="durable-resume",
                champion=_candidate("champion", "old"),
                challengers=(_candidate("challenger", "new"),),
                suite=_suite(),
                policy=ModelBenchmarkPolicy(),
                scorer=ExactMatchScorer(),
            )
        )

    recreated_store = SQLiteStore(database)
    recreated_store.initialize()
    second_repository = SQLiteExperimentRepository(recreated_store)
    second_gateway = FakeGateway(_outputs())
    second = ModelEngineeringLab(
        gateway=second_gateway,
        repository=second_repository,
        clock_ns=StepClock([2, 2]),
    )
    report = asyncio.run(
        second.compare(
            experiment_id="durable-resume",
            champion=_candidate("champion", "old"),
            challengers=(_candidate("challenger", "new"),),
            suite=_suite(),
            policy=ModelBenchmarkPolicy(),
            scorer=ExactMatchScorer(),
        )
    )

    assert second_gateway.calls == 2
    assert report.selected_candidate_id == "challenger"
    assert len(second_repository.get("durable-resume").observations) == 8


def test_resume_rejects_changed_dataset_even_with_same_ref_and_version() -> None:
    repository = InMemoryExperimentRepository()
    first_gateway = FakeGateway(_outputs(), fail_after=1)
    first = ModelEngineeringLab(
        gateway=first_gateway,
        repository=repository,
        clock_ns=StepClock([1, 1]),
    )
    suite = _suite()

    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(
            first.compare(
                experiment_id="dataset-bind",
                champion=_candidate("champion", "old"),
                challengers=(_candidate("challenger", "new"),),
                suite=suite,
                policy=ModelBenchmarkPolicy(),
                scorer=ExactMatchScorer(),
            )
        )

    changed = replace(
        suite,
        cases=(replace(suite.cases[0], expected_text="four"), suite.cases[1]),
    )
    second_gateway = FakeGateway(_outputs())
    second = ModelEngineeringLab(
        gateway=second_gateway,
        repository=repository,
        clock_ns=StepClock([1, 1, 1]),
    )
    with pytest.raises(ValueError, match="definition does not match"):
        asyncio.run(
            second.compare(
                experiment_id="dataset-bind",
                champion=_candidate("champion", "old"),
                challengers=(_candidate("challenger", "new"),),
                suite=changed,
                policy=ModelBenchmarkPolicy(),
                scorer=ExactMatchScorer(),
            )
        )
    assert second_gateway.calls == 0


def test_permission_fingerprint_drift_is_rejected_before_inference() -> None:
    gateway = FakeGateway(_outputs())
    lab = ModelEngineeringLab(
        gateway=gateway,
        repository=InMemoryExperimentRepository(),
        clock_ns=StepClock([1]),
    )

    with pytest.raises(PermissionError, match="may not widen or alter permissions"):
        asyncio.run(
            lab.compare(
                experiment_id="permission-drift",
                champion=_candidate("champion", "old"),
                challengers=(
                    _candidate("challenger", "new", permission="wider-permission"),
                ),
                suite=_suite(),
                policy=ModelBenchmarkPolicy(),
                scorer=ExactMatchScorer(),
            )
        )
    assert gateway.calls == 0


def test_response_identity_mismatch_fails_closed_without_evidence() -> None:
    repository = InMemoryExperimentRepository()
    lab = ModelEngineeringLab(
        gateway=FakeGateway(_outputs(), wrong_identity=True),
        repository=repository,
        clock_ns=StepClock([1]),
    )

    with pytest.raises(RuntimeError, match="request identity"):
        asyncio.run(
            lab.compare(
                experiment_id="bad-id",
                champion=_candidate("champion", "old"),
                challengers=(_candidate("challenger", "new"),),
                suite=_suite(),
                policy=ModelBenchmarkPolicy(),
                scorer=ExactMatchScorer(),
            )
        )
    assert repository.get("bad-id").observations == ()


def test_resource_guardrails_require_observer_and_report_host_metrics() -> None:
    policy = ModelBenchmarkPolicy(
        max_host_cpu_regression_percent=5,
        max_host_memory_regression_percent=5,
    )
    gateway = FakeGateway(_outputs())
    without_observer = ModelEngineeringLab(
        gateway=gateway,
        repository=InMemoryExperimentRepository(),
        clock_ns=StepClock([1]),
    )

    with pytest.raises(ValueError, match="resource observer"):
        asyncio.run(
            without_observer.compare(
                experiment_id="no-observer",
                champion=_candidate("champion", "old"),
                challengers=(_candidate("challenger", "new"),),
                suite=_suite(),
                policy=policy,
                scorer=ExactMatchScorer(),
            )
        )
    assert gateway.calls == 0

    observer_values = [
        (10, 20),
        (11, 21),
        (10, 20),
        (12, 22),
        (9, 19),
        (10, 20),
        (9, 19),
        (10, 20),
    ]
    with_observer = ModelEngineeringLab(
        gateway=FakeGateway(_outputs()),
        repository=InMemoryExperimentRepository(),
        resource_observer=SequenceObserver(observer_values),
        clock_ns=StepClock([1, 1, 1, 1]),
    )
    report = asyncio.run(
        with_observer.compare(
            experiment_id="observer",
            champion=_candidate("champion", "old"),
            challengers=(_candidate("challenger", "new"),),
            suite=_suite(),
            policy=policy,
            scorer=ExactMatchScorer(),
        )
    )

    assert report.summaries[0].host_cpu_mean_percent is not None
    assert report.summaries[0].host_memory_mean_percent is not None
