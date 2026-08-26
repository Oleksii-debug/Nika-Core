from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
import re


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class TrainingBackend(StrEnum):
    """Supported external training backends for Nika model-development runs."""

    OLMO_CORE = "olmo-core"
    TORCHTITAN = "torchtitan"


@dataclass(frozen=True, slots=True)
class DenseDecoderConfig:
    """Bias-free Llama-style dense decoder architecture used for deterministic planning."""

    vocab_size: int
    hidden_size: int
    num_layers: int
    num_attention_heads: int
    num_key_value_heads: int
    intermediate_size: int
    max_sequence_length: int = 2048
    tie_embeddings: bool = True

    def __post_init__(self) -> None:
        for value, name in (
            (self.vocab_size, "vocab_size"),
            (self.hidden_size, "hidden_size"),
            (self.num_layers, "num_layers"),
            (self.num_attention_heads, "num_attention_heads"),
            (self.num_key_value_heads, "num_key_value_heads"),
            (self.intermediate_size, "intermediate_size"),
            (self.max_sequence_length, "max_sequence_length"),
        ):
            if value < 1:
                raise ValueError(f"{name} must be positive")
        if self.hidden_size % self.num_attention_heads:
            raise ValueError("hidden_size must be divisible by num_attention_heads")
        if self.num_key_value_heads > self.num_attention_heads:
            raise ValueError("num_key_value_heads may not exceed num_attention_heads")
        if self.num_attention_heads % self.num_key_value_heads:
            raise ValueError("num_attention_heads must be divisible by num_key_value_heads")
        if self.intermediate_size < self.hidden_size:
            raise ValueError("intermediate_size must be at least hidden_size")


@dataclass(frozen=True, slots=True)
class DecoderParameterEstimate:
    embeddings: int
    attention: int
    mlp: int
    norms: int
    output_head: int

    @property
    def total(self) -> int:
        return self.embeddings + self.attention + self.mlp + self.norms + self.output_head


@dataclass(frozen=True, slots=True)
class ScalingTarget:
    target_id: str
    target_parameters: int
    tolerance_fraction: float = 0.05
    compute_optimal_reference_tokens_per_parameter: float = 20.0

    def __post_init__(self) -> None:
        if not self.target_id.strip():
            raise ValueError("target_id must not be empty")
        if self.target_parameters < 1:
            raise ValueError("target_parameters must be positive")
        if not isfinite(self.tolerance_fraction) or not 0 <= self.tolerance_fraction < 1:
            raise ValueError("tolerance_fraction must be finite and in [0, 1)")
        ratio = self.compute_optimal_reference_tokens_per_parameter
        if not isfinite(ratio) or ratio <= 0:
            raise ValueError("compute_optimal_reference_tokens_per_parameter must be positive")

    @property
    def reference_tokens(self) -> int:
        return round(
            self.target_parameters * self.compute_optimal_reference_tokens_per_parameter
        )

    def accepts(self, parameter_count: int) -> bool:
        delta = abs(parameter_count - self.target_parameters)
        return delta <= self.target_parameters * self.tolerance_fraction


@dataclass(frozen=True, slots=True)
class TokenizerManifest:
    tokenizer_id: str
    version: str
    vocab_size: int
    sha256: str
    license_id: str

    def __post_init__(self) -> None:
        if not self.tokenizer_id.strip() or not self.version.strip():
            raise ValueError("tokenizer identity must be complete")
        if self.vocab_size < 1:
            raise ValueError("tokenizer vocab_size must be positive")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("tokenizer sha256 must be a lowercase 64-character hex digest")
        if not self.license_id.strip():
            raise ValueError("tokenizer license_id must not be empty")


@dataclass(frozen=True, slots=True)
class DatasetSourceManifest:
    source_id: str
    version: str
    source_uri: str
    sha256: str
    license_id: str
    token_count: int
    training_permitted: bool

    def __post_init__(self) -> None:
        for value, name in (
            (self.source_id, "source_id"),
            (self.version, "version"),
            (self.source_uri, "source_uri"),
            (self.license_id, "license_id"),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("dataset sha256 must be a lowercase 64-character hex digest")
        if self.token_count < 1:
            raise ValueError("dataset token_count must be positive")


@dataclass(frozen=True, slots=True)
class TrainingCorpusManifest:
    corpus_id: str
    version: str
    sources: tuple[DatasetSourceManifest, ...]

    def __post_init__(self) -> None:
        if not self.corpus_id.strip() or not self.version.strip():
            raise ValueError("corpus identity must be complete")
        if not self.sources:
            raise ValueError("at least one dataset source is required")
        identities = [(source.source_id, source.version) for source in self.sources]
        if len(identities) != len(set(identities)):
            raise ValueError("dataset source identities must be unique")

    @property
    def unique_token_count(self) -> int:
        return sum(source.token_count for source in self.sources)


@dataclass(frozen=True, slots=True)
class ModelTrainingPlan:
    run_id: str
    target: ScalingTarget
    model: DenseDecoderConfig
    tokenizer: TokenizerManifest
    corpus: TrainingCorpusManifest
    training_token_budget: int
    code_revision: str
    backend: TrainingBackend = TrainingBackend.OLMO_CORE
    random_seed: int = 42
    allow_dataset_repetition: bool = False

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if self.training_token_budget < 1:
            raise ValueError("training_token_budget must be positive")
        if not _GIT_SHA_RE.fullmatch(self.code_revision):
            raise ValueError("code_revision must be an exact lowercase 40-character git SHA")
        if self.random_seed < 0:
            raise ValueError("random_seed must be non-negative")
        if self.model.vocab_size != self.tokenizer.vocab_size:
            raise ValueError("model and tokenizer vocab sizes must match")

    @property
    def parameter_estimate(self) -> DecoderParameterEstimate:
        return estimate_dense_decoder_parameters(self.model)

    @property
    def estimated_training_flops(self) -> int:
        # Common dense-transformer planning approximation; evidence, not billing truth.
        return 6 * self.parameter_estimate.total * self.training_token_budget

    def readiness_blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        if not self.target.accepts(self.parameter_estimate.total):
            blockers.append("model parameter estimate is outside the target tolerance")
        denied = [
            f"{source.source_id}@{source.version}"
            for source in self.corpus.sources
            if not source.training_permitted
        ]
        if denied:
            blockers.append("training permission is not recorded for: " + ", ".join(denied))
        if (
            self.training_token_budget > self.corpus.unique_token_count
            and not self.allow_dataset_repetition
        ):
            blockers.append(
                "training token budget exceeds unique corpus tokens and repetition is disabled"
            )
        return tuple(blockers)

    @property
    def ready(self) -> bool:
        return not self.readiness_blockers()


def estimate_dense_decoder_parameters(config: DenseDecoderConfig) -> DecoderParameterEstimate:
    """Estimate trainable parameters for a bias-free RMSNorm/SwiGLU/GQA decoder."""

    head_size = config.hidden_size // config.num_attention_heads
    kv_width = config.num_key_value_heads * head_size

    embeddings = config.vocab_size * config.hidden_size
    attention_per_layer = (
        config.hidden_size * config.hidden_size
        + 2 * config.hidden_size * kv_width
        + config.hidden_size * config.hidden_size
    )
    mlp_per_layer = 3 * config.hidden_size * config.intermediate_size
    norms_per_layer = 2 * config.hidden_size

    output_head = 0 if config.tie_embeddings else embeddings
    return DecoderParameterEstimate(
        embeddings=embeddings,
        attention=config.num_layers * attention_per_layer,
        mlp=config.num_layers * mlp_per_layer,
        norms=config.num_layers * norms_per_layer + config.hidden_size,
        output_head=output_head,
    )
