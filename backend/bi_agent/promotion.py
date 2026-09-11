"""显式假设计算推广预算；实绩和成本能力门槛明确。

本版不连接广告接口，也不建费用表；confirmed_inputs由当前用户明确输入
或页面表单产生，模型不能自行把ERP成本/优惠解释为实耗。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator
from zoneinfo import ZoneInfo

from .metrics import MAX_SPAN_DAYS, Coverage, ToolResult

BEIJING = ZoneInfo("Asia/Shanghai")
ASSUMPTION_BASIS = "用户输入假设"
NO_SOURCE_LIMITATION = "没有费用实绩来源，结果基于用户输入假设，不代表账号实际剩余额度"
NOT_AVAILABLE_LIMITATION = "尚未取得推广实耗及完整同口径成本，当前只能进行明确假设的预算测算"

_MODE_AMOUNTS = ("sales_estimate", "target_ratio", "budget", "assumed_spend")


# ---------------------------------------------------------------------------
# 投影契约（单一真源）
#
# runtime/models.py 的持久化白名单与 business_query/tool.py 的脱敏投影都引用这里，
# 不再手抄；下方 _project() 在生产方一侧强制行键与声明完全一致，新增列必须先改契约。
# ---------------------------------------------------------------------------

# 模式集只写一次：Literal 供请求校验/schema，frozenset 供持久化白名单复用。
PromotionMode = Literal["sales_cap", "budget_scenario", "actual_budget", "contribution_cap"]
PROMOTION_MODE_VALUES = frozenset(get_args(PromotionMode))

# 列名 -> 校验类别；date=ISO日期串，label=枚举串，decimal=十进制串，int=非负整数。
PROMOTION_COLUMN_KINDS: Mapping[str, str] = {
    "mode": "label",
    "basis": "label",
    "spent_through": "date",
    "sales_estimate": "decimal",
    "target_ratio": "decimal",
    "spend_cap": "decimal",
    "budget": "decimal",
    "assumed_spend": "decimal",
    "remaining_budget": "decimal",
    "overrun": "decimal",
    "daily_allowance": "decimal",
    "remaining_days": "int",
}

PROMOTION_RESULT_COLUMNS = frozenset(PROMOTION_COLUMN_KINDS)
PROMOTION_DATE_RESULT_COLUMNS = frozenset(
    key for key, kind in PROMOTION_COLUMN_KINDS.items() if kind == "date")
PROMOTION_LABEL_RESULT_COLUMNS = frozenset(
    key for key, kind in PROMOTION_COLUMN_KINDS.items() if kind == "label")
PROMOTION_NUMERIC_RESULT_COLUMNS = frozenset(
    key for key, kind in PROMOTION_COLUMN_KINDS.items() if kind in ("decimal", "int"))

# 标签列的可取值集合：mode 只允许四种模式，basis 只允许假设口径。
PROMOTION_LABEL_VALUES: Mapping[str, frozenset[str]] = {
    "mode": PROMOTION_MODE_VALUES,
    "basis": frozenset({ASSUMPTION_BASIS}),
}
# 声明了 label 却没有可取集合的列，在白名单里会变成无规则列，先在源头拦住。
assert frozenset(PROMOTION_LABEL_VALUES) == PROMOTION_LABEL_RESULT_COLUMNS
assert (PROMOTION_DATE_RESULT_COLUMNS | PROMOTION_LABEL_RESULT_COLUMNS
        | PROMOTION_NUMERIC_RESULT_COLUMNS) == PROMOTION_RESULT_COLUMNS

SPEND_CAP_COLUMNS: tuple[str, ...] = ("mode", "spend_cap", "sales_estimate", "target_ratio",
                                      "basis")
BUDGET_SCENARIO_COLUMNS: tuple[str, ...] = ("mode", "budget", "assumed_spend",
                                            "remaining_budget", "overrun", "remaining_days",
                                            "daily_allowance", "spent_through", "basis")

SPEND_CAP_DEFINITIONS: Mapping[str, str] = {
    "spend_cap": "上限=假设销售额×假设费用率；预测销售不达预期时阈值需调整",
}
BUDGET_SCENARIO_DEFINITIONS: Mapping[str, str] = {
    "remaining_budget": "剩余预算=预算-假设已花（下限0）",
    "overrun": "超支=假设已花-预算（下限0），单列展示",
    "daily_allowance": "日均可用=剩余预算/剩余天数；周期结束不除零",
}
PROMOTION_METRIC_DEFINITIONS: Mapping[str, str] = {
    **SPEND_CAP_DEFINITIONS, **BUDGET_SCENARIO_DEFINITIONS,
}

PERIOD_ENDED_LIMITATION = "周期已结束或无剩余天数，无法计算日均可用"
ASSUMPTION_ONLY_LIMITATION = "预算测算只能使用当前用户明确输入的假设，不能沿用上轮参数"
PROMOTION_PUBLIC_LIMITATIONS = frozenset({
    NO_SOURCE_LIMITATION, NOT_AVAILABLE_LIMITATION, PERIOD_ENDED_LIMITATION,
    ASSUMPTION_ONLY_LIMITATION,
})

# 确认校验的问题是按键名生成的模板，按键名白名单收紧成可校验模式。
_CONFIRMATION_KEYS = "(?:" + "|".join(_MODE_AMOUNTS) + ")"
PROMOTION_LIMITATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^缺少明确确认的 {_CONFIRMATION_KEYS}$"),
    re.compile(rf"^{_CONFIRMATION_KEYS} 与当前输入不一致，请重新确认$"),
)


def _project(declared: tuple[str, ...], values: Mapping[str, object]) -> dict[str, object]:
    """按声明列顺序产出结果行；行键与契约不一致即为开发期缺陷，直接失败。"""
    extra = set(values) - set(declared)
    missing = set(declared) - set(values)
    if extra or missing:
        raise ValueError(
            f"promotion_column_drift:{sorted(missing)}:{sorted(extra)}")
    unknown = set(declared) - PROMOTION_RESULT_COLUMNS
    if unknown:
        raise ValueError(f"promotion_column_undeclared:{sorted(unknown)}")
    return {key: values[key] for key in declared}


class PromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: PromotionMode
    start: date
    end: date
    currency: Literal["CNY"] = "CNY"
    sales_estimate: Decimal | None = Field(default=None, ge=0)
    target_ratio: Decimal | None = Field(default=None, ge=0, le=1)
    budget: Decimal | None = Field(default=None, ge=0)
    assumed_spend: Decimal | None = Field(default=None, ge=0)
    spent_through: date | None = None  # 假设实耗覆盖的排他截止日

    @model_validator(mode="after")
    def _check(self) -> "PromotionRequest":
        if self.end <= self.start:
            raise ValueError("end必须晚于start（排他区间）")
        if (self.end - self.start).days > MAX_SPAN_DAYS:
            raise ValueError(f"日期跨度最多{MAX_SPAN_DAYS}天")
        if self.mode == "sales_cap":
            if self.sales_estimate is None or self.target_ratio is None:
                raise ValueError("sales_cap需要sales_estimate和target_ratio")
            if self.budget is not None or self.assumed_spend is not None or self.spent_through is not None:
                raise ValueError("sales_cap不接受预算场景字段")
        elif self.mode == "budget_scenario":
            if self.budget is None or self.assumed_spend is None or self.spent_through is None:
                raise ValueError("budget_scenario需要budget、assumed_spend和spent_through")
            if self.sales_estimate is not None or self.target_ratio is not None:
                raise ValueError("budget_scenario不接受销售额假设字段")
            if not (self.start <= self.spent_through <= self.end):
                raise ValueError("spent_through必须在[start,end]范围内")
        else:
            for field_name in _MODE_AMOUNTS + ("spent_through",):
                if getattr(self, field_name) is not None:
                    raise ValueError(f"{self.mode}模式当前不可用，不接受假设金额")
        return self


def _decimal_input(confirmed: dict[str, object], key: str) -> Decimal | None:
    value = confirmed.get(key)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)) and not isinstance(value, bool):
        try:
            return Decimal(str(value))
        except ArithmeticError:
            return None
    return None


def _check_confirmed(request: PromotionRequest,
                     confirmed_inputs: dict[str, object]) -> list[str]:
    """执行前核对request金额与confirmed_inputs一致；返回缺失/不一致字段说明。"""
    problems: list[str] = []
    if request.mode == "sales_cap":
        pairs = (("sales_estimate", request.sales_estimate),
                 ("target_ratio", request.target_ratio))
    elif request.mode == "budget_scenario":
        pairs = (("budget", request.budget), ("assumed_spend", request.assumed_spend))
    else:
        return problems
    for key, expected in pairs:
        confirmed = _decimal_input(confirmed_inputs, key)
        if confirmed is None:
            problems.append(f"缺少明确确认的 {key}")
        elif expected is not None and confirmed != expected:
            problems.append(f"{key} 与当前输入不一致，请重新确认")
    return problems


def evaluate_promotion(request: PromotionRequest, *, confirmed_inputs: dict[str, object],
                       now: datetime) -> ToolResult:
    """确定性情景测算：实绩与成本能力未满足时明确缺数据，不用0或推测补齐。"""
    filters: dict[str, object] = {
        "mode": request.mode,
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
        "currency": request.currency,
    }
    if request.mode in ("actual_budget", "contribution_cap"):
        return ToolResult(
            status="missing_data",
            coverage=Coverage(status="missing", start=None, end=None),
            limitations=[NOT_AVAILABLE_LIMITATION], filters=filters)
    problems = _check_confirmed(request, confirmed_inputs)
    if problems:
        return ToolResult(
            status="invalid_parameters",
            coverage=Coverage(status="missing", start=None, end=None),
            limitations=problems + [ASSUMPTION_ONLY_LIMITATION],
            filters=filters)
    limitations = [NO_SOURCE_LIMITATION]
    if request.mode == "sales_cap":
        cap = request.sales_estimate * request.target_ratio  # type: ignore[operator]
        return ToolResult(
            status="ok",
            data=[_project(SPEND_CAP_COLUMNS, {
                "mode": "sales_cap",
                "spend_cap": format(cap, "f"),
                "sales_estimate": format(request.sales_estimate, "f"),
                "target_ratio": format(request.target_ratio, "f"),  # type: ignore[union-attr]
                "basis": ASSUMPTION_BASIS,
            })],
            metric_definition=dict(SPEND_CAP_DEFINITIONS),
            filters=filters,
            coverage=Coverage(status="missing", start=None, end=None),
            limitations=limitations)
    # budget_scenario
    assert request.budget is not None and request.assumed_spend is not None
    assert request.spent_through is not None
    remaining = max(Decimal(0), request.budget - request.assumed_spend)
    overrun = max(Decimal(0), request.assumed_spend - request.budget)
    days = (request.end - request.spent_through).days
    if days > 0:
        daily_allowance: Decimal | None = remaining / days
    else:
        daily_allowance = None
        limitations.append(PERIOD_ENDED_LIMITATION)
    data = _project(BUDGET_SCENARIO_COLUMNS, {
        "mode": "budget_scenario",
        "budget": format(request.budget, "f"),
        "assumed_spend": format(request.assumed_spend, "f"),
        "remaining_budget": format(remaining, "f"),
        "overrun": format(overrun, "f"),
        "remaining_days": days,
        "daily_allowance": format(daily_allowance, "f") if daily_allowance is not None else None,
        "spent_through": request.spent_through.isoformat(),
        "basis": ASSUMPTION_BASIS,
    })
    return ToolResult(
        status="ok", data=[data],
        metric_definition=dict(BUDGET_SCENARIO_DEFINITIONS),
        filters=filters,
        coverage=Coverage(status="missing", start=None, end=None),
        limitations=limitations)
