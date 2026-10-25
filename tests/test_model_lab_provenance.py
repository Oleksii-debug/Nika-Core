from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from nika_core.experiments.repository import InMemoryExperimentRepository
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
    ModelBenchmarkPolicy,
    ModelCandidate,
    ModelEngineeringLab,
)


class FakeGateway:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.fail_after = fail_after
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("simulated crash")
        return ModelResponse(
            request_id=request.request_id,
            text="answer",
            provider_id=request.provider_id,
            provider_kind=ProviderKind.LOCAL,
            model=request.model,
        )


class VersionedScorer:
    def __init__(self, scorer_version: str = "1") -> None:
        self.scorer_id = "test_scorer"
        self.scorer_version = scorer_version

    def score(self, case, response) -> float:
        return 1.0 if response.text == case.expected_text else 0.0


class UnversionedScorer:
    def score(self, case, response) -> float:
        return 1.0


class IncrementingClock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        self.value += 1_000_000
        return self.value


def _candidate(candidate_id: str, model: str) -> ModelCandidate:
    return ModelCandidate(
        candidate_id=candidate_id,
        version="1",
        provider_id="local",
        model=model,
        permission_fingerprint="inference-only-v1",
    )


def _suite(*, privacy: PrivacyClass = PrivacyClass.PRIVATE) -> EvaluationSuite:
    return EvaluationSuite(
        dataset_ref="eval://provenance",
        dataset_version="2026.08",
        split=EvaluationSplit.HELD_OUT,
        cases=(
            EvaluationCase(
                case_id="one",
                messages=(ModelMessage(role="user", content="first"),),
                expected_text="answer",
            ),
            EvaluationCase(
                case_id="two",
                messages=(ModelMessage(role="user", content="second"),),
                expected_text="answer",
            ),
        ),
        privacy=privacy,
    )


def _seed_partial(
    repository: InMemoryExperimentRepository,
    *,
    experiment_id: str,
    suite: EvaluationSuite,
    policy: ModelBenchmarkPolicy,
    scorer: VersionedScorer,
) -> None:
    lab = ModelEngineeringLab(
        gateway=FakeGateway(fail_after=1),
        repository=repository,
        clock_ns=IncrementingClock(),
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(
            lab.compare(
                experiment_id=experiment_id,
                champion=_candidate("champion", "old"),
                challengers=(_candidate("challenger", "new"),),
                suite=suite,
                policy=policy,
                scorer=scorer,
            )
        )


def _assert_resume_rejected(
    repository: InMemoryExperimentRepository,
    *,
    experiment_id: str,
    suite: EvaluationSuite,
    policy: ModelBenchmarkPolicy,
    scorer: VersionedScorer,
) -> None:
    gateway = FakeGateway()
    lab = ModelEngineeringLab(
        gateway=gateway,
        repository=repository,
        clock_ns=IncrementingClock(),
    )
    with pytest.raises(ValueError, match="definition does not match"):
        asyncio.run(
            lab.compare(
                experiment_id=experiment_id,
                champion=_candidate("champion", "old"),
                challengers=(_candidate("challenger", "new"),),
                suite=suite,
                policy=policy,
                scorer=scorer,
            )
        )
    assert gateway.calls == 0


def test_resume_binds_privacy_class() -> None:
    repository = InMemoryExperimentRepository()
    original = _suite(privacy=PrivacyClass.PRIVATE)
    policy = ModelBenchmarkPolicy(request_timeout_seconds=30)
    scorer = VersionedScorer()
    _seed_partial(
        repository,
        experiment_id="privacy",
        suite=original,
        policy=policy,
        scorer=scorer,
    )

    _assert_resume_rejected(
        repository,
        experiment_id="privacy",
        suite=replace(original, privacy=PrivacyClass.PUBLIC),
        policy=policy,
        scorer=scorer,
    )


def test_resume_binds_request_timeout() -> None:
    repository = InMemoryExperimentRepository()
    suite = _suite()
    scorer = VersionedScorer()
    _seed_partial(
        repository,
        experiment_id="timeout",
        suite=suite,
        policy=ModelBenchmarkPolicy(request_timeout_seconds=30),
        scorer=scorer,
    )

    _assert_resume_rejected(
        repository,
        experiment_id="timeout",
        suite=suite,
        policy=ModelBenchmarkPolicy(request_timeout_seconds=31),
        scorer=scorer,
    )


def test_resume_binds_quality_scorer_version() -> None:
    repository = InMemoryExperimentRepository()
    suite = _suite()
    policy = ModelBenchmarkPolicy()
    _seed_partial(
        repository,
        experiment_id="scorer",
        suite=suite,
        policy=policy,
        scorer=VersionedScorer("1"),
    )

    _assert_resume_rejected(
        repository,
        experiment_id="scorer",
        suite=suite,
        policy=policy,
        scorer=VersionedScorer("2"),
    )


def test_unversioned_quality_scorer_is_rejected_before_inference() -> None:
    repository = InMemoryExperimentRepository()
    gateway = FakeGateway()
    lab = ModelEngineeringLab(
        gateway=gateway,
        repository=repository,
        clock_ns=IncrementingClock(),
    )

    with pytest.raises(ValueError, match="scorer_id and scorer_version"):
        asyncio.run(
            lab.compare(
                experiment_id="unversioned",
                champion=_candidate("champion", "old"),
                challengers=(_candidate("challenger", "new"),),
                suite=_suite(),
                policy=ModelBenchmarkPolicy(),
                scorer=UnversionedScorer(),
            )
        )
    assert gateway.calls == 0
    with pytest.raises(KeyError, match="unknown experiment"):
        repository.get("unversioned")
