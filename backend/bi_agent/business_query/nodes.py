"""Input nodes for the deterministic business-query graph."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import ValidationError

from bi_agent.metrics import METRIC_DEFINITIONS, QueryRequest, resolve_period
from bi_agent.runtime.models import (
    DomainStatus,
    ErrorEnvelope,
    RecoveryAction,
    RunStatus,
)

from .graph import transition_state
from .state import BusinessQueryNode, BusinessQueryRuntime


_GROUP_BY = frozenset({"total", "day", "shop", "product"})
_COMPARE = frozenset({"none", "previous_period"})
_VALIDATION_PROBLEMS = {
    "start": "invalid_date_range",
    "end": "invalid_date_range",
    "metrics": "invalid_metric",
    "group_by": "invalid_group_by",
    "compare": "invalid_compare",
    "top_n": "invalid_top_n",
    "shop_ids": "invalid_shop",
}
_ProblemCode = Literal[
    "missing_parameters",
    "invalid_parameters",
    "invalid_date_range",
    "invalid_metric",
    "invalid_group_by",
    "invalid_compare",
    "invalid_top_n",
    "invalid_shop",
    "forbidden",
]


def resolve_parameters(runtime: BusinessQueryRuntime) -> BusinessQueryRuntime:
    """Fill omitted filters and replace aliases without persisting real shop IDs."""
    if runtime.state.status is not RunStatus.RUNNING:
        return runtime
    runtime.state = transition_state(
        runtime.state, BusinessQueryNode.RESOLVE_PARAMETERS
    )

    args = dict(runtime.resolved_args)
    alias_reverse = {
        alias: shop_id for shop_id, alias in runtime.context.shop_aliases.items()
    }
    shop_aliases = _resolve_shops(args, runtime, alias_reverse)

    if "start" not in args or "end" not in args:
        period = resolve_period(runtime.context.question, now=runtime.context.now)
        previous_start = runtime.context.previous_filters.get("start")
        previous_end = runtime.context.previous_filters.get("end")
        if period is not None:
            args.setdefault("start", period[0].isoformat())
            args.setdefault("end", period[1].isoformat())
        elif previous_start and previous_end:
            args.setdefault("start", previous_start)
            args.setdefault("end", previous_end)

    if "metrics" not in args or not args.get("metrics"):
        previous_metrics = runtime.context.previous_filters.get("metrics")
        args["metrics"] = (
            list(previous_metrics) if isinstance(previous_metrics, list) else ["paid_amount"]
        )

    runtime.resolved_args = args
    runtime.state = runtime.state.model_copy(
        update={
            "normalized_request": _normalized_request(args, shop_aliases=shop_aliases)
        }
    )
    if "shop_ids" not in args or not args["shop_ids"]:
        _finish_needs_input(
            runtime,
            stage="resolve_parameters",
            code="missing_parameters",
            problem="missing_parameters",
            recovery=RecoveryAction.ASK_USER,
            public_message="缺少查询参数，请补充后重试。",
        )
    return runtime


def validate_parameters(runtime: BusinessQueryRuntime) -> BusinessQueryRuntime:
    """Validate resolved query arguments and retain only a safe error code."""
    if runtime.state.status is not RunStatus.RUNNING:
        return runtime
    runtime.state = transition_state(
        runtime.state, BusinessQueryNode.VALIDATE_PARAMETERS
    )
    try:
        request = QueryRequest.model_validate(runtime.resolved_args)
    except ValidationError as error:
        _finish_needs_input(
            runtime,
            stage="validate_parameters",
            code="invalid_parameters",
            problem=_problem_from_validation(error),
            recovery=RecoveryAction.CORRECT_PARAMETERS,
            public_message="查询参数无效，请调整后重试。",
        )
        return runtime

    runtime.request = request
    aliases = runtime.state.normalized_request.get("shop_aliases")
    normalized_request: dict[str, object] = {
        "metrics": list(request.metrics),
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
        "group_by": request.group_by,
        "compare": request.compare,
        "top_n": request.top_n,
        "currency": request.currency,
    }
    if isinstance(aliases, list):
        normalized_request["shop_aliases"] = aliases
    runtime.state = runtime.state.model_copy(
        update={"normalized_request": normalized_request}
    )
    return runtime


def authorize_scope(runtime: BusinessQueryRuntime) -> BusinessQueryRuntime:
    """Reject non-authorized shops before the graph can reach query execution."""
    if runtime.state.status is not RunStatus.RUNNING:
        return runtime
    runtime.state = transition_state(runtime.state, BusinessQueryNode.AUTHORIZE_SCOPE)
    request = runtime.request
    if request is None or not set(request.shop_ids) <= runtime.context.allowed_shop_ids:
        runtime.state = runtime.state.model_copy(
            update={
                "status": RunStatus.FAILED,
                "target_status": DomainStatus.FAILED,
                "problems": ["forbidden"],
                "error": ErrorEnvelope(
                    code="forbidden",
                    stage="authorize_scope",
                    retryable=False,
                    recovery=RecoveryAction.NONE,
                    public_message="查询范围无权限。",
                    problems=["forbidden"],
                ),
            }
        )
        return runtime

    runtime.state = transition_state(
        runtime.state, BusinessQueryNode.EXECUTE_FIXED_QUERY
    )
    return runtime


def _resolve_shops(
    args: dict[str, object],
    runtime: BusinessQueryRuntime,
    alias_reverse: dict[str, str],
) -> list[str] | None:
    raw_shops = args.get("shop_ids")
    if isinstance(raw_shops, list) and raw_shops:
        mapped: list[str] = []
        aliases: list[str] = []
        for value in raw_shops:
            raw_shop = str(value)
            mapped.append(alias_reverse.get(raw_shop, raw_shop))
            aliases.append(raw_shop if raw_shop in alias_reverse else "invalid_shop")
        args["shop_ids"] = mapped
        return aliases
    if not raw_shops:
        previous_shops = runtime.context.previous_filters.get("shop_ids")
        if isinstance(previous_shops, list) and previous_shops:
            resolved = [str(shop_id) for shop_id in previous_shops]
            args["shop_ids"] = resolved
            return [
                runtime.context.shop_aliases.get(shop_id, "invalid_shop")
                for shop_id in resolved
            ]
        return None
    return ["invalid_shop"]


def _normalized_request(
    args: dict[str, object], *, shop_aliases: list[str] | None
) -> dict[str, object]:
    normalized: dict[str, object] = {}
    if shop_aliases is not None:
        normalized["shop_aliases"] = shop_aliases
    metrics = args.get("metrics")
    if isinstance(metrics, list) and all(
        isinstance(metric, str) and metric in METRIC_DEFINITIONS for metric in metrics
    ):
        normalized["metrics"] = list(metrics)
    for boundary in ("start", "end"):
        value = _normalized_date(args.get(boundary))
        if value is not None:
            normalized[boundary] = value
    group_by = args.get("group_by")
    if isinstance(group_by, str) and group_by in _GROUP_BY:
        normalized["group_by"] = group_by
    compare = args.get("compare")
    if isinstance(compare, str) and compare in _COMPARE:
        normalized["compare"] = compare
    top_n = args.get("top_n")
    if isinstance(top_n, int) and not isinstance(top_n, bool) and 1 <= top_n <= 500:
        normalized["top_n"] = top_n
    if args.get("currency") == "CNY":
        normalized["currency"] = "CNY"
    return normalized


def _normalized_date(value: object) -> str | None:
    if type(value) is date:
        return value.isoformat()
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _problem_from_validation(error: ValidationError) -> _ProblemCode:
    for detail in error.errors(include_url=False):
        location = detail.get("loc", ())
        if location and isinstance(location[0], str):
            problem = _VALIDATION_PROBLEMS.get(location[0])
            if problem is not None:
                return problem  # type: ignore[return-value]
    return "invalid_parameters"


def _finish_needs_input(
    runtime: BusinessQueryRuntime,
    *,
    stage: Literal["resolve_parameters", "validate_parameters"],
    code: Literal["missing_parameters", "invalid_parameters"],
    problem: _ProblemCode,
    recovery: RecoveryAction,
    public_message: Literal["缺少查询参数，请补充后重试。", "查询参数无效，请调整后重试。"],
) -> None:
    runtime.state = runtime.state.model_copy(
        update={
            "status": RunStatus.NEEDS_INPUT,
            "target_status": DomainStatus.NEEDS_INPUT,
            "problems": [problem],
            "error": ErrorEnvelope(
                code=code,
                stage=stage,
                retryable=False,
                recovery=recovery,
                public_message=public_message,
                problems=[problem],
            ),
        }
    )
