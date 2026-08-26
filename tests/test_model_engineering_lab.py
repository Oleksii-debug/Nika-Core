from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from nika_core.experiments.contracts import (
    ExperimentStatus,
    MetricObservation,
    MetricRule,
    PromotionPolicy,
)
from nika_core.experiments.engine import ExperimentEngine
from nika_core.experiments.repository import InMemoryExperimentRepository
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderCapabilities,
    ProviderKind,
)
from nika_core.model_gateway.gateway import ModelGateway
from nika_core.model_lab import (
    BenchmarkCandidate,
    BenchmarkCase,
    BenchmarkDefinitionMismatchError,
    BenchmarkEvidenceIntegrityError,
    BenchmarkPlan,
    BenchmarkResponseIdentityError,
    ExactTextMatchEvaluator,
    ModelEngineeringLab,
)


class StepClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.001
        return self.value


class ScriptedProvider:
    def __init__(
        self,
        *,
        provider_id: str,
        model: str,
        outputs: dict[str, str],
        supports_private_data: bool = True,
        response_provider_id: str | None = None,
        response_model: str | None = None,
        fail_once_on: str | None = None,
        include_usage: bool = True,
    ) -> None:
        self._capabilities = ProviderCapabilities(
            provider_id=provider_id,
            kind=ProviderKind.LOCAL,
            supports_private_data=supports_private_data,
        )
        self.model = model
        self.outputs = outputs
        self.response_provider_id = response_provider_id
        self.response_model = response_model
        self.fail_once_on = fail_once_on
        self.include_usage = include_usage
        self.failed_once = False
        self.calls: list[str] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        prompt = request.messages[-1].content
        self.calls.append(prompt)
        if self.fail_once_on == prompt and not self.failed_once:
            self.failed_once = True
            raise ModelGatewayError(
                ModelErrorCode.PROVIDER_ERROR,
                "injected benchmark provider failure",
                provider_id=self.capabilities.provider_id,
            )
        usage = ModelUsage()
        if self.include_usage:
            usage = ModelUsage(input_tokens=3, output_tokens=1, total_tokens=4)
        return ModelResponse(
            request_id=request.request_id,
            text=self.outputs[prompt],
            provider_id=self.response_provider_id or self.capabilities.provider_id,
            provider_kind=ProviderKind.LOCAL,
            model=self.response_model or self.model,
            usage=usage,
            latency_ms=4.0,
        )


def _candidate(candidate_id: str, provider_id: str, model: str) -> BenchmarkCandidate:
    return BenchmarkCandidate(
        candidate_id=candidate_id,
        provider_id=provider_id,
        model=model,
        expected_model_id=model,
        version="1",
        artifact_ref=f"model-config:{provider_id}:{model}",
        permission_fingerprint="perm:v1",
    )


def _case(
    case_id: str,
    prompt: str,
    expected: str,
    *,
    privacy: PrivacyClass = PrivacyClass.PRIVATE,
) -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id,
        dataset_ref="eval-suite:core-text",
        dataset_version="2026-08-26-v1",
        messages=(ModelMessage(role="user", content=prompt),),
        reference=(("expected_text", expected),),
        privacy=privacy,
        timeout_seconds=5.0,
    )


def _plan(
    benchmark_id: str,
    champion: BenchmarkCandidate,
    challenger: BenchmarkCandidate,
    cases: tuple[BenchmarkCase, ...],
    *,
    with_latency_guardrail: bool = False,
    primary_metric: str = "exact_match",
) -> BenchmarkPlan:
    guardrails: tuple[MetricRule, ...] = ()
    if with_latency_guardrail:
        guardrails = (
            MetricRule(
                metric="gateway_latency_ms",
                higher_is_better=False,
                max_regression=0.5,
            ),
        )
    return BenchmarkPlan(
        benchmark_id=benchmark_id,
        champion=champion,
        challengers=(challenger,),
        cases=cases,
        policy=PromotionPolicy(
            primary_metric=primary_metric,
            minimum_improvement=0.1 if primary_metric == "exact_match" else 0.0,
            minimum_replays=len(cases),
            guardrails=guardrails,
        ),
        temperature=0.0,
        metadata=(("purpose", "regression-benchmark"),),
    )


def _lab(
    champion_provider: ScriptedProvider,
    challenger_provider: ScriptedProvider,
    repository: InMemoryExperimentRepository | None = None,
    *,
    evaluator: ExactTextMatchEvaluator | None = None,
) -> tuple[ModelEngineeringLab, InMemoryExperimentRepository, ExperimentEngine]:
    repo = repository or InMemoryExperimentRepository()
    engine = ExperimentEngine(repo)
    gateway = ModelGateway()
    gateway.register(champion_provider)
    gateway.register(challenger_provider)
    lab = ModelEngineeringLab(
        gateway=gateway,
        experiment_engine=engine,
        experiment_repository=repo,
        evaluator=evaluator or ExactTextMatchEvaluator(),
        monotonic=StepClock(),
    )
    return lab, repo, engine


def test_model_lab_promotes_better_candidate_and_rolls_back() -> None:
    cases = (
        _case("math", "secret-prompt-alpha", "4"),
        _case("capital", "capital-of-france", "Paris"),
    )
    champion = _candidate("champion", "local-a", "model-a:1")
    challenger = _candidate("challenger", "local-b", "model-b:2")
    champion_provider = ScriptedProvider(
        provider_id="local-a",
        model="model-a:1",
        outputs={"secret-prompt-alpha": "5", "capital-of-france": "Paris"},
    )
    challenger_provider = ScriptedProvider(
        provider_id="local-b",
        model="model-b:2",
        outputs={"secret-prompt-alpha": "4", "capital-of-france": "Paris"},
    )
    lab, _, _ = _lab(champion_provider, challenger_provider)
    plan = _plan(
        "quality-latency-v1",
        champion,
        challenger,
        cases,
        with_latency_guardrail=True,
    )

    result = asyncio.run(lab.run(plan))

    assert result.snapshot.status is ExperimentStatus.PROMOTED
    assert result.snapshot.selected_candidate_id == "challenger"
    assert result.executed_cases == 4
    assert result.reused_cases == 0
    assert len(result.snapshot.observations) == 8
    definition_text = repr(result.snapshot.definition)
    assert "secret-prompt-alpha" not in definition_text
    assert "capital-of-france" not in definition_text
    assert "model-lab-sha256=" in definition_text

    rolled_back = lab.rollback("quality-latency-v1")
    assert rolled_back.status is ExperimentStatus.ROLLED_BACK
    assert rolled_back.selected_candidate_id == "champion"


def test_model_lab_resume_reuses_complete_case_evidence() -> None:
    cases = (
        _case("one", "prompt-one", "A"),
        _case("two", "prompt-two", "B"),
    )
    champion = _candidate("champion", "local-a", "model-a:1")
    challenger = _candidate("challenger", "local-b", "model-b:2")
    champion_provider = ScriptedProvider(
        provider_id="local-a",
        model="model-a:1",
        outputs={"prompt-one": "wrong", "prompt-two": "B"},
    )
    challenger_provider = ScriptedProvider(
        provider_id="local-b",
        model="model-b:2",
        outputs={"prompt-one": "A", "prompt-two": "B"},
        fail_once_on="prompt-two",
    )
    lab, _, _ = _lab(champion_provider, challenger_provider)
    plan = _plan("resume-v1", champion, challenger, cases)

    with pytest.raises(ModelGatewayError):
        asyncio.run(lab.run(plan))

    assert champion_provider.calls == ["prompt-one", "prompt-two"]
    assert challenger_provider.calls == ["prompt-one", "prompt-two"]

    result = asyncio.run(lab.run(plan))

    assert result.snapshot.status is ExperimentStatus.PROMOTED
    assert result.executed_cases == 1
    assert result.reused_cases == 3
    assert champion_provider.calls == ["prompt-one", "prompt-two"]
    assert challenger_provider.calls == ["prompt-one", "prompt-two", "prompt-two"]


def test_model_lab_rejects_torn_multi_metric_case_evidence() -> None:
    case = _case("one", "prompt-one", "A")
    champion = _candidate("champion", "local-a", "model-a:1")
    challenger = _candidate("challenger", "local-b", "model-b:2")
    champion_provider = ScriptedProvider(
        provider_id="local-a",
        model="model-a:1",
        outputs={"prompt-one": "A"},
    )
    challenger_provider = ScriptedProvider(
        provider_id="local-b",
        model="model-b:2",
        outputs={"prompt-one": "A"},
    )
    lab, _, engine = _lab(champion_provider, challenger_provider)
    plan = _plan(
        "torn-v1",
        champion,
        challenger,
        (case,),
        with_latency_guardrail=True,
    )
    definition = lab.compile_definition(plan)
    engine.create(definition)
    engine.start(definition.experiment_id)
    engine.record(
        definition.experiment_id,
        MetricObservation(
            candidate_id="champion",
            replay_id="one",
            metric="exact_match",
            value=1.0,
        ),
    )

    with pytest.raises(BenchmarkEvidenceIntegrityError, match="partial candidate/case evidence"):
        asyncio.run(lab.run(plan))

    assert champion_provider.calls == []
    assert challenger_provider.calls == []


def test_model_lab_definition_binds_request_evaluator_and_reference_content() -> None:
    case = _case("one", "private-prompt", "private-reference")
    champion = _candidate("champion", "local-a", "model-a:1")
    challenger = _candidate("challenger", "local-b", "model-b:2")
    champion_provider = ScriptedProvider(
        provider_id="local-a",
        model="model-a:1",
        outputs={"private-prompt": "private-reference"},
    )
    challenger_provider = ScriptedProvider(
        provider_id="local-b",
        model="model-b:2",
        outputs={"private-prompt": "private-reference"},
    )
    lab, repo, engine = _lab(champion_provider, challenger_provider)
    plan = _plan("identity-v1", champion, challenger, (case,))
    definition = lab.compile_definition(plan)
    engine.create(definition)

    assert "private-prompt" not in repr(definition)
    assert "private-reference" not in repr(definition)

    changed_temperature = replace(plan, temperature=0.2)
    with pytest.raises(BenchmarkDefinitionMismatchError):
        asyncio.run(lab.run(changed_temperature))

    class EvaluatorV2(ExactTextMatchEvaluator):
        version = "2"

    lab_v2, _, _ = _lab(
        champion_provider,
        challenger_provider,
        repository=repo,
        evaluator=EvaluatorV2(),
    )
    with pytest.raises(BenchmarkDefinitionMismatchError):
        asyncio.run(lab_v2.run(plan))


def test_model_lab_rejects_provider_or_model_identity_substitution() -> None:
    case = _case("one", "prompt-one", "A")
    champion = _candidate("champion", "local-a", "model-a:1")
    challenger = _candidate("challenger", "local-b", "model-b:2")
    champion_provider = ScriptedProvider(
        provider_id="local-a",
        model="model-a:1",
        outputs={"prompt-one": "A"},
        response_model="different-model:9",
    )
    challenger_provider = ScriptedProvider(
        provider_id="local-b",
        model="model-b:2",
        outputs={"prompt-one": "A"},
    )
    lab, repo, _ = _lab(champion_provider, challenger_provider)
    plan = _plan("identity-substitution-v1", champion, challenger, (case,))

    with pytest.raises(BenchmarkResponseIdentityError):
        asyncio.run(lab.run(plan))

    snapshot = repo.get("model-lab:identity-substitution-v1")
    assert snapshot.status is ExperimentStatus.RUNNING
    assert snapshot.observations == ()


def test_model_lab_preserves_model_gateway_sensitive_privacy_policy() -> None:
    case = _case(
        "one",
        "sensitive-prompt",
        "A",
        privacy=PrivacyClass.SENSITIVE,
    )
    champion = _candidate("champion", "local-a", "model-a:1")
    challenger = _candidate("challenger", "local-b", "model-b:2")
    champion_provider = ScriptedProvider(
        provider_id="local-a",
        model="model-a:1",
        outputs={"sensitive-prompt": "A"},
        supports_private_data=False,
    )
    challenger_provider = ScriptedProvider(
        provider_id="local-b",
        model="model-b:2",
        outputs={"sensitive-prompt": "A"},
    )
    lab, _, _ = _lab(champion_provider, challenger_provider)
    plan = _plan("privacy-v1", champion, challenger, (case,))

    with pytest.raises(ModelGatewayError) as exc_info:
        asyncio.run(lab.run(plan))

    assert exc_info.value.code is ModelErrorCode.INVALID_REQUEST
    assert champion_provider.calls == []


def test_model_lab_fails_closed_when_required_metric_is_unavailable() -> None:
    case = _case("one", "prompt-one", "A")
    champion = _candidate("champion", "local-a", "model-a:1")
    challenger = _candidate("challenger", "local-b", "model-b:2")
    champion_provider = ScriptedProvider(
        provider_id="local-a",
        model="model-a:1",
        outputs={"prompt-one": "A"},
        include_usage=False,
    )
    challenger_provider = ScriptedProvider(
        provider_id="local-b",
        model="model-b:2",
        outputs={"prompt-one": "A"},
        include_usage=False,
    )
    lab, repo, _ = _lab(champion_provider, challenger_provider)
    plan = _plan(
        "missing-metric-v1",
        champion,
        challenger,
        (case,),
        primary_metric="total_tokens",
    )

    with pytest.raises(BenchmarkEvidenceIntegrityError, match="required metric"):
        asyncio.run(lab.run(plan))

    snapshot = repo.get("model-lab:missing-metric-v1")
    assert snapshot.observations == ()
