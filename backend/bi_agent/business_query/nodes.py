"""Input nodes for the deterministic business-query graph."""

from __future__ import annotations

from datetime import date
from time import monotonic
from typing import Literal

from pydantic import ValidationError

from bi_agent import metrics
from bi_agent.catalog import CatalogUnauthorized, build_catalog
from bi_agent.metrics import Coverage, METRIC_DEFINITIONS, QueryRequest, ToolResult, resolve_period
from bi_agent.runtime.models import (
    ArtifactPersistenceError,
    DomainStatus,
    ErrorEnvelope,
    NewArtifact,
    RecoveryAction,
    RunStatus,
)

from .graph import transition_state
from .state import BusinessQueryNode, BusinessQueryRuntime
from .tool import to_public_artifact


_GROUP_BY = frozenset({"total", "day", "shop", "product"})
_COMPARE = frozenset({"none", "previous_period"})
_SHOP_IDS_SOURCE = "_shop_ids_source"
_REF_SHOPS = "refs"
_PREVIOUS_FILTER_SHOPS = "previous_filters"
_UNRECOGNIZED_SHOPS = "unrecognized"
_MISSING_SHOPS = "missing"
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
    """Fill omitted filters and replace refs without persisting real shop IDs."""
    if runtime.state.status is not RunStatus.RUNNING:
        return runtime
    runtime.state = transition_state(
        runtime.state, BusinessQueryNode.RESOLVE_PARAMETERS
    )

    args = dict(runtime.resolved_args)
    ref_reverse = {
        ref: shop_id for shop_id, ref in runtime.context.shop_refs.items()
    }
    shop_refs, shop_source = _resolve_shops(args, runtime, ref_reverse)
    args[_SHOP_IDS_SOURCE] = shop_source

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
            "normalized_request": _normalized_request(args, shop_refs=shop_refs)
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
        arguments = dict(runtime.resolved_args)
        arguments.pop(_SHOP_IDS_SOURCE, None)
        request = QueryRequest.model_validate(arguments)
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
    refs = runtime.state.normalized_request.get("shop_refs")
    normalized_request: dict[str, object] = {
        "metrics": list(request.metrics),
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
        "group_by": request.group_by,
        "compare": request.compare,
        "top_n": request.top_n,
        "currency": request.currency,
    }
    if isinstance(refs, list):
        normalized_request["shop_refs"] = refs
    runtime.state = runtime.state.model_copy(
        update={"normalized_request": normalized_request}
    )
    return runtime


def authorize_scope(
    runtime: BusinessQueryRuntime, *, advance_to_execution: bool = True
) -> BusinessQueryRuntime:
    """Reject non-authorized shops before the graph can reach query execution."""
    if runtime.state.status is not RunStatus.RUNNING:
        return runtime
    runtime.state = transition_state(runtime.state, BusinessQueryNode.AUTHORIZE_SCOPE)
    request = runtime.request
    shop_source = runtime.resolved_args.get(_SHOP_IDS_SOURCE)
    if (
        request is None
        or shop_source not in {_REF_SHOPS, _PREVIOUS_FILTER_SHOPS}
        or not set(request.shop_ids) <= runtime.context.allowed_shop_ids
    ):
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

    if advance_to_execution:
        runtime.state = transition_state(
            runtime.state, BusinessQueryNode.EXECUTE_FIXED_QUERY
        )
    return runtime


_LIMITATION_CODES = {
    "覆盖未完成，拒绝部分汇总；缺口见coverage.gaps": "coverage_incomplete",
    "数据截止未知（回填未完成）": "data_as_of_unknown",
    "店铺尚未同步，无法查询": "shop_not_synced",
    "部分店铺已停用，仅返回剩余范围": "shops_inactive",
    "所选店铺均已停用，无法查询": "shops_inactive",
    "上期覆盖不足，无法比较，仅返回绝对值": "comparison_coverage_incomplete",
    "本次查询时间预算已耗尽": "deadline_exceeded",
    "查询超时": "query_timeout",
    "店铺不在授权范围": "forbidden",
    "同批支付额为0或无支付，同批退款率不可计算": "cohort_rate_not_computable",
}


def execute_fixed_query(runtime: BusinessQueryRuntime, conn: object) -> BusinessQueryRuntime:
    """Execute the already authorized query exactly once, unless it is expired."""
    if runtime.state.status is not RunStatus.RUNNING:
        return runtime
    runtime.state = transition_state(
        runtime.state, BusinessQueryNode.EXECUTE_FIXED_QUERY
    )
    request = runtime.request
    if request is None:
        _set_failure(
            runtime,
            code="result_contract_violation",
            stage=BusinessQueryNode.EXECUTE_FIXED_QUERY,
            public_message="查询结果异常。",
        )
        return runtime
    if monotonic() >= runtime.context.deadline:
        _set_failure(
            runtime,
            code="deadline_exceeded",
            stage=BusinessQueryNode.EXECUTE_FIXED_QUERY,
            public_message="查询已超时，请稍后重试。",
        )
        return runtime

    try:
        runtime.result = metrics.query_business(
            conn,
            request,
            allowed_shop_ids=runtime.context.allowed_shop_ids,
            now=runtime.context.now,
            deadline=runtime.context.deadline,
        )
    except Exception:  # noqa: BLE001 - provider diagnostics must not leave this boundary
        runtime.result = ToolResult(
            status="unavailable",
            coverage=Coverage(status="missing", start=None, end=None),
            limitations=[],
        )
        return runtime

    # 目录投影在建立即用同一个 conn：结果行里出现未授权店铺就是越权，宁可不出数。
    try:
        runtime.catalog = build_catalog(
            conn, runtime.result, allowed_shop_ids=runtime.context.allowed_shop_ids
        )
    except CatalogUnauthorized:
        runtime.result = None
        _set_failure(
            runtime,
            code="forbidden",
            stage=BusinessQueryNode.EXECUTE_FIXED_QUERY,
            public_message="查询范围无权限。",
        )
    except Exception:  # noqa: BLE001 - 名称解析失败不得把未核验的行发出去
        runtime.result = None
        _set_failure(
            runtime,
            code="result_contract_violation",
            stage=BusinessQueryNode.EXECUTE_FIXED_QUERY,
            public_message="查询结果异常。",
        )
    return runtime


def classify_result(runtime: BusinessQueryRuntime) -> BusinessQueryRuntime:
    """Map a ToolResult onto one safe domain outcome without retaining raw text."""
    if runtime.state.status is not RunStatus.RUNNING:
        return runtime
    runtime.state = transition_state(runtime.state, BusinessQueryNode.CLASSIFY_RESULT)
    result = runtime.result
    if result is None:
        _set_failure(
            runtime,
            code="result_contract_violation",
            stage=BusinessQueryNode.CLASSIFY_RESULT,
            public_message="查询结果异常。",
        )
        return runtime

    target_status, error = _classify(result)
    runtime.state = runtime.state.model_copy(
        update={
            "tool_status": result.status,
            "target_status": target_status,
            "coverage": result.coverage,
            "data_as_of": result.data_as_of,
            "limitations": _limitation_codes(result.limitations),
            "problems": list(error.problems) if error is not None else [],
            "error": error,
        }
    )
    return runtime


def persist_artifact(runtime: BusinessQueryRuntime, store: object) -> BusinessQueryRuntime:
    """Save only the strict public projection and convert persistence errors safely."""
    if runtime.state.status is not RunStatus.RUNNING:
        return runtime
    runtime.state = transition_state(runtime.state, BusinessQueryNode.PERSIST_ARTIFACT)
    result = runtime.result
    if result is None or runtime.catalog is None:
        _set_failure(
            runtime,
            code="result_contract_violation",
            stage=BusinessQueryNode.PERSIST_ARTIFACT,
            public_message="查询结果异常。",
        )
        return runtime
    try:
        public_payload = to_public_artifact(result, runtime.catalog)
        artifact = NewArtifact(
            payload=public_payload,
            data_as_of=result.data_as_of,
            coverage=result.coverage.model_dump(mode="json"),
        )
    except (ValidationError, ValueError):
        _set_failure(
            runtime,
            code="result_contract_violation",
            stage=BusinessQueryNode.PERSIST_ARTIFACT,
            public_message="查询结果异常。",
        )
        return runtime

    try:
        ref = store.save_artifact(runtime.state.run_id, artifact)  # type: ignore[attr-defined]
    except ArtifactPersistenceError:
        _set_failure(
            runtime,
            code="artifact_persistence_failed",
            stage=BusinessQueryNode.PERSIST_ARTIFACT,
            public_message="结果保存失败，请稍后重试。",
        )
        return runtime
    runtime.state = runtime.state.model_copy(
        update={"artifact_refs": [*runtime.state.artifact_refs, ref]}
    )
    return runtime


def finalize_run(runtime: BusinessQueryRuntime, store: object) -> BusinessQueryRuntime:
    """Finish the Run with the terminal status selected by classification."""
    if runtime.state.node is not BusinessQueryNode.PERSIST_ARTIFACT:
        return runtime
    runtime.state = transition_state(runtime.state, BusinessQueryNode.FINALIZE)
    target_status = runtime.state.target_status or DomainStatus.FAILED
    status = _run_status(target_status)
    runtime.state = runtime.state.model_copy(
        update={"status": status, "revision": runtime.state.revision + 1}
    )
    from bi_agent.runtime.models import RunCompletion

    store.finish(  # type: ignore[attr-defined]
        runtime.state.run_id,
        RunCompletion(
            expected_revision=runtime.state.revision - 1,
            node=runtime.state.node.value,
            status=status,
            state=runtime.state.model_dump(mode="json"),
            payload=_event_payload(runtime),
            error_code=runtime.state.error.code if runtime.state.error else None,
        ),
    )
    return runtime


def _classify(result: ToolResult) -> tuple[DomainStatus, ErrorEnvelope | None]:
    if result.status == "ok" and result.coverage.status == "complete":
        return DomainStatus.SUCCESS, None
    if (
        result.status == "ok"
        and result.coverage.status == "partial"
        and bool(result.data)
    ):
        return DomainStatus.PARTIAL, None
    if result.status == "missing_data":
        return DomainStatus.MISSING_DATA, None
    if result.status == "invalid_parameters":
        return DomainStatus.NEEDS_INPUT, ErrorEnvelope(
            code="invalid_parameters",
            stage="classify_result",
            retryable=False,
            recovery=RecoveryAction.CORRECT_PARAMETERS,
            public_message="查询参数无效",
            problems=["invalid_parameters"],
        )
    if result.status == "forbidden":
        return DomainStatus.FAILED, ErrorEnvelope(
            code="forbidden",
            stage="classify_result",
            retryable=False,
            recovery=RecoveryAction.NONE,
            public_message="查询范围无权限。",
            problems=["forbidden"],
        )
    if result.status == "unavailable":
        return DomainStatus.FAILED, ErrorEnvelope(
            code="unavailable",
            stage="classify_result",
            retryable=True,
            recovery=RecoveryAction.RETRY_LATER,
            public_message="查询暂不可用，请稍后重试。",
            problems=["unavailable"],
        )
    return DomainStatus.FAILED, ErrorEnvelope(
        code="result_contract_violation",
        stage="classify_result",
        retryable=False,
        recovery=RecoveryAction.NONE,
        public_message="查询结果异常。",
        problems=["result_contract_violation"],
    )


def _limitation_codes(limitations: list[str]) -> list[str]:
    codes: list[str] = []
    for limitation in limitations:
        code = _LIMITATION_CODES.get(limitation)
        if code is None and limitation.startswith("结果超过") and limitation.endswith("组，请缩小日期范围或店铺范围"):
            code = "result_too_large"
        if code is not None and code not in codes:
            codes.append(code)
    return codes


def _set_failure(
    runtime: BusinessQueryRuntime,
    *,
    code: str,
    stage: BusinessQueryNode,
    public_message: str,
) -> None:
    recovery = (
        RecoveryAction.RETRY_LATER
        if code in {"deadline_exceeded", "artifact_persistence_failed"}
        else RecoveryAction.NONE
    )
    runtime.state = runtime.state.model_copy(
        update={
            "status": RunStatus.FAILED,
            "target_status": DomainStatus.FAILED,
            "problems": [code],
            "error": ErrorEnvelope(
                code=code,  # type: ignore[arg-type]
                stage=stage.value,
                retryable=recovery is RecoveryAction.RETRY_LATER,
                recovery=recovery,
                public_message=public_message,  # type: ignore[arg-type]
                problems=[code],  # type: ignore[list-item]
            ),
        }
    )


def _run_status(status: DomainStatus) -> RunStatus:
    return {
        DomainStatus.SUCCESS: RunStatus.SUCCEEDED,
        DomainStatus.NEEDS_INPUT: RunStatus.NEEDS_INPUT,
        DomainStatus.MISSING_DATA: RunStatus.MISSING_DATA,
        DomainStatus.PARTIAL: RunStatus.PARTIAL,
        DomainStatus.FAILED: RunStatus.FAILED,
    }[status]


def _event_payload(runtime: BusinessQueryRuntime) -> dict[str, object]:
    state = runtime.state
    payload: dict[str, object] = {}
    if state.problems:
        payload["problem_codes"] = list(state.problems)
    if state.tool_status is not None:
        payload["tool_status"] = state.tool_status
    if state.target_status is not None:
        payload["target_status"] = state.target_status.value
    if state.coverage is not None:
        payload["coverage_status"] = state.coverage.status
    if state.data_as_of is not None:
        payload["data_as_of"] = state.data_as_of.isoformat()
    if state.limitations:
        payload["limitation_codes"] = list(state.limitations)
    if state.artifact_refs:
        payload["artifact_refs"] = [ref.model_dump(mode="json") for ref in state.artifact_refs]
    if runtime.result is not None:
        payload["result_count"] = len(runtime.result.data)
    return payload


def _resolve_shops(
    args: dict[str, object],
    runtime: BusinessQueryRuntime,
    ref_reverse: dict[str, str],
) -> tuple[list[str] | None, str]:
    """Turn model-supplied opaque refs back into real shop IDs.

    Anything that is not a known ref is kept verbatim and flagged
    ``invalid_shop``: authorize_scope only runs when every value resolved, so
    passing a real ERP key or a made-up name cannot reach the query.
    """
    raw_shops = args.get("shop_ids")
    if isinstance(raw_shops, list) and raw_shops:
        mapped: list[object] = []
        refs: list[str] = []
        all_recognized = True
        for value in raw_shops:
            if isinstance(value, str) and value in ref_reverse:
                mapped.append(ref_reverse[value])
                refs.append(value)
                continue
            mapped.append(value)
            refs.append("invalid_shop")
            all_recognized = False
        args["shop_ids"] = mapped
        return (
            refs,
            _REF_SHOPS if all_recognized else _UNRECOGNIZED_SHOPS,
        )
    if not raw_shops:
        previous_shops = runtime.context.previous_filters.get("shop_ids")
        if isinstance(previous_shops, list) and previous_shops:
            resolved = [str(shop_id) for shop_id in previous_shops]
            args["shop_ids"] = resolved
            return (
                [
                    runtime.context.shop_refs.get(shop_id, "invalid_shop")
                    for shop_id in resolved
                ],
                _PREVIOUS_FILTER_SHOPS,
            )
        return None, _MISSING_SHOPS
    return ["invalid_shop"], _UNRECOGNIZED_SHOPS


def _normalized_request(
    args: dict[str, object], *, shop_refs: list[str] | None
) -> dict[str, object]:
    normalized: dict[str, object] = {}
    if shop_refs is not None:
        normalized["shop_refs"] = shop_refs
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
