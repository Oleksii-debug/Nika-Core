from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from nika_core.model_engineering import (
    AcceleratorObserverPort,
    CandidateBenchmarkReport,
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelBenchmarkRunner,
    ModelCandidate,
    ModelCompletionPort,
    ModelScoringPort,
    benchmark_report_sha256,
)
from nika_core.model_engineering.contracts import (
    BenchmarkExecutionConfig,
    validate_candidate_benchmark_report,
    validate_evaluation_set,
    validate_model_candidate,
)
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelFailureEffect,
    ModelGatewayError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderKind,
)
from nika_core.resources.contracts import ResourceObserverPort
from nika_core.training_evaluation_attestation import (
    AttestedTrainingCandidateGateway,
    LoadedModelAttestedCompletionPort,
)
from nika_core.training_evaluation_binding import TrainingEvaluationBinding

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_IDENTITY_BYTES = 512
_DEFAULT_SCORER_ID = "exact-match-nfc-v1"


def _canonical_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty canonical text")
    if any(not character.isprintable() for character in value):
        raise ValueError(f"{name} must not contain control characters")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    if len(encoded) > _MAX_IDENTITY_BYTES:
        raise ValueError(f"{name} exceeds the configured byte limit")
    return value


def _sha256(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be an exact lowercase SHA-256 digest")
    return value


class TrainingEvaluationExecutionError(RuntimeError):
    """Safe Loop-C abort before incomplete benchmark evidence can escape."""

    def __init__(
        self,
        message: str,
        *,
        code: ModelErrorCode | None = None,
        failure_effect: ModelFailureEffect | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None and not any(code is member for member in ModelErrorCode):
            raise TypeError("code must be a ModelErrorCode")
        if failure_effect is not None and not any(
            failure_effect is member for member in ModelFailureEffect
        ):
            raise TypeError("failure_effect must be a ModelFailureEffect")
        self.code = code
        self.failure_effect = failure_effect


@dataclass(frozen=True, slots=True, init=False)
class AttestedChallengerBenchmarkResult:
    """Complete challenger benchmark bound to Loop-C identity and attestor trust.

    Direct construction is disabled to prevent accidental evidence fabrication.
    Python object construction history is not a security boundary, so every trusted
    consumer must call ``revalidated()`` or a method that does so.
    """

    binding: TrainingEvaluationBinding
    report: CandidateBenchmarkReport
    attestor_id: str
    attestor_sha256: str

    def __init_subclass__(cls, **_: object) -> None:
        raise TypeError("AttestedChallengerBenchmarkResult cannot be subclassed")

    def _validate(self) -> None:
        try:
            binding = self.binding.revalidated()
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("binding must be canonical") from exc
        if type(self.report) is not CandidateBenchmarkReport:
            raise TypeError("report must be an exact CandidateBenchmarkReport")
        try:
            validate_candidate_benchmark_report(self.report)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("benchmark report must be canonical") from exc
        attestor_id = _canonical_text(self.attestor_id, name="attestor_id")
        attestor_sha256 = _sha256(self.attestor_sha256, name="attestor_sha256")
        candidate = self.report.candidate
        if (
            candidate.candidate_id != binding.challenger_candidate_id
            or candidate.provider_id != binding.challenger_provider_id
            or candidate.provider_kind is not ProviderKind.LOCAL
            or candidate.request_model != binding.challenger_model_id
            or candidate.expected_response_model != binding.challenger_model_id
            or candidate.model_sha256 != binding.challenger_sha256
        ):
            raise ValueError("benchmark candidate does not match training binding")
        if (
            self.report.evaluation_set_sha256 != binding.evaluation_set_sha256
            or self.report.evaluation_purpose is not EvaluationPurpose.HELD_OUT
        ):
            raise ValueError("benchmark evaluation does not match training binding")
        if (
            self.report.completion_rate != 1.0
            or any(
                result.completion_succeeded is not True
                for result in self.report.case_results
            )
        ):
            raise ValueError(
                "attested challenger benchmark requires complete successful coverage"
            )
        # Force validated values to be consumed so mutated/behavioral carriers cannot
        # hide behind dataclass construction history.
        if attestor_id != self.attestor_id or attestor_sha256 != self.attestor_sha256:
            raise ValueError("attestor evidence is not canonical")

    def revalidated(self) -> AttestedChallengerBenchmarkResult:
        if type(self) is not AttestedChallengerBenchmarkResult:
            raise TypeError(
                "result must be an exact AttestedChallengerBenchmarkResult"
            )
        try:
            self._validate()
            return _build_result(
                binding=self.binding.revalidated(),
                report=self.report,
                attestor_id=self.attestor_id,
                attestor_sha256=self.attestor_sha256,
            )
        except AttributeError as exc:
            raise ValueError("attested benchmark result fields are incomplete") from exc

    def evidence_payload(self) -> dict[str, object]:
        result = self.revalidated()
        return {
            "schema": "nika-attested-challenger-benchmark-v1",
            "job_id": result.binding.job_id,
            "binding_sha256": result.binding.binding_sha256,
            "challenger_candidate_id": result.binding.challenger_candidate_id,
            "challenger_sha256": result.binding.challenger_sha256,
            "evaluation_set_sha256": result.binding.evaluation_set_sha256,
            "execution_config_sha256": result.report.execution_config_sha256,
            "benchmark_run_id": result.report.run.run_id,
            "benchmark_report_sha256": benchmark_report_sha256(result.report),
            "attestor_id": result.attestor_id,
            "attestor_sha256": result.attestor_sha256,
            "case_count": len(result.report.case_results),
        }

    @property
    def evidence_sha256(self) -> str:
        encoded = json.dumps(
            self.evidence_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _build_result(
    *,
    binding: TrainingEvaluationBinding,
    report: CandidateBenchmarkReport,
    attestor_id: str,
    attestor_sha256: str,
) -> AttestedChallengerBenchmarkResult:
    result = object.__new__(AttestedChallengerBenchmarkResult)
    object.__setattr__(result, "binding", binding)
    object.__setattr__(result, "report", report)
    object.__setattr__(result, "attestor_id", attestor_id)
    object.__setattr__(result, "attestor_sha256", attestor_sha256)
    result._validate()
    return result


def _snapshot_candidate(candidate: ModelCandidate) -> ModelCandidate:
    try:
        validate_model_candidate(candidate)
        return ModelCandidate(
            candidate_id=candidate.candidate_id,
            provider_id=candidate.provider_id,
            provider_kind=candidate.provider_kind,
            request_model=candidate.request_model,
            expected_response_model=candidate.expected_response_model,
            engine_provenance_ref=candidate.engine_provenance_ref,
            engine_license_ref=candidate.engine_license_ref,
            model_provenance_ref=candidate.model_provenance_ref,
            model_license_ref=candidate.model_license_ref,
            model_sha256=candidate.model_sha256,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("challenger must be canonical") from exc


def _snapshot_evaluation_set(evaluation_set: EvaluationSet) -> EvaluationSet:
    try:
        validate_evaluation_set(evaluation_set)
        cases = tuple(
            EvaluationCase(
                case_id=case.case_id,
                messages=tuple(
                    ModelMessage(role=message.role, content=message.content)
                    for message in case.messages
                ),
                expected_text=case.expected_text,
                pass_score=case.pass_score,
                weight=case.weight,
            )
            for case in evaluation_set.cases
        )
        return EvaluationSet(
            evaluation_set_id=evaluation_set.evaluation_set_id,
            version=evaluation_set.version,
            provenance_ref=evaluation_set.provenance_ref,
            license_ref=evaluation_set.license_ref,
            purpose=evaluation_set.purpose,
            privacy=evaluation_set.privacy,
            cases=cases,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("evaluation set must be canonical") from exc


class _AbortOnAttestedGatewayFailure:
    """Keep ModelBenchmarkRunner from flattening proof failures into case evidence."""

    def __init__(self, gateway: ModelCompletionPort) -> None:
        self._gateway = gateway
        self._provider_calls_started = 0

    @property
    def provider_calls_started(self) -> int:
        return self._provider_calls_started

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._provider_calls_started += 1
        try:
            return await self._gateway.complete(request)
        except ModelGatewayError as exc:
            raise TrainingEvaluationExecutionError(
                "attested challenger benchmark provider effect failed",
                code=exc.code,
                failure_effect=exc.failure_effect,
            ) from None


def _validate_preflight(
    *,
    binding: TrainingEvaluationBinding,
    challenger: ModelCandidate,
    evaluation_set: EvaluationSet,
) -> tuple[TrainingEvaluationBinding, ModelCandidate, EvaluationSet]:
    try:
        canonical_binding = binding.revalidated()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("binding must be canonical") from exc
    canonical_challenger = _snapshot_candidate(challenger)
    canonical_evaluation = _snapshot_evaluation_set(evaluation_set)
    if (
        canonical_challenger.candidate_id
        != canonical_binding.challenger_candidate_id
        or canonical_challenger.provider_id
        != canonical_binding.challenger_provider_id
        or canonical_challenger.provider_kind is not ProviderKind.LOCAL
        or canonical_challenger.request_model
        != canonical_binding.challenger_model_id
        or canonical_challenger.expected_response_model
        != canonical_binding.challenger_model_id
        or canonical_challenger.model_sha256
        != canonical_binding.challenger_sha256
    ):
        raise ValueError("challenger does not match training evaluation binding")
    if canonical_evaluation.purpose is not EvaluationPurpose.HELD_OUT:
        raise ValueError("Loop-C challenger benchmark requires held-out evaluation")
    if (
        canonical_evaluation.content_sha256
        != canonical_binding.evaluation_set_sha256
    ):
        raise ValueError("evaluation set does not match training evaluation binding")
    return canonical_binding, canonical_challenger, canonical_evaluation


async def run_attested_challenger_benchmark(
    *,
    binding: TrainingEvaluationBinding,
    challenger: ModelCandidate,
    evaluation_set: EvaluationSet,
    effect_port: LoadedModelAttestedCompletionPort,
    expected_attestor_id: str,
    expected_attestor_sha256: str,
    timeout_seconds: float = 60.0,
    temperature: float | None = 0.0,
    scorer: ModelScoringPort | None = None,
    scorer_id: str | None = None,
    resource_observer: ResourceObserverPort | None = None,
    accelerator_observer: AcceleratorObserverPort | None = None,
) -> AttestedChallengerBenchmarkResult:
    """Execute one fully-attested held-out challenger benchmark.

    Any ModelGatewayError from the attested boundary aborts the whole Loop-C
    benchmark immediately instead of being normalized into a failed benchmark
    case. That preserves #1298 UNKNOWN effect truth and prevents later cases from
    running after an uncertain loaded-artifact proof.
    """

    (
        canonical_binding,
        canonical_challenger,
        canonical_evaluation,
    ) = _validate_preflight(
        binding=binding,
        challenger=challenger,
        evaluation_set=evaluation_set,
    )
    effective_scorer_id = _DEFAULT_SCORER_ID if scorer is None else scorer_id
    if type(effective_scorer_id) is not str:
        raise TypeError("custom scorer requires a canonical scorer_id")
    execution_config = BenchmarkExecutionConfig(
        timeout_seconds=timeout_seconds,
        temperature=temperature,
        scorer_id=effective_scorer_id,
    )
    attested_gateway = AttestedTrainingCandidateGateway(
        effect_port,
        binding=canonical_binding,
        expected_attestor_id=expected_attestor_id,
        expected_attestor_sha256=expected_attestor_sha256,
    )
    fail_fast_gateway = _AbortOnAttestedGatewayFailure(attested_gateway)
    runner = ModelBenchmarkRunner(
        fail_fast_gateway,
        scorer=scorer,
        scorer_id=scorer_id,
        resource_observer=resource_observer,
        accelerator_observer=accelerator_observer,
    )
    try:
        report = await runner.benchmark(
            canonical_challenger,
            canonical_evaluation,
            timeout_seconds=execution_config.timeout_seconds,
            temperature=execution_config.temperature,
        )
    except TrainingEvaluationExecutionError:
        raise
    except Exception:
        effect = (
            ModelFailureEffect.UNKNOWN
            if fail_fast_gateway.provider_calls_started
            else ModelFailureEffect.NO_EFFECT
        )
        raise TrainingEvaluationExecutionError(
            "attested challenger benchmark infrastructure failed",
            failure_effect=effect,
        ) from None
    if report.execution_config_sha256 != execution_config.evidence_sha256:
        raise TrainingEvaluationExecutionError(
            "benchmark execution configuration identity changed",
            failure_effect=ModelFailureEffect.UNKNOWN,
        )
    try:
        return _build_result(
            binding=canonical_binding,
            report=report,
            attestor_id=expected_attestor_id,
            attestor_sha256=expected_attestor_sha256,
        )
    except (AttributeError, TypeError, ValueError):
        raise TrainingEvaluationExecutionError(
            "attested challenger benchmark evidence is inconsistent",
            failure_effect=ModelFailureEffect.UNKNOWN,
        ) from None


__all__ = [
    "AttestedChallengerBenchmarkResult",
    "TrainingEvaluationExecutionError",
    "run_attested_challenger_benchmark",
]
