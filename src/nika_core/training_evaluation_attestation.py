from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from nika_core.model_engineering.contracts import (
    BenchmarkSuiteReport,
    CandidateBenchmarkReport,
    EvaluationPurpose,
    validate_candidate_benchmark_report,
)
from nika_core.model_engineering.reporting import benchmark_report_sha256
from nika_core.training_evaluation_binding import TrainingEvaluationBinding

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class TrainingEvaluationAttestationError(RuntimeError):
    """Safe failure while proving that exact old/new model bytes were evaluated."""


@dataclass(frozen=True, slots=True)
class TrainingEvaluationAttestation:
    binding_sha256: str
    evaluation_set_sha256: str
    execution_config_sha256: str
    base_candidate_id: str
    challenger_candidate_id: str
    base_model_sha256: str
    challenger_model_sha256: str
    base_run_id: str
    challenger_run_id: str
    base_report_sha256: str
    challenger_report_sha256: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.binding_sha256, "binding_sha256"),
            (self.evaluation_set_sha256, "evaluation_set_sha256"),
            (self.execution_config_sha256, "execution_config_sha256"),
            (self.base_model_sha256, "base_model_sha256"),
            (self.challenger_model_sha256, "challenger_model_sha256"),
            (self.base_report_sha256, "base_report_sha256"),
            (self.challenger_report_sha256, "challenger_report_sha256"),
        ):
            if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
                raise ValueError(f"{name} must be an exact lowercase SHA-256 digest")
        for value, name in (
            (self.base_candidate_id, "base_candidate_id"),
            (self.challenger_candidate_id, "challenger_candidate_id"),
            (self.base_run_id, "base_run_id"),
            (self.challenger_run_id, "challenger_run_id"),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"{name} must be non-empty canonical text")
        if self.base_candidate_id == self.challenger_candidate_id:
            raise ValueError("old/new candidate identities must be distinct")
        if self.base_run_id == self.challenger_run_id:
            raise ValueError("old/new benchmark runs must be distinct")

    def revalidated(self) -> TrainingEvaluationAttestation:
        if type(self) is not TrainingEvaluationAttestation:
            raise TypeError(
                "attestation must be an exact TrainingEvaluationAttestation"
            )
        try:
            return TrainingEvaluationAttestation(
                binding_sha256=self.binding_sha256,
                evaluation_set_sha256=self.evaluation_set_sha256,
                execution_config_sha256=self.execution_config_sha256,
                base_candidate_id=self.base_candidate_id,
                challenger_candidate_id=self.challenger_candidate_id,
                base_model_sha256=self.base_model_sha256,
                challenger_model_sha256=self.challenger_model_sha256,
                base_run_id=self.base_run_id,
                challenger_run_id=self.challenger_run_id,
                base_report_sha256=self.base_report_sha256,
                challenger_report_sha256=self.challenger_report_sha256,
            )
        except AttributeError as exc:
            raise ValueError("attestation fields must be complete") from exc

    def _evidence_sha256_unchecked(self) -> str:
        payload = {
            "schema": "nika-training-evaluation-attestation-v1",
            "binding_sha256": self.binding_sha256,
            "evaluation_set_sha256": self.evaluation_set_sha256,
            "execution_config_sha256": self.execution_config_sha256,
            "base_candidate_id": self.base_candidate_id,
            "challenger_candidate_id": self.challenger_candidate_id,
            "base_model_sha256": self.base_model_sha256,
            "challenger_model_sha256": self.challenger_model_sha256,
            "base_run_id": self.base_run_id,
            "challenger_run_id": self.challenger_run_id,
            "base_report_sha256": self.base_report_sha256,
            "challenger_report_sha256": self.challenger_report_sha256,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def evidence_sha256(self) -> str:
        return self.revalidated()._evidence_sha256_unchecked()


def _report_by_candidate(
    suite: BenchmarkSuiteReport,
    candidate_id: str,
) -> CandidateBenchmarkReport:
    matches = tuple(
        report for report in suite.reports if report.candidate.candidate_id == candidate_id
    )
    if len(matches) != 1:
        raise TrainingEvaluationAttestationError(
            "benchmark suite must contain exactly one report for each bound candidate"
        )
    return matches[0]


def _require_attested_success(
    report: CandidateBenchmarkReport,
    *,
    expected_sha256: str,
) -> None:
    successful = tuple(
        item for item in report.case_results if item.completion_succeeded
    )
    if not successful:
        raise TrainingEvaluationAttestationError(
            "benchmark report has no successful provider effect to attest"
        )
    if any(item.loaded_artifact_sha256 != expected_sha256 for item in successful):
        raise TrainingEvaluationAttestationError(
            "benchmark report does not attest the expected loaded model bytes"
        )


def attest_training_benchmark_suite(
    *,
    binding: TrainingEvaluationBinding,
    suite: BenchmarkSuiteReport,
) -> TrainingEvaluationAttestation:
    """Bind Loop-C training identity to old/new benchmark effect evidence.

    This function does not execute a benchmark or claim a winner. It only accepts
    a two-candidate held-out suite whose successful provider effects attest the exact
    base and trained challenger digests already frozen by TrainingEvaluationBinding.
    """

    if type(binding) is not TrainingEvaluationBinding:
        raise TypeError("binding must be an exact TrainingEvaluationBinding")
    if type(suite) is not BenchmarkSuiteReport:
        raise TypeError("suite must be an exact BenchmarkSuiteReport")

    try:
        canonical_binding = binding.revalidated()
        BenchmarkSuiteReport.__post_init__(suite)
    except (AttributeError, TypeError, ValueError) as exc:
        raise TrainingEvaluationAttestationError(
            "training/evaluation carriers are not canonical"
        ) from exc

    if len(suite.reports) != 2:
        raise TrainingEvaluationAttestationError(
            "old/new training evaluation requires exactly two benchmark reports"
        )
    if suite.evaluation_set_sha256 != canonical_binding.evaluation_set_sha256:
        raise TrainingEvaluationAttestationError(
            "benchmark suite evaluation set does not match training binding"
        )

    base_report = _report_by_candidate(suite, canonical_binding.base_candidate_id)
    challenger_report = _report_by_candidate(
        suite,
        canonical_binding.challenger_candidate_id,
    )
    try:
        validate_candidate_benchmark_report(base_report)
        validate_candidate_benchmark_report(challenger_report)
    except (AttributeError, TypeError, ValueError) as exc:
        raise TrainingEvaluationAttestationError(
            "benchmark report evidence is not canonical"
        ) from exc

    for report in (base_report, challenger_report):
        if report.evaluation_purpose is not EvaluationPurpose.HELD_OUT:
            raise TrainingEvaluationAttestationError(
                "training old/new evaluation requires held-out benchmark evidence"
            )
        if (
            report.evaluation_set_sha256 != suite.evaluation_set_sha256
            or report.execution_config_sha256 != suite.execution_config_sha256
        ):
            raise TrainingEvaluationAttestationError(
                "benchmark report does not match suite evaluation/config identity"
            )

    if base_report.candidate.model_sha256 != canonical_binding.base_sha256:
        raise TrainingEvaluationAttestationError(
            "base benchmark candidate digest does not match training binding"
        )
    if (
        challenger_report.candidate.model_sha256
        != canonical_binding.challenger_sha256
    ):
        raise TrainingEvaluationAttestationError(
            "challenger benchmark candidate digest does not match training binding"
        )

    _require_attested_success(
        base_report,
        expected_sha256=canonical_binding.base_sha256,
    )
    _require_attested_success(
        challenger_report,
        expected_sha256=canonical_binding.challenger_sha256,
    )

    return TrainingEvaluationAttestation(
        binding_sha256=canonical_binding.binding_sha256,
        evaluation_set_sha256=suite.evaluation_set_sha256,
        execution_config_sha256=suite.execution_config_sha256,
        base_candidate_id=canonical_binding.base_candidate_id,
        challenger_candidate_id=canonical_binding.challenger_candidate_id,
        base_model_sha256=canonical_binding.base_sha256,
        challenger_model_sha256=canonical_binding.challenger_sha256,
        base_run_id=base_report.run.run_id,
        challenger_run_id=challenger_report.run.run_id,
        base_report_sha256=benchmark_report_sha256(base_report),
        challenger_report_sha256=benchmark_report_sha256(challenger_report),
    )
