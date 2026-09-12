import ast
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_research_metric_evidence_helpers import total_return_evidence

from nika_core.trading_research import heldout, metric_evidence, metrics
from nika_core.trading_research.contracts import CausalityViolation, Partition, TradingResearchError
from nika_core.trading_research.dataset import ValidationIssue, ValidationReport
from nika_core.trading_research.heldout import (
    CandidateScore,
    HeldOutAssessment,
    HeldOutProtocol,
    PartitionResult,
    PartitionWindow,
    RefitPolicy,
    ReplayDataQuality,
    SelectionDecision,
    StrategyArtifactFingerprint,
    bind_held_out_test,
    select_validation_candidate,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)
HASH = "a" * 64
UNIVERSE = "b" * 64
QUALITY = "d" * 64
ALGORITHM = "e" * 64
CONFIG = "f" * 64
FEATURES = "1" * 64
FITTED = "2" * 64
CLEAN = ReplayDataQuality(0, 0, 0, QUALITY)


def protocol() -> HeldOutProtocol:
    return HeldOutProtocol(
        PartitionWindow(Partition.TRAIN, BASE, BASE + timedelta(days=10)),
        PartitionWindow(
            Partition.VALIDATION,
            BASE + timedelta(days=10),
            BASE + timedelta(days=15),
        ),
        PartitionWindow(
            Partition.TEST,
            BASE + timedelta(days=16),
            BASE + timedelta(days=20),
        ),
    )


def artifact(strategy_id: str = "chosen") -> StrategyArtifactFingerprint:
    return StrategyArtifactFingerprint(
        strategy_id,
        "v1",
        ALGORITHM,
        CONFIG,
        FEATURES,
        FITTED,
        7,
        BASE + timedelta(days=9),
        BASE + timedelta(days=9),
    )


def score(strategy_id: str = "chosen") -> CandidateScore:
    evidence = total_return_evidence(BASE, "1")
    return CandidateScore(
        artifact(strategy_id),
        Partition.VALIDATION,
        evidence.metric_name,
        evidence.definition_sha256,
        evidence.value,
        HASH,
        CLEAN,
        UNIVERSE,
        BASE + timedelta(days=9),
        BASE + timedelta(days=15),
        metric_evidence=evidence,
    )


def result_for(selection: SelectionDecision) -> PartitionResult:
    p = protocol()
    evidence = total_return_evidence(BASE, "0.5")
    return PartitionResult(
        selection.strategy_artifact,
        Partition.TEST,
        evidence.metric_name,
        evidence.definition_sha256,
        evidence.value,
        selection.dataset_semantic_hash,
        CLEAN,
        selection.universe_fingerprint,
        selection.universe_cutoff_at,
        p.test.end_at,
        metric_evidence=evidence,
    )


def test_selection_and_assessment_are_factory_only_evidence() -> None:
    with pytest.raises(TypeError, match="select_validation_candidate"):
        SelectionDecision()
    with pytest.raises(TypeError, match="bind_held_out_test"):
        HeldOutAssessment()


def test_candidate_artifact_mutation_before_selection_fails_closed() -> None:
    p = protocol()
    candidate = score()
    object.__setattr__(
        candidate.strategy_artifact,
        "fitted_state_sha256",
        "3" * 64,
    )
    with pytest.raises(TradingResearchError, match="changed after construction"):
        select_validation_candidate(
            p,
            (candidate,),
            selected_at=p.validation.end_at,
        )


def test_result_artifact_mutation_before_binding_fails_closed() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p,
        (score(),),
        selected_at=p.validation.end_at,
    )
    result = result_for(selected)
    object.__setattr__(
        result.strategy_artifact,
        "fitted_state_sha256",
        "3" * 64,
    )
    with pytest.raises(TradingResearchError, match="changed after construction"):
        bind_held_out_test(p, selected, result)


def test_assessment_snapshots_original_evidence_before_later_mutation() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p,
        (score(),),
        selected_at=p.validation.end_at,
    )
    result = result_for(selected)
    assessment = bind_held_out_test(p, selected, result)

    object.__setattr__(selected, "selected_at", p.test.start_at + timedelta(days=2))
    object.__setattr__(result, "evaluated_at", p.test.start_at)
    object.__setattr__(p.validation, "end_at", p.test.start_at + timedelta(days=1))

    assert assessment.require_promotion_metric() == Decimal("0.5")


def test_mutating_bound_selection_chronology_cannot_change_promotion_authority() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p,
        (score(),),
        selected_at=p.validation.end_at,
    )
    assessment = bind_held_out_test(p, selected, result_for(selected))
    object.__setattr__(
        assessment.selection,
        "selected_at",
        assessment.protocol.test.start_at + timedelta(seconds=1),
    )
    with pytest.raises(TradingResearchError, match="changed after binding"):
        assessment.require_promotion_metric()


def test_mutating_bound_result_chronology_cannot_change_promotion_authority() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p,
        (score(),),
        selected_at=p.validation.end_at,
    )
    assessment = bind_held_out_test(p, selected, result_for(selected))
    object.__setattr__(
        assessment.test_result,
        "evaluated_at",
        assessment.protocol.test.end_at - timedelta(seconds=1),
    )
    with pytest.raises(TradingResearchError, match="changed after binding"):
        assessment.require_promotion_metric()


def test_mutating_bound_metric_value_or_identity_breaks_authority_seal() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p,
        (score(),),
        selected_at=p.validation.end_at,
    )
    assessment = bind_held_out_test(p, selected, result_for(selected))
    object.__setattr__(assessment.test_result, "metric_value", Decimal(999))
    with pytest.raises(TradingResearchError):
        assessment.require_promotion_metric()


def test_mutating_bound_strategy_artifact_breaks_immutable_identity() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p,
        (score(),),
        selected_at=p.validation.end_at,
    )
    assessment = bind_held_out_test(p, selected, result_for(selected))
    object.__setattr__(
        assessment.selection.strategy_artifact,
        "config_sha256",
        "9" * 64,
    )
    with pytest.raises(TradingResearchError, match="strategy artifact identity changed"):
        assessment.require_promotion_metric()


def test_protocol_fingerprint_includes_refit_policy_and_rejects_mutation() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p,
        (score(),),
        selected_at=p.validation.end_at,
    )
    assessment = bind_held_out_test(p, selected, result_for(selected))
    original = assessment.protocol.fingerprint
    object.__setattr__(
        assessment.protocol,
        "refit_policy",
        RefitPolicy.REFIT_TRAIN_VALIDATION,
    )
    assert assessment.protocol.fingerprint != original
    with pytest.raises(TradingResearchError):
        assessment.require_promotion_metric()


def test_quality_fingerprint_binds_exact_validation_issue_evidence() -> None:
    first = ValidationReport(
        duplicates=(
            ValidationIssue(
                "duplicate",
                "same event",
                (1, 2),
            ),
        ),
    )
    second = ValidationReport(
        duplicates=(
            ValidationIssue(
                "duplicate",
                "different event",
                (1, 2),
            ),
        ),
    )
    first_quality = ReplayDataQuality.from_report(first)
    second_quality = ReplayDataQuality.from_report(second)
    assert first_quality.duplicate_count == second_quality.duplicate_count == 1
    assert first_quality.evidence_sha256 != second_quality.evidence_sha256


def test_canonical_strategy_and_digest_fields_fail_closed() -> None:
    with pytest.raises(TradingResearchError, match="canonical non-empty identity"):
        StrategyArtifactFingerprint(
            " chosen",
            "v1",
            ALGORITHM,
            CONFIG,
            FEATURES,
            FITTED,
            7,
            BASE + timedelta(days=9),
            BASE + timedelta(days=9),
        )
    with pytest.raises(TradingResearchError, match="lowercase SHA-256"):
        StrategyArtifactFingerprint(
            "chosen",
            "v1",
            "A" * 64,
            CONFIG,
            FEATURES,
            FITTED,
            7,
            BASE + timedelta(days=9),
            BASE + timedelta(days=9),
        )
    with pytest.raises(TradingResearchError, match="non-negative integer"):
        StrategyArtifactFingerprint(
            "chosen",
            "v1",
            ALGORITHM,
            CONFIG,
            FEATURES,
            FITTED,
            True,  # type: ignore[arg-type]
            BASE + timedelta(days=9),
            BASE + timedelta(days=9),
        )


def test_strategy_definition_change_is_not_hidden_by_same_strategy_id() -> None:
    p = protocol()
    selected = select_validation_candidate(
        p,
        (score(),),
        selected_at=p.validation.end_at,
    )
    changed_config = StrategyArtifactFingerprint(
        "chosen",
        "v1",
        ALGORITHM,
        "9" * 64,
        FEATURES,
        FITTED,
        7,
        BASE + timedelta(days=9),
        BASE + timedelta(days=9),
    )
    evidence = total_return_evidence(BASE, "0.5")
    with pytest.raises(CausalityViolation, match="strategy definition"):
        bind_held_out_test(
            p,
            selected,
            PartitionResult(
                changed_config,
                Partition.TEST,
                evidence.metric_name,
                evidence.definition_sha256,
                evidence.value,
                selected.dataset_semantic_hash,
                CLEAN,
                selected.universe_fingerprint,
                selected.universe_cutoff_at,
                p.test.end_at,
                metric_evidence=evidence,
            ),
        )


def test_dev26_evaluation_modules_have_no_broker_network_or_real_money_route() -> None:
    allowed_import_roots = {
        "__future__",
        "ast",
        "collections",
        "contracts",
        "dataclasses",
        "dataset",
        "datetime",
        "decimal",
        "enum",
        "hashlib",
        "itertools",
        "json",
        "metric_evidence",
        "metrics",
        "nika_core",
    }
    for module in (heldout, metric_evidence, metrics):
        source = inspect.getsource(module)
        tree = ast.parse(source)
        imported_roots = {
            node.module.split(".", maxsplit=1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported_roots.update(
            alias.name.split(".", maxsplit=1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert imported_roots <= allowed_import_roots
        banned = {"requests", "httpx", "socket", "broker", "place_order", "fund_account"}
        assert not (imported_roots & banned)
