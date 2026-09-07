"""独立测试数据库内的事务、权限和聚合检查。

单个用例在管理员连接的外层事务中准备数据，结束回滚；禁止连接生产库。
无测试DSN时显式skip——skip不是通过证明。
"""

import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import psycopg

BEIJING = ZoneInfo("Asia/Shanghai")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _ms(moment: datetime) -> int:
    """北京时间转快麦毫秒时间戳。"""
    return int(moment.timestamp() * 1000)


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(os.environ["BI_TEST_ADMIN_DSN"])
        if not self.conn.info.dbname.endswith("_test"):
            self.fail(f"测试必须连接 *_test 数据库，实际 {self.conn.info.dbname}")
        if (self.conn.info.host or "") not in LOCAL_HOSTS:
            self.fail(f"测试必须连接本地测试实例，实际 {self.conn.info.host}")

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def _seed_shop(self, shop_id: str = "S1"):
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name) VALUES (%s, 'fxg', %s) "
            "ON CONFLICT (shop_id) DO NOTHING",
            (shop_id, "店铺A"),
        )

    # -- 权限 ----------------------------------------------------------------

    def test_read_role_cannot_write(self):
        with psycopg.connect(os.environ["BI_TEST_READER_DSN"]) as conn:
            self.assertTrue(conn.info.dbname.endswith("_test"))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("INSERT INTO bi.shops(shop_id) VALUES ('forbidden')")

    def test_read_role_cannot_read_base_tables(self):
        with psycopg.connect(os.environ["BI_TEST_READER_DSN"]) as conn:
            self.assertTrue(conn.info.dbname.endswith("_test"))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT count(*) FROM bi.orders")

    def test_reader_can_read_reporting_views_only(self):
        with psycopg.connect(os.environ["BI_TEST_READER_DSN"]) as conn:
            self.assertTrue(conn.info.dbname.endswith("_test"))
            conn.execute("SELECT count(*) FROM reporting.v_payments").fetchone()
            conn.execute("SELECT count(*) FROM reporting.v_refunds").fetchone()
            conn.execute("SELECT count(*) FROM reporting.v_coverage").fetchone()
            conn.execute("SELECT count(*) FROM reporting.v_shops").fetchone()

    def test_statement_timeout_enforced_for_reader(self):
        with psycopg.connect(os.environ["BI_TEST_READER_DSN"]) as conn:
            value = conn.execute("SHOW statement_timeout").fetchone()[0]
            self.assertEqual(value, "5s")
            read_only = conn.execute("SHOW default_transaction_read_only").fetchone()[0]
        self.assertEqual(read_only, "on")

    def test_sync_role_can_write_fact_tables(self):
        self._seed_shop()
        with self.conn.transaction():
            self.conn.execute("SET LOCAL ROLE bi_sync")
            self.conn.execute(
                "INSERT INTO bi.orders(shop_id, erp_id, source, source_updated_at, batch_id) "
                "VALUES ('S1', 'EX', 'erp.trade.list.query', now(), 'b1')")
        self.conn.execute("RESET ROLE")

    # -- 交易规范化与支付重建 -------------------------------------------------

    def _trade(self, erp_id: str, commercial_ids: list[str], pay_amount: str,
               pay_time: datetime, upd_time: datetime, items: list[dict]) -> dict:
        from bi_agent.sync import normalise_trade

        raw = {
            "sid": erp_id,
            "userId": "S1",
            "payAmount": pay_amount,
            "payTime": _ms(pay_time),
            "updTime": _ms(upd_time),
            "orders": items,
        }
        if len(commercial_ids) == 1:
            raw["tid"] = commercial_ids[0]
        elif len(commercial_ids) > 1:
            raw["tids"] = ",".join(commercial_ids)
            raw["tid"] = commercial_ids[0]
        trade = normalise_trade(raw)
        self.assertEqual(trade["normalization_status"], "normal")
        return trade

    def test_single_order_payment_uses_head(self):
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING)
        upd_time = datetime(2026, 9, 1, 13, 0, tzinfo=BEIJING)
        trade = self._trade("E1", ["C1"], "300.00", pay_time, upd_time, [
            {"oid": "E1-1", "tid": "C1", "itemSysId": "P_A", "skuSysId": "S_A1",
             "num": "2", "payAmount": "200.00", "cost": "50"},
            {"oid": "E1-2", "tid": "C1", "itemSysId": "P_B", "skuSysId": "S_B1",
             "num": "1", "payAmount": "100.00", "cost": "40"},
        ])
        batch = "batch-head"
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id=batch))
            row = self.conn.execute(
                "SELECT amount, paid_at, basis, verified FROM bi.order_payments "
                "WHERE shop_id='S1' AND commercial_id='C1'").fetchone()
        self.assertEqual(row[0], Decimal("300.00"))
        self.assertEqual(row[1], pay_time)
        self.assertEqual(row[2], "head")
        self.assertTrue(row[3])
        verified_items = self.conn.execute(
            "SELECT count(*) FROM bi.order_items WHERE shop_id='S1' AND allocation_verified"
        ).fetchone()[0]
        self.assertEqual(verified_items, 2)

    def test_split_orders_produce_one_payment(self):
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 2, 10, 0, tzinfo=BEIJING)
        upd_time = datetime(2026, 9, 2, 11, 0, tzinfo=BEIJING)
        batch = "batch-split"
        trade_e3 = self._trade("E3", ["C3"], "40.00", pay_time, upd_time, [
            {"oid": "E3-1", "tid": "C3", "itemSysId": "P_A", "num": "1", "payAmount": "40.00"},
        ])
        trade_e4 = self._trade("E4", ["C3"], "60.00", pay_time, upd_time, [
            {"oid": "E4-1", "tid": "C3", "itemSysId": "P_B", "num": "1", "payAmount": "60.00"},
        ])
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade_e3, batch_id=batch))
            self.assertTrue(apply_trade(self.conn, trade_e4, batch_id=batch))
            row = self.conn.execute(
                "SELECT amount, verified, basis FROM bi.order_payments "
                "WHERE shop_id='S1' AND commercial_id='C3'").fetchone()
        self.assertEqual(row[0], Decimal("100.00"))
        self.assertTrue(row[1])
        self.assertEqual(row[2], "items")

    def test_merged_orders_keep_each_payment(self):
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 3, 9, 0, tzinfo=BEIJING)
        upd_time = datetime(2026, 9, 3, 10, 0, tzinfo=BEIJING)
        batch = "batch-merge"
        trade = self._trade("E5", ["C4", "C5"], "200.00", pay_time, upd_time, [
            {"oid": "E5-1", "tid": "C4", "itemSysId": "P_A", "num": "1", "payAmount": "80.00"},
            {"oid": "E5-2", "tid": "C5", "itemSysId": "P_B", "num": "1", "payAmount": "120.00"},
        ])
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id=batch))
            rows = self.conn.execute(
                "SELECT commercial_id, amount, verified FROM bi.order_payments "
                "WHERE shop_id='S1' AND commercial_id IN ('C4','C5') ORDER BY commercial_id"
            ).fetchall()
        self.assertEqual(rows[0][0], "C4")
        self.assertEqual(rows[0][1], Decimal("80.00"))
        self.assertTrue(rows[0][2])
        self.assertEqual(rows[1][0], "C5")
        self.assertEqual(rows[1][1], Decimal("120.00"))
        self.assertTrue(rows[1][2])

    def test_replay_old_version_does_not_regress(self):
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING)
        new_trade = self._trade("E1", ["C1"], "300.00", pay_time,
                                datetime(2026, 9, 2, 12, 0, tzinfo=BEIJING), [
                                    {"oid": "E1-1", "tid": "C1", "itemSysId": "P_A",
                                     "num": "2", "payAmount": "200.00"},
                                    {"oid": "E1-2", "tid": "C1", "itemSysId": "P_B",
                                     "num": "1", "payAmount": "100.00"},
                                ])
        old_trade = self._trade("E1", ["C1"], "280.00", pay_time,
                                datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING), [
                                    {"oid": "E1-1", "tid": "C1", "itemSysId": "P_A",
                                     "num": "2", "payAmount": "180.00"},
                                ])
        batch = "batch-version"
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, new_trade, batch_id=batch))
            self.assertFalse(apply_trade(self.conn, old_trade, batch_id=batch))
            amount = self.conn.execute(
                "SELECT amount FROM bi.order_payments WHERE commercial_id='C1'").fetchone()[0]
            items = self.conn.execute(
                "SELECT count(*) FROM bi.order_items WHERE erp_id='E1'").fetchone()[0]
        self.assertEqual(amount, Decimal("300.00"))
        self.assertEqual(items, 2)

    def test_replay_same_version_is_idempotent(self):
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING)
        trade = self._trade("E1", ["C1"], "300.00", pay_time,
                            datetime(2026, 9, 1, 13, 0, tzinfo=BEIJING), [
                                {"oid": "E1-1", "tid": "C1", "itemSysId": "P_A",
                                 "num": "2", "payAmount": "200.00"},
                            ])
        batch = "batch-idem"
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id=batch))
            self.assertFalse(apply_trade(self.conn, trade, batch_id="batch-idem-2"))
            count = self.conn.execute(
                "SELECT count(*) FROM bi.order_payments WHERE commercial_id='C1'").fetchone()[0]
            items = self.conn.execute(
                "SELECT count(*) FROM bi.order_items WHERE erp_id='E1'").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(items, 1)

    def test_orphan_payment_marked_unverified(self):
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING)
        with_tid = self._trade("E9", ["C9"], "50.00", pay_time,
                               datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING), [
                                   {"oid": "E9-1", "tid": "C9", "itemSysId": "P_A",
                                    "num": "1", "payAmount": "50.00"},
                               ])
        without_tid = self._trade("E9", [], "50.00", pay_time,
                                  datetime(2026, 9, 2, 12, 0, tzinfo=BEIJING), [
                                      {"oid": "E9-1", "itemSysId": "P_A",
                                       "num": "1", "payAmount": "50.00"},
                                  ])
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, with_tid, batch_id="b1"))
            self.assertTrue(apply_trade(self.conn, without_tid, batch_id="b2"))
            row = self.conn.execute(
                "SELECT amount, verified, basis FROM bi.order_payments "
                "WHERE commercial_id='C9'").fetchone()
        self.assertIsNone(row[0])
        self.assertFalse(row[1])

    def test_missing_items_field_keeps_old_details(self):
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING)
        trade = self._trade("E1", ["C1"], "300.00", pay_time,
                            datetime(2026, 9, 1, 13, 0, tzinfo=BEIJING), [
                                {"oid": "E1-1", "tid": "C1", "itemSysId": "P_A",
                                 "num": "2", "payAmount": "200.00"},
                            ])
        batch = "batch-items"
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id=batch))
            stripped = dict(trade)
            stripped["items_present"] = False
            stripped["source_updated_at"] = datetime(2026, 9, 2, 13, 0, tzinfo=BEIJING)
            self.assertTrue(apply_trade(self.conn, stripped, batch_id=batch))
            items = self.conn.execute(
                "SELECT count(*) FROM bi.order_items WHERE erp_id='E1'").fetchone()[0]
        self.assertEqual(items, 1)

    # -- 售后规范化与去重 ------------------------------------------------------

    def test_aftersale_platform_success_candidate(self):
        from bi_agent.sync import apply_aftersale, normalise_aftersale

        self._seed_shop()
        self.conn.execute(
            "INSERT INTO bi.orders(shop_id, erp_id, commercial_ids, source, "
            "source_updated_at, batch_id) VALUES ('S1','E1',ARRAY['C1'],"
            "'erp.trade.list.query', now(), 'b')")

        def aftersale(aid: str, **overrides):
            raw = {
                "aftersaleId": aid, "userId": "S1", "tid": "C1",
                "refundId": f"PR_{aid}", "rawRefundMoney": "30.00",
                "onlineStatus": 7, "status": 9,
                "platformCompleteTime": _ms(datetime(2026, 9, 2, 8, 0, tzinfo=BEIJING)),
                "modified": _ms(datetime(2026, 9, 2, 9, 0, tzinfo=BEIJING)),
            }
            raw.update(overrides)
            return normalise_aftersale(raw)

        pending = aftersale("A_pending", onlineStatus=2)
        self.assertFalse(pending["platform_success"])
        voided = aftersale("A_voided", status=10)
        self.assertFalse(voided["platform_success"])
        closed = aftersale("A_closed", onlineStatus=6)
        self.assertFalse(closed["platform_success"])
        success = aftersale("A_ok")
        self.assertTrue(success["platform_success"])

        with self.conn.transaction():
            self.assertTrue(apply_aftersale(self.conn, success, batch_id="b"))
            row = self.conn.execute(
                "SELECT matched, refund_canonical FROM bi.aftersales "
                "WHERE aftersale_id='A_ok'").fetchone()
        self.assertTrue(row[0])
        self.assertTrue(row[1])

    def test_duplicate_platform_refund_dedup(self):
        from bi_agent.sync import apply_aftersale, mark_refund_canonical, normalise_aftersale

        self._seed_shop()
        complete = _ms(datetime(2026, 9, 2, 8, 0, tzinfo=BEIJING))
        modified = _ms(datetime(2026, 9, 2, 9, 0, tzinfo=BEIJING))

        def raw(aid: str, amount: str, refund_id: str):
            return normalise_aftersale({
                "aftersaleId": aid, "userId": "S1", "tid": "C1",
                "refundId": refund_id, "rawRefundMoney": amount,
                "onlineStatus": 7, "status": 9,
                "platformCompleteTime": complete, "modified": modified,
            })

        with self.conn.transaction():
            # 同金额重复：只确认一次
            apply_aftersale(self.conn, raw("A1", "30.00", "PR_DUP"), batch_id="b")
            apply_aftersale(self.conn, raw("A2", "30.00", "PR_DUP"), batch_id="b")
            mark_refund_canonical(self.conn, "S1", {"PR_DUP"})
            rows = self.conn.execute(
                "SELECT aftersale_id, refund_canonical FROM bi.aftersales "
                "WHERE platform_refund_id='PR_DUP' ORDER BY aftersale_id").fetchall()
        self.assertEqual(rows[0][0], "A1")
        self.assertTrue(rows[0][1])
        self.assertFalse(rows[1][1])

        with self.conn.transaction():
            # 金额不一致组：未验证，禁止取最大值
            apply_aftersale(self.conn, raw("A3", "10.00", "PR_MIX"), batch_id="b")
            apply_aftersale(self.conn, raw("A4", "20.00", "PR_MIX"), batch_id="b")
            mark_refund_canonical(self.conn, "S1", {"PR_MIX"})
            rows = self.conn.execute(
                "SELECT refund_canonical FROM bi.aftersales "
                "WHERE platform_refund_id='PR_MIX'").fetchall()
        self.assertFalse(any(row[0] for row in rows))

    def test_aftersale_without_order_still_stored(self):
        from bi_agent.sync import apply_aftersale, normalise_aftersale

        self._seed_shop()
        record = normalise_aftersale({
            "aftersaleId": "A_X", "userId": "S1", "tid": "C_UNKNOWN",
            "refundId": "PR_X", "rawRefundMoney": "12.00",
            "onlineStatus": 7, "status": 9,
            "platformCompleteTime": _ms(datetime(2026, 9, 2, 8, 0, tzinfo=BEIJING)),
            "modified": _ms(datetime(2026, 9, 2, 9, 0, tzinfo=BEIJING)),
        })
        with self.conn.transaction():
            self.assertTrue(apply_aftersale(self.conn, record, batch_id="b"))
            row = self.conn.execute(
                "SELECT matched, raw_platform_amount FROM bi.aftersales "
                "WHERE aftersale_id='A_X'").fetchone()
        self.assertFalse(row[0])
        self.assertEqual(row[1], Decimal("12.00"))

    def test_invalid_records_rejected(self):
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        trade = normalise_trade({"sid": "", "userId": "S1", "updTime": _ms(datetime(2026, 9, 1, tzinfo=BEIJING))})
        self.assertEqual(trade["normalization_status"], "invalid")
        with self.conn.transaction():
            self.assertFalse(apply_trade(self.conn, trade, batch_id="b"))


    # -- 窗口事务与水位（任务4） ----------------------------------------------

    def _client(self):
        from bi_agent.config import SyncSettings
        from bi_agent.kuaimai import KuaimaiClient
        from pydantic import SecretStr

        settings = SyncSettings(
            writer_dsn=SecretStr("postgresql://localhost/bi_agent_test"),
            shop_ids=frozenset({"S1"}),
            app_key=SecretStr("k"), app_secret=SecretStr("s"),
            access_token=SecretStr("t"), refresh_token=SecretStr("r"),
        )
        return KuaimaiClient(settings, httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"success": True, "total": 0}))))

    def _state_row(self, entity: str, shop_id: str = "S1"):
        source = "erp.trade.list.query" if entity == "orders" else "erp.aftersale.list.query"
        return self.conn.execute(
            "SELECT watermark, covered, data_as_of, last_error_code FROM bi.sync_state "
            "WHERE source=%s AND entity=%s AND shop_id=%s",
            (source, entity, shop_id),
        ).fetchone()

    def test_interrupted_window_does_not_advance_watermark(self):
        """4.1：分页中断后水位不动、零业务；不能把失败变成零数据。"""
        from bi_agent.kuaimai import KuaimaiError
        from bi_agent.sync import Window, sync_window

        self._seed_shop()
        old_watermark = datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING)
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark) "
            "VALUES ('erp.trade.list.query', 'orders', 'S1', %s)",
            (old_watermark,),
        )

        def interrupted_fetch(*args, **kwargs):
            yield {"sid": "E1", "userId": "S1", "updTime": 1788537600000,
                   "tid": "C1", "payAmount": "100.00", "orders": []}
            raise KuaimaiError("timeout")

        window = Window(datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING),
                        datetime(2026, 9, 6, 0, 0, tzinfo=BEIJING))
        client = self._client()
        with patch("bi_agent.sync.fetch_window", side_effect=interrupted_fetch):
            with self.assertRaises(KuaimaiError):
                sync_window(self.conn, client, entity="orders", shop_id="S1",
                            window=window, mode="incremental")
        self.assertEqual(self._state_row("orders")[0], old_watermark)
        count = self.conn.execute(
            "SELECT count(*) FROM bi.orders WHERE shop_id='S1'").fetchone()[0]
        self.assertEqual(count, 0)

    def test_successful_incremental_advances_watermark_and_covers(self):
        """4.6：补跑→覆盖缺口闭合；连续增量扩展业务覆盖终点。"""
        from bi_agent.sync import Window, sync_window

        self._seed_shop()
        base = datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING)
        prior_end = base  # 已有覆盖终点与水位一致：连续增量
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered) "
            "VALUES ('erp.trade.list.query', 'orders', 'S1', %s, "
            "tstzmultirange(tstzrange(%s, %s, '[)')))",
            (base, base - timedelta(days=5), prior_end))

        records = [
            {"sid": "E1", "userId": "S1", "updTime": _ms(base + timedelta(hours=2)),
             "tid": "C1", "payAmount": "100.00",
             "payTime": _ms(base + timedelta(hours=1)),
             "orders": [{"oid": "E1-1", "tid": "C1", "itemSysId": "P_A",
                          "num": "1", "payAmount": "100.00"}]},
        ]
        window = Window(base, base + timedelta(days=1))

        def full_fetch(*args, **kwargs):
            yield from records

        client = self._client()
        with patch("bi_agent.sync.fetch_window", side_effect=full_fetch):
            accepted = sync_window(self.conn, client, entity="orders", shop_id="S1",
                                   window=window, mode="incremental")
        self.assertEqual(accepted, 1)
        watermark, covered, _, _ = self._state_row("orders")
        self.assertEqual(watermark, window.end)
        contains = self.conn.execute(
            "SELECT covered @> tstzmultirange(tstzrange(%s, %s, '[)')) "
            "FROM bi.sync_state WHERE source='erp.trade.list.query' "
            "AND entity='orders' AND shop_id='S1'",
            (base, base + timedelta(hours=1, seconds=1)),
        ).fetchone()[0]
        self.assertTrue(contains)
        # 覆盖不能越过已观察到的业务时间盲目延伸整天
        beyond = self.conn.execute(
            "SELECT covered @> tstzmultirange(tstzrange(%s, %s, '[)')) "
            "FROM bi.sync_state WHERE source='erp.trade.list.query' "
            "AND entity='orders' AND shop_id='S1'",
            (base + timedelta(hours=2), window.end),
        ).fetchone()[0]
        self.assertFalse(beyond)

    def test_fetch_window_cursor_pagination_contract(self):
        """4.2：首请求不传cursor；后续传上一页cursor；hasNext=true无游标报错。"""
        from bi_agent.kuaimai import KuaimaiError
        from bi_agent.sync import Window, fetch_window

        requests: list[dict] = []
        pages = [
            {"success": True, "total": 2, "hasNext": True, "cursor": "c1",
             "list": [{"sid": "E1", "userId": "S1", "updTime": 1, "payAmount": "1"}]},
            {"success": True, "total": 2, "hasNext": False,
             "list": [{"sid": "E2", "userId": "S1", "updTime": 2, "payAmount": "2"}]},
        ]

        class FakeClient:
            def call(self, method, params):
                requests.append(dict(params))
                return pages[len(requests) - 1]

        window = Window(datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING),
                        datetime(2026, 9, 6, 0, 0, tzinfo=BEIJING))
        rows = list(fetch_window(FakeClient(), entity="orders", shop_id="S1",
                                 window=window, mode="incremental"))
        self.assertEqual(len(rows), 2)
        self.assertNotIn("cursor", requests[0])
        self.assertEqual(requests[1]["cursor"], "c1")
        self.assertEqual(requests[0]["timeType"], "upd_time")
        self.assertEqual(requests[0]["queryType"], "0")

        pages_stuck = [pages[0], pages[0]]

        class StuckClient:
            def __init__(self):
                self.n = 0

            def call(self, method, params):
                self.n += 1
                return pages_stuck[min(self.n - 1, 1)]

        with self.assertRaises(KuaimaiError):
            list(fetch_window(StuckClient(), entity="orders", shop_id="S1",
                              window=window, mode="incremental"))

    def test_fetch_window_aftersales_contract(self):
        """4.2：售后分页不附订单参数，按total判断末页。"""
        from bi_agent.sync import Window, fetch_window

        requests: list[dict] = []
        pages = [
            {"success": True, "total": 1,
             "list": [{"aftersaleId": "A1", "userId": "S1", "rawRefundMoney": "30"}]},
        ]

        class FakeClient:
            def call(self, method, params):
                requests.append(dict(params))
                return pages[0]

        window = Window(datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING),
                        datetime(2026, 9, 6, 0, 0, tzinfo=BEIJING))
        rows = list(fetch_window(FakeClient(), entity="aftersales_occurrence",
                                 shop_id="S1", window=window, mode="backfill"))
        self.assertEqual(len(rows), 1)
        self.assertNotIn("timeType", requests[0])
        self.assertNotIn("useHasNext", requests[0])
        self.assertIn("startPlatformCompleteTime", requests[0])
        self.assertEqual(requests[0]["asVersion"], "2")


if __name__ == "__main__":
    unittest.main()
