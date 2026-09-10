"""Safe projections for business-query tool results.

This module deliberately depends on aliases rather than ``SessionState`` so the
state graph and the legacy Agent can share one projection implementation.
"""

from __future__ import annotations

from collections.abc import Mapping

from bi_agent.metrics import ToolResult
from bi_agent.runtime.models import validate_artifact_payload, validate_model_payload


_PUBLIC_RESULT_COLUMNS = {
    "day", "shop_id", "product_id", "line_kind", "currency", "basis",
    "paid_amount", "paid_orders", "erp_documents", "aov", "refund_amount",
    "cash_difference", "cohort_refund_rate", "quantity", "product_paid_amount",
    "spend_cap", "budget", "actual_spend", "remaining_budget", "over_budget",
    "remaining_days", "daily_cap", "contribution_cap",
}
_PUBLIC_FILTER_COLUMNS = {
    "start", "end", "shop_ids", "metrics", "group_by", "compare", "top_n", "currency",
}


def to_model_result(
    result: ToolResult, shop_aliases: Mapping[str, str]
) -> dict[str, object]:
    """Return the validated alias-only payload permitted for the model."""
    payload = _safe_result(result, shop_aliases, model_view=True)
    return validate_model_payload(payload)


def to_public_artifact(
    result: ToolResult, shop_aliases: Mapping[str, str]
) -> dict[str, object]:
    """Return the validated public artifact payload, never ERP identifiers."""
    payload = _safe_result(result, shop_aliases, model_view=False)
    return validate_artifact_payload(payload)


def _safe_result(
    result: ToolResult, shop_aliases: Mapping[str, str], *, model_view: bool
) -> dict[str, object]:
    """Keep allowlisted aggregates and replace every shop/product identifier."""
    payload = result.model_dump(mode="json")
    product_aliases: dict[str, str] = {}

    def map_shop(value: object) -> str:
        alias = shop_aliases.get(str(value))
        if alias is None:
            return "未授权店铺"
        return alias if model_view else f"店铺{alias.removeprefix('shop_')}"

    def map_product(value: object) -> object:
        if not isinstance(value, str):
            return value
        if value not in product_aliases:
            product_aliases[value] = f"商品{chr(ord('A') + len(product_aliases) % 26)}"
        return product_aliases[value]

    rows: list[dict[str, object]] = []
    raw_rows = payload.get("data")
    if isinstance(raw_rows, list):
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            clean = {key: value for key, value in row.items() if key in _PUBLIC_RESULT_COLUMNS}
            if "shop_id" in clean:
                clean["shop_id"] = map_shop(clean["shop_id"])
            if "product_id" in clean:
                clean["product_id"] = map_product(clean["product_id"])
            rows.append(clean)

    filters: dict[str, object] = {}
    raw_filters = payload.get("filters")
    if isinstance(raw_filters, dict):
        filters = {
            key: value for key, value in raw_filters.items()
            if key in _PUBLIC_FILTER_COLUMNS
        }
    if isinstance(filters.get("shop_ids"), list):
        filters["shop_ids"] = [map_shop(shop_id) for shop_id in filters["shop_ids"]]

    return {
        "status": payload.get("status"),
        "metric_definition": payload.get("metric_definition"),
        "coverage": payload.get("coverage"),
        "limitations": payload.get("limitations"),
        "data_as_of": payload.get("data_as_of"),
        "filters": filters,
        "data": rows,
    }
