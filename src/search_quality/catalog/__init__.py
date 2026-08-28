"""Full-catalog lexical search used by the website baseline."""

from .index import (
    CATALOG_SCHEMA_VERSION,
    DEFAULT_CATALOG_INDEX,
    EXPECTED_PRODUCT_COUNT,
    CatalogIndexMetadata,
    build_catalog_index,
)
from .search import (
    CatalogBatchSearchFailed,
    CatalogProduct,
    CatalogSearchDeadlineExceeded,
    CatalogSearchHit,
    CatalogSearchResult,
    CatalogSearchService,
    InvalidCatalogQuery,
    validate_catalog_query,
)

__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "DEFAULT_CATALOG_INDEX",
    "EXPECTED_PRODUCT_COUNT",
    "CatalogIndexMetadata",
    "CatalogProduct",
    "CatalogBatchSearchFailed",
    "CatalogSearchHit",
    "CatalogSearchResult",
    "CatalogSearchService",
    "CatalogSearchDeadlineExceeded",
    "InvalidCatalogQuery",
    "build_catalog_index",
    "validate_catalog_query",
]
