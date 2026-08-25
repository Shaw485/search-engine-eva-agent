"""Load and validate the small Stage 0 product fixture."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .models import Product


def load_products(path: str | Path) -> list[Product]:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("sample product file must contain a JSON array")
    products = [Product.from_dict(item) for item in payload]
    product_ids = [product.product_id for product in products]
    if len(product_ids) != len(set(product_ids)):
        raise ValueError("sample product_id values must be unique")
    return products


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m search_quality.sample_data <products.json>")
    products = load_products(sys.argv[1])
    print(f"valid sample: {len(products)} products")


if __name__ == "__main__":
    main()
