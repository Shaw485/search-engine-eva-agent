"""Pure query-route generation for the zero-result backoff strategy."""

from __future__ import annotations

import hashlib
import logging

from search_quality.catalog import validate_catalog_query

from .contracts import (
    QueryRoute,
    QueryRoutePlan,
    StrategySpec,
    query_route_id,
    query_route_plan_id,
)

logger = logging.getLogger("search_quality.diagnostic_experiments")


def generate_query_routes(
    query: str,
    *,
    strategy: StrategySpec,
    primary_returned_at_k: int,
    model_tokens: tuple[str, ...] = (),
    product_id_tokens: tuple[str, ...] = (),
) -> QueryRoutePlan:
    """Return strict-AND primary and bounded drop-one fallback routes.

    Fallback routes are generated only after a zero-result primary observation.
    Numeric/model tokens and explicit product-ID tokens are never removed.
    """

    validated_strategy = StrategySpec.model_validate(
        strategy.model_dump(mode="python"),
        strict=True,
    )
    if validated_strategy.strategy_id != "zero-result-drop-one-token-backoff-v1":
        raise ValueError("unsupported diagnostic experiment strategy")
    if (
        isinstance(primary_returned_at_k, bool)
        or not isinstance(primary_returned_at_k, int)
        or not 0 <= primary_returned_at_k <= validated_strategy.top_k
    ):
        raise ValueError("primary_returned_at_k is outside the strategy Top K")
    tokens = validate_catalog_query(query, top_k=validated_strategy.top_k)
    explicit_models = _explicit_protected_tokens(
        model_tokens,
        argument_name="model_tokens",
        query_tokens=tokens,
        top_k=validated_strategy.top_k,
    )
    explicit_product_ids = _explicit_protected_tokens(
        product_id_tokens,
        argument_name="product_id_tokens",
        query_tokens=tokens,
        top_k=validated_strategy.top_k,
    )
    protected = tuple(
        token
        for token in tokens
        if any(character.isdigit() for character in token)
        or token in explicit_models
        or token in explicit_product_ids
    )
    primary = _route(kind="primary", tokens=tokens, dropped_token=None)
    fallback_routes: list[QueryRoute] = []
    if primary_returned_at_k == 0:
        seen: set[tuple[str, ...]] = set()
        protected_set = set(protected)
        for index, token in enumerate(tokens):
            if token in protected_set:
                continue
            candidate = tokens[:index] + tokens[index + 1 :]
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            fallback_routes.append(
                _route(kind="fallback", tokens=candidate, dropped_token=token)
            )
            if len(fallback_routes) >= validated_strategy.max_fallback_routes:
                break

    body = {
        "fallback_routes": tuple(
            route.model_dump(mode="python") for route in fallback_routes
        ),
        "fallback_triggered": primary_returned_at_k == 0,
        "primary": primary.model_dump(mode="python"),
        "primary_returned_at_k": primary_returned_at_k,
        "protected_token_sha256s": tuple(_sha256(token) for token in protected),
        "query_sha256": _sha256(query),
        "schema_version": "query-route-plan-v1",
        "strategy_spec_id": validated_strategy.strategy_spec_id,
    }
    plan = QueryRoutePlan.model_validate(
        {**body, "route_plan_id": query_route_plan_id(body)},
        strict=True,
    )
    logger.info(
        "diagnostic_query_routes_generated",
        extra={
            "fallback_route_count": len(plan.fallback_routes),
            "fallback_triggered": plan.fallback_triggered,
            "protected_token_count": len(plan.protected_token_sha256s),
            "query_route_plan_id": plan.route_plan_id,
            "query_token_count": len(plan.primary.tokens),
            "strategy_spec_id": plan.strategy_spec_id,
        },
    )
    return plan


def _route(
    *,
    kind: str,
    tokens: tuple[str, ...],
    dropped_token: str | None,
) -> QueryRoute:
    body = {
        "dropped_token_sha256": (
            _sha256(dropped_token) if dropped_token is not None else None
        ),
        "kind": kind,
        "operator": "strict_and",
        "tokens": tokens,
    }
    return QueryRoute.model_validate(
        {**body, "route_id": query_route_id(body)},
        strict=True,
    )


def _explicit_protected_tokens(
    values: tuple[str, ...],
    *,
    argument_name: str,
    query_tokens: tuple[str, ...],
    top_k: int,
) -> frozenset[str]:
    if not isinstance(values, tuple) or any(
        not isinstance(item, str) for item in values
    ):
        raise TypeError(f"{argument_name} must be a tuple of text values")
    normalized: list[str] = []
    for value in values:
        tokens = validate_catalog_query(value, top_k=top_k)
        if len(tokens) != 1:
            raise ValueError(f"each {argument_name} item must normalize to one token")
        token = tokens[0]
        if token not in query_tokens:
            raise ValueError(f"{argument_name} item is absent from the Query")
        normalized.append(token)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{argument_name} items must be unique")
    return frozenset(normalized)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
