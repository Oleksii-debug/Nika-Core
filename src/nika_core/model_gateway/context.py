from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum


class ContextTruncationReason(StrEnum):
    ITEM_LIMIT = "item_limit"
    TEXT_LIMIT = "text_limit"
    TOKEN_LIMIT = "token_limit"


@dataclass(frozen=True, slots=True)
class ModelContextItem:
    """One already-ranked retrieved context item.

    Ranking and authorization belong to the retrieval owner. This contract only
    carries the stable order into bounded model-context assembly.
    """

    item_id: str
    text: str

    def __post_init__(self) -> None:
        if not self.item_id.strip():
            raise ValueError("context item_id must not be empty")
        if not self.text.strip():
            raise ValueError("context text must not be empty")


@dataclass(frozen=True, slots=True)
class ModelContextBounds:
    """Provider-neutral limits for retrieved context only.

    ``max_text_chars`` is an exact Python text-character bound on the rendered
    retrieved-context section. It is not a token approximation. A token limit is
    enforceable only when a caller supplies a truthful estimator for its route.
    """

    max_items: int
    max_text_chars: int
    max_estimated_tokens: int | None = None

    def __post_init__(self) -> None:
        _positive_int("max_items", self.max_items)
        _positive_int("max_text_chars", self.max_text_chars)
        if self.max_estimated_tokens is not None:
            _positive_int("max_estimated_tokens", self.max_estimated_tokens)


@dataclass(frozen=True, slots=True)
class ModelContextEvidence:
    input_items: int
    selected_items: int
    omitted_items: int
    selected_text_chars: int
    max_items: int
    max_text_chars: int
    estimated_tokens: int | None
    max_estimated_tokens: int | None
    stop_reason: ContextTruncationReason | None

    @property
    def truncated(self) -> bool:
        return self.omitted_items > 0

    def to_metadata(self) -> dict[str, str]:
        """Return privacy-safe machine-readable evidence for ModelRequest metadata."""
        return {
            "model_context.input_items": str(self.input_items),
            "model_context.selected_items": str(self.selected_items),
            "model_context.omitted_items": str(self.omitted_items),
            "model_context.selected_text_chars": str(self.selected_text_chars),
            "model_context.max_items": str(self.max_items),
            "model_context.max_text_chars": str(self.max_text_chars),
            "model_context.estimated_tokens": (
                "unavailable" if self.estimated_tokens is None else str(self.estimated_tokens)
            ),
            "model_context.max_estimated_tokens": (
                "unbounded"
                if self.max_estimated_tokens is None
                else str(self.max_estimated_tokens)
            ),
            "model_context.truncated": str(self.truncated).lower(),
            "model_context.stop_reason": (
                "none" if self.stop_reason is None else self.stop_reason.value
            ),
        }


@dataclass(frozen=True, slots=True)
class ModelContextAssembly:
    text: str
    selected: tuple[ModelContextItem, ...]
    evidence: ModelContextEvidence


TokenEstimator = Callable[[str], int]


def assemble_model_context(
    ranked_items: Sequence[ModelContextItem],
    *,
    bounds: ModelContextBounds,
    token_estimator: TokenEstimator | None = None,
) -> ModelContextAssembly:
    """Select a deterministic ranked prefix within explicit retrieved-context bounds.

    Whole items are selected in the caller-supplied ranking order. If the next
    higher-ranked whole item would exceed a bound, assembly stops instead of
    skipping it for lower-ranked items or silently slicing its text. This function
    never receives or edits system/instruction messages.
    """
    if bounds.max_estimated_tokens is not None and token_estimator is None:
        raise ValueError("max_estimated_tokens requires a token_estimator")

    input_items = len(ranked_items)
    selected: list[ModelContextItem] = []
    rendered: list[str] = []
    selected_text_chars = 0
    stop_reason: ContextTruncationReason | None = None

    for item in ranked_items:
        if not isinstance(item, ModelContextItem):
            raise TypeError("ranked_items must contain ModelContextItem values")
        if len(selected) >= bounds.max_items:
            stop_reason = ContextTruncationReason.ITEM_LIMIT
            break

        fragment = _render_item(len(selected) + 1, item)
        candidate_chars = selected_text_chars + (2 if rendered else 0) + len(fragment)
        if candidate_chars > bounds.max_text_chars:
            stop_reason = ContextTruncationReason.TEXT_LIMIT
            break

        if bounds.max_estimated_tokens is not None:
            candidate = "\n\n".join((*rendered, fragment))
            estimate = _estimate_tokens(token_estimator, candidate)
            if estimate > bounds.max_estimated_tokens:
                stop_reason = ContextTruncationReason.TOKEN_LIMIT
                break

        selected.append(item)
        rendered.append(fragment)
        selected_text_chars = candidate_chars

    text = "\n\n".join(rendered)
    estimated_tokens = (
        None if token_estimator is None else _estimate_tokens(token_estimator, text)
    )
    evidence = ModelContextEvidence(
        input_items=input_items,
        selected_items=len(selected),
        omitted_items=input_items - len(selected),
        selected_text_chars=selected_text_chars,
        max_items=bounds.max_items,
        max_text_chars=bounds.max_text_chars,
        estimated_tokens=estimated_tokens,
        max_estimated_tokens=bounds.max_estimated_tokens,
        stop_reason=stop_reason,
    )
    return ModelContextAssembly(text=text, selected=tuple(selected), evidence=evidence)


def _render_item(index: int, item: ModelContextItem) -> str:
    return f"[retrieved-context {index}]\n{item.text}"


def _estimate_tokens(estimator: TokenEstimator | None, text: str) -> int:
    if estimator is None:
        raise RuntimeError("token estimator is unavailable")
    value = estimator(text)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("token_estimator must return a non-negative integer")
    return value


def _positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
