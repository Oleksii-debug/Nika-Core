from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from nika_core.model_engineering import (
    AcceleratorObserverPort,
    CandidateBenchmarkReport,
    EvaluationPurpose,
    EvaluationSet,
    ModelCandidate,
    ModelScoringPort,
    benchmark_report_sha256,
)
from nika_core.model_engineering.contracts import (
    validate_candidate_benchmark_report,
    validate_evaluation_set,
    validate_model_candidate,
)
from nika_core.model_gateway.contracts import ModelFailureEffect, ProviderKind
from nika_core.resources.contracts import ResourceObserverPort
from nika_core.training_evaluation_attestation import LoadedModelAttestedCompletionPort
from nika_core.training_evaluation_binding import TrainingEvaluationBinding
from nika_core.training_evaluation_execution import (
    AttestedCaseReceipt,
    TrainingEvaluationExecutionError,
    run_attested_challenger_benchmark,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_IDENTITY_BYTES = 512


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


def _expected_request_id(
    *,
    run_id: str,
    configuration_sha256: str,
    case_id: str,
) -> str:
    raw = (
        f"nika-model-benchmark-v3\0{run_id}\0"
        f"{configuration_sha256}\0{case_id}"
    ).encode()
    return f"model-bench-{hashlib.sha256(raw).hexdigest()[:32]}"


@dataclass(frozen=True, slots=True)
class _ChampionBindingView:
    """Private role adapter over the single canonical TrainingEvaluationBinding."""

    source: TrainingEvaluationBinding

    def __post_init__(self) -> None:
        if type(self.source) is not TrainingEvaluationBinding:
            raise TypeError("source must be an exact TrainingEvaluationBinding")
        self.source.revalidated()

    def revalidated(self) -> _ChampionBindingView:
        return _ChampionBindingView(self.source.revalidated())

    @property
    def job_id(self) -> str:
        return self.source.job_id

    @property
    def challenger_candidate_id(self) -> str:
        return self.source.base_candidate_id

    @property
    def challenger_provider_id(self) -> str:
        return self.source.base_provider_id

    @property
    def challenger_model_id(self) -> str:
        return self.source.base_model_id

    @property
    def challenger_sha256(self) -> str:
        return self.source.base_sha256

    @property
    def challenger_size_bytes(self) -> int:
        return self.source.base_size_bytes

    @property
    def descriptor_digest(self) -> str:
        return self.source.base_descriptor_digest

    @property
    def descriptor_registry_key(self) -> str:
        return self.source.base_descriptor_registry_key

    @property
    def evaluation_set_sha256(self) -> str:
        return self.source.evaluation_set_sha256

    @property
    def binding_sha256(self) -> str:
        return self.source.binding_sha256


def _receipt_payload(receipt: AttestedCaseReceipt) -> dict[str, str]:
    item = receipt.revalidated()
    return {
        "schema": "nika-attested-champion-case-v1",
        "case_id": item.case_id,
        "request_id": item.request_id,
        "binding_sha256": item.binding_sha256,
        "provider_id": item.provider_id,
        "model_id": item.model_id,
        "artifact_sha256": item.artifact_sha256,
        "descriptor_digest": item.descriptor_digest,
        "attestor_id": item.attestor_id,
        "attestor_sha256": item.attestor_sha256,
    }


def _payload_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class AttestedChampionBenchmarkResult:
    """Complete champion benchmark bound to the canonical Loop-C v2 binding."""

    binding: TrainingEvaluationBinding
    report: CandidateBenchmarkReport
    case_receipts: tuple[AttestedCaseReceipt, ...]
    attestor_id: str
    attestor_sha256: str

    def __init_subclass__(cls, **_: object) -> None:
        raise TypeError("AttestedChampionBenchmarkResult cannot be subclassed")

    def _validate(self) -> None:
        if type(self.binding) is not TrainingEvaluationBinding:
            raise TypeError("binding must be an exact TrainingEvaluationBinding")
        binding = self.binding.revalidated()
        if type(self.report) is not CandidateBenchmarkReport:
            raise TypeError("report must be an exact CandidateBenchmarkReport")
        try:
            validate_candidate_benchmark_report(self.report)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("champion benchmark report must be canonical") from exc
        attestor_id = _canonical_text(self.attestor_id, name="attestor_id")
        attestor_sha256 = _sha256(self.attestor_sha256, name="attestor_sha256")

        candidate = self.report.candidate
        if (
            candidate.candidate_id != binding.base_candidate_id
            or candidate.provider_id != binding.base_provider_id
            or candidate.provider_kind is not ProviderKind.LOCAL
            or candidate.request_model != binding.base_model_id
            or candidate.expected_response_model != binding.base_model_id
            or candidate.model_sha256 != binding.base_sha256
        ):
            raise ValueError("benchmark candidate does not match base binding")
        if (
            self.report.evaluation_set_sha256 != binding.evaluation_set_sha256
            or self.report.evaluation_purpose is not EvaluationPurpose.HELD_OUT
        ):
            raise ValueError("benchmark evaluation does not match base binding")
        if (
            self.report.completion_rate != 1.0
            or any(
                item.completion_succeeded is not True
                for item in self.report.case_results
            )
        ):
            raise ValueError(
                "attested champion benchmark requires complete successful coverage"
            )
        if type(self.case_receipts) is not tuple:
            raise TypeError("case_receipts must be a canonical tuple")
        if len(self.case_receipts) != len(self.report.case_results):
            raise ValueError("attested receipt coverage does not match benchmark cases")

        receipts = tuple(item.revalidated() for item in self.case_receipts)
        expected_case_ids = tuple(item.case_id for item in self.report.case_results)
        if tuple(item.case_id for item in receipts) != expected_case_ids:
            raise ValueError("attested receipt case order does not match benchmark report")
        if len({item.request_id for item in receipts}) != len(receipts):
            raise ValueError("attested receipt request identities must be unique")
        for receipt in receipts:
            if receipt.request_id != _expected_request_id(
                run_id=self.report.run.run_id,
                configuration_sha256=self.report.run.configuration_sha256,
                case_id=receipt.case_id,
            ):
                raise ValueError("attested receipt request identity is inconsistent")
            if (
                receipt.binding_sha256 != binding.binding_sha256
                or receipt.provider_id != binding.base_provider_id
                or receipt.model_id != binding.base_model_id
                or receipt.artifact_sha256 != binding.base_sha256
                or receipt.descriptor_digest != binding.base_descriptor_digest
                or receipt.attestor_id != attestor_id
                or receipt.attestor_sha256 != attestor_sha256
            ):
                raise ValueError(
                    "attested case receipt does not match champion benchmark authority"
                )

    def revalidated(self) -> AttestedChampionBenchmarkResult:
        if type(self) is not AttestedChampionBenchmarkResult:
            raise TypeError(
                "result must be an exact AttestedChampionBenchmarkResult"
            )
        try:
            self._validate()
            return _build_result(
                binding=self.binding.revalidated(),
                report=self.report,
                case_receipts=tuple(
                    item.revalidated() for item in self.case_receipts
                ),
                attestor_id=self.attestor_id,
                attestor_sha256=self.attestor_sha256,
            )
        except AttributeError as exc:
            raise ValueError(
                "attested champion benchmark fields are incomplete"
            ) from exc

    def evidence_payload(self) -> dict[str, object]:
        result = self.revalidated()
        receipt_payloads: list[dict[str, object]] = []
        for receipt in result.case_receipts:
            payload: dict[str, object] = _receipt_payload(receipt)
            payload["receipt_sha256"] = _payload_sha256(payload)
            receipt_payloads.append(payload)
        return {
            "schema": "nika-attested-champion-benchmark-v1",
            "job_id": result.binding.job_id,
            "binding_sha256": result.binding.binding_sha256,
            "champion_candidate_id": result.binding.base_candidate_id,
            "champion_sha256": result.binding.base_sha256,
            "evaluation_set_sha256": result.binding.evaluation_set_sha256,
            "execution_config_sha256": result.report.execution_config_sha256,
            "benchmark_run_id": result.report.run.run_id,
            "benchmark_report_sha256": benchmark_report_sha256(result.report),
            "attestor_id": result.attestor_id,
            "attestor_sha256": result.attestor_sha256,
            "case_count": len(result.report.case_results),
            "case_receipts": receipt_payloads,
        }

    @property
    def evidence_sha256(self) -> str:
        return _payload_sha256(self.evidence_payload())


def _build_result(
    *,
    binding: TrainingEvaluationBinding,
    report: CandidateBenchmarkReport,
    case_receipts: tuple[AttestedCaseReceipt, ...],
    attestor_id: str,
    attestor_sha256: str,
) -> AttestedChampionBenchmarkResult:
    result = object.__new__(AttestedChampionBenchmarkResult)
    object.__setattr__(result, "binding", binding)
    object.__setattr__(result, "report", report)
    object.__setattr__(result, "case_receipts", case_receipts)
    object.__setattr__(result, "attestor_id", attestor_id)
    object.__setattr__(result, "attestor_sha256", attestor_sha256)
    result._validate()
    return result


def _validate_champion_preflight(
    *,
    binding: TrainingEvaluationBinding,
    champion: ModelCandidate,
    evaluation_set: EvaluationSet,
) -> tuple[TrainingEvaluationBinding, ModelCandidate, EvaluationSet]:
    if type(binding) is not TrainingEvaluationBinding:
        raise TypeError("binding must be an exact TrainingEvaluationBinding")
    canonical_binding = binding.revalidated()
    if type(champion) is not ModelCandidate:
        raise TypeError("champion must be an exact ModelCandidate")
    try:
        validate_model_candidate(champion)
        validate_evaluation_set(evaluation_set)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("champion benchmark input is not canonical") from exc
    if (
        champion.candidate_id != canonical_binding.base_candidate_id
        or champion.provider_id != canonical_binding.base_provider_id
        or champion.provider_kind is not ProviderKind.LOCAL
        or champion.request_model != canonical_binding.base_model_id
        or champion.expected_response_model != canonical_binding.base_model_id
        or champion.model_sha256 != canonical_binding.base_sha256
    ):
        raise ValueError("champion does not match training evaluation binding")
    if evaluation_set.purpose is not EvaluationPurpose.HELD_OUT:
        raise ValueError("Loop-C champion benchmark requires held-out evaluation")
    if evaluation_set.content_sha256 != canonical_binding.evaluation_set_sha256:
        raise ValueError("evaluation set does not match training evaluation binding")
    return canonical_binding, champion, evaluation_set


async def run_attested_champion_benchmark(
    *,
    binding: TrainingEvaluationBinding,
    champion: ModelCandidate,
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
) -> AttestedChampionBenchmarkResult:
    """Run the bound champion through the incumbent fail-fast attested path."""

    (
        canonical_binding,
        canonical_champion,
        canonical_evaluation,
    ) = _validate_champion_preflight(
        binding=binding,
        champion=champion,
        evaluation_set=evaluation_set,
    )
    view = _ChampionBindingView(canonical_binding)
    inner = await run_attested_challenger_benchmark(
        binding=view,  # type: ignore[arg-type]
        challenger=canonical_champion,
        evaluation_set=canonical_evaluation,
        effect_port=effect_port,
        expected_attestor_id=expected_attestor_id,
        expected_attestor_sha256=expected_attestor_sha256,
        timeout_seconds=timeout_seconds,
        temperature=temperature,
        scorer=scorer,
        scorer_id=scorer_id,
        resource_observer=resource_observer,
        accelerator_observer=accelerator_observer,
    )
    try:
        canonical_inner = inner.revalidated()
        return _build_result(
            binding=canonical_binding,
            report=canonical_inner.report,
            case_receipts=canonical_inner.case_receipts,
            attestor_id=canonical_inner.attestor_id,
            attestor_sha256=canonical_inner.attestor_sha256,
        )
    except (AttributeError, TypeError, ValueError):
        raise TrainingEvaluationExecutionError(
            "attested champion benchmark evidence is inconsistent",
            failure_effect=ModelFailureEffect.UNKNOWN,
        ) from None


__all__ = [
    "AttestedChampionBenchmarkResult",
    "run_attested_champion_benchmark",
]
