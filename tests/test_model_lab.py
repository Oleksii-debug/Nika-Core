from __future__ import annotations

import pytest

from nika_core.model_lab import (
    CORE_100M,
    DatasetSourceManifest,
    default_dense_scaling_ladder,
    DenseDecoderConfig,
    estimate_dense_decoder_parameters,
    ModelTrainingPlan,
    PILOT_20M,
    SCALE_1B,
    TokenizerManifest,
    TrainingCorpusManifest,
)


SHA = "a" * 64
REVISION = "b" * 40


def test_default_scaling_ladder_is_inside_declared_targets() -> None:
    ladder = default_dense_scaling_ladder()

    assert [entry.target for entry in ladder] == [PILOT_20M, CORE_100M, SCALE_1B]
    assert [entry.estimated_parameters for entry in ladder] == [
        20_199_360,
        100_092_672,
        1_005_252_480,
    ]
    assert all(entry.target.accepts(entry.estimated_parameters) for entry in ladder)


def test_parameter_estimate_accounts_for_untied_output_head() -> None:
    tied = DenseDecoderConfig(
        vocab_size=1000,
        hidden_size=256,
        num_layers=4,
        num_attention_heads=8,
        num_key_value_heads=2,
        intermediate_size=704,
    )
    untied = DenseDecoderConfig(
        vocab_size=1000,
        hidden_size=256,
        num_layers=4,
        num_attention_heads=8,
        num_key_value_heads=2,
        intermediate_size=704,
        tie_embeddings=False,
    )

    assert (
        estimate_dense_decoder_parameters(untied).total
        - estimate_dense_decoder_parameters(tied).total
        == 256_000
    )


def test_model_training_plan_fails_closed_on_data_permission_and_repetition() -> None:
    entry = default_dense_scaling_ladder()[0]
    tokenizer = TokenizerManifest(
        tokenizer_id="nika-tokenizer",
        version="v1",
        vocab_size=32_000,
        sha256=SHA,
        license_id="Apache-2.0",
    )
    corpus = TrainingCorpusManifest(
        corpus_id="nika-pilot",
        version="2026-08-26",
        sources=(
            DatasetSourceManifest(
                source_id="licensed",
                version="v1",
                source_uri="dataset://licensed/v1",
                sha256=SHA,
                license_id="CC-BY-4.0",
                token_count=100_000_000,
                training_permitted=True,
            ),
            DatasetSourceManifest(
                source_id="unreviewed",
                version="v2",
                source_uri="dataset://unreviewed/v2",
                sha256=SHA,
                license_id="unknown",
                token_count=50_000_000,
                training_permitted=False,
            ),
        ),
    )

    plan = ModelTrainingPlan(
        run_id="pilot-001",
        target=entry.target,
        model=entry.model,
        tokenizer=tokenizer,
        corpus=corpus,
        training_token_budget=400_000_000,
        code_revision=REVISION,
    )

    assert not plan.ready
    assert plan.readiness_blockers() == (
        "training permission is not recorded for: unreviewed@v2",
        "training token budget exceeds unique corpus tokens and repetition is disabled",
    )


def test_model_training_plan_requires_exact_code_revision() -> None:
    entry = default_dense_scaling_ladder()[0]
    tokenizer = TokenizerManifest(
        tokenizer_id="nika-tokenizer",
        version="v1",
        vocab_size=32_000,
        sha256=SHA,
        license_id="Apache-2.0",
    )
    corpus = TrainingCorpusManifest(
        corpus_id="nika-pilot",
        version="v1",
        sources=(
            DatasetSourceManifest(
                source_id="source",
                version="v1",
                source_uri="dataset://source/v1",
                sha256=SHA,
                license_id="CC-BY-4.0",
                token_count=500_000_000,
                training_permitted=True,
            ),
        ),
    )

    with pytest.raises(ValueError, match="exact lowercase 40-character git SHA"):
        ModelTrainingPlan(
            run_id="pilot-001",
            target=entry.target,
            model=entry.model,
            tokenizer=tokenizer,
            corpus=corpus,
            training_token_budget=400_000_000,
            code_revision="main",
        )


def test_dense_decoder_config_rejects_invalid_gqa_shape() -> None:
    with pytest.raises(ValueError, match="divisible by num_key_value_heads"):
        DenseDecoderConfig(
            vocab_size=32_000,
            hidden_size=768,
            num_layers=12,
            num_attention_heads=12,
            num_key_value_heads=5,
            intermediate_size=2048,
        )
