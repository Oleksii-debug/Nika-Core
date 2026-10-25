from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from nika_core.product_search import (
    ProductAvailability,
    ProductObservation,
    ProductSearchCodec,
    ProductSearchCriteria,
    ProductSearchError,
    ProductSearchService,
    ProductSort,
)
from nika_core.research.models import (
    FreshnessState,
    ResearchEvidence,
    ResearchResultItem,
    ResearchResultSet,
    SourceKind,
)


def _evidence(source_id: str, locator: str) -> ResearchEvidence:
    return ResearchEvidence(
        source_id=source_id,
        source_kind=SourceKind.HTTP,
        locator=locator,
        observed_at="2026-08-26T20:00:00+00:00",
        freshness=FreshnessState.CURRENT,
    )


def _result_set() -> ResearchResultSet:
    first_evidence = _evidence("shop-a", "https://shop-a.example/item/1")
    second_evidence = _evidence("shop-b", "https://shop-b.example/item/2")
    return ResearchResultSet(
        result_set_id="rs-1",
        workspace_id="product-search",
        query="usb microphone",
        created_at="2026-08-26T20:01:00+00:00",
        items=(
            ResearchResultItem(
                ordinal=1,
                document_id="doc-1",
                title="Microphone A",
                snippet="USB microphone with stand",
                rank=-3.0,
                why_matched="Literal-token full-text match for: usb microphone",
                evidence=(first_evidence,),
            ),
            ResearchResultItem(
                ordinal=2,
                document_id="doc-2",
                title="Microphone B",
                snippet="Compact USB microphone",
                rank=-2.0,
                why_matched="Literal-token full-text match for: usb microphone",
                evidence=(second_evidence,),
            ),
        ),
    )


def _observation(
    *,
    product_id: str,
    document_id: str,
    source_id: str,
    locator: str,
    price: str,
    seller: str,
    availability: ProductAvailability = ProductAvailability.IN_STOCK,
) -> ProductObservation:
    return ProductObservation(
        product_id=product_id,
        document_id=document_id,
        source_id=source_id,
        locator=locator,
        observed_at="2026-08-26T20:00:00+00:00",
        seller=seller,
        price_amount=Decimal(price),
        currency="uah",
        availability=availability,
    )


def test_projection_preserves_exact_research_evidence_and_relevance_order() -> None:
    result = ProductSearchService().project(
        result_set=_result_set(),
        observations=(
            _observation(
                product_id="sku-b",
                document_id="doc-2",
                source_id="shop-b",
                locator="https://shop-b.example/item/2",
                price="1200.00",
                seller="Shop B",
            ),
            _observation(
                product_id="sku-a",
                document_id="doc-1",
                source_id="shop-a",
                locator="https://shop-a.example/item/1",
                price="1500.00",
                seller="Shop A",
            ),
        ),
    )

    assert [card.product_id for card in result.cards] == ["sku-a", "sku-b"]
    assert result.cards[0].currency == "UAH"
    assert result.cards[0].evidence[0].source_id == "shop-a"
    assert result.cards[0].why_matched.startswith("Literal-token")


def test_projection_rejects_observation_without_exact_provenance() -> None:
    observation = _observation(
        product_id="sku-a",
        document_id="doc-1",
        source_id="shop-a",
        locator="https://attacker.example/item/1",
        price="1500.00",
        seller="Shop A",
    )

    with pytest.raises(
        ProductSearchError,
        match="not bound to exact research evidence",
    ):
        ProductSearchService().project(
            result_set=_result_set(),
            observations=(observation,),
        )


def test_price_filter_sort_and_availability_are_deterministic() -> None:
    result = ProductSearchService().project(
        result_set=_result_set(),
        observations=(
            _observation(
                product_id="sku-a",
                document_id="doc-1",
                source_id="shop-a",
                locator="https://shop-a.example/item/1",
                price="1500.00",
                seller="Shop A",
            ),
            _observation(
                product_id="sku-b",
                document_id="doc-2",
                source_id="shop-b",
                locator="https://shop-b.example/item/2",
                price="1200.00",
                seller="Shop B",
            ),
        ),
        criteria=ProductSearchCriteria(
            currency="uah",
            min_price=Decimal("1000"),
            max_price=Decimal("1600"),
            require_available=True,
            sort=ProductSort.PRICE_LOW_TO_HIGH,
        ),
    )

    assert [card.product_id for card in result.cards] == ["sku-b", "sku-a"]


def test_price_rules_fail_closed_without_explicit_currency() -> None:
    with pytest.raises(ProductSearchError, match="currency is required"):
        ProductSearchService().project(
            result_set=_result_set(),
            observations=(),
            criteria=ProductSearchCriteria(max_price=Decimal("2000")),
        )


def test_duplicate_product_identity_is_rejected() -> None:
    observation = _observation(
        product_id="same-sku",
        document_id="doc-1",
        source_id="shop-a",
        locator="https://shop-a.example/item/1",
        price="1500.00",
        seller="Shop A",
    )
    duplicate = replace(
        observation,
        document_id="doc-2",
        source_id="shop-b",
        locator="https://shop-b.example/item/2",
    )

    with pytest.raises(ProductSearchError, match="duplicate product_id"):
        ProductSearchService().project(
            result_set=_result_set(),
            observations=(observation, duplicate),
        )


def test_codec_is_deterministic_round_trip_and_rejects_future_schema() -> None:
    result = ProductSearchService().project(
        result_set=_result_set(),
        observations=(
            _observation(
                product_id="sku-a",
                document_id="doc-1",
                source_id="shop-a",
                locator="https://shop-a.example/item/1",
                price="1500.00",
                seller="Shop A",
            ),
        ),
        criteria=ProductSearchCriteria(currency="uah"),
    )

    encoded = ProductSearchCodec.dumps(result)
    restored = ProductSearchCodec.loads(encoded)

    assert restored == result
    assert ProductSearchCodec.dumps(restored) == encoded

    future = encoded.replace('"schema_version":1', '"schema_version":2')
    with pytest.raises(ProductSearchError, match="unsupported product search schema_version"):
        ProductSearchCodec.loads(future)


def test_runtime_types_fail_closed_instead_of_leaking_attribute_errors() -> None:
    bad_observation = replace(
        _observation(
            product_id="sku-a",
            document_id="doc-1",
            source_id="shop-a",
            locator="https://shop-a.example/item/1",
            price="1500.00",
            seller="Shop A",
        ),
        availability="in_stock",  # type: ignore[arg-type]
    )
    with pytest.raises(ProductSearchError, match="ProductAvailability"):
        ProductSearchService().project(
            result_set=_result_set(),
            observations=(bad_observation,),
        )

    with pytest.raises(ProductSearchError, match="limit must be an integer"):
        ProductSearchService().project(
            result_set=_result_set(),
            observations=(),
            criteria=ProductSearchCriteria(limit=True),  # type: ignore[arg-type]
        )

    bad_price = replace(
        _observation(
            product_id="sku-a",
            document_id="doc-1",
            source_id="shop-a",
            locator="https://shop-a.example/item/1",
            price="1500.00",
            seller="Shop A",
        ),
        price_amount=1500.0,  # type: ignore[arg-type]
    )
    with pytest.raises(ProductSearchError, match="price_amount must be a Decimal"):
        ProductSearchService().project(
            result_set=_result_set(),
            observations=(bad_price,),
        )


def test_accessible_text_contains_structured_fields_without_visual_dependency() -> None:
    result = ProductSearchService().project(
        result_set=_result_set(),
        observations=(
            _observation(
                product_id="sku-a",
                document_id="doc-1",
                source_id="shop-a",
                locator="https://shop-a.example/item/1",
                price="1500.00",
                seller="Shop A",
            ),
        ),
    )

    rendered = ProductSearchService.render_text(result)

    assert "Product search results" in rendered
    assert "Seller: Shop A" in rendered
    assert "Price: 1500.00 UAH" in rendered
    assert "Source: https://shop-a.example/item/1" in rendered
