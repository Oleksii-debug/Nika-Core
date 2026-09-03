from __future__ import annotations

from decimal import Decimal

import pytest

from nika_core.product_search import (
    ProductAvailability,
    ProductChangeKind,
    ProductDeltaService,
    ProductObservation,
    ProductSearchCriteria,
    ProductSearchError,
    ProductSearchService,
)
from nika_core.research.models import (
    FreshnessState,
    ResearchEvidence,
    ResearchResultItem,
    ResearchResultSet,
    SourceKind,
)


def _evidence(source_id: str, locator: str, observed_at: str) -> ResearchEvidence:
    return ResearchEvidence(
        source_id=source_id,
        source_kind=SourceKind.HTTP,
        locator=locator,
        observed_at=observed_at,
        freshness=FreshnessState.CURRENT,
    )


def _result_set(
    *,
    result_set_id: str,
    observed_at: str,
    first_rank: float = -3.0,
    include_second: bool = False,
) -> ResearchResultSet:
    items = [
        ResearchResultItem(
            ordinal=1,
            document_id="doc-1",
            title="Microphone A",
            snippet="USB microphone with stand",
            rank=first_rank,
            why_matched="Literal-token full-text match for: usb microphone",
            evidence=(
                _evidence(
                    "shop-a",
                    "https://shop-a.example/item/1",
                    observed_at,
                ),
            ),
        )
    ]
    if include_second:
        items.append(
            ResearchResultItem(
                ordinal=2,
                document_id="doc-2",
                title="Microphone B",
                snippet="Compact USB microphone",
                rank=-2.0,
                why_matched="Literal-token full-text match for: usb microphone",
                evidence=(
                    _evidence(
                        "shop-b",
                        "https://shop-b.example/item/2",
                        observed_at,
                    ),
                ),
            )
        )
    return ResearchResultSet(
        result_set_id=result_set_id,
        workspace_id="product-search",
        query="usb microphone",
        items=tuple(items),
        created_at=observed_at,
    )


def _observation(
    *,
    product_id: str,
    document_id: str,
    source_id: str,
    locator: str,
    observed_at: str,
    price: str,
    seller: str,
) -> ProductObservation:
    return ProductObservation(
        product_id=product_id,
        document_id=document_id,
        source_id=source_id,
        locator=locator,
        observed_at=observed_at,
        seller=seller,
        price_amount=Decimal(price),
        currency="UAH",
        availability=ProductAvailability.IN_STOCK,
    )


def _project(
    *,
    result_set_id: str,
    observed_at: str,
    first_price: str = "1500.00",
    first_rank: float = -3.0,
    include_second: bool = False,
    criteria: ProductSearchCriteria = ProductSearchCriteria(),
):
    observations = [
        _observation(
            product_id="sku-a",
            document_id="doc-1",
            source_id="shop-a",
            locator="https://shop-a.example/item/1",
            observed_at=observed_at,
            price=first_price,
            seller="Shop A",
        )
    ]
    if include_second:
        observations.append(
            _observation(
                product_id="sku-b",
                document_id="doc-2",
                source_id="shop-b",
                locator="https://shop-b.example/item/2",
                observed_at=observed_at,
                price="1200.00",
                seller="Shop B",
            )
        )
    return ProductSearchService().project(
        result_set=_result_set(
            result_set_id=result_set_id,
            observed_at=observed_at,
            first_rank=first_rank,
            include_second=include_second,
        ),
        observations=tuple(observations),
        criteria=criteria,
    )


def test_delta_reports_new_and_materially_changed_products() -> None:
    previous = _project(
        result_set_id="rs-old",
        observed_at="2026-08-26T20:00:00+00:00",
    )
    current = _project(
        result_set_id="rs-new",
        observed_at="2026-08-27T20:00:00+00:00",
        first_price="1400.00",
        include_second=True,
    )

    delta = ProductDeltaService.compare(previous=previous, current=current)

    assert [change.kind for change in delta.changes] == [
        ProductChangeKind.NEW,
        ProductChangeKind.CHANGED,
    ]
    assert [change.product_id for change in delta.changes] == ["sku-b", "sku-a"]
    assert delta.changes[1].changed_fields == ("price_amount",)
    rendered = ProductDeltaService.render_text(delta)
    assert "Previous price: 1500.00 UAH" in rendered
    assert "Current price: 1400.00 UAH" in rendered


def test_delta_ignores_rank_snippet_evidence_timestamp_and_observation_time_churn() -> None:
    previous = _project(
        result_set_id="rs-old",
        observed_at="2026-08-26T20:00:00+00:00",
        first_rank=-3.0,
    )
    current = _project(
        result_set_id="rs-new",
        observed_at="2026-08-27T20:00:00+00:00",
        first_rank=-9.0,
    )

    delta = ProductDeltaService.compare(previous=previous, current=current)

    assert delta.changes == ()


def test_delta_requires_same_query_workspace_and_criteria() -> None:
    previous = _project(
        result_set_id="rs-old",
        observed_at="2026-08-26T20:00:00+00:00",
    )
    current = _project(
        result_set_id="rs-new",
        observed_at="2026-08-27T20:00:00+00:00",
        criteria=ProductSearchCriteria(currency="UAH"),
    )

    with pytest.raises(ProductSearchError, match="different criteria"):
        ProductDeltaService.compare(previous=previous, current=current)
