from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from nika_core.research.models import (
    FreshnessState,
    ResearchEvidence,
    ResearchResultItem,
    ResearchResultSet,
    SourceKind,
)


class ProductSearchError(ValueError):
    """Raised when product projection cannot be proven from research evidence."""


class ProductAvailability(StrEnum):
    IN_STOCK = "in_stock"
    PREORDER = "preorder"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


class ProductSort(StrEnum):
    RELEVANCE = "relevance"
    PRICE_LOW_TO_HIGH = "price_low_to_high"
    PRICE_HIGH_TO_LOW = "price_high_to_low"


@dataclass(frozen=True, slots=True)
class ProductObservation:
    """Structured product facts extracted from one exact research evidence item."""

    product_id: str
    document_id: str
    source_id: str
    locator: str
    observed_at: str
    seller: str
    price_amount: Decimal | None = None
    currency: str | None = None
    availability: ProductAvailability = ProductAvailability.UNKNOWN


@dataclass(frozen=True, slots=True)
class ProductSearchCriteria:
    currency: str | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    require_available: bool = False
    seller_allowlist: tuple[str, ...] = ()
    sort: ProductSort = ProductSort.RELEVANCE
    limit: int = 20


@dataclass(frozen=True, slots=True)
class ProductCard:
    product_id: str
    document_id: str
    title: str
    snippet: str
    seller: str
    price_amount: Decimal | None
    currency: str | None
    availability: ProductAvailability
    source_id: str
    locator: str
    observed_at: str
    research_rank: float
    why_matched: str
    evidence: tuple[ResearchEvidence, ...]


@dataclass(frozen=True, slots=True)
class ProductSearchResult:
    result_set_id: str
    workspace_id: str
    query: str
    criteria: ProductSearchCriteria
    cards: tuple[ProductCard, ...]


def _required(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ProductSearchError(f"{field_name} is required")
    return normalized


def _currency(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().upper()
    if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
        raise ProductSearchError("currency must be a three-letter ASCII code")
    return normalized


def _decimal(value: Decimal | None, field_name: str) -> Decimal | None:
    if value is None:
        return None
    if not value.is_finite() or value < 0:
        raise ProductSearchError(f"{field_name} must be a finite non-negative amount")
    return value


class ProductSearchService:
    """Project structured product facts onto provenance-backed research results."""

    def project(
        self,
        *,
        result_set: ResearchResultSet,
        observations: tuple[ProductObservation, ...],
        criteria: ProductSearchCriteria = ProductSearchCriteria(),
    ) -> ProductSearchResult:
        normalized = self._validate_criteria(criteria)
        items = {item.document_id: item for item in result_set.items}
        if len(items) != len(result_set.items):
            raise ProductSearchError("research result contains duplicate document IDs")

        seen_products: set[str] = set()
        cards: list[ProductCard] = []
        for observation in observations:
            product_id = _required(observation.product_id, "product_id")
            if product_id in seen_products:
                raise ProductSearchError(f"duplicate product_id: {product_id}")
            seen_products.add(product_id)

            item = items.get(_required(observation.document_id, "document_id"))
            if item is None:
                raise ProductSearchError(
                    f"observation document is outside research result: {observation.document_id}"
                )
            card = self._card_from_observation(item, observation)
            if self._matches(card, normalized):
                cards.append(card)

        cards.sort(key=lambda card: self._sort_key(card, normalized))
        limited = tuple(cards[: normalized.limit])
        return ProductSearchResult(
            result_set_id=result_set.result_set_id,
            workspace_id=result_set.workspace_id,
            query=result_set.query,
            criteria=normalized,
            cards=limited,
        )

    @staticmethod
    def render_text(result: ProductSearchResult) -> str:
        lines = [
            "Product search results",
            f"Query: {result.query}",
            f"Results: {len(result.cards)}",
            "",
        ]
        for index, card in enumerate(result.cards, start=1):
            price = "Not recorded"
            if card.price_amount is not None and card.currency is not None:
                price = f"{card.price_amount} {card.currency}"
            lines.extend(
                (
                    f"{index}. {card.title}",
                    f"Seller: {card.seller}",
                    f"Price: {price}",
                    f"Availability: {card.availability.value}",
                    f"Why matched: {card.why_matched}",
                    f"Source: {card.locator}",
                    f"Observed: {card.observed_at}",
                    f"Snippet: {card.snippet}",
                    "",
                )
            )
        return "\n".join(lines).rstrip() + "\n"

    def _card_from_observation(
        self,
        item: ResearchResultItem,
        observation: ProductObservation,
    ) -> ProductCard:
        source_id = _required(observation.source_id, "source_id")
        locator = _required(observation.locator, "locator")
        observed_at = _required(observation.observed_at, "observed_at")
        seller = _required(observation.seller, "seller")
        price = _decimal(observation.price_amount, "price_amount")
        currency = _currency(observation.currency)
        if (price is None) != (currency is None):
            raise ProductSearchError("price_amount and currency must be recorded together")

        matching_evidence = tuple(
            evidence
            for evidence in item.evidence
            if evidence.source_id == source_id
            and evidence.locator == locator
            and evidence.observed_at == observed_at
        )
        if not matching_evidence:
            raise ProductSearchError(
                "product observation is not bound to exact research evidence"
            )
        return ProductCard(
            product_id=_required(observation.product_id, "product_id"),
            document_id=item.document_id,
            title=item.title,
            snippet=item.snippet,
            seller=seller,
            price_amount=price,
            currency=currency,
            availability=observation.availability,
            source_id=source_id,
            locator=locator,
            observed_at=observed_at,
            research_rank=item.rank,
            why_matched=item.why_matched,
            evidence=matching_evidence,
        )

    @staticmethod
    def _validate_criteria(criteria: ProductSearchCriteria) -> ProductSearchCriteria:
        currency = _currency(criteria.currency)
        min_price = _decimal(criteria.min_price, "min_price")
        max_price = _decimal(criteria.max_price, "max_price")
        if min_price is not None and max_price is not None and min_price > max_price:
            raise ProductSearchError("min_price must not exceed max_price")
        price_filtering = min_price is not None or max_price is not None
        price_sorting = criteria.sort is not ProductSort.RELEVANCE
        if (price_filtering or price_sorting) and currency is None:
            raise ProductSearchError(
                "currency is required for price filtering or price sorting"
            )
        sellers = tuple(
            dict.fromkeys(
                _required(value, "seller_allowlist item")
                for value in criteria.seller_allowlist
            )
        )
        if criteria.limit < 1 or criteria.limit > 100:
            raise ProductSearchError("limit must be between 1 and 100")
        return ProductSearchCriteria(
            currency=currency,
            min_price=min_price,
            max_price=max_price,
            require_available=criteria.require_available,
            seller_allowlist=sellers,
            sort=criteria.sort,
            limit=criteria.limit,
        )

    @staticmethod
    def _matches(card: ProductCard, criteria: ProductSearchCriteria) -> bool:
        if criteria.require_available and card.availability is not ProductAvailability.IN_STOCK:
            return False
        if criteria.seller_allowlist and card.seller not in criteria.seller_allowlist:
            return False

        price_filtering = criteria.min_price is not None or criteria.max_price is not None
        price_sorting = criteria.sort is not ProductSort.RELEVANCE
        if price_filtering or price_sorting:
            if card.price_amount is None or card.currency != criteria.currency:
                return False
        if criteria.min_price is not None and card.price_amount < criteria.min_price:
            return False
        if criteria.max_price is not None and card.price_amount > criteria.max_price:
            return False
        return True

    @staticmethod
    def _sort_key(
        card: ProductCard,
        criteria: ProductSearchCriteria,
    ) -> tuple[object, ...]:
        if criteria.sort is ProductSort.PRICE_LOW_TO_HIGH:
            assert card.price_amount is not None
            return (card.price_amount, card.research_rank, card.product_id)
        if criteria.sort is ProductSort.PRICE_HIGH_TO_LOW:
            assert card.price_amount is not None
            return (-card.price_amount, card.research_rank, card.product_id)
        return (card.research_rank, card.product_id)


class ProductSearchCodec:
    """Strict deterministic JSON codec for durable Product Search handoff."""

    SCHEMA_VERSION = 1

    @classmethod
    def dumps(cls, result: ProductSearchResult) -> str:
        payload = {
            "schema_version": cls.SCHEMA_VERSION,
            "result_set_id": result.result_set_id,
            "workspace_id": result.workspace_id,
            "query": result.query,
            "criteria": cls._criteria_payload(result.criteria),
            "cards": [cls._card_payload(card) for card in result.cards],
        }
        cls._validate_result(result)
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def loads(cls, raw: str) -> ProductSearchResult:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProductSearchError("invalid product search JSON") from exc
        if not isinstance(payload, dict):
            raise ProductSearchError("product search payload must be an object")
        cls._expect_keys(
            payload,
            {"schema_version", "result_set_id", "workspace_id", "query", "criteria", "cards"},
            "payload",
        )
        if payload["schema_version"] != cls.SCHEMA_VERSION:
            raise ProductSearchError("unsupported product search schema_version")
        cards_raw = payload["cards"]
        if not isinstance(cards_raw, list):
            raise ProductSearchError("cards must be a list")
        criteria = cls._criteria_from_payload(payload["criteria"])
        cards = tuple(cls._card_from_payload(value) for value in cards_raw)
        product_ids = [card.product_id for card in cards]
        if len(product_ids) != len(set(product_ids)):
            raise ProductSearchError("stored cards contain duplicate product IDs")
        result = ProductSearchResult(
            result_set_id=cls._string(payload["result_set_id"], "result_set_id"),
            workspace_id=cls._string(payload["workspace_id"], "workspace_id"),
            query=cls._string(payload["query"], "query"),
            criteria=criteria,
            cards=cards,
        )
        cls._validate_result(result)
        return result

    @classmethod
    def _validate_result(cls, result: ProductSearchResult) -> None:
        criteria = ProductSearchService._validate_criteria(result.criteria)
        if criteria != result.criteria:
            raise ProductSearchError("product search criteria are not normalized")
        product_ids = [card.product_id for card in result.cards]
        if len(product_ids) != len(set(product_ids)):
            raise ProductSearchError("stored cards contain duplicate product IDs")
        if len(result.cards) > criteria.limit:
            raise ProductSearchError("stored card count exceeds criteria limit")
        for card in result.cards:
            if not ProductSearchService._matches(card, criteria):
                raise ProductSearchError("stored card does not satisfy search criteria")
            cls._validate_card(card)
        expected = tuple(
            sorted(
                result.cards,
                key=lambda card: ProductSearchService._sort_key(card, criteria),
            )
        )
        if result.cards != expected:
            raise ProductSearchError("stored cards are not in canonical sort order")

    @staticmethod
    def _criteria_payload(criteria: ProductSearchCriteria) -> dict[str, object]:
        return {
            "currency": criteria.currency,
            "min_price": str(criteria.min_price) if criteria.min_price is not None else None,
            "max_price": str(criteria.max_price) if criteria.max_price is not None else None,
            "require_available": criteria.require_available,
            "seller_allowlist": list(criteria.seller_allowlist),
            "sort": criteria.sort.value,
            "limit": criteria.limit,
        }

    @classmethod
    def _criteria_from_payload(cls, raw: Any) -> ProductSearchCriteria:
        if not isinstance(raw, dict):
            raise ProductSearchError("criteria must be an object")
        cls._expect_keys(
            raw,
            {
                "currency",
                "min_price",
                "max_price",
                "require_available",
                "seller_allowlist",
                "sort",
                "limit",
            },
            "criteria",
        )
        sellers = raw["seller_allowlist"]
        if not isinstance(sellers, list) or not all(
            isinstance(value, str) for value in sellers
        ):
            raise ProductSearchError("seller_allowlist must be a list of strings")
        if not isinstance(raw["require_available"], bool):
            raise ProductSearchError("require_available must be a boolean")
        if not isinstance(raw["limit"], int) or isinstance(raw["limit"], bool):
            raise ProductSearchError("limit must be an integer")
        try:
            sort = ProductSort(raw["sort"])
        except (TypeError, ValueError) as exc:
            raise ProductSearchError("invalid product sort") from exc
        criteria = ProductSearchCriteria(
            currency=cls._optional_string(raw["currency"], "currency"),
            min_price=cls._optional_decimal(raw["min_price"], "min_price"),
            max_price=cls._optional_decimal(raw["max_price"], "max_price"),
            require_available=raw["require_available"],
            seller_allowlist=tuple(sellers),
            sort=sort,
            limit=raw["limit"],
        )
        return ProductSearchService._validate_criteria(criteria)

    @classmethod
    def _card_payload(cls, card: ProductCard) -> dict[str, object]:
        return {
            "product_id": card.product_id,
            "document_id": card.document_id,
            "title": card.title,
            "snippet": card.snippet,
            "seller": card.seller,
            "price_amount": (
                str(card.price_amount) if card.price_amount is not None else None
            ),
            "currency": card.currency,
            "availability": card.availability.value,
            "source_id": card.source_id,
            "locator": card.locator,
            "observed_at": card.observed_at,
            "research_rank": card.research_rank,
            "why_matched": card.why_matched,
            "evidence": [cls._evidence_payload(value) for value in card.evidence],
        }

    @classmethod
    def _card_from_payload(cls, raw: Any) -> ProductCard:
        if not isinstance(raw, dict):
            raise ProductSearchError("card must be an object")
        cls._expect_keys(
            raw,
            {
                "product_id",
                "document_id",
                "title",
                "snippet",
                "seller",
                "price_amount",
                "currency",
                "availability",
                "source_id",
                "locator",
                "observed_at",
                "research_rank",
                "why_matched",
                "evidence",
            },
            "card",
        )
        evidence_raw = raw["evidence"]
        if not isinstance(evidence_raw, list):
            raise ProductSearchError("card evidence must be a list")
        try:
            availability = ProductAvailability(raw["availability"])
        except (TypeError, ValueError) as exc:
            raise ProductSearchError("invalid product availability") from exc
        rank = raw["research_rank"]
        if not isinstance(rank, (int, float)) or isinstance(rank, bool):
            raise ProductSearchError("research_rank must be numeric")
        if not math.isfinite(float(rank)):
            raise ProductSearchError("research_rank must be finite")
        card = ProductCard(
            product_id=cls._string(raw["product_id"], "product_id"),
            document_id=cls._string(raw["document_id"], "document_id"),
            title=cls._string(raw["title"], "title"),
            snippet=cls._string(raw["snippet"], "snippet", allow_empty=True),
            seller=cls._string(raw["seller"], "seller"),
            price_amount=cls._optional_decimal(raw["price_amount"], "price_amount"),
            currency=_currency(cls._optional_string(raw["currency"], "currency")),
            availability=availability,
            source_id=cls._string(raw["source_id"], "source_id"),
            locator=cls._string(raw["locator"], "locator"),
            observed_at=cls._string(raw["observed_at"], "observed_at"),
            research_rank=float(rank),
            why_matched=cls._string(raw["why_matched"], "why_matched"),
            evidence=tuple(cls._evidence_from_payload(value) for value in evidence_raw),
        )
        cls._validate_card(card)
        return card

    @staticmethod
    def _validate_card(card: ProductCard) -> None:
        _required(card.product_id, "product_id")
        _required(card.document_id, "document_id")
        _required(card.title, "title")
        _required(card.seller, "seller")
        _required(card.source_id, "source_id")
        _required(card.locator, "locator")
        _required(card.observed_at, "observed_at")
        _required(card.why_matched, "why_matched")
        _decimal(card.price_amount, "price_amount")
        normalized_currency = _currency(card.currency)
        if normalized_currency != card.currency:
            raise ProductSearchError("stored card currency is not normalized")
        if (card.price_amount is None) != (card.currency is None):
            raise ProductSearchError("stored card price and currency must occur together")
        if not math.isfinite(card.research_rank):
            raise ProductSearchError("research_rank must be finite")
        if not card.evidence:
            raise ProductSearchError("stored card must retain provenance evidence")
        if not any(
            evidence.source_id == card.source_id
            and evidence.locator == card.locator
            and evidence.observed_at == card.observed_at
            for evidence in card.evidence
        ):
            raise ProductSearchError("stored card is not bound to its evidence")

    @staticmethod
    def _evidence_payload(evidence: ResearchEvidence) -> dict[str, object]:
        return {
            "source_id": evidence.source_id,
            "source_kind": evidence.source_kind.value,
            "locator": evidence.locator,
            "observed_at": evidence.observed_at,
            "freshness": evidence.freshness.value if evidence.freshness is not None else None,
        }

    @classmethod
    def _evidence_from_payload(cls, raw: Any) -> ResearchEvidence:
        if not isinstance(raw, dict):
            raise ProductSearchError("evidence must be an object")
        cls._expect_keys(
            raw,
            {"source_id", "source_kind", "locator", "observed_at", "freshness"},
            "evidence",
        )
        try:
            source_kind = SourceKind(raw["source_kind"])
            freshness = (
                FreshnessState(raw["freshness"])
                if raw["freshness"] is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise ProductSearchError("invalid research evidence enum value") from exc
        return ResearchEvidence(
            source_id=cls._string(raw["source_id"], "source_id"),
            source_kind=source_kind,
            locator=cls._string(raw["locator"], "locator"),
            observed_at=cls._string(raw["observed_at"], "observed_at"),
            freshness=freshness,
        )

    @staticmethod
    def _expect_keys(raw: dict[str, Any], expected: set[str], label: str) -> None:
        if set(raw) != expected:
            raise ProductSearchError(f"{label} has unsupported fields")

    @staticmethod
    def _string(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
        if not isinstance(value, str):
            raise ProductSearchError(f"{field_name} must be a string")
        if allow_empty:
            return value
        return _required(value, field_name)

    @classmethod
    def _optional_string(cls, value: Any, field_name: str) -> str | None:
        if value is None:
            return None
        return cls._string(value, field_name)

    @staticmethod
    def _optional_decimal(value: Any, field_name: str) -> Decimal | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ProductSearchError(f"{field_name} must be a decimal string or null")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ProductSearchError(f"{field_name} is not a valid decimal") from exc
        return _decimal(parsed, field_name)
