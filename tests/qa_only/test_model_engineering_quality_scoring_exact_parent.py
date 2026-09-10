from __future__ import annotations

import asyncio
import json
from math import inf, nan

import pytest

from nika_core.model_engineering import (
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ExactMatchScorer,
    ModelBenchmarkError,
    ModelBenchmarkRunner,
    ModelCandidate,
    benchmark_report_json,
)
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelResponse,
    PrivacyClass,
    ProviderKind,
)


class _Gateway:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, request):
        return ModelResponse(
            request_id=request.request_id,
            text=self._text,
            provider_id="local-test",
            provider_kind=ProviderKind.LOCAL,
            model="test-model",
        )


class _ConstantScorer:
    def __init__(self, value: float) -> None:
        self._value = value

    def score(self, case, response):
        del case, response
        return self._value


class _StructuredFieldScorer:
    """Test implementation proving deterministic structured scoring fits the port."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        self._fields = fields

    def score(self, case, response):
        expected = json.loads(case.expected_text)
        actual = json.loads(response.text)
        if not isinstance(expected, dict) or not isinstance(actual, dict):
            return 0.0
        matches = sum(expected.get(field) == actual.get(field) for field in self._fields)
        return matches / len(self._fields)


class _RuleScorer:
    """Test implementation proving deterministic rule scoring fits the port."""

    def score(self, case, response):
        del case
        rules = (
            response.text.startswith("PASS:"),
            "evidence=" in response.text,
            response.text.endswith("ok"),
        )
        return sum(rules) / len(rules)


class _SubjectiveScorer:
    """Synthetic stand-in for a human/LLM-style non-deterministic judge."""

    deterministic = False

    def score(self, case, response):
        del case, response
        return 0.75


def _candidate() -> ModelCandidate:
    return ModelCandidate(
        candidate_id="candidate",
        provider_id="local-test",
        provider_kind=ProviderKind.LOCAL,
        request_model="test-model",
        expected_response_model="test-model",
        engine_provenance_ref="engine:test",
        engine_license_ref="license:engine-test",
        model_provenance_ref="model:test",
        model_license_ref="license:model-test",
    )


def _evaluation(expected_text: str, *, pass_score: float = 1.0) -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="quality-scoring-oracle",
        version="1",
        provenance_ref="qa:one-shot-64",
        license_ref="license:internal-qa",
        purpose=EvaluationPurpose.DEVELOPMENT,
        privacy=PrivacyClass.PUBLIC,
        cases=(
            EvaluationCase(
                case_id="case",
                messages=(ModelMessage("user", "synthetic prompt"),),
                expected_text=expected_text,
                pass_score=pass_score,
            ),
        ),
    )


def _run(text: str, scorer, *, expected_text: str = "expected", pass_score: float = 1.0):
    runner = ModelBenchmarkRunner(_Gateway(text), scorer=scorer)
    return asyncio.run(
        runner.benchmark(
            _candidate(),
            _evaluation(expected_text, pass_score=pass_score),
        )
    )


def _has_nondeterministic_marker(value) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).casefold().replace("-", "_")
            if "determin" in normalized_key and item is False:
                return True
            if (
                any(token in normalized_key for token in ("score", "evaluat", "judge"))
                and isinstance(item, str)
                and item.casefold().replace("-", "_")
                in {"non_deterministic", "nondeterministic", "subjective"}
            ):
                return True
            if _has_nondeterministic_marker(item):
                return True
        return False
    if isinstance(value, list):
        return any(_has_nondeterministic_marker(item) for item in value)
    return False


def test_exact_match_is_nfc_normalized_trimmed_and_case_sensitive() -> None:
    scorer = ExactMatchScorer()
    case = _evaluation("é").cases[0]

    composed = ModelResponse(
        request_id="request",
        text="  e\u0301  ",
        provider_id="local-test",
        provider_kind=ProviderKind.LOCAL,
        model="test-model",
    )
    wrong_case = ModelResponse(
        request_id="request",
        text="É",
        provider_id="local-test",
        provider_kind=ProviderKind.LOCAL,
        model="test-model",
    )

    assert scorer.score(case, composed) == 1.0
    assert scorer.score(case, wrong_case) == 0.0


def test_structured_field_match_is_repeatable_and_ignores_unselected_fields() -> None:
    scorer = _StructuredFieldScorer(("answer", "status"))
    expected = '{"answer":42,"status":"ok","trace":"expected-only"}'
    response = '{"trace":"different","status":"ok","answer":42}'

    scores = {
        _run(response, scorer, expected_text=expected).case_results[0].score
        for _ in range(8)
    }

    assert scores == {1.0}


def test_rule_based_criteria_produce_deterministic_fractional_score() -> None:
    report = _run(
        "PASS: evidence=present but-not-ok",
        _RuleScorer(),
        pass_score=0.5,
    )

    result = report.case_results[0]
    assert result.score == pytest.approx(2 / 3)
    assert result.passed is True


def test_bounded_numeric_score_preserves_fraction_and_threshold_semantics() -> None:
    report = _run("irrelevant", _ConstantScorer(0.375), pass_score=0.4)

    result = report.case_results[0]
    assert result.score == pytest.approx(0.375)
    assert result.passed is False
    assert report.weighted_quality_score == pytest.approx(0.375)


@pytest.mark.parametrize("score", (nan, inf, -inf, -0.0001, 1.0001))
def test_non_finite_or_out_of_range_score_fails_closed(score: float) -> None:
    with pytest.raises(ModelBenchmarkError, match="non-finite or out-of-range"):
        _run("irrelevant", _ConstantScorer(score))


def test_subjective_scorer_cannot_masquerade_as_deterministic_evidence() -> None:
    try:
        report = _run("irrelevant", _SubjectiveScorer(), pass_score=0.5)
    except ModelBenchmarkError:
        return

    payload = json.loads(benchmark_report_json(report))
    assert _has_nondeterministic_marker(payload), (
        "a non-deterministic scorer must either be rejected or explicitly marked as "
        "non-deterministic benchmark evidence"
    )
