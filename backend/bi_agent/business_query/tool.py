"""Safe projections for business-query tool results.

This module deliberately depends on aliases rather than ``SessionState`` so the
state graph and the legacy Agent can share one projection implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from bi_agent.metrics import ToolResult
from bi_agent.runtime.models import (
    ARTIFACT_FILTER_COLUMNS,
    ARTIFACT_RESULT_COLUMNS,
    DomainResult,
    validate_artifact_payload,
    validate_model_payload,
)

from .state import BusinessQueryContext, BusinessQueryExecution, BusinessQueryInput

if TYPE_CHECKING:
    from bi_agent.llm import ToolCall

# 白名单单一定义在 runtime/models.py（推广列由 promotion.py 供给），
# 这里只引用，避免第二份手抄集合与校验端漂移。
_PUBLIC_RESULT_COLUMNS = ARTIFACT_RESULT_COLUMNS
_PUBLIC_FILTER_COLUMNS = ARTIFACT_FILTER_COLUMNS


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


def run_business_query(
    conn: object,
    store: object,
    tool_input: BusinessQueryInput,
    context: BusinessQueryContext,
) -> DomainResult:
    """Execute one graph-backed query and expose only its public domain result."""
    from .graph import _execute_business_query_graph

    return _execute_business_query_graph(conn, store, tool_input, context).domain_result


def execute_business_query_tool(
    call: "ToolCall",
    session_state: object,
    context: BusinessQueryContext,
    conn: object,
    store: object,
) -> BusinessQueryExecution:
    """Adapt a legacy model tool call to the deterministic query graph.

    Real identifiers stay in the request-local context and the returned
    ``session_filters``; persisted graph state only receives safe aliases.
    """
    from .graph import _execute_business_query_graph

    aliases = getattr(session_state, "shop_aliases", {})
    if not isinstance(aliases, dict):
        aliases = {}
    graph_context = BusinessQueryContext(
        chat_id=context.chat_id,
        user_message_id=context.user_message_id,
        subject_id=context.subject_id,
        question=context.question,
        previous_filters=dict(context.previous_filters),
        shop_aliases=dict(aliases),
        allowed_shop_ids=context.allowed_shop_ids,
        now=context.now,
        deadline=context.deadline,
        attempt_no=context.attempt_no,
    )
    return _execute_business_query_graph(
        conn,
        store,
        BusinessQueryInput(
            tool_call_id=call.id,
            arguments=dict(call.arguments) if call.arguments is not None else None,
            arguments_error=call.arguments_error,
        ),
        graph_context,
    )


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
