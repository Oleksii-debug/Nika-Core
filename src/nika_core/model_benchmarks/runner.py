from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from math import fsum, isfinite

from nika_core.model_benchmarks.contracts import (
    BenchmarkCase,
    BenchmarkCaseEvidence,
    BenchmarkDataset,
    BenchmarkRunEvidence,
    BenchmarkScorer,
    ModelBenchmarkCandidate,
    ModelCompletionPort,
    MonotonicClockPort,
    ResourceEvidence,
    validate_resource_snapshot,
)
from nika_core.model_gateway.contracts import ModelRequest, ModelResponse
from nika_core.resources.contracts import ResourceObserverPort


@dataclass(frozen=True, slots=True)
class _SystemMonotonicClock:
    def now(self) -> float:
        return time.perf_counter()


class ModelBenchmarkRunner:
    """F6 evidence collector; it has no promotion, download, or routing authority."""

    def __init__(
        self,
        *,
        completion: ModelCompletionPort,
        resources: ResourceObserverPort,
        scorer: BenchmarkScorer,
        clock: MonotonicClockPort | None = None,
    ) -> None:
        self._completion = completion
        self._resources = resources
        self._scorer = scorer
        self._clock = clock or _SystemMonotonicClock()

    async def run(
        self,
        *,
        run_id: str,
        dataset: BenchmarkDataset,
        candidate: ModelBenchmarkCandidate,
    ) -> BenchmarkRunEvidence:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must not be empty")
        if run_id != run_id.strip():
            raise ValueError("run_id must not contain surrounding whitespace")

        dataset_digest = _dataset_digest(dataset)
        evidence: list[BenchmarkCaseEvidence] = []
        for case in dataset.cases:
            evidence.append(
                await self._run_case(
                    run_id=run_id,
                    dataset=dataset,
                    dataset_digest=dataset_digest,
                    case=case,
                    candidate=candidate,
                )
            )

        count = len(evidence)
        return BenchmarkRunEvidence(
            run_id=run_id,
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.version,
            dataset_digest=dataset_digest,
            candidate_id=candidate.candidate_id,
            provider_id=candidate.provider_id,
            provider_kind=candidate.provider_kind,
            requested_model=candidate.model,
            cases=tuple(evidence),
            mean_quality_score=fsum(item.quality_score for item in evidence) / count,
            mean_latency_ms=fsum(item.latency_ms for item in evidence) / count,
        )

    async def _run_case(
        self,
        *,
        run_id: str,
        dataset: BenchmarkDataset,
        dataset_digest: str,
        case: BenchmarkCase,
        candidate: ModelBenchmarkCandidate,
    ) -> BenchmarkCaseEvidence:
        request_id = f"benchmark:{run_id}:{candidate.candidate_id}:{case.case_id}"
        request = ModelRequest(
            request_id=request_id,
            messages=case.messages,
            model=candidate.model,
            provider_id=candidate.provider_id,
            fallback_provider_ids=(),
            privacy=candidate.privacy,
            timeout_seconds=float(candidate.timeout_seconds),
            metadata={
                "purpose": "model_benchmark",
                "benchmark_run_id": run_id,
                "benchmark_dataset_id": dataset.dataset_id,
                "benchmark_dataset_version": dataset.version,
                "benchmark_case_id": case.case_id,
                "benchmark_candidate_id": candidate.candidate_id,
            },
        )

        before = validate_resource_snapshot(self._resources.snapshot())
        started = self._clock_value("benchmark start")
        response = await self._completion.complete(request)
        finished = self._clock_value("benchmark finish")
        after = validate_resource_snapshot(self._resources.snapshot())

        if finished < started:
            raise ValueError("monotonic benchmark clock moved backwards")
        self._validate_response(request, response, candidate)

        score = self._scorer.score(case, response.text)
        quality_score = _quality_score(score)
        latency_ms = (finished - started) * 1000.0
        if not isfinite(latency_ms):
            raise ValueError("benchmark latency must be finite")

        return BenchmarkCaseEvidence(
            run_id=run_id,
            dataset_id=dataset.dataset_id,
            dataset_version=dataset.version,
            dataset_digest=dataset_digest,
            case_id=case.case_id,
            case_digest=_case_digest(case),
            candidate_id=candidate.candidate_id,
            requested_provider_id=candidate.provider_id,
            provider_kind=candidate.provider_kind,
            requested_model=candidate.model,
            response_model=response.model,
            license_reference=candidate.license_reference,
            artifact_digest=candidate.artifact_digest,
            response_digest=_text_digest(response.text),
            quality_score=quality_score,
            latency_ms=latency_ms,
            resources=ResourceEvidence(before=before, after=after),
        )

    def _clock_value(self, label: str) -> float:
        value = self._clock.now()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{label} clock value must be numeric")
        number = float(value)
        if not isfinite(number):
            raise ValueError(f"{label} clock value must be finite")
        return number

    @staticmethod
    def _validate_response(
        request: ModelRequest,
        response: ModelResponse,
        candidate: ModelBenchmarkCandidate,
    ) -> None:
        if response.request_id != request.request_id:
            raise ValueError("benchmark response request_id mismatch")
        if response.provider_id != candidate.provider_id:
            raise ValueError("benchmark response provider_id mismatch")
        if response.provider_kind is not candidate.provider_kind:
            raise ValueError("benchmark response provider_kind mismatch")
        if not isinstance(response.model, str) or not response.model.strip():
            raise ValueError("benchmark response model must not be empty")
        if response.model != response.model.strip():
            raise ValueError("benchmark response model must not contain surrounding whitespace")
        if candidate.expected_response_model is not None:
            if response.model != candidate.expected_response_model:
                raise ValueError("benchmark response model identity mismatch")
        if not isinstance(response.text, str) or not response.text.strip():
            raise ValueError("benchmark response text must not be empty")


def _quality_score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("benchmark scorer must return a numeric score")
    score = float(value)
    if not isfinite(score) or not 0 <= score <= 1:
        raise ValueError("benchmark scorer score must be finite and in the range [0, 1]")
    return score


def _dataset_digest(dataset: BenchmarkDataset) -> str:
    payload = {
        "dataset_id": dataset.dataset_id,
        "version": dataset.version,
        "cases": [_case_payload(case) for case in dataset.cases],
    }
    return _json_digest(payload)


def _case_digest(case: BenchmarkCase) -> str:
    return _json_digest(_case_payload(case))


def _case_payload(case: BenchmarkCase) -> dict[str, object]:
    return {
        "case_id": case.case_id,
        "messages": [
            {"role": message.role, "content": message.content} for message in case.messages
        ],
    }


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
