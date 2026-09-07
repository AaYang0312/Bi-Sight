"""字段规范化、窗口分页、事务、水位、补查与同步CLI。

本模块内的事务函数不自行提交；上层窗口同步统一提交整个窗口。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")

ORDER_SOURCE = "erp.trade.list.query"
AFTERSALE_SOURCE = "erp.aftersale.list.query"

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
        "line_id": str(raw_item.get("oid") or raw_item.get("id") or f"{erp_id}#{index}"),
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
    split_parent = str(raw.get("splitParentId") or "").strip() or None
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
        if existing is None or existing[0] > trade["source_updated_at"]:
            return False
        # 同版本：内容不同属于冲突，进入定向完整补查，不猜哪个新
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
        "platform_refund_id": (str(raw.get("refundId") or raw.get("platformRefundId") or "").strip() or None),
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
