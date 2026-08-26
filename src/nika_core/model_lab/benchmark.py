from __future__ import annotations

import asyncio
from hashlib import sha256
import json
from math import isfinite
from statistics import fmean
from time import perf_counter
from typing import Mapping, Protocol

from nika_core.model_gateway.contracts import (
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ProviderKind,
)
from nika_core.resources.contracts import ResourceObserverPort, ResourceSnapshot

from nika_core.model_lab.contracts import (
    AttemptStatus,
    BenchmarkAttempt,
    BenchmarkCase,
    BenchmarkRunEvidence,
    BenchmarkScorer,
    BenchmarkSuite,
    MetricValue,
    ModelCandidate,
)


class ModelCompletionPort(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class ExactMatchScorer:
    scorer_id = "exact_match"

    def score(self, case: BenchmarkCase, response_text: str) -> tuple[MetricValue, ...]:
        if case.reference_text is None:
            raise ValueError("exact_match scorer requires reference_text")
        value = 1.0 if response_text == case.reference_text else 0.0
        return (MetricValue(metric="quality.exact_match", value=value),)


def _hash_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _messages_hash(case: BenchmarkCase) -> str:
    payload = [
        {"role": message.role, "content": message.content}
        for message in case.messages
    ]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def suite_sha256(suite: BenchmarkSuite) -> str:
    cases = []
    for case in suite.cases:
        cases.append(
            {
                "case_id": case.case_id,
                "prompt_sha256": _messages_hash(case),
                "dataset_ref": case.dataset_ref,
                "dataset_version": case.dataset_version,
                "scorer_id": case.scorer_id,
                "privacy": case.privacy.value,
                "reference_sha256": (
                    _hash_text(case.reference_text)
                    if case.reference_text is not None
                    else None
                ),
                "tags": list(case.tags),
            }
        )
    payload = {
        "schema": 1,
        "suite_id": suite.suite_id,
        "version": suite.version,
        "repetitions": suite.repetitions,
        "timeout_seconds": suite.timeout_seconds,
        "temperature": suite.temperature,
        "cases": cases,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _safe_snapshot(observer: ResourceObserverPort | None) -> ResourceSnapshot | None:
    if observer is None:
        return None
    snapshot = observer.snapshot()
    for name, value in (
        ("cpu_percent", snapshot.cpu_percent),
        ("memory_percent", snapshot.memory_percent),
    ):
        if not isfinite(float(value)) or not 0 <= value <= 100:
            raise ValueError(f"resource snapshot {name} must be finite and in [0, 100]")
    if snapshot.available_memory_bytes < 0:
        raise ValueError("resource snapshot available_memory_bytes must be non-negative")
    return snapshot


def _attempt_metrics(
    *,
    scorer_metrics: tuple[MetricValue, ...],
    response: ModelResponse,
    wall_latency_ms: float,
    resource_before: ResourceSnapshot | None,
    resource_after: ResourceSnapshot | None,
) -> tuple[MetricValue, ...]:
    names = [metric.metric for metric in scorer_metrics]
    if len(names) != len(set(names)):
        raise ValueError("scorer returned duplicate metric names")
    if any(name.startswith("model_lab.") for name in names):
        raise ValueError("scorer metrics may not use the reserved model_lab. namespace")

    infrastructure = [
        MetricValue(metric="model_lab.wall_latency_ms", value=wall_latency_ms),
    ]
    if response.latency_ms is not None:
        if not isfinite(float(response.latency_ms)) or response.latency_ms < 0:
            raise ValueError("provider latency must be finite and non-negative")
        infrastructure.append(
            MetricValue(metric="model_lab.provider_latency_ms", value=response.latency_ms)
        )
    if response.usage.total_tokens is not None:
        if response.usage.total_tokens < 0:
            raise ValueError("total_tokens must be non-negative")
        infrastructure.append(
            MetricValue(metric="model_lab.total_tokens", value=float(response.usage.total_tokens))
        )
    if resource_before is not None and resource_after is not None:
        infrastructure.extend(
            (
                MetricValue(
                    metric="model_lab.cpu_percent",
                    value=max(resource_before.cpu_percent, resource_after.cpu_percent),
                ),
                MetricValue(
                    metric="model_lab.memory_percent",
                    value=max(resource_before.memory_percent, resource_after.memory_percent),
                ),
            )
        )
    return (*scorer_metrics, *infrastructure)


class ModelBenchmarkRunner:
    def __init__(
        self,
        gateway: ModelCompletionPort,
        *,
        resource_observer: ResourceObserverPort | None = None,
    ) -> None:
        self._gateway = gateway
        self._resource_observer = resource_observer

    async def run(
        self,
        *,
        run_id: str,
        candidate: ModelCandidate,
        suite: BenchmarkSuite,
        scorers: Mapping[str, BenchmarkScorer],
        allow_cloud: bool = False,
        continue_on_failure: bool = False,
    ) -> BenchmarkRunEvidence:
        if candidate.provider_kind is ProviderKind.CLOUD and not allow_cloud:
            raise PermissionError("cloud benchmark requires explicit allow_cloud=True")

        scorer_ids = set(scorers)
        required_scorers = {case.scorer_id for case in suite.cases}
        missing = required_scorers - scorer_ids
        if missing:
            raise ValueError(f"missing benchmark scorers: {sorted(missing)!r}")
        for scorer_id, scorer in scorers.items():
            if scorer.scorer_id != scorer_id:
                raise ValueError("scorer mapping key must equal scorer.scorer_id")

        attempts: list[BenchmarkAttempt] = []
        digest = suite_sha256(suite)
        expected = len(suite.cases) * suite.repetitions

        for case in suite.cases:
            scorer = scorers[case.scorer_id]
            for repetition in range(1, suite.repetitions + 1):
                attempt_id = f"{case.case_id}:{repetition}"
                request_id = f"{run_id}:{attempt_id}"
                prompt_digest = _messages_hash(case)
                reference_digest = (
                    _hash_text(case.reference_text)
                    if case.reference_text is not None
                    else None
                )
                request = ModelRequest(
                    request_id=request_id,
                    messages=case.messages,
                    model=candidate.model,
                    provider_id=candidate.provider_id,
                    fallback_provider_ids=(),
                    privacy=case.privacy,
                    timeout_seconds=suite.timeout_seconds,
                    temperature=suite.temperature,
                    metadata={
                        "model_lab_run_id": run_id,
                        "model_lab_suite_id": suite.suite_id,
                        "model_lab_case_id": case.case_id,
                        "model_lab_repetition": str(repetition),
                    },
                )
                before = _safe_snapshot(self._resource_observer)
                started = perf_counter()
                try:
                    response = await self._gateway.complete(request)
                except asyncio.CancelledError:
                    raise
                except ModelGatewayError as error:
                    wall_ms = (perf_counter() - started) * 1000.0
                    after = _safe_snapshot(self._resource_observer)
                    attempts.append(
                        BenchmarkAttempt(
                            attempt_id=attempt_id,
                            case_id=case.case_id,
                            repetition=repetition,
                            status=AttemptStatus.FAILED,
                            request_id=request_id,
                            prompt_sha256=prompt_digest,
                            reference_sha256=reference_digest,
                            provider_id=error.provider_id or candidate.provider_id,
                            provider_kind=candidate.provider_kind,
                            model=candidate.model,
                            wall_latency_ms=wall_ms,
                            resource_before=before,
                            resource_after=after,
                            error_code=f"model_gateway:{error.code.value}",
                        )
                    )
                    if not continue_on_failure:
                        return BenchmarkRunEvidence(
                            run_id=run_id,
                            candidate=candidate,
                            suite_id=suite.suite_id,
                            suite_version=suite.version,
                            suite_sha256=digest,
                            expected_attempts=expected,
                            attempts=tuple(attempts),
                        )
                    continue

                wall_ms = (perf_counter() - started) * 1000.0
                after = _safe_snapshot(self._resource_observer)
                identity_error = self._identity_error(
                    request_id=request_id,
                    candidate=candidate,
                    response=response,
                )
                if identity_error is not None:
                    attempts.append(
                        BenchmarkAttempt(
                            attempt_id=attempt_id,
                            case_id=case.case_id,
                            repetition=repetition,
                            status=AttemptStatus.FAILED,
                            request_id=request_id,
                            prompt_sha256=prompt_digest,
                            reference_sha256=reference_digest,
                            provider_id=response.provider_id,
                            provider_kind=response.provider_kind,
                            model=response.model,
                            wall_latency_ms=wall_ms,
                            provider_latency_ms=response.latency_ms,
                            resource_before=before,
                            resource_after=after,
                            error_code=identity_error,
                        )
                    )
                    if not continue_on_failure:
                        return BenchmarkRunEvidence(
                            run_id=run_id,
                            candidate=candidate,
                            suite_id=suite.suite_id,
                            suite_version=suite.version,
                            suite_sha256=digest,
                            expected_attempts=expected,
                            attempts=tuple(attempts),
                        )
                    continue

                scorer_metrics = scorer.score(case, response.text)
                metrics = _attempt_metrics(
                    scorer_metrics=scorer_metrics,
                    response=response,
                    wall_latency_ms=wall_ms,
                    resource_before=before,
                    resource_after=after,
                )
                attempts.append(
                    BenchmarkAttempt(
                        attempt_id=attempt_id,
                        case_id=case.case_id,
                        repetition=repetition,
                        status=AttemptStatus.SUCCESS,
                        request_id=request_id,
                        prompt_sha256=prompt_digest,
                        reference_sha256=reference_digest,
                        provider_id=response.provider_id,
                        provider_kind=response.provider_kind,
                        model=response.model,
                        wall_latency_ms=wall_ms,
                        metrics=metrics,
                        response_sha256=_hash_text(response.text),
                        response_characters=len(response.text),
                        provider_latency_ms=response.latency_ms,
                        input_tokens=response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens,
                        total_tokens=response.usage.total_tokens,
                        resource_before=before,
                        resource_after=after,
                    )
                )

        return BenchmarkRunEvidence(
            run_id=run_id,
            candidate=candidate,
            suite_id=suite.suite_id,
            suite_version=suite.version,
            suite_sha256=digest,
            expected_attempts=expected,
            attempts=tuple(attempts),
        )

    @staticmethod
    def _identity_error(
        *,
        request_id: str,
        candidate: ModelCandidate,
        response: ModelResponse,
    ) -> str | None:
        if response.request_id != request_id:
            return "identity_mismatch:request_id"
        if response.provider_id != candidate.provider_id:
            return "identity_mismatch:provider_id"
        if response.provider_kind is not candidate.provider_kind:
            return "identity_mismatch:provider_kind"
        if response.model != candidate.model:
            return "identity_mismatch:model"
        return None


def _resource_payload(snapshot: ResourceSnapshot | None) -> dict[str, float | int] | None:
    if snapshot is None:
        return None
    return {
        "cpu_percent": snapshot.cpu_percent,
        "memory_percent": snapshot.memory_percent,
        "available_memory_bytes": snapshot.available_memory_bytes,
    }


def evidence_document(evidence: BenchmarkRunEvidence) -> str:
    candidate = evidence.candidate
    attempts = []
    for attempt in evidence.attempts:
        attempts.append(
            {
                "attempt_id": attempt.attempt_id,
                "case_id": attempt.case_id,
                "repetition": attempt.repetition,
                "status": attempt.status.value,
                "request_id": attempt.request_id,
                "prompt_sha256": attempt.prompt_sha256,
                "reference_sha256": attempt.reference_sha256,
                "provider_id": attempt.provider_id,
                "provider_kind": attempt.provider_kind.value,
                "model": attempt.model,
                "wall_latency_ms": attempt.wall_latency_ms,
                "metrics": [
                    {"metric": metric.metric, "value": metric.value}
                    for metric in attempt.metrics
                ],
                "response_sha256": attempt.response_sha256,
                "response_characters": attempt.response_characters,
                "provider_latency_ms": attempt.provider_latency_ms,
                "input_tokens": attempt.input_tokens,
                "output_tokens": attempt.output_tokens,
                "total_tokens": attempt.total_tokens,
                "resource_before": _resource_payload(attempt.resource_before),
                "resource_after": _resource_payload(attempt.resource_after),
                "error_code": attempt.error_code,
            }
        )
    payload = {
        "schema": 1,
        "run_id": evidence.run_id,
        "candidate": {
            "candidate_id": candidate.candidate_id,
            "provider_id": candidate.provider_id,
            "provider_kind": candidate.provider_kind.value,
            "model": candidate.model,
            "model_version": candidate.model_version,
            "license_reference": candidate.license_reference,
            "provenance_reference": candidate.provenance_reference,
            "permission_fingerprint": candidate.permission_fingerprint,
            "artifact_sha256": candidate.artifact_sha256,
        },
        "suite_id": evidence.suite_id,
        "suite_version": evidence.suite_version,
        "suite_sha256": evidence.suite_sha256,
        "expected_attempts": evidence.expected_attempts,
        "complete": evidence.complete,
        "attempts": attempts,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def evidence_sha256(evidence: BenchmarkRunEvidence) -> str:
    return sha256(evidence_document(evidence).encode("utf-8")).hexdigest()


def metric_means(evidence: BenchmarkRunEvidence) -> dict[str, float]:
    if not evidence.complete:
        raise ValueError("incomplete benchmark evidence cannot be summarized for promotion")
    values: dict[str, list[float]] = {}
    for attempt in evidence.attempts:
        for metric in attempt.metrics:
            values.setdefault(metric.metric, []).append(float(metric.value))
    return {name: fmean(items) for name, items in sorted(values.items())}
