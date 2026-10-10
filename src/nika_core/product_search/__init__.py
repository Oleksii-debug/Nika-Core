"""Deterministic Product Search domain projection over Universal Research."""

from nika_core.product_search.delta import (
    ProductChange,
    ProductChangeKind,
    ProductDeltaService,
    ProductSearchDelta,
)
from nika_core.product_search.service import (
    ProductAvailability,
    ProductCard,
    ProductObservation,
    ProductSearchCodec,
    ProductSearchCriteria,
    ProductSearchError,
    ProductSearchResult,
    ProductSearchService,
    ProductSort,
)

__all__ = [
    "ProductAvailability",
    "ProductCard",
    "ProductChange",
    "ProductChangeKind",
    "ProductDeltaService",
    "ProductObservation",
    "ProductSearchCodec",
    "ProductSearchCriteria",
    "ProductSearchDelta",
    "ProductSearchError",
    "ProductSearchResult",
    "ProductSearchService",
    "ProductSort",
]
