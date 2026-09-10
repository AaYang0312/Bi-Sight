"""Public models and safe persistence contracts for deterministic query runs."""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from bi_agent.metrics import Coverage, METRIC_DEFINITIONS

PersistenceNode = Literal[
    "received", "resolve_parameters", "validate_parameters", "authorize_scope",
    "execute_fixed_query", "classify_result", "persist_artifact", "finalize",
]
ErrorCode = Literal[
    "missing_parameters", "invalid_parameters", "forbidden", "deadline_exceeded",
    "unavailable", "result_contract_violation", "artifact_persistence_failed",
    "invalid_transition",
]
ProblemCode = Literal[
    "missing_parameters", "invalid_parameters", "invalid_date_range", "invalid_metric",
    "invalid_group_by", "invalid_compare", "invalid_top_n", "invalid_shop", "forbidden",
    "deadline_exceeded", "unavailable", "result_contract_violation",
    "artifact_persistence_failed", "invalid_transition",
]
PublicMessage = Literal[
    "查询参数无效",
    "查询参数无效，请调整后重试。",
    "缺少查询参数，请补充后重试。",
    "查询范围无权限。",
    "查询暂不可用。",
    "查询暂不可用，请稍后重试。",
    "查询已超时，请稍后重试。",
    "查询结果异常。",
    "结果保存失败，请稍后重试。",
]

_METRICS = frozenset(METRIC_DEFINITIONS)
_TOOL_STATUSES = frozenset({
    "ok", "missing_data", "invalid_parameters", "forbidden", "unavailable",
})
_PUBLIC_PAYLOAD_STATUSES = _TOOL_STATUSES | frozenset({"needs_input", "partial", "failed"})
_RUN_STATUSES = frozenset({
    "running", "succeeded", "needs_input", "missing_data", "partial", "failed",
})
_DOMAIN_STATUSES = frozenset({"success", "needs_input", "missing_data", "partial", "failed"})
_GROUP_BY = frozenset({"total", "day", "shop", "product"})
_COMPARE = frozenset({"none", "previous_period"})
_COVERAGE_STATUSES = frozenset({"complete", "partial", "missing"})
_PROBLEM_CODES = frozenset({
    "missing_parameters", "invalid_parameters", "invalid_date_range", "invalid_metric",
    "invalid_group_by", "invalid_compare", "invalid_top_n", "invalid_shop", "forbidden",
    "deadline_exceeded", "unavailable", "result_contract_violation",
    "artifact_persistence_failed", "invalid_transition",
})
_LIMITATION_CODES = frozenset({
    "coverage_incomplete", "data_as_of_unknown", "shop_not_synced", "shops_inactive",
    "comparison_coverage_incomplete", "deadline_exceeded", "query_timeout", "forbidden",
    "result_too_large", "cohort_rate_not_computable",
})
_PUBLIC_LIMITATIONS = frozenset({
    "店铺不在授权范围",
    "本次查询时间预算已耗尽",
    "查询超时",
    "店铺尚未同步，无法查询",
    "部分店铺已停用，仅返回剩余范围",
    "所选店铺均已停用，无法查询",
    "覆盖未完成，拒绝部分汇总；缺口见coverage.gaps",
    "数据截止未知（回填未完成）",
    "上期覆盖不足，无法比较，仅返回绝对值",
    "比较仅支持total/shop分组",
    "同批支付额为0或无支付，同批退款率不可计算",
})
_PUBLIC_LIMITATION_PATTERNS = (
    re.compile(r"^存在[0-9]+条未匹配的平台成功退款，退款归属未确认$"),
    re.compile(r"^结果超过[0-9]+组，请缩小日期范围或店铺范围$"),
)
_STATE_KEYS = frozenset({
    "run_id", "node", "status", "revision", "normalized_request", "problems",
    "tool_status", "target_status", "coverage", "data_as_of", "limitations",
    "artifact_refs", "error",
})
_EVENT_KEYS = frozenset({
    "problem_codes", "tool_status", "target_status", "coverage_status", "data_as_of",
    "limitation_codes", "artifact_refs", "result_count",
})
_NORMALIZED_REQUEST_KEYS = frozenset({
    "shop_aliases", "metrics", "start", "end", "group_by", "compare", "top_n", "currency",
})
_ARTIFACT_KEYS = frozenset({
    "status", "metric_definition", "coverage", "limitations", "data_as_of", "filters", "data",
})
_FILTER_KEYS = frozenset({
    "start", "end", "shop_ids", "metrics", "group_by", "compare", "top_n", "currency",
})
_RESULT_COLUMNS = frozenset({
    "day", "shop_id", "product_id", "line_kind", "currency", "basis",
    "paid_amount", "paid_orders", "erp_documents", "aov", "refund_amount",
    "cash_difference", "cohort_refund_rate", "quantity", "product_paid_amount",
    "spend_cap", "budget", "actual_spend", "remaining_budget", "over_budget",
    "remaining_days", "daily_cap", "contribution_cap",
})
_NUMERIC_RESULT_COLUMNS = _RESULT_COLUMNS - {
    "day", "shop_id", "product_id", "line_kind", "currency", "basis",
}
_LINE_KINDS = frozenset({"sale", "gift", "suite", "combination", "processing"})
_NODES = frozenset({
    "received", "resolve_parameters", "validate_parameters", "authorize_scope",
    "execute_fixed_query", "classify_result", "persist_artifact", "finalize",
})
_ALIAS_RE = re.compile(r"^shop_[1-9][0-9]*$")
_PUBLIC_SHOP_RE = re.compile(r"^店铺[1-9][0-9]*$")
_PRODUCT_ALIAS_RE = re.compile(r"^商品[A-Z]+$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_GAP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}~[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


def _unsafe_payload() -> None:
    raise ValueError("unsafe_persistence_payload")


def _mapping(value: object, *, allowed: frozenset[str],
             required: frozenset[str] = frozenset()) -> dict[str, object]:
    if not isinstance(value, dict) or not required <= value.keys() or not value.keys() <= allowed:
        _unsafe_payload()
    return value


def _string_in(value: object, allowed: frozenset[str]) -> None:
    if not isinstance(value, str) or value not in allowed:
        _unsafe_payload()


def _non_negative_int(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _unsafe_payload()


def _positive_int(value: object) -> None:
    _non_negative_int(value)
    if value == 0:
        _unsafe_payload()


def _date_string(value: object) -> None:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        _unsafe_payload()
    try:
        date.fromisoformat(value)
    except ValueError:
        _unsafe_payload()


def _datetime_string(value: object) -> None:
    if not isinstance(value, str) or "T" not in value:
        _unsafe_payload()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _unsafe_payload()
    if parsed.tzinfo is None:
        _unsafe_payload()


def _alias(value: object, *, public: bool) -> None:
    if not isinstance(value, str):
        _unsafe_payload()
    pattern = _PUBLIC_SHOP_RE if public else _ALIAS_RE
    if not pattern.fullmatch(value):
        _unsafe_payload()


def _alias_or_invalid_shop(value: object) -> None:
    if value == "invalid_shop":
        return
    _alias(value, public=False)


def _string_list(value: object, validator) -> None:
    if not isinstance(value, list):
        _unsafe_payload()
    for item in value:
        validator(item)


def _coverage(value: object) -> dict[str, object]:
    coverage = _mapping(
        value,
        allowed=frozenset({"status", "start", "end", "gaps"}),
        required=frozenset({"status", "start", "end", "gaps"}),
    )
    _string_in(coverage["status"], _COVERAGE_STATUSES)
    for boundary in ("start", "end"):
        if coverage[boundary] is not None:
            _date_string(coverage[boundary])
    if not isinstance(coverage["gaps"], list):
        _unsafe_payload()
    for gap in coverage["gaps"]:
        if not isinstance(gap, str) or not _GAP_RE.fullmatch(gap):
            _unsafe_payload()
    return coverage


def _artifact_refs(value: object) -> None:
    if not isinstance(value, list):
        _unsafe_payload()
    for item in value:
        ref = _mapping(item, allowed=frozenset({"id", "type"}),
                       required=frozenset({"id", "type"}))
        if not isinstance(ref["id"], str):
            _unsafe_payload()
        try:
            UUID(ref["id"])
        except ValueError:
            _unsafe_payload()
        if ref["type"] != "metric_result":
            _unsafe_payload()


def _normalized_request(value: object) -> dict[str, object]:
    request = _mapping(value, allowed=_NORMALIZED_REQUEST_KEYS)
    if "shop_aliases" in request:
        _string_list(request["shop_aliases"], _alias_or_invalid_shop)
    if "metrics" in request:
        _string_list(request["metrics"], lambda item: _string_in(item, _METRICS))
    for boundary in ("start", "end"):
        if boundary in request:
            _date_string(request[boundary])
    if "group_by" in request:
        _string_in(request["group_by"], _GROUP_BY)
    if "compare" in request:
        _string_in(request["compare"], _COMPARE)
    if "top_n" in request:
        _positive_int(request["top_n"])
        if request["top_n"] > 500:
            _unsafe_payload()
    if "currency" in request and request["currency"] != "CNY":
        _unsafe_payload()
    return request


def _state_error(value: object) -> None:
    if not isinstance(value, dict):
        _unsafe_payload()
    try:
        ErrorEnvelope.model_validate(value)
    except ValidationError as error:
        raise ValueError("unsafe_persistence_payload") from error


def validate_persisted_state(value: object) -> dict[str, object]:
    state = _mapping(value, allowed=_STATE_KEYS)
    if state and "node" not in state:
        _unsafe_payload()
    if "run_id" in state:
        if not isinstance(state["run_id"], str):
            _unsafe_payload()
        try:
            UUID(state["run_id"])
        except ValueError:
            _unsafe_payload()
    if "node" in state:
        _string_in(state["node"], _NODES)
    if "status" in state:
        _string_in(state["status"], _RUN_STATUSES)
    if "revision" in state:
        _non_negative_int(state["revision"])
    if "normalized_request" in state:
        _normalized_request(state["normalized_request"])
    if "problems" in state:
        _string_list(state["problems"], lambda item: _string_in(item, _PROBLEM_CODES))
    if "tool_status" in state and state["tool_status"] is not None:
        _string_in(state["tool_status"], _TOOL_STATUSES)
    if "target_status" in state and state["target_status"] is not None:
        _string_in(state["target_status"], _DOMAIN_STATUSES)
    if "coverage" in state and state["coverage"] is not None:
        _coverage(state["coverage"])
    if "data_as_of" in state and state["data_as_of"] is not None:
        _datetime_string(state["data_as_of"])
    if "limitations" in state:
        _string_list(state["limitations"], lambda item: _string_in(item, _LIMITATION_CODES))
    if "artifact_refs" in state:
        _artifact_refs(state["artifact_refs"])
    if "error" in state and state["error"] is not None:
        _state_error(state["error"])
    return state


def validate_event_payload(value: object) -> dict[str, object]:
    payload = _mapping(value, allowed=_EVENT_KEYS)
    if "problem_codes" in payload:
        _string_list(payload["problem_codes"], lambda item: _string_in(item, _PROBLEM_CODES))
    if "tool_status" in payload:
        _string_in(payload["tool_status"], _TOOL_STATUSES)
    if "target_status" in payload:
        _string_in(payload["target_status"], _DOMAIN_STATUSES)
    if "coverage_status" in payload:
        _string_in(payload["coverage_status"], _COVERAGE_STATUSES)
    if "data_as_of" in payload:
        _datetime_string(payload["data_as_of"])
    if "limitation_codes" in payload:
        _string_list(payload["limitation_codes"], lambda item: _string_in(item, _LIMITATION_CODES))
    if "artifact_refs" in payload:
        _artifact_refs(payload["artifact_refs"])
    if "result_count" in payload:
        _non_negative_int(payload["result_count"])
    return payload


def _public_limitation(value: object) -> None:
    if not isinstance(value, str) or (
        value not in _PUBLIC_LIMITATIONS
        and not any(pattern.fullmatch(value) for pattern in _PUBLIC_LIMITATION_PATTERNS)
    ):
        _unsafe_payload()


def _numeric_result(value: object) -> None:
    if value is None:
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, str) and _DECIMAL_RE.fullmatch(value):
        return
    _unsafe_payload()


def _result_rows(value: object, *, public: bool) -> None:
    if not isinstance(value, list):
        _unsafe_payload()
    for item in value:
        row = _mapping(item, allowed=_RESULT_COLUMNS)
        if not row:
            _unsafe_payload()
        for key, result_value in row.items():
            if key in _NUMERIC_RESULT_COLUMNS:
                _numeric_result(result_value)
            elif key == "day":
                _date_string(result_value)
            elif key == "shop_id":
                _alias(result_value, public=public)
            elif key == "product_id":
                if not isinstance(result_value, str) or not _PRODUCT_ALIAS_RE.fullmatch(result_value):
                    _unsafe_payload()
            elif key == "line_kind":
                _string_in(result_value, _LINE_KINDS)
            elif key == "currency":
                if result_value != "CNY":
                    _unsafe_payload()
            elif key == "basis" and result_value != "用户输入假设":
                _unsafe_payload()


def _filters(value: object, *, public: bool) -> None:
    filters = _mapping(value, allowed=_FILTER_KEYS)
    for boundary in ("start", "end"):
        if boundary in filters:
            _date_string(filters[boundary])
    if "shop_ids" in filters:
        _string_list(filters["shop_ids"], lambda item: _alias(item, public=public))
    if "metrics" in filters:
        _string_list(filters["metrics"], lambda item: _string_in(item, _METRICS))
    if "group_by" in filters:
        _string_in(filters["group_by"], _GROUP_BY)
    if "compare" in filters:
        _string_in(filters["compare"], _COMPARE)
    if "top_n" in filters:
        _positive_int(filters["top_n"])
        if filters["top_n"] > 500:
            _unsafe_payload()
    if "currency" in filters and filters["currency"] != "CNY":
        _unsafe_payload()


def _metric_definitions(value: object) -> None:
    definitions = _mapping(value, allowed=_METRICS)
    for metric, definition in definitions.items():
        if definition != METRIC_DEFINITIONS[metric]:
            _unsafe_payload()


def _public_metric_payload(value: object, *, public: bool) -> dict[str, object]:
    payload = _mapping(value, allowed=_ARTIFACT_KEYS, required=frozenset({"status"}))
    _string_in(payload["status"], _PUBLIC_PAYLOAD_STATUSES)
    if "metric_definition" in payload:
        _metric_definitions(payload["metric_definition"])
    if "coverage" in payload:
        _coverage(payload["coverage"])
    if "limitations" in payload:
        _string_list(payload["limitations"], _public_limitation)
    if "data_as_of" in payload and payload["data_as_of"] is not None:
        _datetime_string(payload["data_as_of"])
    if "filters" in payload:
        _filters(payload["filters"], public=public)
    if "data" in payload:
        _result_rows(payload["data"], public=public)
    return payload


def validate_model_payload(value: object) -> dict[str, object]:
    return _public_metric_payload(value, public=False)


def validate_artifact_payload(value: object) -> dict[str, object]:
    return _public_metric_payload(value, public=True)


def validate_coverage_payload(value: object) -> dict[str, object]:
    return _coverage(value)


def validate_normalized_request(value: object) -> dict[str, object]:
    return _normalized_request(value)


NormalizedRequest = Annotated[dict[str, object], BeforeValidator(validate_normalized_request)]
PersistedState = Annotated[dict[str, object], BeforeValidator(validate_persisted_state)]
EventPayload = Annotated[dict[str, object], BeforeValidator(validate_event_payload)]
ModelPayload = Annotated[dict[str, object], BeforeValidator(validate_model_payload)]
ArtifactPayload = Annotated[dict[str, object], BeforeValidator(validate_artifact_payload)]
CoveragePayload = Annotated[dict[str, object], BeforeValidator(validate_coverage_payload)]


class DomainStatus(StrEnum):
    SUCCESS = "success"
    NEEDS_INPUT = "needs_input"
    MISSING_DATA = "missing_data"
    PARTIAL = "partial"
    FAILED = "failed"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    NEEDS_INPUT = "needs_input"
    MISSING_DATA = "missing_data"
    PARTIAL = "partial"
    FAILED = "failed"


class RecoveryAction(StrEnum):
    NONE = "none"
    ASK_USER = "ask_user"
    CORRECT_PARAMETERS = "correct_parameters"
    RETRY_LATER = "retry_later"


class RunEventType(StrEnum):
    ENTERED = "entered"
    COMPLETED = "completed"
    FAILED = "failed"
    TRANSITIONED = "transitioned"


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    code: ErrorCode
    stage: PersistenceNode
    retryable: bool
    recovery: RecoveryAction
    public_message: PublicMessage
    problems: list[ProblemCode] = Field(default_factory=list)


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: UUID
    type: Literal["metric_result"]


class DomainArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    ref: ArtifactRef
    public_payload: ArtifactPayload


class DomainResult(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    run_id: UUID
    status: DomainStatus
    model_payload: ModelPayload
    artifacts: list[DomainArtifact] = Field(default_factory=list)
    data_as_of: datetime | None = None
    coverage: Coverage | None = None
    error: ErrorEnvelope | None = None


class NewQueryRun(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    chat_id: UUID
    user_message_id: UUID
    subject_id: str
    tool_call_id: str
    domain: Literal["business_query"] = "business_query"
    attempt_no: int = Field(ge=1)
    normalized_request: NormalizedRequest = Field(default_factory=dict)
    state: PersistedState = Field(default_factory=dict)


class RunTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    expected_revision: int = Field(ge=0)
    node: PersistenceNode
    event_type: RunEventType = RunEventType.TRANSITIONED
    status: RunStatus
    state: PersistedState
    normalized_request: NormalizedRequest | None = None
    payload: EventPayload = Field(default_factory=dict)
    error_code: ErrorCode | None = None


class NewArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    artifact_type: Literal["metric_result"] = "metric_result"
    payload: ArtifactPayload
    data_as_of: datetime | None = None
    coverage: CoveragePayload | None = None


class RunCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    expected_revision: int = Field(ge=0)
    node: PersistenceNode
    status: RunStatus
    state: PersistedState
    payload: EventPayload = Field(default_factory=dict)
    error_code: ErrorCode | None = None


class TurnContext(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    chat_id: UUID
    user_message_id: UUID
    subject_id: str


class RunContextNotFound(Exception):
    def __init__(self) -> None:
        super().__init__("run_context_not_found")


class RunNotFound(Exception):
    def __init__(self) -> None:
        super().__init__("run_not_found")


class StaleRunRevision(Exception):
    def __init__(self) -> None:
        super().__init__("stale_run_revision")


class ArtifactPersistenceError(Exception):
    def __init__(self, _reason: str | None = None) -> None:
        super().__init__("artifact_persistence_error")


class QueryRunStore(Protocol):
    def create_run(self, record: NewQueryRun) -> UUID: ...

    def transition(self, run_id: UUID, transition: RunTransition) -> None: ...

    def save_artifact(self, run_id: UUID, artifact: NewArtifact) -> ArtifactRef: ...

    def finish(self, run_id: UUID, completion: RunCompletion) -> None: ...
