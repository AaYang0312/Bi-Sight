"""字段规范化、窗口分页、事务、水位、补查与同步CLI。

本模块内的事务函数不自行提交；上层窗口同步统一提交整个窗口。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import load_sync_settings
from .kuaimai import KuaimaiClient, KuaimaiError, parse_page

logger = logging.getLogger(__name__)

BEIJING = ZoneInfo("Asia/Shanghai")

ORDER_SOURCE = "erp.trade.list.query"
# 官方 erp.trade.list.query 明确排除淘系、拼多多订单；淘系（淘宝/天猫）改走
# 交易模块销售出库通道，只有非敏感字段（收件人/买家昵称/平台支付金额等不返回）。
OUTSTOCK_SOURCE = "erp.trade.outstock.simple.query"
AFTERSALE_SOURCE = "erp.aftersale.list.query"

# 平台→订单源路由：键为 bi.shops.platform（小写），未命中回退默认源。
# sync_state 主键含 source，淘系出库通道的覆盖/水位与抖音通道互不干扰。
ORDER_SOURCE_BY_PLATFORM = {
    "tb": OUTSTOCK_SOURCE,
    "tm": OUTSTOCK_SOURCE,
}

# ---------------------------------------------------------------------------
# PII 红线（2026-09-12 淘系接入约定）：下列字段在出库/售后响应中出现（部分
# 平台值已脱敏但仍非空），一律不规范化、不入库、不写日志。入库字段集维持
# sql/001_init.sql 现有列，一个都不加；未来扩列评审必须先复核本清单，
# tests/test_core.py 以本清单断言规范化键集与列白名单不漂移。
# 注意：platformPaymentAmount 不属于 PII，只是淘系不返回（现有列自然为 NULL）。
# ---------------------------------------------------------------------------
PII_FORBIDDEN_FIELDS = frozenset({
    "buyerNick", "buyerMessage", "buyerName", "buyerPhone",
    "receiverName", "receiverPhone", "receiverMobile", "receiverAddress",
    "receiverState", "receiverCity", "receiverDistrict", "receiverStreet",
    "receiverZip", "receiverCountry", "taobaoId", "ptConsignTime",
    "invoiceName", "invoiceRemark", "invoiceKind", "tradeInvoice",
    "shopName", "sellerNick", "openUid", "mobileTail",
})

# 单实例同步锁；锁放在整个CLI运行入口，sync_window内部仅负责单窗口事务
LOCK_ID = 7319041
# 增量重叠；回填开始前记录T0，完成后补拉[T0,固定T1)
SYNC_OVERLAP = timedelta(minutes=10)
PAGE_SIZE = 200
# 同批退款tids补查单次ID数量；执行4.7时按官方文档或小样本确认后固定
COHORT_TIDS_BATCH = 50
MAX_QUERY_DAYS = 366


# ---------------------------------------------------------------------------
# 基础解析：金额与时间
# ---------------------------------------------------------------------------


def to_decimal(value: Any) -> Decimal | None:
    """解析金额；缺失/非法（NaN、Infinity、非数值）返回None，负值保留由语义判断。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, Decimal):
            number = value
        elif isinstance(value, (int, float, str)):
            number = Decimal(str(value).strip())
        else:
            return None
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return number


def parse_timestamp(value: Any) -> datetime | None:
    """解析快麦时间：毫秒整数或字符串（北京时间）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return datetime.fromtimestamp(float(value) / 1000.0, tz=BEIJING)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return datetime.fromtimestamp(float(text) / 1000.0, tz=BEIJING)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=BEIJING)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed


# ---------------------------------------------------------------------------
# 交易规范化
# ---------------------------------------------------------------------------


def _unique(values) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        text = (value or "").strip()
        if text and text not in seen:
            seen[text] = None
    return list(seen)


def _commercial_ids(raw: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    values = [str(raw.get("tid") or "")]
    tids = raw.get("tids")
    if isinstance(tids, str):
        values.extend(tids.split(","))
    elif isinstance(tids, list):
        values.extend(str(t) for t in tids)
    values.extend(str(item["commercial_id"] or "") for item in items)
    return _unique(values)


def _normalise_item(raw_item: dict[str, Any], erp_id: str, index: int,
                    fallback_paid_at: datetime | None) -> dict[str, Any]:
    quantity = to_decimal(raw_item.get("num")) or Decimal(0)
    gift_quantity = to_decimal(raw_item.get("giftNum")) or Decimal(0)
    if gift_quantity > 0 and quantity == 0:
        line_kind = "gift"
    else:
        line_kind = str(raw_item.get("lineKind") or "sale")
    allocated = to_decimal(raw_item.get("payAmount"))
    return {
        "line_id": str(raw_item.get("id") or raw_item.get("oid")
                       or f"{erp_id}#{index}"),
        "commercial_id": (str(raw_item.get("tid") or "").strip() or None),
        "platform_line_id": (str(raw_item.get("platformOid") or "").strip() or None),
        "product_id": (str(raw_item.get("itemSysId") or "").strip() or None),
        "sku_id": (str(raw_item.get("skuSysId") or "").strip() or None),
        "paid_at": parse_timestamp(raw_item.get("payTime")) or fallback_paid_at,
        "quantity": quantity,
        "gift_quantity": gift_quantity,
        "raw_paid_amount": to_decimal(raw_item.get("payAmount")),
        "raw_payment": to_decimal(raw_item.get("payment")),
        "raw_unit_cost": to_decimal(raw_item.get("cost")),
        "allocated_paid_amount": allocated,
        "allocation_verified": False,
        "line_kind": line_kind,
        "active": True,
    }


def _split_parent_id(raw: dict[str, Any]) -> str | None:
    """拆单父单映射：交易查询用 splitParentId；出库通道为 splitSid
    （官方文档：splitType=1 时为拆单主单 sid，否则 -1）。-1/空视为无拆单。"""
    for key in ("splitParentId", "splitSid"):
        text = str(raw.get(key) or "").strip()
        if text and text != "-1":
            return text
    return None


def normalise_trade(raw: dict[str, Any], *, source: str = ORDER_SOURCE) -> dict[str, Any]:
    """白名单规范化一单ERP交易；缺少orders字段与合法空列表不同。"""
    erp_id = str(raw.get("sid") or "").strip()
    shop_id = str(raw.get("userId") or "").strip()
    source_updated_at = parse_timestamp(raw.get("updTime")) or parse_timestamp(raw.get("modified"))
    paid_at = parse_timestamp(raw.get("payTime"))
    raw_pay_amount = to_decimal(raw.get("payAmount"))
    status = "normal"
    if not erp_id or not shop_id or source_updated_at is None:
        status = "invalid"
    elif raw_pay_amount is not None and raw_pay_amount < 0:
        status = "needs_review"
    items_present = "orders" in raw
    raw_items = raw.get("orders") if isinstance(raw.get("orders"), list) else []
    items = [_normalise_item(item, erp_id, index, paid_at)
             for index, item in enumerate(raw_items) if isinstance(item, dict)]
    split_parent = _split_parent_id(raw)
    trade: dict[str, Any] = {
        "shop_id": shop_id,
        "erp_id": erp_id,
        "commercial_ids": _commercial_ids(raw, items),
        "split_parent_id": split_parent,
        "source": source,
        "source_updated_at": source_updated_at,
        "platform_modified_at": parse_timestamp(raw.get("modified")),
        "paid_at": paid_at,
        "raw_pay_amount": raw_pay_amount,
        "raw_payment": to_decimal(raw.get("payment")),
        "raw_platform_payment": to_decimal(raw.get("platformPaymentAmount")),
        "raw_cost": to_decimal(raw.get("cost")),
        "raw_gross_profit": to_decimal(raw.get("grossProfit")),
        "active": str(raw.get("status") or "").strip().lower()
        not in {"cancel", "cancelled", "canceled", "trade_closed"},
        "normalization_status": status,
        "items_present": items_present,
        "items": items,
    }
    return trade


# ---------------------------------------------------------------------------
# 商业订单支付重建：单头或已核验行级分摊，禁止猜测
# ---------------------------------------------------------------------------


def _load_orders(conn, shop_id: str, commercial_id: str) -> list[tuple]:
    return conn.execute(
        "SELECT erp_id, commercial_ids, paid_at, raw_pay_amount, source_updated_at "
        "FROM bi.orders WHERE shop_id=%s AND commercial_ids @> %s AND active",
        (shop_id, [commercial_id]),
    ).fetchall()


def _determine_payment(conn, shop_id: str, commercial_id: str,
                       orders: list[tuple]) -> tuple[Decimal | None, datetime | None, str, bool, datetime | None]:
    """返回 (amount, paid_at, basis, verified, source_updated_at)。"""
    source_updated_at = max(order[4] for order in orders) if orders else None
    if not orders:
        return None, None, "orphan", False, source_updated_at
    if len(orders) == 1 and len(orders[0][1] or []) == 1:
        paid_at, head = orders[0][2], orders[0][3]
        if head is not None and head >= 0 and paid_at is not None:
            return head, paid_at, "head", True, source_updated_at
    # 行级分摊路径：涉及拆合单或单头不可用
    involved = _unique(
        cid for order in orders for cid in (order[1] or []))
    if not involved:
        return None, None, "undetermined", False, source_updated_at
    # 所有引用涉及商业单的有效订单都要参与交叉核对
    all_orders = conn.execute(
        "SELECT erp_id, commercial_ids, paid_at, raw_pay_amount, source_updated_at "
        "FROM bi.orders WHERE shop_id=%s AND commercial_ids && %s AND active",
        (shop_id, involved),
    ).fetchall()
    source_updated_at = max(order[4] for order in all_orders) if all_orders else source_updated_at
    heads = [order[3] for order in all_orders]
    total_head: Decimal | None
    if all(head is not None for head in heads):
        total_head = sum(heads, Decimal(0))
    else:
        total_head = None
    item_rows: dict[str, list[tuple[Decimal, datetime | None]]] = {}
    for cid in involved:
        item_rows[cid] = conn.execute(
            "SELECT allocated_paid_amount, paid_at FROM bi.order_items "
            "WHERE shop_id=%s AND commercial_id=%s AND active AND allocated_paid_amount IS NOT NULL",
            (shop_id, cid),
        ).fetchall()
        if not item_rows[cid]:
            return None, None, "undetermined", False, source_updated_at
    total_items = sum((amount for rows in item_rows.values() for amount, _ in rows), Decimal(0))
    if total_head is None or total_head != total_items:
        return None, None, "undetermined", False, source_updated_at
    target = item_rows[commercial_id]
    amount = sum((row[0] for row in target), Decimal(0))
    pay_times = {row[1] for row in target if row[1] is not None}
    paid_at = pay_times.pop() if len(pay_times) == 1 else None
    verified = paid_at is not None
    return amount, paid_at, "items", verified, source_updated_at


def rebuild_payments(conn, shop_id: str, commercial_ids: set[str]) -> None:
    """重建受影响商业订单的支付事实；不确定金额或时间留NULL并令verified=false。"""
    cids = {cid for cid in commercial_ids if cid}
    for commercial_id in sorted(cids):
        orders = _load_orders(conn, shop_id, commercial_id)
        amount, paid_at, basis, verified, source_updated_at = _determine_payment(
            conn, shop_id, commercial_id, orders)
        conn.execute(
            """
            INSERT INTO bi.order_payments
                (shop_id, commercial_id, paid_at, amount, currency, basis, verified, source_updated_at)
            VALUES (%s, %s, %s, %s, 'CNY', %s, %s, %s)
            ON CONFLICT (shop_id, commercial_id) DO UPDATE SET
                paid_at = EXCLUDED.paid_at,
                amount = EXCLUDED.amount,
                currency = 'CNY',
                basis = EXCLUDED.basis,
                verified = EXCLUDED.verified,
                source_updated_at = EXCLUDED.source_updated_at
            """,
            (shop_id, commercial_id, paid_at, amount, basis, verified, source_updated_at),
        )
        if verified:
            conn.execute(
                "UPDATE bi.order_items SET allocation_verified = true "
                "WHERE shop_id=%s AND commercial_id=%s AND active AND allocated_paid_amount IS NOT NULL",
                (shop_id, commercial_id),
            )
        else:
            conn.execute(
                "UPDATE bi.order_items SET allocation_verified = false "
                "WHERE shop_id=%s AND commercial_id=%s",
                (shop_id, commercial_id),
            )


# ---------------------------------------------------------------------------
# 版本化入库
# ---------------------------------------------------------------------------

_ORDER_COLUMNS = (
    "shop_id, erp_id, commercial_ids, split_parent_id, source, source_updated_at, "
    "platform_modified_at, paid_at, raw_pay_amount, raw_payment, raw_platform_payment, "
    "raw_cost, raw_gross_profit, active, normalization_status, batch_id"
)


def _trade_row(trade: dict[str, Any], batch_id: str) -> tuple:
    return (
        trade["shop_id"], trade["erp_id"], trade["commercial_ids"], trade["split_parent_id"],
        trade["source"], trade["source_updated_at"], trade["platform_modified_at"],
        trade["paid_at"], trade["raw_pay_amount"], trade["raw_payment"],
        trade["raw_platform_payment"], trade["raw_cost"], trade["raw_gross_profit"],
        trade["active"], trade["normalization_status"], batch_id,
    )


def apply_trade(conn, trade: dict[str, Any], *, batch_id: str) -> bool:
    """版本保护入库；不自行提交。返回是否接受此版本，False时不得替换明细。"""
    if trade["normalization_status"] == "invalid":
        return False
    existing = conn.execute(
        "SELECT commercial_ids FROM bi.orders WHERE shop_id=%s AND erp_id=%s",
        (trade["shop_id"], trade["erp_id"]),
    ).fetchone()
    old_ids = set(existing[0] or []) if existing else set()
    row = _trade_row(trade, batch_id)
    updated = conn.execute(
        f"""
        INSERT INTO bi.orders ({_ORDER_COLUMNS})
        VALUES ({", ".join(["%s"] * 16)})
        ON CONFLICT (shop_id, erp_id) DO UPDATE SET
            commercial_ids = EXCLUDED.commercial_ids,
            split_parent_id = EXCLUDED.split_parent_id,
            source = EXCLUDED.source,
            source_updated_at = EXCLUDED.source_updated_at,
            platform_modified_at = EXCLUDED.platform_modified_at,
            paid_at = EXCLUDED.paid_at,
            raw_pay_amount = EXCLUDED.raw_pay_amount,
            raw_payment = EXCLUDED.raw_payment,
            raw_platform_payment = EXCLUDED.raw_platform_payment,
            raw_cost = EXCLUDED.raw_cost,
            raw_gross_profit = EXCLUDED.raw_gross_profit,
            active = EXCLUDED.active,
            normalization_status = EXCLUDED.normalization_status,
            batch_id = EXCLUDED.batch_id
        WHERE EXCLUDED.source_updated_at > bi.orders.source_updated_at
        RETURNING erp_id
        """,
        row,
    ).fetchone()
    if updated is None:
        existing = conn.execute(
            "SELECT source_updated_at FROM bi.orders WHERE shop_id=%s AND erp_id=%s",
            (trade["shop_id"], trade["erp_id"]),
        ).fetchone()
        if existing is None or existing[0] >= trade["source_updated_at"]:
            return False
        # 仅当数据库版本严格更旧才可能走到这里；同版本不视为冲突
        conn.execute(
            "UPDATE bi.orders SET normalization_status='version_conflict' "
            "WHERE shop_id=%s AND erp_id=%s",
            (trade["shop_id"], trade["erp_id"]),
        )
        return False
    if trade["items_present"]:
        conn.execute(
            "DELETE FROM bi.order_items WHERE shop_id=%s AND erp_id=%s",
            (trade["shop_id"], trade["erp_id"]),
        )
        for item in trade["items"]:
            conn.execute(
                """
                INSERT INTO bi.order_items
                    (shop_id, erp_id, line_id, commercial_id, platform_line_id, product_id,
                     sku_id, paid_at, quantity, gift_quantity, raw_paid_amount, raw_payment,
                     raw_unit_cost, allocated_paid_amount, allocation_verified, line_kind, active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    trade["shop_id"], trade["erp_id"], item["line_id"], item["commercial_id"],
                    item["platform_line_id"], item["product_id"], item["sku_id"],
                    item["paid_at"], item["quantity"], item["gift_quantity"],
                    item["raw_paid_amount"], item["raw_payment"], item["raw_unit_cost"],
                    item["allocated_paid_amount"], item["allocation_verified"],
                    item["line_kind"], item["active"],
                ),
            )
    old_ids |= set(trade["commercial_ids"])
    rebuild_payments(conn, str(trade["shop_id"]), old_ids)
    return True


# ---------------------------------------------------------------------------
# 售后规范化与去重
# ---------------------------------------------------------------------------


def normalise_aftersale(raw: dict[str, Any], *,
                        source: str = AFTERSALE_SOURCE) -> dict[str, Any]:
    """售后单头规范化；platform_success只是候选条件，canonical由去重决定。"""
    aftersale_id = str(raw.get("aftersaleId") or raw.get("id") or "").strip()
    shop_id = str(raw.get("userId") or "").strip()
    source_updated_at = (parse_timestamp(raw.get("modified"))
                         or parse_timestamp(raw.get("modifiedTime"))
                         or parse_timestamp(raw.get("updTime")))
    platform_completed_at = parse_timestamp(raw.get("platformCompleteTime"))
    system_completed_at = (parse_timestamp(raw.get("systemCompleteTime"))
                           or parse_timestamp(raw.get("completeTime")))
    online_status = raw.get("onlineStatus")
    work_status = raw.get("status")
    online_value = int(online_status) if isinstance(online_status, (int, str)) and str(online_status).lstrip("-").isdigit() else None
    work_value = int(work_status) if isinstance(work_status, (int, str)) and str(work_status).lstrip("-").isdigit() else None
    platform_success = bool(
        online_value == 7
        and platform_completed_at is not None
        and work_value not in (10, 11)
    )
    return {
        "shop_id": shop_id,
        "aftersale_id": aftersale_id,
        "platform_refund_id": (str(raw.get("platformId") or raw.get("refundId")
                                   or raw.get("platformRefundId") or "").strip() or None),
        "commercial_id": (str(raw.get("tid") or "").strip() or None),
        "erp_id": (str(raw.get("sid") or "").strip() or None),
        "raw_platform_amount": to_decimal(raw.get("rawRefundMoney")),
        "raw_system_amount": to_decimal(raw.get("refundMoney")),
        "online_status": online_value,
        "work_status": work_value,
        "platform_completed_at": platform_completed_at,
        "system_completed_at": system_completed_at,
        "source_updated_at": source_updated_at,
        "platform_success": platform_success,
        "valid": bool(aftersale_id and shop_id and source_updated_at is not None),
    }


def apply_aftersale(conn, aftersale: dict[str, Any], *, batch_id: str) -> bool:
    """售后版本化UPSERT；无原订单也入库；matched随原单到达可再刷新。"""
    if not aftersale.get("valid"):
        return False
    commercial_id = aftersale["commercial_id"]
    matched = False
    if commercial_id:
        matched = conn.execute(
            "SELECT 1 FROM bi.orders WHERE shop_id=%s AND commercial_ids @> %s LIMIT 1",
            (aftersale["shop_id"], [commercial_id]),
        ).fetchone() is not None
    updated = conn.execute(
        """
        INSERT INTO bi.aftersales
            (shop_id, aftersale_id, platform_refund_id, commercial_id, erp_id,
             raw_platform_amount, raw_system_amount, online_status, work_status,
             platform_completed_at, system_completed_at, source_updated_at,
             platform_success, matched, batch_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (shop_id, aftersale_id) DO UPDATE SET
            platform_refund_id = EXCLUDED.platform_refund_id,
            commercial_id = EXCLUDED.commercial_id,
            erp_id = EXCLUDED.erp_id,
            raw_platform_amount = EXCLUDED.raw_platform_amount,
            raw_system_amount = EXCLUDED.raw_system_amount,
            online_status = EXCLUDED.online_status,
            work_status = EXCLUDED.work_status,
            platform_completed_at = EXCLUDED.platform_completed_at,
            system_completed_at = EXCLUDED.system_completed_at,
            source_updated_at = EXCLUDED.source_updated_at,
            platform_success = EXCLUDED.platform_success,
            batch_id = EXCLUDED.batch_id
        WHERE EXCLUDED.source_updated_at > bi.aftersales.source_updated_at
        RETURNING aftersale_id
        """,
        (
            aftersale["shop_id"], aftersale["aftersale_id"], aftersale["platform_refund_id"],
            commercial_id, aftersale["erp_id"], aftersale["raw_platform_amount"],
            aftersale["raw_system_amount"], aftersale["online_status"], aftersale["work_status"],
            aftersale["platform_completed_at"], aftersale["system_completed_at"],
            aftersale["source_updated_at"], aftersale["platform_success"], matched, batch_id,
        ),
    ).fetchone()
    if updated is None:
        # 版本护栏拦截：历史行仍补齐缺失的平台退款号，并维持canonical判定
        if aftersale["platform_refund_id"]:
            conn.execute(
                "UPDATE bi.aftersales SET platform_refund_id=%s "
                "WHERE shop_id=%s AND aftersale_id=%s AND platform_refund_id IS NULL",
                (aftersale["platform_refund_id"], aftersale["shop_id"],
                 aftersale["aftersale_id"]),
            )
            mark_refund_canonical(conn, aftersale["shop_id"],
                                  {aftersale["platform_refund_id"]})
        return False
    if aftersale["platform_refund_id"]:
        mark_refund_canonical(conn, aftersale["shop_id"], {aftersale["platform_refund_id"]})
    if commercial_id:
        refresh_aftersale_matched(conn, aftersale["shop_id"], {commercial_id})
    return True


def refresh_aftersale_matched(conn, shop_id: str, commercial_ids: set[str]) -> None:
    """原单到达后刷新售后匹配标记。"""
    for commercial_id in sorted(c for c in commercial_ids if c):
        conn.execute(
            """
            UPDATE bi.aftersales a SET matched = EXISTS (
                SELECT 1 FROM bi.orders o
                WHERE o.shop_id = a.shop_id AND o.active
                  AND o.commercial_ids @> ARRAY[a.commercial_id])
            WHERE a.shop_id=%s AND a.commercial_id=%s
            """,
            (shop_id, commercial_id),
        )


def mark_refund_canonical(conn, shop_id: str, platform_refund_ids: set[str]) -> None:
    """平台售后号相同的拆分工单只确认一次实际退款。

    金额一致的组确定唯一canonical；金额不一致的组设未验证，禁止取最大值。
    """
    for refund_id in sorted(r for r in platform_refund_ids if r):
        rows = conn.execute(
            "SELECT aftersale_id, raw_platform_amount, platform_success FROM bi.aftersales "
            "WHERE shop_id=%s AND platform_refund_id=%s ORDER BY aftersale_id",
            (shop_id, refund_id),
        ).fetchall()
        if not rows:
            continue
        if len(rows) == 1:
            conn.execute(
                "UPDATE bi.aftersales SET refund_canonical=%s "
                "WHERE shop_id=%s AND platform_refund_id=%s",
                (bool(rows[0][2]), shop_id, refund_id),
            )
            continue
        amounts = {row[1] for row in rows}
        if len(amounts) == 1:
            canonical_id = rows[0][0]
            conn.execute(
                "UPDATE bi.aftersales SET refund_canonical=(aftersale_id=%s AND platform_success) "
                "WHERE shop_id=%s AND platform_refund_id=%s",
                (canonical_id, shop_id, refund_id),
            )
        else:
            conn.execute(
                "UPDATE bi.aftersales SET refund_canonical=false "
                "WHERE shop_id=%s AND platform_refund_id=%s",
                (shop_id, refund_id),
            )


# ---------------------------------------------------------------------------
# 窗口与分页拉取
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """业务或修改时间窗口，内部归属一律 [start, end)。"""

    start: datetime
    end: datetime


def day_windows(start: datetime, end: datetime) -> Iterator[Window]:
    """把范围切成每窗口不超过一天的小窗口。"""
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=1), end)
        yield Window(cursor, chunk_end)
        cursor = chunk_end


def _fmt(moment: datetime) -> str:
    return moment.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def _fetch_orders_cursor(client: KuaimaiClient, *, shop_id: str, window: Window,
                         time_type: str, query_type: str,
                         method: str = ORDER_SOURCE) -> Iterator[dict[str, Any]]:
    """非归档订单：官方cursor+hasNext分页；不能用'本页少于200'作为唯一结束条件。

    method 参数化订单源：交易查询与出库通道的 cursor/queryType 参数形状一致，复用同一实现。"""
    cursor: str | None = None
    while True:
        params: dict[str, str] = {
            "userIds": shop_id,
            "timeType": time_type,
            "startTime": _fmt(window.start),
            "endTime": _fmt(window.end),
            "pageSize": str(PAGE_SIZE),
            "queryType": query_type,
            "useHasNext": "true",
            "useCursor": "true",
        }
        if cursor is not None:
            params["cursor"] = cursor
        page = parse_page(
            client.call(method, params), allow_omitted_list=True)
        if not page.rows:
            return
        yield from page.rows
        if page.verified_empty:
            return
        if page.has_next is False:
            return
        if page.cursor is None:
            # hasNext=true却无数据/游标：无结束证据
            raise KuaimaiError("invalid_response")
        if cursor is not None and page.cursor == cursor:
            raise KuaimaiError("invalid_response")
        cursor = page.cursor


def _fetch_orders_paged(client: KuaimaiClient, *, shop_id: str, window: Window,
                        time_type: str | None, query_type: str,
                        method: str = ORDER_SOURCE) -> Iterator[dict[str, Any]]:
    """归档通道：页码分页；按total判断末页并检查计数一致性。"""
    page_no = 1
    collected = 0
    total: int | None = None
    while True:
        params: dict[str, str] = {
            "userIds": shop_id,
            "pageNo": str(page_no),
            "pageSize": str(PAGE_SIZE),
            "queryType": query_type,
            "startTime": _fmt(window.start),
            "endTime": _fmt(window.end),
        }
        if time_type:
            params["timeType"] = time_type
        page = parse_page(
            client.call(method, params), allow_omitted_list=True)
        if page.verified_empty:
            return
        if total is None and page.total is not None:
            total = page.total
        if page.total is not None and total is not None and page.total != total:
            # 数据漂移：重跑由上层决定，不确认覆盖
            raise KuaimaiError("upstream")
        yield from page.rows
        collected += len(page.rows)
        if total is not None:
            if collected >= total:
                return
            if len(page.rows) < PAGE_SIZE:
                raise KuaimaiError("invalid_response")
        else:
            # 归档通道不返回total：以不足一页作为末页证据
            if len(page.rows) < PAGE_SIZE:
                return
        page_no += 1


def _fetch_aftersales_paged(client: KuaimaiClient, *, shop_id: str, window: Window,
                            start_param: str, end_param: str,
                            extra_params: dict[str, str] | None = None) -> Iterator[dict[str, Any]]:
    """售后：userIds/pageNo/pageSize=200/asVersion=2；按total判断末页。"""
    page_no = 1
    collected = 0
    total: int | None = None
    while True:
        params: dict[str, str] = {
            "userIds": shop_id,
            "pageNo": str(page_no),
            "pageSize": str(PAGE_SIZE),
            "asVersion": "2",
            start_param: _fmt(window.start),
            end_param: _fmt(window.end),
        }
        if extra_params:
            params.update(extra_params)
        page = parse_page(
            client.call(AFTERSALE_SOURCE, params), allow_omitted_list=True)
        if page.verified_empty:
            return
        if page.total is None:
            raise KuaimaiError("invalid_response")
        if total is not None and page.total != total:
            raise KuaimaiError("upstream")
        total = page.total
        yield from page.rows
        collected += len(page.rows)
        if collected >= total:
            return
        if len(page.rows) < PAGE_SIZE:
            raise KuaimaiError("invalid_response")
        page_no += 1


def fetch_window(client: KuaimaiClient, *, entity: str, shop_id: str,
                 window: Window, mode: str,
                 order_source: str = ORDER_SOURCE) -> Iterator[dict[str, Any]]:
    """拉取一个窗口；结束前必须证明分页完整，否则抛KuaimaiError。

    初始订单回填按pay_time建立支付业务覆盖；归档边界附近分别核对
    queryType=0/1，使用两通道覆盖且依主键幂等去重。
    order_source 决定订单实体请求的接口方法（淘系走出库通道）；售后不分平台。
    """
    if entity == "orders":
        if mode in ("incremental", "scan"):
            yield from _fetch_orders_cursor(client, shop_id=shop_id, window=window,
                                            time_type="upd_time", query_type="0",
                                            method=order_source)
        elif mode in ("backfill", "replay", "reconcile", "probe"):
            yield from _fetch_orders_cursor(client, shop_id=shop_id, window=window,
                                            time_type="pay_time", query_type="0",
                                            method=order_source)
            yield from _fetch_orders_paged(client, shop_id=shop_id, window=window,
                                           time_type="pay_time", query_type="1",
                                           method=order_source)
        else:
            raise ValueError(f"未知模式 {mode}")
    elif entity == "aftersales_occurrence":
        if mode in ("incremental", "scan"):
            yield from _fetch_aftersales_paged(client, shop_id=shop_id, window=window,
                                               start_param="startModified",
                                               end_param="endModified")
        elif mode in ("backfill", "replay", "reconcile", "probe"):
            # 售后发生额用startPlatformCompleteTime/endPlatformCompleteTime建立覆盖
            yield from _fetch_aftersales_paged(client, shop_id=shop_id, window=window,
                                               start_param="startPlatformCompleteTime",
                                               end_param="endPlatformCompleteTime")
        else:
            raise ValueError(f"未知模式 {mode}")
    elif entity == "aftersales_cohort":
        raise ValueError("aftersales_cohort由check_cohort_window按已回填商业单补查")
    else:
        raise ValueError(f"未知实体 {entity}")


# ---------------------------------------------------------------------------
# 窗口事务与覆盖/水位维护
# ---------------------------------------------------------------------------


def _ensure_state(conn, source: str, entity: str, shop_id: str) -> None:
    conn.execute(
        "INSERT INTO bi.sync_state(source, entity, shop_id) VALUES (%s, %s, %s) "
        "ON CONFLICT (source, entity, shop_id) DO NOTHING",
        (source, entity, shop_id),
    )


def _extend_incremental_coverage(conn, *, source: str, entity: str, shop_id: str,
                                 batch_id: str, window: Window) -> None:
    """确认期间新增支付/退款的收录后才扩展已建立的业务覆盖终点。"""
    if entity == "orders":
        business_end = conn.execute(
            "SELECT max(paid_at) FROM bi.orders WHERE shop_id=%s AND batch_id=%s",
            (shop_id, batch_id),
        ).fetchone()[0]
    else:
        business_end = conn.execute(
            "SELECT max(platform_completed_at) FROM bi.aftersales "
            "WHERE shop_id=%s AND batch_id=%s AND platform_success",
            (shop_id, batch_id),
        ).fetchone()[0]
    if business_end is None:
        return
    state = conn.execute(
        "SELECT covered FROM bi.sync_state WHERE source=%s AND entity=%s AND shop_id=%s FOR UPDATE",
        (source, entity, shop_id),
    ).fetchone()
    if state is None:
        return
    uppers = [rng.upper for rng in state[0]
              if rng.upper is not None and rng.upper != datetime.max.replace(tzinfo=BEIJING)]
    if not uppers:
        return  # 未建立业务覆盖，不靠增量直接填平历史缺口
    prev_end = max(uppers)
    if business_end <= prev_end:
        return
    if window.start - prev_end > SYNC_OVERLAP:
        return  # 覆盖不连续
    # 终点含边界时刻的已收录支付（[start,end)归属，多加1秒覆盖边界）
    conn.execute(
        "UPDATE bi.sync_state SET covered = covered + tstzmultirange(tstzrange(%s, %s, '[)')) "
        "WHERE source=%s AND entity=%s AND shop_id=%s",
        (prev_end, business_end + timedelta(seconds=1), source, entity, shop_id),
    )




def _record_window_success(conn, *, source: str, entity: str, shop_id: str,
                           window: Window, mode: str, batch_id: str) -> None:
    _ensure_state(conn, source, entity, shop_id)
    if mode in ("incremental", "scan"):
        conn.execute(
            "UPDATE bi.sync_state SET watermark=%s, last_success_at=now(), last_error_code=NULL "
            "WHERE source=%s AND entity=%s AND shop_id=%s",
            (window.end, source, entity, shop_id),
        )
        _extend_incremental_coverage(conn, source=source, entity=entity, shop_id=shop_id,
                                     batch_id=batch_id, window=window)
    else:
        # 仅完成对应业务时间回填/replay的窗口可加入覆盖；
        # upd_time成功本身不能证明该修改窗口就是支付覆盖
        conn.execute(
            "UPDATE bi.sync_state SET "
            "covered = covered + tstzmultirange(tstzrange(%s, %s, '[)')), "
            "last_success_at = now(), last_error_code = NULL "
            "WHERE source=%s AND entity=%s AND shop_id=%s",
            (window.start, window.end, source, entity, shop_id),
        )


def record_failure(conn, *, source: str, entity: str, shop_id: str, code: str) -> None:
    """异常回滚后另开短事务记录；保留旧成功水位和已完成窗口。"""
    _ensure_state(conn, source, entity, shop_id)
    conn.execute(
        "UPDATE bi.sync_state SET last_attempt_at=now(), last_error_code=%s "
        "WHERE source=%s AND entity=%s AND shop_id=%s",
        (code, source, entity, shop_id),
    )


def sync_window(conn, client: KuaimaiClient, *, entity: str, shop_id: str,
                window: Window, mode: str,
                order_source: str = ORDER_SOURCE) -> int:
    """单个窗口事务：拉取、规范化、入库、去重；成功后推进状态。成功返回写入记录数。"""
    batch_id = uuid.uuid4().hex
    source = order_source if entity == "orders" else AFTERSALE_SOURCE
    accepted = 0
    refund_ids: set[str] = set()
    touched_commercials: set[str] = set()
    with conn.transaction():
        for raw in fetch_window(client, entity=entity, shop_id=shop_id, window=window,
                                mode=mode, order_source=order_source):
            if entity == "orders":
                trade = normalise_trade(raw, source=order_source)
                if trade["normalization_status"] == "invalid":
                    continue
                apply_trade(conn, trade, batch_id=batch_id)
                accepted += 1
                touched_commercials.update(trade["commercial_ids"])
            else:
                aftersale = normalise_aftersale(raw)
                if apply_aftersale(conn, aftersale, batch_id=batch_id):
                    accepted += 1
                if aftersale["platform_refund_id"]:
                    refund_ids.add(aftersale["platform_refund_id"])
                if aftersale["commercial_id"]:
                    touched_commercials.add(aftersale["commercial_id"])
        if entity != "orders":
            refresh_aftersale_matched(conn, shop_id, touched_commercials)
        mark_refund_canonical(conn, shop_id, refund_ids)
        _record_window_success(conn, source=source, entity=entity, shop_id=shop_id,
                               window=window, mode=mode, batch_id=batch_id)
    return accepted


def check_cohort_window(conn, client: KuaimaiClient, *, shop_id: str,
                        window: Window) -> int:
    """同批退款：用已回填商业单的tids分批补查，并建立cohort覆盖。

    另取status=2,12未结工单属于增量/reconcile的modified扫描，不在此处。
    """
    batch_id = uuid.uuid4().hex
    refund_ids: set[str] = set()
    touched: set[str] = set()
    with conn.transaction():
        commercials = [
            row[0] for row in conn.execute(
                "SELECT commercial_id FROM bi.order_payments "
                "WHERE shop_id=%s AND paid_at >= %s AND paid_at < %s AND verified",
                (shop_id, window.start, window.end),
            ).fetchall()
        ]
        for offset in range(0, len(commercials), COHORT_TIDS_BATCH):
            chunk = commercials[offset:offset + COHORT_TIDS_BATCH]
            page_no = 1
            collected = 0
            total: int | None = None
            while True:
                params = {
                    "userIds": shop_id,
                    "pageNo": str(page_no),
                    "pageSize": str(PAGE_SIZE),
                    "asVersion": "2",
                    "tids": ",".join(chunk),
                }
                page = parse_page(
                    client.call(AFTERSALE_SOURCE, params), allow_omitted_list=True)
                if page.verified_empty:
                    break
                if page.total is None:
                    raise KuaimaiError("invalid_response")
                if total is not None and page.total != total:
                    raise KuaimaiError("upstream")
                total = page.total
                for raw in page.rows:
                    aftersale = normalise_aftersale(raw)
                    apply_aftersale(conn, aftersale, batch_id=batch_id)
                    if aftersale["platform_refund_id"]:
                        refund_ids.add(aftersale["platform_refund_id"])
                    if aftersale["commercial_id"]:
                        touched.add(aftersale["commercial_id"])
                collected += len(page.rows)
                if collected >= total:
                    break
                if len(page.rows) < PAGE_SIZE:
                    raise KuaimaiError("invalid_response")
                page_no += 1
        refresh_aftersale_matched(conn, shop_id, touched)
        mark_refund_canonical(conn, shop_id, refund_ids)
        _ensure_state(conn, AFTERSALE_SOURCE, "aftersales_cohort", shop_id)
        conn.execute(
            "UPDATE bi.sync_state SET "
            "covered = covered + tstzmultirange(tstzrange(%s, %s, '[)')), "
            "last_success_at = now(), last_error_code = NULL, "
            "data_as_of = greatest(coalesce(data_as_of, '1970-01-01 00:00+00'), now()) "
            "WHERE source=%s AND entity='aftersales_cohort' AND shop_id=%s",
            (window.start, window.end, AFTERSALE_SOURCE, shop_id),
        )
    return len(commercials)


def refetch_orders_for_commercials(conn, client: KuaimaiClient, *, shop_id: str,
                                   commercial_ids: set[str],
                                   order_source: str = ORDER_SOURCE) -> int:
    """增量收到更早商业单退款时按已发布的tid条件补拉原单。

    单次tid查询参数及数量上限需在4.7真实核验后固定；当前按单tid逐个补拉。
    """
    accepted = 0
    for commercial_id in sorted(commercial_ids):
        if not commercial_id:
            continue
        cursor: str | None = None
        while True:
            params: dict[str, str] = {
                "userIds": shop_id,
                "timeType": "upd_time",
                "startTime": _fmt(datetime.now(BEIJING) - timedelta(days=MAX_QUERY_DAYS)),
                "endTime": _fmt(datetime.now(BEIJING)),
                "pageSize": str(PAGE_SIZE),
                "queryType": "0",
                "useHasNext": "true",
                "useCursor": "true",
                "tid": commercial_id,
            }
            if cursor is not None:
                params["cursor"] = cursor
            page = parse_page(
                client.call(order_source, params), allow_omitted_list=True)
            batch_id = uuid.uuid4().hex
            with conn.transaction():
                for raw in page.rows:
                    trade = normalise_trade(raw, source=order_source)
                    if trade["normalization_status"] == "invalid":
                        continue
                    if apply_trade(conn, trade, batch_id=batch_id):
                        accepted += 1
            refresh_aftersale_matched(conn, shop_id, {commercial_id})
            if page.verified_empty or page.has_next is False:
                break
            if page.cursor is None or (cursor is not None and page.cursor == cursor):
                raise KuaimaiError("invalid_response")
            cursor = page.cursor
    return accepted


def unmatched_commercials(conn, shop_id: str) -> set[str]:
    """售后已到、原单未到的商业订单号，等待按tid补拉。"""
    rows = conn.execute(
        "SELECT DISTINCT a.commercial_id FROM bi.aftersales a "
        "WHERE a.shop_id=%s AND a.commercial_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM bi.orders o WHERE o.shop_id=a.shop_id "
        "                AND o.active AND o.commercial_ids @> ARRAY[a.commercial_id])",
        (shop_id,),
    ).fetchall()
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# 店铺同步
# ---------------------------------------------------------------------------


def sync_shops(conn, client: KuaimaiClient) -> int:
    """拉取店铺档案并更新bi.shops；不输出店铺名称到控制台。"""
    page_no = 1
    collected = 0
    with conn.transaction():
        while True:
            page = parse_page(
                client.call("erp.shop.list.query", {
                    "pageNo": str(page_no), "pageSize": str(PAGE_SIZE)}),
                allow_omitted_list=True,
            )
            if not page.rows:
                break
            for raw in page.rows:
                shop_id = str(raw.get("userId") or "").strip()
                if not shop_id:
                    continue
                state = str(raw.get("state") or "").strip().lower()
                enabled = state not in {"disable", "disabled", "deleted", "0"}
                conn.execute(
                    "INSERT INTO bi.shops(shop_id, platform, display_name, enabled) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (shop_id) DO UPDATE SET platform=EXCLUDED.platform, "
                    "display_name=EXCLUDED.display_name, enabled=EXCLUDED.enabled",
                    (shop_id, str(raw.get("source") or "unknown"),
                     str(raw.get("title") or raw.get("nick") or raw.get("shopName") or ""),
                     enabled),
                )
                collected += 1
            if len(page.rows) < PAGE_SIZE:
                break
            page_no += 1
    return collected


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _connect(dsn: str):
    # 每个conn.transaction()都是独立提交，不能让默认外层事务拖到CLI结束才提交
    return __import__("psycopg").connect(dsn, autocommit=True)


def _require_single_shop(settings) -> str:
    if len(settings.shop_ids) != 1:
        raise SystemExit("probe要求仅配置一个店铺（BI_SHOP_IDS）")
    return next(iter(settings.shop_ids))


def _shop_order_source(conn, shop_id: str) -> str:
    """按 bi.shops.platform 路由订单源；tb/tm→出库通道，其余平台回退默认源。

    店铺档案缺失时报错退出：若静默回退默认源，淘系店会被 trade.list.query
    “验证为空”造成假覆盖，必须先运行 shops 同步。
    """
    row = conn.execute(
        "SELECT platform FROM bi.shops WHERE shop_id=%s", (shop_id,)).fetchone()
    if row is None:
        raise SystemExit(f"店铺 {shop_id} 不在 bi.shops，请先运行 shops 同步")
    platform = (row[0] or "").strip().lower()
    return ORDER_SOURCE_BY_PLATFORM.get(platform, ORDER_SOURCE)


def _shop_error(exc: BaseException) -> str:
    """CLI 逐店隔离时输出的脱敏错误标识：KuaimaiError 取 code，其余取消息文本。"""
    if isinstance(exc, KuaimaiError):
        return exc.code
    return str(exc) or exc.__class__.__name__


def _run_window(conn, client: KuaimaiClient, *, entity: str, shop_id: str,
                window: Window, mode: str,
                order_source: str = ORDER_SOURCE) -> int:
    try:
        return sync_window(conn, client, entity=entity, shop_id=shop_id,
                           window=window, mode=mode, order_source=order_source)
    except KuaimaiError as exc:
        record_failure(conn,
                       source=order_source if entity == "orders" else AFTERSALE_SOURCE,
                       entity=entity, shop_id=shop_id, code=exc.code)
        logger.warning("sync window failed entity=%s shop=%s code=%s", entity, shop_id, exc.code)
        raise


def _backfill_shop(conn, client: KuaimaiClient, *, shop_id: str, days: int,
                   t0: datetime, order_source: str = ORDER_SOURCE) -> dict[str, int]:
    """回填最近N天并补拉[T0,T1)；T1 在回填窗口跑完后取固定时刻。

    补拉扫描（upd_time）成功后才推进修改水位并发布 data_as_of，
    使后续 incremental 能直接从水位续跑（原实现 t1=t0 使扫描为空转，
    回填后的店永远无法进入增量）。"""
    if days > MAX_QUERY_DAYS:
        raise SystemExit(f"回填跨度最多{MAX_QUERY_DAYS}天")
    start = t0 - timedelta(days=days)
    stats = {"orders": 0, "aftersales_occurrence": 0, "cohort_windows": 0}
    for window in day_windows(start, t0):
        stats["orders"] += _run_window(conn, client, entity="orders", shop_id=shop_id,
                                       window=window, mode="backfill",
                                       order_source=order_source)
    for window in day_windows(start, t0):
        stats["aftersales_occurrence"] += _run_window(
            conn, client, entity="aftersales_occurrence", shop_id=shop_id,
            window=window, mode="backfill")
        check_cohort_window(conn, client, shop_id=shop_id, window=window)
        stats["cohort_windows"] += 1
    # 补拉回填期间的变化：修改时间扫描推进水位
    t1 = datetime.now(BEIJING)
    for window in day_windows(t0, t1):
        _run_window(conn, client, entity="orders", shop_id=shop_id,
                    window=window, mode="scan", order_source=order_source)
        _run_window(conn, client, entity="aftersales_occurrence", shop_id=shop_id,
                    window=window, mode="scan")
    with conn.transaction():
        for entity in ("orders", "aftersales_occurrence", "aftersales_cohort"):
            state_source = order_source if entity == "orders" else AFTERSALE_SOURCE
            _ensure_state(conn, state_source, entity, shop_id)
            conn.execute(
                "UPDATE bi.sync_state SET data_as_of=%s "
                "WHERE source=%s AND entity=%s AND shop_id=%s",
                (t1, state_source, entity, shop_id),
            )
    return stats


def _incremental_shop(conn, client: KuaimaiClient, *, shop_id: str,
                      run_end: datetime,
                      order_source: str = ORDER_SOURCE) -> dict[str, int]:
    """增量：从watermark-10分钟到本次固定run_end，逐日窗口推进。"""
    stats = {"orders": 0, "aftersales_occurrence": 0}
    for entity, source in (("orders", order_source),
                           ("aftersales_occurrence", AFTERSALE_SOURCE)):
        state = conn.execute(
            "SELECT watermark FROM bi.sync_state WHERE source=%s AND entity=%s AND shop_id=%s",
            (source, entity, shop_id),
        ).fetchone()
        if state is None or state[0] <= datetime(1970, 1, 2, tzinfo=BEIJING):
            raise SystemExit(f"{entity} 增量前必须先完成backfill建立水位")
        start = state[0] - SYNC_OVERLAP
        for window in day_windows(start, run_end):
            stats[entity] += _run_window(conn, client, entity=entity, shop_id=shop_id,
                                         window=window, mode="incremental",
                                         order_source=order_source)
    # 增量收到更早商业单退款时，按已发布的tid条件补拉原单
    missing = unmatched_commercials(conn, shop_id)
    if missing:
        refetch_orders_for_commercials(conn, client, shop_id=shop_id,
                                       commercial_ids=missing, order_source=order_source)
    return stats


def _reconcile_shop(conn, client: KuaimaiClient, *, shop_id: str, days: int,
                    run_end: datetime,
                    order_source: str = ORDER_SOURCE) -> dict[str, int]:
    """按支付日/退款完成日重核最近N天，并刷新cohort覆盖与data_as_of。"""
    if days > MAX_QUERY_DAYS:
        raise SystemExit(f"重核跨度最多{MAX_QUERY_DAYS}天")
    start = run_end - timedelta(days=days)
    stats = {"orders": 0, "aftersales_occurrence": 0, "cohort_windows": 0}
    for window in day_windows(start, run_end):
        stats["orders"] += _run_window(conn, client, entity="orders", shop_id=shop_id,
                                       window=window, mode="reconcile",
                                       order_source=order_source)
        stats["aftersales_occurrence"] += _run_window(
            conn, client, entity="aftersales_occurrence", shop_id=shop_id,
            window=window, mode="reconcile")
        check_cohort_window(conn, client, shop_id=shop_id, window=window)
        stats["cohort_windows"] += 1
    missing = unmatched_commercials(conn, shop_id)
    if missing:
        refetch_orders_for_commercials(conn, client, shop_id=shop_id,
                                       commercial_ids=missing, order_source=order_source)
    return stats


def _replay_entity(conn, client: KuaimaiClient, *, shop_id: str, entity: str,
                   start: datetime, end: datetime,
                   order_source: str = ORDER_SOURCE) -> int:
    """历史范围replay：业务时间窗口重跑并加入覆盖。"""
    count = 0
    if entity == "orders":
        for window in day_windows(start, end):
            count += _run_window(conn, client, entity="orders", shop_id=shop_id,
                                 window=window, mode="replay",
                                 order_source=order_source)
    elif entity == "aftersales_occurrence":
        for window in day_windows(start, end):
            count += _run_window(conn, client, entity="aftersales_occurrence",
                                 shop_id=shop_id, window=window, mode="replay")
    elif entity == "aftersales_cohort":
        for window in day_windows(start, end):
            check_cohort_window(conn, client, shop_id=shop_id, window=window)
    else:
        raise SystemExit(f"未知实体 {entity}")
    return count


def _probe(client: KuaimaiClient, *, shop_id: str, start: datetime,
           end: datetime, order_source: str = ORDER_SOURCE) -> dict[str, object]:
    """拉全页但只输出数量、金额字段覆盖和质量统计，不输出客户/订单号。"""
    window = Window(start, end)
    pay_total = Decimal(0)
    pay_present = 0
    pay_negative = 0
    order_count = 0
    commercial_ids: set[str] = set()
    tid_counts: dict[str, int] = {}
    for raw in fetch_window(client, entity="orders", shop_id=shop_id,
                            window=window, mode="probe",
                            order_source=order_source):
        order_count += 1
        amount = to_decimal(raw.get("payAmount"))
        if amount is not None:
            pay_present += 1
            if amount < 0:
                pay_negative += 1
            else:
                pay_total += amount
        tid = str(raw.get("tid") or "").strip()
        if tid:
            tid_counts[tid] = tid_counts.get(tid, 0) + 1
    commercial_ids = set(tid_counts)
    refund_count = 0
    refund_success = 0
    refund_amount = Decimal(0)
    for raw in fetch_window(client, entity="aftersales_occurrence", shop_id=shop_id,
                            window=window, mode="probe"):
        aftersale = normalise_aftersale(raw)
        refund_count += 1
        if aftersale["platform_success"]:
            refund_success += 1
            if aftersale["raw_platform_amount"] is not None:
                refund_amount += aftersale["raw_platform_amount"]
    split_or_merge = sum(1 for c in commercial_ids if tid_counts[c] > 1)
    return {
        "window": {"start": _fmt(start), "end": _fmt(end)},
        "orders": order_count,
        "pay_amount_present": pay_present,
        "pay_amount_negative": pay_negative,
        "pay_amount_sum": str(pay_total),
        "commercials": len(commercial_ids),
        "split_or_merge_commercials": split_or_merge,
        "aftersales": refund_count,
        "platform_success": refund_success,
        "platform_success_amount": str(refund_amount),
    }


def _refresh_session(conn, client: KuaimaiClient, *, shop_id: str = "__company__") -> None:
    """同一锁内检查到期窗口、距上次调用至少一小时；只记录期限和成功时刻。"""
    source = "open.token.refresh"
    _ensure_state(conn, source, "session", shop_id)
    state = conn.execute(
        "SELECT token_expires_at, last_refresh_at FROM bi.sync_state "
        "WHERE source=%s AND entity='session' AND shop_id=%s",
        (source, shop_id),
    ).fetchone()
    now = datetime.now(BEIJING)
    expires_at, last_refresh = state
    if expires_at is not None and expires_at - now > timedelta(days=7):
        print(json.dumps({"action": "refresh-session", "skipped": "not_in_window"}))
        return
    if last_refresh is not None and now - last_refresh < timedelta(hours=1):
        print(json.dumps({"action": "refresh-session", "skipped": "rate_limited"}))
        return
    expires = client.refresh_session(now=now)
    conn.execute(
        "UPDATE bi.sync_state SET token_expires_at=%s, last_refresh_at=%s, "
        "last_success_at=%s, last_error_code=NULL "
        "WHERE source=%s AND entity='session' AND shop_id=%s",
        (expires, now, now, source, shop_id),
    )
    print(json.dumps({"action": "refresh-session", "expires_at": expires.isoformat()}))


def _parse_date(text: str) -> datetime:
    parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=BEIJING)
    return parsed


def _setup_logging(log_dir: str = "logs") -> None:
    """一行JSON、字段白名单；文件轮转10MiB×5；不记录凭证/请求体/DSN。"""
    import json as json_module
    import logging.handlers
    import os as os_module

    class JsonFormatter(logging.Formatter):
        _ALLOWED = ("request_id", "tool", "entity", "shop_id", "window",
                    "rows", "duration_ms", "data_as_of", "error_code", "attempt")

        def format(self, record: logging.LogRecord) -> str:
            payload = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                       "level": record.levelname, "logger": record.name}
            for key in self._ALLOWED:
                if hasattr(record, key):
                    payload[key] = getattr(record, key)
            if record.exc_text:
                payload["error_code"] = "exception"
            return json_module.dumps(payload, ensure_ascii=False)

    os_module.makedirs(log_dir, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        os_module.path.join(log_dir, "sync.log"), maxBytes=10 * 1024 * 1024,
        backupCount=5, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="bi_agent.sync")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("shops")
    probe = sub.add_parser("probe")
    probe.add_argument("--start", required=True, help="含，YYYY-MM-DD（北京时间）")
    probe.add_argument("--end", required=True, help="排他，YYYY-MM-DD（北京时间）")
    backfill = sub.add_parser("backfill")
    backfill.add_argument("--days", type=int, default=90)
    sub.add_parser("incremental")
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--days", type=int, default=7)
    replay = sub.add_parser("replay")
    replay.add_argument("--entity", required=True,
                        choices=["orders", "aftersales_occurrence", "aftersales_cohort"])
    replay.add_argument("--start", required=True)
    replay.add_argument("--end", required=True)
    sub.add_parser("refresh-session")
    args = parser.parse_args(argv)

    settings = load_sync_settings(os.environ)
    conn = _connect(settings.writer_dsn.get_secret_value())
    locked = False
    try:
        locked = conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone()[0]
        if not locked:
            raise RuntimeError("已有同步任务运行")
        http = httpx.Client(timeout=30.0)
        client = KuaimaiClient(settings, http)
        try:
            if args.command == "shops":
                count = sync_shops(conn, client)
                print(json.dumps({"action": "shops", "updated": count}))
            elif args.command == "probe":
                shop_id = _require_single_shop(settings)
                summary = _probe(client, shop_id=shop_id,
                                 start=_parse_date(args.start), end=_parse_date(args.end),
                                 order_source=_shop_order_source(conn, shop_id))
                print(json.dumps(summary, ensure_ascii=False))
            elif args.command == "backfill":
                t0 = datetime.now(BEIJING)  # 回填开始前记录T0；完成后在_backfill_shop内补拉[T0,T1)
                for shop_id in sorted(settings.shop_ids):
                    try:
                        order_source = _shop_order_source(conn, shop_id)
                        stats = _backfill_shop(conn, client, shop_id=shop_id,
                                               days=args.days, t0=t0,
                                               order_source=order_source)
                    except (KuaimaiError, SystemExit) as exc:
                        print(json.dumps({"action": "backfill", "shop_id": shop_id,
                                          "error": _shop_error(exc)}, ensure_ascii=False))
                        continue
                    print(json.dumps({"action": "backfill", "shop_id": shop_id,
                                      "order_source": order_source, "stats": stats}))
            elif args.command == "incremental":
                run_end = datetime.now(BEIJING)
                for shop_id in sorted(settings.shop_ids):
                    try:
                        order_source = _shop_order_source(conn, shop_id)
                        stats = _incremental_shop(conn, client, shop_id=shop_id,
                                                  run_end=run_end,
                                                  order_source=order_source)
                    except (KuaimaiError, SystemExit) as exc:
                        print(json.dumps({"action": "incremental", "shop_id": shop_id,
                                          "error": _shop_error(exc)}, ensure_ascii=False))
                        continue
                    print(json.dumps({"action": "incremental", "shop_id": shop_id,
                                      "stats": stats}))
            elif args.command == "reconcile":
                run_end = datetime.now(BEIJING)
                for shop_id in sorted(settings.shop_ids):
                    try:
                        order_source = _shop_order_source(conn, shop_id)
                        stats = _reconcile_shop(conn, client, shop_id=shop_id,
                                                days=args.days, run_end=run_end,
                                                order_source=order_source)
                    except (KuaimaiError, SystemExit) as exc:
                        print(json.dumps({"action": "reconcile", "shop_id": shop_id,
                                          "error": _shop_error(exc)}, ensure_ascii=False))
                        continue
                    print(json.dumps({"action": "reconcile", "shop_id": shop_id,
                                      "stats": stats}))
            elif args.command == "replay":
                start = _parse_date(args.start)
                end = _parse_date(args.end)
                if (end - start).days > MAX_QUERY_DAYS:
                    raise SystemExit(f"replay跨度最多{MAX_QUERY_DAYS}天")
                for shop_id in sorted(settings.shop_ids):
                    try:
                        order_source = _shop_order_source(conn, shop_id)
                        count = _replay_entity(conn, client, shop_id=shop_id,
                                               entity=args.entity, start=start, end=end,
                                               order_source=order_source)
                    except (KuaimaiError, SystemExit) as exc:
                        print(json.dumps({"action": "replay", "shop_id": shop_id,
                                          "error": _shop_error(exc)}, ensure_ascii=False))
                        continue
                    print(json.dumps({"action": "replay", "shop_id": shop_id,
                                      "entity": args.entity, "accepted": count}))
            elif args.command == "refresh-session":
                _refresh_session(conn, client)
        finally:
            http.close()
    finally:
        if locked:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
