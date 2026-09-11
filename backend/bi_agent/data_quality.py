"""数据就绪判定：覆盖、业务截止、质量三件事分开取证。

口径来自 docs/superpowers/plans/2026-09-11-data-and-query-closure.md Task 1：

- `covered` 只说明哪段业务时间已完整入库，不说明数据对不对。
- `data_as_of` 只说明已完整处理的源数据截止时刻，禁止用 `last_success_at` 顶替。
- `quality_status` 只说明来源是否按版本化口径核验过；`unknown` 可出数但必须披露，
  `failed` 一律禁止出数。历史遗留的“没核验过”是 unknown，不是 failed。

本模块不 import `bi_agent.metrics`：查询请求按属性读取，指标层反过来依赖这里，
避免两个模块互相引用后各自演化出口径。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Literal, Sequence
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")

# 指标依赖哪些业务实体：覆盖门禁的唯一真源，指标层从这里引用。
ENTITY_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "paid_amount": ("orders",),
    "paid_orders": ("orders",),
    "erp_documents": ("orders",),
    "aov": ("orders",),
    "quantity": ("orders",),
    "product_paid_amount": ("orders",),
    "refund_amount": ("orders", "aftersales_occurrence"),
    "cash_difference": ("orders", "aftersales_occurrence"),
    "cohort_refund_rate": ("orders", "aftersales_cohort"),
}

# 同一覆盖来源名：同步状态按数据来源记录。
ENTITY_SOURCES: dict[str, str] = {
    "orders": "erp.trade.list.query",
    "aftersales_occurrence": "erp.aftersale.list.query",
    "aftersales_cohort": "erp.aftersale.list.query",
}

_COVERAGE_SQL = """
SELECT shop_id,
       tstzmultirange(tstzrange(%s, %s, '[)')) * covered AS covered_part,
       tstzmultirange(tstzrange(%s, %s, '[)')) - covered AS missing,
       data_as_of, quality_status, quality_rule
FROM reporting.v_coverage
WHERE source=%s AND entity=%s AND shop_id = ANY(%s)
"""

_BATCHES_SQL = """
SELECT DISTINCT batch_id FROM reporting.v_source_batches
WHERE source=%s AND entity=%s AND shop_id = ANY(%s)
  AND window_kind = 'business' AND window_start < %s AND window_end > %s
"""

# 平台成功退款找不到原单：退款归属未确认，不能当已核验数据出数。
# 定义在本模块，指标层引用同一份，避免两边口径漂移。
UNMATCHED_REFUNDS_SQL = """
SELECT count(*) FROM reporting.v_refunds
WHERE shop_id = ANY(%s) AND platform_success AND refund_canonical
  AND platform_completed_at >= %s AND platform_completed_at < %s
  AND (commercial_id IS NULL OR NOT matched)
"""

# 核验口径版本：规则一变，旧的 passed 自动失效（降级为 unknown）。
QUALITY_RULE = "kuaimai-reconcile/1"

QualityStatus = Literal["unknown", "passed", "failed"]
CoverageStatus = Literal["complete", "partial", "missing"]
Window = tuple[str, str]
Span = tuple[date, date]


@dataclass(frozen=True)
class CoverageGap:
    """结构化缺口：归因到实体与店铺，供恢复策略决定该怎么回答。

    shop_id 是 ERP 主键，只能留在服务端对象里；对外投影依旧走 coverage
    的日期串缺口，不能让缺口反而成为名称/主键的泄露面。
    """

    entity: str
    shop_id: str
    start: date
    end: date

    @property
    def window(self) -> Window:
        return (self.start.isoformat(), self.end.isoformat())


@dataclass(frozen=True)
class CoverageAssessment:
    """一次请求的就绪结论。原请求窗口在此冻结，建议窗口只是建议。"""

    status: CoverageStatus
    requested_window: Window
    covered_windows: tuple[Window, ...]
    missing_windows: tuple[Window, ...]
    data_as_of: datetime | None
    quality_status: QualityStatus
    source_batches: tuple[str, ...]
    gaps: tuple[CoverageGap, ...]
    suggested_window: Window | None


def _effective_quality(status: object, rule: object) -> QualityStatus:
    """passed 只在口径版本仍旧时成立；旧版本的对账结果不能自动沿用。"""
    if str(status) == "passed" and str(rule or "") != QUALITY_RULE:
        return "unknown"
    return str(status) if str(status) in ("unknown", "passed", "failed") else "unknown"


def reconcile_source_quality(conn, *, shop_id: str, entity: str,
                             start: datetime, end: datetime) -> QualityStatus:
    """跑一次可审计的核验，并回写质量状态；返回新的状态。

    “同步成功”本身不是核验：只有本窗口内确实落了 reconcile 批次凭证，
    才有资格改质量状态。没凭证就维持 unknown，既不谎称已核验，
    也不凭空降级成 failed。
    """
    source = ENTITY_SOURCES[entity]
    evidence = conn.execute(
        "SELECT count(*) FROM reporting.v_source_batches "
        "WHERE source=%s AND entity=%s AND shop_id=%s "
        "AND window_kind = 'business' AND window_start < %s AND window_end > %s",
        (source, entity, shop_id, end, start),
    ).fetchone()[0]
    if not evidence:
        return "unknown"

    unmatched = conn.execute(UNMATCHED_REFUNDS_SQL, ([shop_id], start, end)).fetchone()[0]
    status: QualityStatus = "failed" if unmatched else "passed"
    conn.execute(
        "UPDATE bi.sync_state SET quality_status=%s, quality_checked_at=now(), "
        "quality_rule=%s, quality_reason=%s "
        "WHERE source=%s AND entity=%s AND shop_id=%s",
        (status, QUALITY_RULE, "unmatched_success_refunds" if unmatched else None,
         source, entity, shop_id),
    )
    return status


def required_entities(metrics: Sequence[str]) -> list[str]:
    """这次查询到底依赖哪些业务实体。"""
    return sorted({entity for metric in metrics for entity in ENTITY_REQUIREMENTS[metric]})


def _spans(value) -> list[Span]:
    """把 SQL 算好的 multirange 读成北京时间下的日期区间对。

    必须显式换回北京时区再取日期：数据库会话默认 UTC，直接 .date()
    会把其峰8点的边界算成前一天，整段缺口都会偏早一天。
    """
    spans: list[Span] = []
    for rng in value or []:
        if rng.lower is None or rng.upper is None:
            continue
        spans.append((rng.lower.astimezone(BEIJING).date(),
                      rng.upper.astimezone(BEIJING).date()))
    return spans


def _windows(spans: Iterable[Span]) -> tuple[Window, ...]:
    return tuple(sorted({(span_start.isoformat(), span_end.isoformat())
                         for span_start, span_end in spans}))


def _suggested(covered_windows: tuple[Window, ...], requested: Window,
               status: CoverageStatus) -> Window | None:
    """请求内最大的连续已覆盖段；只返回来当建议，调用方不得回写窗口。"""
    if status == "complete":
        return requested
    if not covered_windows:
        return None
    return max(covered_windows,
               key=lambda item: (date.fromisoformat(item[1])
                                 - date.fromisoformat(item[0]), item[0]))


def assess_query_coverage(conn, request) -> CoverageAssessment:
    """在跑指标 SQL 之前判定：能不能出数、缺哪一段、来源是否已核验。

    只读 reporting 视图，所以聊天 API 的 bi_app 身份也能调用。
    """
    start, end = request.start, request.end
    start_ts = datetime(start.year, start.month, start.day, tzinfo=BEIJING)
    end_ts = datetime(end.year, end.month, end.day, tzinfo=BEIJING)
    requested: Window = (start.isoformat(), end.isoformat())
    shop_ids = sorted({str(shop_id) for shop_id in request.shop_ids})

    covered_spans: list[Span] = []
    missing_spans: list[Span] = []
    gaps: list[CoverageGap] = []
    cutoffs: list[datetime] = []
    qualities: list[QualityStatus] = []
    batches: list[str] = []
    pairs = 0

    for entity in required_entities(request.metrics):
        source = ENTITY_SOURCES[entity]
        # 一个实体一次查完：区间运算留在 SQL 里，不按店铺逐条往返。
        states = {str(row[0]): row for row in conn.execute(
            _COVERAGE_SQL, (start_ts, end_ts, start_ts, end_ts,
                            source, entity, shop_ids)).fetchall()}
        batches.extend(str(row[0]) for row in conn.execute(
            _BATCHES_SQL, (source, entity, shop_ids, end_ts, start_ts)).fetchall())

        for shop_id in shop_ids:
            pairs += 1
            row = states.get(shop_id)
            if row is None:
                # 没有同步状态行就是从未取过数：未知，不是“确实没有交易”。
                qualities.append("unknown")
                gaps.append(CoverageGap(entity=entity, shop_id=shop_id,
                                        start=start, end=end))
                missing_spans.append((start, end))
                continue
            qualities.append(_effective_quality(row[4], row[5]))
            if row[3] is not None:
                cutoffs.append(row[3])
            covered_spans.extend(_spans(row[1]))
            missing = _spans(row[2])
            if missing:
                missing_spans.extend(missing)
                gaps.extend(CoverageGap(entity=entity, shop_id=shop_id,
                                        start=gap[0], end=gap[1]) for gap in missing)

    if "failed" in qualities:
        quality_status: QualityStatus = "failed"
    elif qualities and all(item == "passed" for item in qualities):
        quality_status = "passed"
    else:
        quality_status = "unknown"

    covered_windows = _windows(covered_spans)
    missing_windows = _windows(missing_spans)
    if not missing_windows:
        status: CoverageStatus = "complete"
    elif covered_windows:
        status = "partial"
    else:
        status = "missing"

    return CoverageAssessment(
        status=status,
        requested_window=requested,
        covered_windows=covered_windows,
        missing_windows=missing_windows,
        # 共同截止：任一依赖项没有推进 data_as_of，整体截止就是未知。
        data_as_of=min(cutoffs) if len(cutoffs) == pairs else None,
        quality_status=quality_status,
        source_batches=tuple(sorted(set(batches))),
        gaps=tuple(gaps),
        suggested_window=_suggested(covered_windows, requested, status),
    )
