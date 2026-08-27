"""Full-catalog lexical search used by the website baseline."""

from .index import (
    CATALOG_SCHEMA_VERSION,
    DEFAULT_CATALOG_INDEX,
    EXPECTED_PRODUCT_COUNT,
    CatalogIndexMetadata,
    build_catalog_index,
)
from .search import (
    CatalogProduct,
    CatalogSearchHit,
    CatalogSearchResult,
    CatalogSearchService,
    InvalidCatalogQuery,
)

__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "DEFAULT_CATALOG_INDEX",
    "EXPECTED_PRODUCT_COUNT",
    "CatalogIndexMetadata",
    "CatalogProduct",
    "CatalogSearchHit",
    "CatalogSearchResult",
    "CatalogSearchService",
    "InvalidCatalogQuery",
    "build_catalog_index",
]
