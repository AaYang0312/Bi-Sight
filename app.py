"""内部页面：认证、固定筛选、确定性结果展示与下载。

服务端身份 subject 与范围 allowed_shop_ids 不来自URL/工具参数。
开发模式只绑定本机回环地址；生产使用Streamlit内置OIDC。
"""

from __future__ import annotations

import csv
import io
import os
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import psycopg
import streamlit as st

from bi_agent.config import AppSettings, load_app_settings
from bi_agent.metrics import (
    METRIC_DEFINITIONS,
    QueryRequest,
    query_business,
)

BEIJING = ZoneInfo("Asia/Shanghai")
QUERY_BUDGET_SECONDS = 30
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_METRIC_LABELS = {
    "paid_amount": "支付金额",
    "paid_orders": "支付商业订单数",
    "erp_documents": "ERP单据数",
    "aov": "客单价",
    "refund_amount": "平台退款发生额",
    "cash_difference": "期间收支差额",
    "cohort_refund_rate": "同批退款率",
    "quantity": "商品销量",
    "product_paid_amount": "商品支付金额",
}


def render_result(result) -> None:
    """展示一个ToolResult；固定筛选与聊天共用同一金额呈现路径。"""
    st.caption(f"数据截止：{result.data_as_of.isoformat() if result.data_as_of else '未知'}")
    if result.status == "ok":
        st.dataframe(result.data, hide_index=True)
    else:
        st.warning(f"查询未返回数据（{result.status}）")
        if result.data:
            st.dataframe(result.data, hide_index=True)
    with st.expander("指标口径与限制"):
        for name, text in result.metric_definition.items():
            st.markdown(f"- **{_METRIC_LABELS.get(name, name)}**：{text}")
        for item in result.limitations:
            st.write(f"- {item}")
    if result.coverage.status != "complete":
        st.info(f"覆盖状态：{result.coverage.status}；缺口：{', '.join(result.coverage.gaps) or '整个范围'}")


def _money(value: str | int | None) -> str:
    """金额展示用Decimal舍入到分；缺失显示不可计算，不用0补齐。"""
    if value is None:
        return "不可计算"
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return str(value)


def _is_number(cell: object) -> bool:
    try:
        Decimal(str(cell))
    except (InvalidOperation, ValueError):
        return False
    return True


def _csv_cell(cell: object) -> str:
    """对可能以 = + - @、制表符开头的文本标签做公式注入转义；金额列按数值输出。"""
    text = "" if cell is None else str(cell)
    if _is_number(text):
        return text
    if text.startswith(("=", "+", "-", "@", "\t")):
        return "'" + text
    return text


def _csv_bytes(rows: list[dict]) -> bytes:
    if not rows:
        return b""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_cell(value) for key, value in row.items()})
    return buffer.getvalue().encode("utf-8-sig")


def _exclusive_end(inclusive_end: date) -> date:
    """日期选择器中的包含结束日转换为排他end。"""
    return inclusive_end + timedelta(days=1)


def _assert_local_bind() -> None:
    address = st.get_option("server.address")
    if address not in LOCAL_HOSTS:
        st.error("development环境只允许绑定127.0.0.1/localhost/::1；"
                 "请使用 --server.address 127.0.0.1 启动")
        st.stop()


def _subject(settings: AppSettings) -> str | None:
    """返回服务端身份；无权限时返回None并提示。"""
    if settings.environment == "production":
        if not st.user.is_logged_in:
            st.login()
            st.stop()
        subject = f"{st.user.get('iss', '')}|{st.user.get('sub', '')}"
        if subject not in settings.allowed_subjects:
            st.error("当前账号没有访问权限")
            st.stop()
        return subject
    _assert_local_bind()
    return "local-development"


def _sync_status(conn, shop_ids: list[str]) -> None:
    rows = conn.execute(
        "SELECT entity, shop_id, data_as_of, last_success_at, last_error_code, covered "
        "FROM reporting.v_coverage WHERE shop_id = ANY(%s) ORDER BY entity, shop_id",
        (sorted(set(shop_ids)),),
    ).fetchall()
    st.sidebar.subheader("同步状态")
    st.sidebar.caption("当前覆盖：抖音试点店铺（fxg）")
    if not rows:
        st.sidebar.warning("尚无同步记录")
        return
    for entity, shop_id, data_as_of, last_success, error_code, covered in rows:
        label = {"orders": "订单", "aftersales_occurrence": "售后工单",
                 "aftersales_cohort": "同批退款"}.get(entity, entity)
        if error_code:
            st.sidebar.error(f"{label}：上次同步失败（{error_code}）")
        else:
            st.sidebar.write(f"{label}：正常")
        if data_as_of:
            st.sidebar.caption(f"数据截止 {data_as_of.astimezone(BEIJING):%Y-%m-%d %H:%M}")
        if covered:
            starts = [rng.lower for rng in covered if rng.lower is not None]
            ends = [rng.upper for rng in covered if rng.upper is not None]
            if starts and ends:
                st.sidebar.caption(f"覆盖 {min(starts):%Y-%m-%d} ~ {max(ends):%Y-%m-%d}")


def _run_query(settings: AppSettings, request: QueryRequest):
    now = datetime.now(BEIJING)
    with psycopg.connect(settings.reader_dsn.get_secret_value()) as conn:
        return query_business(conn, request, allowed_shop_ids=settings.shop_ids,
                              now=now, deadline=time.monotonic() + QUERY_BUDGET_SECONDS)


def _filters_and_query(settings: AppSettings) -> None:
    st.subheader("经营查询")
    shop_options = sorted(settings.shop_ids)
    col1, col2 = st.columns(2)
    with col1:
        start = st.date_input("开始日期", value=date.today() - timedelta(days=7),
                              format="YYYY-MM-DD")
    with col2:
        inclusive_end = st.date_input("结束日期（含）", value=date.today() - timedelta(days=1),
                                      format="YYYY-MM-DD")
    shops = st.multiselect("店铺", shop_options, default=shop_options[:1])
    metric_names = st.multiselect(
        "指标", sorted(_METRIC_LABELS), default=["paid_amount", "paid_orders"],
        format_func=lambda name: _METRIC_LABELS[name])
    group_by = st.radio("维度", ["total", "day", "shop", "product"],
                        format_func=lambda value: {"total": "合计", "day": "按日",
                                                   "shop": "按店铺", "product": "按商品"}[value],
                        horizontal=True)
    compare = st.checkbox("对比上一等长周期")
    if st.button("查询", type="primary"):
        try:
            request = QueryRequest(
                start=start, end=_exclusive_end(inclusive_end), shop_ids=shops,
                metrics=metric_names, group_by=group_by,
                compare="previous_period" if compare else "none")
        except ValueError as exc:
            st.error(f"参数无效：{exc}")
            return
        if inclusive_end < start:
            st.error("结束日期不能早于开始日期")
            return
        try:
            result = _run_query(settings, request)
        except psycopg.OperationalError:
            st.error("数据库连接失败，请检查只读配置")
            return
        render_result(result)
        if result.status == "ok" and result.data:
            st.download_button(
                "下载当前结果CSV",
                data=_csv_bytes(result.data),
                file_name=f"bi_report_{start.isoformat()}_{inclusive_end.isoformat()}.csv",
                mime="text/csv")
        if result.status == "ok" and group_by == "day":
            series = [(row.get("day"), row.get("paid_amount")) for row in result.data
                      if row.get("paid_amount") is not None]
            if series:
                st.subheader("支付金额趋势")
                st.line_chart({day: float(Decimal(str(value))) for day, value in series})
        if result.status == "ok" and group_by == "product":
            ranked = [(row.get("product_id"), row.get("product_paid_amount"))
                      for row in result.data
                      if row.get("product_paid_amount") is not None
                      and row.get("product_id")]
            if ranked:
                st.subheader("商品支付金额排行")
                st.bar_chart({name: float(Decimal(str(value))) for name, value in ranked})


def main() -> None:
    st.set_page_config(page_title="电商经营分析", page_icon="📊")
    st.title("电商经营分析（抖音试点）")
    try:
        settings = load_app_settings(os.environ)
    except ValueError as exc:
        st.error(f"应用配置错误：{exc}")
        st.stop()
    subject = _subject(settings)
    if st.session_state.get("subject") != subject:
        st.session_state.clear()
        st.session_state["subject"] = subject
    try:
        with psycopg.connect(settings.reader_dsn.get_secret_value()) as conn:
            _sync_status(conn, sorted(settings.shop_ids))
    except psycopg.OperationalError:
        st.sidebar.error("数据库连接失败")

    _filters_and_query(settings)
    st.divider()
    _chat_section(settings)


def _chat_section(settings: AppSettings) -> None:
    """对话入口在任务9接入；模型未配置时固定查询照常工作。"""
    st.subheader("对话查询")
    try:
        from bi_agent.config import load_model_settings

        load_model_settings(os.environ)
        st.caption("对话功能在任务9接入")
    except ValueError as exc:
        st.caption(f"对话功能未启用（{exc}）；固定查询不受影响")


if __name__ == "__main__":
    main()
