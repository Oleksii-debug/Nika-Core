from __future__ import annotations

import pytest

from nika_core.model_gateway.context import (
    ContextTruncationReason,
    ModelContextBounds,
    ModelContextItem,
    assemble_model_context,
)


def test_huge_memory_and_corpus_context_is_item_bounded_and_keeps_ranked_prefix() -> None:
    items = tuple(
        ModelContextItem(
            item_id=f"{'memory' if index % 2 else 'corpus'}:{index:05d}",
            text=f"rank={index:05d} " + ("evidence " * 8),
        )
        for index in range(10_000)
    )

    result = assemble_model_context(
        items,
        bounds=ModelContextBounds(max_items=32, max_text_chars=1_000_000),
    )

    assert tuple(item.item_id for item in result.selected) == tuple(
        item.item_id for item in items[:32]
    )
    assert result.evidence.input_items == 10_000
    assert result.evidence.selected_items == 32
    assert result.evidence.omitted_items == 9_968
    assert result.evidence.stop_reason is ContextTruncationReason.ITEM_LIMIT
    assert result.evidence.selected_text_chars == len(result.text)
    assert result.evidence.estimated_tokens is None
    assert result.evidence.to_metadata()["model_context.estimated_tokens"] == "unavailable"


def test_text_bound_stops_at_oversized_higher_ranked_item_instead_of_skipping_it() -> None:
    first = ModelContextItem(item_id="rank-1", text="small first result")
    oversized = ModelContextItem(item_id="rank-2", text="x" * 500)
    lower_ranked = ModelContextItem(item_id="rank-3", text="small lower result")
    first_only = assemble_model_context(
        (first,),
        bounds=ModelContextBounds(max_items=3, max_text_chars=1_000),
    )
    budget_that_could_fit_two_small_items = len(first_only.text) + 80

    result = assemble_model_context(
        (first, oversized, lower_ranked),
        bounds=ModelContextBounds(
            max_items=3,
            max_text_chars=budget_that_could_fit_two_small_items,
        ),
    )

    assert result.selected == (first,)
    assert lower_ranked.text not in result.text
    assert result.evidence.stop_reason is ContextTruncationReason.TEXT_LIMIT
    assert result.evidence.truncated is True
    assert result.evidence.omitted_items == 2
    assert len(result.text) <= budget_that_could_fit_two_small_items


def test_injected_token_estimator_can_apply_a_separate_truthful_bound() -> None:
    first = ModelContextItem(item_id="rank-1", text="alpha")
    second = ModelContextItem(item_id="rank-2", text="bravo" * 20)

    result = assemble_model_context(
        (first, second),
        bounds=ModelContextBounds(
            max_items=2,
            max_text_chars=10_000,
            max_estimated_tokens=40,
        ),
        token_estimator=len,
    )

    assert result.selected == (first,)
    assert result.evidence.stop_reason is ContextTruncationReason.TOKEN_LIMIT
    assert result.evidence.estimated_tokens == len(result.text)
    assert result.evidence.max_estimated_tokens == 40
    assert result.evidence.to_metadata()["model_context.estimated_tokens"] == str(
        len(result.text)
    )


def test_token_limit_without_estimator_fails_closed_instead_of_inventing_counts() -> None:
    with pytest.raises(ValueError, match="requires a token_estimator"):
        assemble_model_context(
            (ModelContextItem(item_id="rank-1", text="evidence"),),
            bounds=ModelContextBounds(
                max_items=1,
                max_text_chars=100,
                max_estimated_tokens=10,
            ),
        )


def test_optional_estimator_reports_estimate_without_implying_a_token_limit() -> None:
    item = ModelContextItem(item_id="rank-1", text="evidence")

    result = assemble_model_context(
        (item,),
        bounds=ModelContextBounds(max_items=1, max_text_chars=100),
        token_estimator=len,
    )

    assert result.evidence.stop_reason is None
    assert result.evidence.truncated is False
    assert result.evidence.estimated_tokens == len(result.text)
    assert result.evidence.max_estimated_tokens is None


@pytest.mark.parametrize("invalid", [-1, True, 1.5, "7"])
def test_invalid_token_estimator_result_fails_closed(invalid: object) -> None:
    def estimator(_: str) -> object:
        return invalid

    with pytest.raises(ValueError, match="non-negative integer"):
        assemble_model_context(
            (ModelContextItem(item_id="rank-1", text="evidence"),),
            bounds=ModelContextBounds(max_items=1, max_text_chars=100),
            token_estimator=estimator,  # type: ignore[arg-type]
        )


def test_same_ranked_input_and_bounds_produce_identical_context_and_evidence() -> None:
    items = tuple(
        ModelContextItem(item_id=f"item-{index}", text=f"text-{index}")
        for index in range(100)
    )
    bounds = ModelContextBounds(max_items=17, max_text_chars=10_000)

    first = assemble_model_context(items, bounds=bounds)
    second = assemble_model_context(items, bounds=bounds)

    assert first == second
    assert tuple(item.item_id for item in first.selected) == tuple(
        f"item-{index}" for index in range(17)
    )
