from __future__ import annotations

from dataclasses import dataclass

from .contracts import DenseDecoderConfig, ScalingTarget, estimate_dense_decoder_parameters


PILOT_20M = ScalingTarget(target_id="pilot-20m", target_parameters=20_000_000)
CORE_100M = ScalingTarget(target_id="core-100m", target_parameters=100_000_000)
SCALE_1B = ScalingTarget(target_id="scale-1b", target_parameters=1_000_000_000)


@dataclass(frozen=True, slots=True)
class ScalingLadderEntry:
    target: ScalingTarget
    model: DenseDecoderConfig

    @property
    def estimated_parameters(self) -> int:
        return estimate_dense_decoder_parameters(self.model).total

    def __post_init__(self) -> None:
        estimate = estimate_dense_decoder_parameters(self.model).total
        if not self.target.accepts(estimate):
            raise ValueError(
                f"{self.target.target_id} architecture has {estimate} parameters, "
                "outside target tolerance"
            )


def default_dense_scaling_ladder(
    *,
    vocab_size: int = 32_000,
    max_sequence_length: int = 2048,
) -> tuple[ScalingLadderEntry, ...]:
    """Return the reviewed 20M -> 100M -> 1B architecture ladder.

    The checked-in defaults intentionally share one tokenizer vocabulary across stages.
    A non-default vocabulary changes parameter counts and therefore requires a fresh
    architecture search rather than silently pretending the targets still match.
    """

    if vocab_size != 32_000:
        raise ValueError(
            "the reviewed ladder is calibrated for vocab_size=32000; "
            "derive and review a new ladder for another tokenizer"
        )

    def model(
        hidden_size: int,
        num_layers: int,
        num_attention_heads: int,
        num_key_value_heads: int,
        intermediate_size: int,
    ) -> DenseDecoderConfig:
        return DenseDecoderConfig(
            vocab_size=vocab_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            num_attention_heads=num_attention_heads,
            num_key_value_heads=num_key_value_heads,
            intermediate_size=intermediate_size,
            max_sequence_length=max_sequence_length,
        )

    return (
        ScalingLadderEntry(
            target=PILOT_20M,
            model=model(320, 9, 8, 4, 832),
        ),
        ScalingLadderEntry(
            target=CORE_100M,
            model=model(768, 12, 12, 4, 2048),
        ),
        ScalingLadderEntry(
            target=SCALE_1B,
            model=model(1920, 24, 24, 8, 5120),
        ),
    )
