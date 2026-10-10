from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from nika_core.product_search.service import (
    ProductCard,
    ProductSearchCodec,
    ProductSearchError,
    ProductSearchResult,
)


class ProductChangeKind(StrEnum):
    NEW = "new"
    CHANGED = "changed"


@dataclass(frozen=True, slots=True)
class ProductChange:
    product_id: str
    kind: ProductChangeKind
    changed_fields: tuple[str, ...]
    previous: ProductCard | None
    current: ProductCard


@dataclass(frozen=True, slots=True)
class ProductSearchDelta:
    previous_result_set_id: str
    current_result_set_id: str
    workspace_id: str
    query: str
    changes: tuple[ProductChange, ...]


class ProductDeltaService:
    """Compare canonical Product Search snapshots without false removal claims."""

    _MATERIAL_FIELDS = (
        "title",
        "seller",
        "price_amount",
        "currency",
        "availability",
        "source_id",
        "locator",
    )

    @classmethod
    def compare(
        cls,
        *,
        previous: ProductSearchResult,
        current: ProductSearchResult,
    ) -> ProductSearchDelta:
        # Reuse the strict canonical result validation before comparing snapshots.
        ProductSearchCodec.dumps(previous)
        ProductSearchCodec.dumps(current)
        if previous.workspace_id != current.workspace_id:
            raise ProductSearchError("cannot compare product results from different workspaces")
        if previous.query != current.query:
            raise ProductSearchError("cannot compare product results from different queries")
        if previous.criteria != current.criteria:
            raise ProductSearchError("cannot compare product results with different criteria")

        previous_by_id = {card.product_id: card for card in previous.cards}
        changes: list[ProductChange] = []
        for current_card in current.cards:
            previous_card = previous_by_id.get(current_card.product_id)
            if previous_card is None:
                changes.append(
                    ProductChange(
                        product_id=current_card.product_id,
                        kind=ProductChangeKind.NEW,
                        changed_fields=(),
                        previous=None,
                        current=current_card,
                    )
                )
                continue

            changed_fields = tuple(
                field_name
                for field_name in cls._MATERIAL_FIELDS
                if getattr(previous_card, field_name) != getattr(current_card, field_name)
            )
            if changed_fields:
                changes.append(
                    ProductChange(
                        product_id=current_card.product_id,
                        kind=ProductChangeKind.CHANGED,
                        changed_fields=changed_fields,
                        previous=previous_card,
                        current=current_card,
                    )
                )

        changes.sort(
            key=lambda change: (
                0 if change.kind is ProductChangeKind.NEW else 1,
                change.product_id,
            )
        )
        return ProductSearchDelta(
            previous_result_set_id=previous.result_set_id,
            current_result_set_id=current.result_set_id,
            workspace_id=current.workspace_id,
            query=current.query,
            changes=tuple(changes),
        )

    @staticmethod
    def render_text(delta: ProductSearchDelta) -> str:
        lines = [
            "Product search changes",
            f"Query: {delta.query}",
            f"New or changed products: {len(delta.changes)}",
            "",
        ]
        for index, change in enumerate(delta.changes, start=1):
            card = change.current
            lines.extend(
                (
                    f"{index}. {card.title}",
                    f"Change: {change.kind.value}",
                    f"Product ID: {change.product_id}",
                )
            )
            if change.changed_fields:
                lines.append(f"Changed fields: {', '.join(change.changed_fields)}")
            if change.previous is not None:
                previous = change.previous
                if "price_amount" in change.changed_fields or "currency" in change.changed_fields:
                    lines.append(
                        "Previous price: "
                        f"{ProductDeltaService._price_text(previous)}"
                    )
                    lines.append(
                        "Current price: "
                        f"{ProductDeltaService._price_text(card)}"
                    )
                if "availability" in change.changed_fields:
                    lines.append(f"Previous availability: {previous.availability.value}")
                    lines.append(f"Current availability: {card.availability.value}")
            lines.extend(
                (
                    f"Seller: {card.seller}",
                    f"Source: {card.locator}",
                    f"Observed: {card.observed_at}",
                    "",
                )
            )
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _price_text(card: ProductCard) -> str:
        if card.price_amount is None or card.currency is None:
            return "Not recorded"
        return f"{card.price_amount} {card.currency}"
