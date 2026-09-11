"""独立测试数据库内的事务、权限和聚合检查。

单个用例在管理员连接的外层事务中准备数据，结束回滚；禁止连接生产库。
无测试DSN时显式skip——skip不是通过证明。
"""

import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4
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

    def test_app_role_writes_chats_but_not_business_facts(self):
        self.conn.execute("SET LOCAL ROLE bi_app")
        self.conn.execute(
            "INSERT INTO bi.app_chats(id, subject_id, title) VALUES (%s, 'subject-a', '新对话')",
            (uuid4(),),
        )
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.conn.transaction():
                self.conn.execute("INSERT INTO bi.shops(shop_id) VALUES ('forbidden')")
        self.conn.execute("RESET ROLE")

    def test_chat_turn_lock_rejects_a_second_connection(self):
        from bi_agent.chats import (ChatBusy, claim_chat_turn, create_chat,
                                    delete_chat, release_chat_turn)

        dsn = os.environ["BI_TEST_ADMIN_DSN"]
        first = psycopg.connect(dsn, autocommit=True)
        second = psycopg.connect(dsn, autocommit=True)
        subject = f"lock-{uuid4()}"
        chat = create_chat(first, subject)
        first_locked = second_locked = False
        try:
            claim_chat_turn(first, chat.id, subject)
            first_locked = True
            with self.assertRaises(ChatBusy):
                claim_chat_turn(second, chat.id, subject)
            release_chat_turn(first, chat.id)
            first_locked = False
            claim_chat_turn(second, chat.id, subject)
            second_locked = True
        finally:
            if first_locked:
                release_chat_turn(first, chat.id)
            if second_locked:
                release_chat_turn(second, chat.id)
            delete_chat(first, subject, chat.id)
            first.close()
            second.close()

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

    def test_shop_sync_persists_the_documented_active_flag(self):
        from bi_agent.sync import sync_shops

        class Client:
            def call(self, method, parameters):
                return {"success": True, "total": 2, "hasNext": False, "list": [
                    {"userId": "S_DISABLED", "state": 1, "active": 0},
                    {"userId": "S_ACTIVE", "state": 4, "active": 1},
                ]}

        self.assertEqual(sync_shops(self.conn, Client()), 2)
        rows = self.conn.execute(
            "SELECT shop_id, enabled FROM bi.shops "
            "WHERE shop_id IN ('S_DISABLED', 'S_ACTIVE') ORDER BY shop_id").fetchall()
        self.assertEqual(rows, [("S_ACTIVE", True), ("S_DISABLED", False)])

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
            self.assertFalse(apply_trade(self.conn, old_trade, batch_id=batch, force=True))
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

    def test_replay_can_re_normalise_an_equal_source_version(self):
        """显式 replay 只允许同版本重规范化，供字段映射修复回填使用。"""
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        updated_at = datetime(2026, 9, 1, 13, 0, tzinfo=BEIJING)
        base = {
            "sid": "E_REPLAY", "userId": "S1", "tid": "C_REPLAY",
            "updTime": _ms(updated_at), "orders": [{"oid": "L_REPLAY"}],
        }
        corrected = {
            **base, "unifiedStatus": "CLOSED", "sysStatus": "FINISHED",
            "orders": [{"oid": "L_REPLAY", "type": 2}],
        }
        with self.conn.transaction():
            self.assertTrue(apply_trade(
                self.conn, normalise_trade(base), batch_id="original"))
            self.assertTrue(apply_trade(
                self.conn, normalise_trade(corrected), batch_id="replay", force=True))
            order = self.conn.execute(
                "SELECT active, unified_status FROM bi.orders WHERE erp_id='E_REPLAY'").fetchone()
            item = self.conn.execute(
                "SELECT source_type, line_kind FROM bi.order_items WHERE erp_id='E_REPLAY'").fetchone()
        self.assertEqual(order, (False, "CLOSED"))
        self.assertEqual(item, (2, "suite"))

    def test_closed_order_has_no_product_daily_row(self):
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        paid_at = datetime(2026, 9, 2, 12, tzinfo=BEIJING)
        trade = normalise_trade({
            "sid": "E_CLOSED", "userId": "S1", "tid": "C_CLOSED",
            "updTime": _ms(paid_at), "payTime": _ms(paid_at),
            "unifiedStatus": "CLOSED",
            "orders": [{"oid": "L_CLOSED", "itemSysId": "P_CLOSED", "type": 0,
                        "num": "1", "payAmount": "10"}],
        })
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id="closed"))
            count = self.conn.execute(
                "SELECT count(*) FROM reporting.v_product_daily WHERE product_id='P_CLOSED'").fetchone()[0]
        self.assertEqual(count, 0)

    def test_closed_paid_order_keeps_cash_payment_and_refund_match(self):
        from bi_agent.sync import apply_aftersale, apply_trade, normalise_aftersale, normalise_trade

        self._seed_shop()
        paid_at = datetime(2026, 9, 2, 12, tzinfo=BEIJING)
        closed = normalise_trade({
            "sid": "E_CASH", "userId": "S1", "tid": "C_CASH",
            "updTime": _ms(paid_at), "payTime": _ms(paid_at), "payAmount": "100",
            "unifiedStatus": "CLOSED",
            "orders": [{"oid": "L_CASH", "tid": "C_CASH", "itemSysId": "P_CASH",
                        "type": 0, "num": "1", "payAmount": "100"}],
        })
        refund = normalise_aftersale({
            "aftersaleId": "A_CASH", "userId": "S1", "tid": "C_CASH",
            "onlineStatus": 7, "status": 9, "rawRefundMoney": "30",
            "platformCompleteTime": _ms(paid_at), "modified": _ms(paid_at),
        })
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, closed, batch_id="cash"))
            self.assertTrue(apply_aftersale(self.conn, refund, batch_id="cash"))
            payment = self.conn.execute(
                "SELECT amount, verified FROM bi.order_payments WHERE commercial_id='C_CASH'").fetchone()
            matched = self.conn.execute(
                "SELECT matched FROM bi.aftersales WHERE aftersale_id='A_CASH'").fetchone()[0]
        self.assertEqual(payment, (Decimal("100"), True))
        self.assertTrue(matched)

    def test_closed_paid_split_orders_keep_one_verified_payment(self):
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        paid_at = datetime(2026, 9, 2, 12, tzinfo=BEIJING)

        def closed_trade(erp_id: str, line_id: str, amount: str):
            return normalise_trade({
                "sid": erp_id, "userId": "S1", "tid": "C_CLOSED_SPLIT",
                "updTime": _ms(paid_at), "payTime": _ms(paid_at), "payAmount": amount,
                "unifiedStatus": "CLOSED",
                "orders": [{"oid": line_id, "tid": "C_CLOSED_SPLIT", "itemSysId": "P_SPLIT",
                            "type": 0, "num": "1", "payAmount": amount}],
            })

        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, closed_trade("E_SPLIT_1", "L_SPLIT_1", "40"),
                                        batch_id="closed-split"))
            self.assertTrue(apply_trade(self.conn, closed_trade("E_SPLIT_2", "L_SPLIT_2", "60"),
                                        batch_id="closed-split"))
            payment = self.conn.execute(
                "SELECT amount, verified, basis FROM bi.order_payments "
                "WHERE commercial_id='C_CLOSED_SPLIT'").fetchone()
        self.assertEqual(payment, (Decimal("100"), True, "items"))

    def test_metric_semantics_migration_appends_line_kind_to_legacy_view(self):
        from pathlib import Path

        self.conn.execute("DROP VIEW reporting.v_product_daily")
        self.conn.execute(
            "CREATE OR REPLACE VIEW reporting.v_product_daily AS "
            "SELECT shop_id, (paid_at AT TIME ZONE 'Asia/Shanghai')::date AS day, product_id, "
            "sum(quantity) AS quantity, sum(gift_quantity) AS gift_quantity, "
            "sum(allocated_paid_amount) AS product_paid_amount, bool_and(allocation_verified) "
            "AS allocation_verified FROM bi.order_items "
            "WHERE active AND line_kind = 'sale' AND product_id IS NOT NULL "
            "AND allocated_paid_amount IS NOT NULL GROUP BY shop_id, day, product_id")
        migration = Path(__file__).parents[1] / "sql" / "003_kuaimai_metric_semantics.sql"
        self.conn.execute(migration.read_text(encoding="utf-8"))
        columns = self.conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='reporting' AND table_name='v_product_daily' "
            "ORDER BY ordinal_position").fetchall()
        self.assertEqual([row[0] for row in columns], [
            "shop_id", "day", "product_id", "quantity", "gift_quantity",
            "product_paid_amount", "allocation_verified", "line_kind",
        ])

    def test_product_daily_keeps_suite_parent_with_kind_label(self):
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        paid_at = datetime(2026, 9, 2, 12, tzinfo=BEIJING)
        suite = normalise_trade({
            "sid": "E_SUITE", "userId": "S1", "tid": "C_SUITE",
            "updTime": _ms(paid_at), "payTime": _ms(paid_at), "payAmount": "100",
            "orders": [{"oid": "L_SUITE", "tid": "C_SUITE", "itemSysId": "P_SUITE",
                        "type": 2, "num": "1", "payAmount": "100"}],
        })
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, suite, batch_id="suite"))
            row = self.conn.execute(
                "SELECT line_kind, quantity, product_paid_amount "
                "FROM reporting.v_product_daily WHERE product_id='P_SUITE'").fetchone()
        self.assertEqual(row, ("suite", Decimal("1"), Decimal("100")))

    def test_status_only_trade_update_deactivates_existing_product_rows(self):
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        paid_at = datetime(2026, 9, 2, 12, tzinfo=BEIJING)
        active = normalise_trade({
            "sid": "E_STATUS", "userId": "S1", "tid": "C_STATUS",
            "updTime": _ms(paid_at), "payTime": _ms(paid_at),
            "orders": [{"oid": "L_STATUS", "tid": "C_STATUS", "itemSysId": "P_STATUS",
                        "type": 0, "num": "1", "payAmount": "10"}],
        })
        closed = normalise_trade({
            "sid": "E_STATUS", "userId": "S1", "tid": "C_STATUS",
            "updTime": _ms(paid_at + timedelta(hours=1)), "payTime": _ms(paid_at),
            "unifiedStatus": "CLOSED",
        })
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, active, batch_id="active"))
            self.assertTrue(apply_trade(self.conn, closed, batch_id="closed"))
            count = self.conn.execute(
                "SELECT count(*) FROM reporting.v_product_daily WHERE product_id='P_STATUS'").fetchone()[0]
        self.assertEqual(count, 0)

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

    def test_documented_trade_mapping_fields_persist(self):
        """A1/A3/A4/A6：新字段及派生规则应一起写入事实表。"""
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        trade = normalise_trade({
            "sid": "E_MAP", "userId": "S1", "tid": "C_MAP",
            "updTime": _ms(datetime(2026, 9, 2, 12, tzinfo=BEIJING)),
            "unifiedStatus": "CLOSED", "sysStatus": "FINISHED",
            "splitType": 1, "splitSid": "E_PARENT",
            "orders": [{"id": "L_MAP", "oid": "P_MAP", "type": 2,
                        "num": "1", "payAmount": "10"}],
        })
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id="mapping"))
            order = self.conn.execute(
                "SELECT unified_status, system_status, split_parent_id, active "
                "FROM bi.orders WHERE erp_id='E_MAP'").fetchone()
            item = self.conn.execute(
                "SELECT platform_line_id, source_type, line_kind "
                "FROM bi.order_items WHERE erp_id='E_MAP'").fetchone()
        self.assertEqual(order, ("CLOSED", "FINISHED", "E_PARENT", False))
        self.assertEqual(item, ("P_MAP", 2, "suite"))

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

    def test_aftersale_finished_and_multi_status_are_persisted_safely(self):
        from bi_agent.sync import apply_aftersale, normalise_aftersale

        self._seed_shop()
        finished = datetime(2026, 9, 2, 8, 0, tzinfo=BEIJING)
        record = normalise_aftersale({
            "aftersaleId": "A_MAP", "userId": "S1", "status": "2,10",
            "onlineStatus": 7, "modified": _ms(finished),
            "finished": _ms(finished), "platformCompleteTime": _ms(finished),
        })
        with self.conn.transaction():
            self.assertTrue(apply_aftersale(self.conn, record, batch_id="mapping"))
            row = self.conn.execute(
                "SELECT work_status, system_completed_at, platform_success "
                "FROM bi.aftersales WHERE aftersale_id='A_MAP'").fetchone()
        self.assertEqual(row, (2, finished, False))

    def test_aftersale_replay_can_re_normalise_an_equal_source_version(self):
        from bi_agent.sync import apply_aftersale, normalise_aftersale

        self._seed_shop()
        modified = datetime(2026, 9, 2, 8, 0, tzinfo=BEIJING)
        base = {
            "aftersaleId": "A_REPLAY", "userId": "S1", "status": 9,
            "onlineStatus": 7, "modified": _ms(modified),
        }
        corrected = {**base, "finished": _ms(modified), "status": "2,10"}
        with self.conn.transaction():
            self.assertTrue(apply_aftersale(
                self.conn, normalise_aftersale(base), batch_id="original"))
            self.assertTrue(apply_aftersale(
                self.conn, normalise_aftersale(corrected), batch_id="replay", force=True))
            row = self.conn.execute(
                "SELECT system_completed_at, platform_success FROM bi.aftersales "
                "WHERE aftersale_id='A_REPLAY'").fetchone()
        self.assertEqual(row, (modified, False))

    def test_aftersale_replay_does_not_regress_an_older_source_version(self):
        from bi_agent.sync import apply_aftersale, normalise_aftersale

        self._seed_shop()
        newer = datetime(2026, 9, 3, 8, 0, tzinfo=BEIJING)
        older = datetime(2026, 9, 2, 8, 0, tzinfo=BEIJING)
        current = normalise_aftersale({
            "aftersaleId": "A_NEWER", "userId": "S1", "status": 9,
            "onlineStatus": 7, "modified": _ms(newer), "finished": _ms(newer),
        })
        stale = normalise_aftersale({
            "aftersaleId": "A_NEWER", "userId": "S1", "status": "2,10",
            "onlineStatus": 7, "modified": _ms(older), "finished": _ms(older),
        })
        with self.conn.transaction():
            self.assertTrue(apply_aftersale(self.conn, current, batch_id="newer"))
            self.assertFalse(apply_aftersale(self.conn, stale, batch_id="replay", force=True))
            finished = self.conn.execute(
                "SELECT system_completed_at FROM bi.aftersales WHERE aftersale_id='A_NEWER'").fetchone()[0]
        self.assertEqual(finished, newer)

    def test_all_disabled_shops_return_no_metric_scope(self):
        import time as time_module

        from bi_agent.metrics import QueryRequest, query_business

        self._seed_shop()
        self.conn.execute("UPDATE bi.shops SET enabled=false WHERE shop_id='S1'")
        result = query_business(
            self.conn,
            QueryRequest(start="2026-09-01", end="2026-09-02", shop_ids=["S1"],
                         metrics=["paid_amount"]),
            allowed_shop_ids=frozenset({"S1"}),
            now=datetime(2026, 9, 3, tzinfo=BEIJING),
            deadline=time_module.monotonic() + 30,
        )
        self.assertEqual(result.status, "missing_data")
        self.assertEqual(result.data, [])
        self.assertIn("所选店铺均已停用，无法查询", result.limitations)

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

    def test_backfill_establishes_watermark_so_incremental_can_start(self):
        """C-2：backfill 结束必须留下非 epoch 水位，否则后续增量永远 SystemExit。"""
        from bi_agent.sync import _backfill_shop, _incremental_shop

        self._seed_shop()
        t0 = datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING)
        t1 = datetime(2026, 9, 5, 6, 0, tzinfo=BEIJING)
        client = self._client()

        # 拉取本身不是本用例要验的：空页让 sync_window 只跑状态写入。
        with patch("bi_agent.sync.fetch_window",
                   side_effect=lambda *args, **kwargs: iter(())):
            _backfill_shop(self.conn, client, shop_id="S1", days=1, t0=t0,
                           now=lambda: t1)

        for entity in ("orders", "aftersales_occurrence"):
            watermark, _covered, data_as_of, error_code = self._state_row(entity)
            self.assertGreater(watermark, datetime(1970, 1, 2, tzinfo=BEIJING))
            self.assertEqual(watermark, t1)
            # data_as_of 必须是回填结束时刻，不是开工瞬间。
            self.assertEqual(data_as_of, t1)
            self.assertIsNone(error_code)

        # 关键回归：旧行为下水位停在 epoch，这里会直接 SystemExit。
        # 必须在两个实体的断言都跑完之后再推增量：_incremental_shop 会把两条水位
        # 一起推到 run_end，放进循环里会让第二个实体的 assertEqual(watermark, t1) 失真。
        stats = _incremental_shop(self.conn, client, shop_id="S1",
                                  run_end=t1 + timedelta(hours=1))
        self.assertEqual(stats["orders"], 0)
        self.assertEqual(self._state_row("orders")[0], t1 + timedelta(hours=1))

    def test_row_cap_does_not_silently_truncate_totals(self):
        """C-3：日行数远超 MAX_ROWS 时，真实SQL下 total/shop 仍必须是完整汇总。"""
        import time as time_module
        from datetime import date

        from bi_agent.metrics import MAX_ROWS, QueryRequest, query_business

        start = date(2025, 9, 1)
        end = start + timedelta(days=366)          # MAX_SPAN_DAYS 上限
        shops = ("S1", "S2", "S3")
        start_ts = datetime(start.year, start.month, start.day, tzinfo=BEIJING)
        end_ts = datetime(end.year, end.month, end.day, tzinfo=BEIJING)
        groups = 366 * len(shops)
        self.assertGreater(groups, MAX_ROWS)

        for shop_id in shops:
            self.conn.execute(
                "INSERT INTO bi.shops(shop_id, platform, display_name) "
                "VALUES (%s, 'fxg', %s) ON CONFLICT (shop_id) DO NOTHING",
                (shop_id, shop_id))
            self.conn.execute(
                "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
                "data_as_of) VALUES ('erp.trade.list.query', 'orders', %s, %s, "
                "tstzmultirange(tstzrange(%s, %s, '[)')), %s) "
                "ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
                "covered = EXCLUDED.covered, data_as_of = EXCLUDED.data_as_of",
                (shop_id, end_ts, start_ts, end_ts, end_ts))
        self.conn.execute(
            """
            INSERT INTO bi.order_payments(shop_id, commercial_id, paid_at, amount,
                                          currency, basis, verified)
            SELECT s.shop_id,
                   'C-' || s.shop_id || '-' || d.n,
                   %s::timestamptz + d.n * interval '1 day' + interval '12 hours',
                   100.00, 'CNY', 'head', true
            FROM (VALUES ('S1'), ('S2'), ('S3')) AS s(shop_id),
                 generate_series(0, 365) AS d(n)
            """,
            (start_ts,))

        def run(group_by, metrics):
            request = QueryRequest(start=start, end=end, shop_ids=list(shops),
                                   metrics=list(metrics), group_by=group_by)
            return query_business(self.conn, request, allowed_shop_ids=frozenset(shops),
                                  now=end_ts, deadline=time_module.monotonic() + 30)

        total = run("total", ["paid_amount", "paid_orders", "aov"])
        self.assertEqual(total.status, "ok", total.limitations)
        # 只拿得到前 500 个日行时会被读成 50000（且标 status=ok）。
        self.assertEqual(Decimal(total.data[0]["paid_amount"]), Decimal(100) * groups)
        self.assertEqual(total.data[0]["paid_orders"], groups)
        self.assertEqual(Decimal(total.data[0]["aov"]), Decimal("100"))

        by_shop = run("shop", ["paid_amount"])
        self.assertEqual(by_shop.status, "ok", by_shop.limitations)
        self.assertEqual([row["shop_id"] for row in by_shop.data], list(shops))
        for row in by_shop.data:
            self.assertEqual(Decimal(row["paid_amount"]), Decimal(100) * 366)

        # 逐日分组确实超上限：宁可拒绝参数，也不给一个偏低的数。
        day = run("day", ["paid_amount"])
        self.assertEqual(day.status, "invalid_parameters")
        self.assertEqual(day.data, [])

    def _split_pair(self, *, sibling_upd: datetime):
        """已核验的单头支付 + 一条行明细未到齐的兄弟拆单（undetermined 来源）。

        返回 (支付时刻, 本次守卫拦截数)；计数是进程级全局量，用差值断言才不被其它用例干扰。
        """
        from bi_agent.sync import GUARD_STATS, apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 2, 10, 0, tzinfo=BEIJING)
        first = self._trade("E3", ["C3"], "40.00", pay_time,
                            datetime(2026, 9, 2, 11, 0, tzinfo=BEIJING), [
                                {"oid": "E3-1", "tid": "C3", "itemSysId": "P_A",
                                 "num": "1", "payAmount": "40.00"}])
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, first, batch_id="guard-a"))
        before = GUARD_STATS.payment_downgrade_blocked
        sibling = self._trade("E4", ["C3"], "60.00", pay_time, sibling_upd, [])
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, sibling, batch_id="guard-b"))
        return pay_time, GUARD_STATS.payment_downgrade_blocked - before

    def _payment_row(self):
        return self.conn.execute(
            "SELECT amount, verified, basis FROM bi.order_payments "
            "WHERE shop_id='S1' AND commercial_id='C3'").fetchone()

    def test_incomplete_sibling_cannot_wipe_a_verified_payment(self):
        """C-5：兄弟拆单未到齐产生的 undetermined 不得清零已核验收入。"""
        pay_time, blocked = self._split_pair(
            sibling_upd=datetime(2026, 9, 2, 10, 30, tzinfo=BEIJING))

        self.assertEqual(self._payment_row(), (Decimal("40.00"), True, "head"))
        self.assertEqual(blocked, 1)
        # 行级核验证据也必须保留（被拦下时不再抹掉）
        self.assertTrue(self.conn.execute(
            "SELECT allocation_verified FROM bi.order_items "
            "WHERE shop_id='S1' AND erp_id='E3'").fetchone()[0])
        # 全域收入（v_shop_daily 带 WHERE verified）没有无声消失
        self.assertEqual(self.conn.execute(
            "SELECT paid_amount FROM reporting.v_shop_daily "
            "WHERE shop_id='S1' AND day = %s", (pay_time.date(),)).fetchone()[0],
            Decimal("40.00"))

    def test_strictly_fresher_undetermined_evidence_still_downgrades(self):
        """守卫只拦“旧证据覆盖新事实”；更新的真证据仍应能推翻核验。"""
        _pay_time, blocked = self._split_pair(
            sibling_upd=datetime(2026, 9, 2, 12, 0, tzinfo=BEIJING))

        self.assertEqual(self._payment_row(), (None, False, "undetermined"))
        self.assertEqual(blocked, 0)

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




# ---------------------------------------------------------------------------
# 任务5合成数据集（人工答案基准，无PII），供指标测试与验收脚本共用
# ---------------------------------------------------------------------------

FROZEN_NOW = datetime(2026, 9, 8, 9, 0, tzinfo=BEIJING)
FROZEN_CUTOFF = datetime(2026, 9, 8, 0, 0, tzinfo=BEIJING)
COVERAGE_START = datetime(2026, 8, 25, 0, 0, tzinfo=BEIJING)
COVERAGE_END = datetime(2026, 9, 8, 0, 0, tzinfo=BEIJING)


def seed_business_case(conn) -> None:
    """冻结时刻2026-09-08 09:00+08；覆盖2026-08-25至2026-09-08；金额均为元。"""
    conn.execute(
        "INSERT INTO bi.shops(shop_id, platform, display_name) VALUES "
        "('S1','fxg','店铺A'), ('S2','fxg','店铺B') "
        "ON CONFLICT (shop_id) DO NOTHING")

    def pay_time(day: int, hour: int) -> datetime:
        return datetime(2026, 9, day, hour, tzinfo=BEIJING)

    def aug(day: int, hour: int) -> datetime:
        return datetime(2026, 8, day, hour, tzinfo=BEIJING)

    trades = [
        # C0 08-31 500：A 1件500
        {"sid": "E0", "userId": "S1", "tid": "C0", "payAmount": "500.00",
         "payTime": _ms(aug(31, 12)), "updTime": _ms(aug(31, 13)),
         "orders": [{"oid": "E0-1", "tid": "C0", "itemSysId": "P_A", "num": "1",
                      "payAmount": "500.00"}]},
        # C1 09-01 300：A 2件200 + B 1件100
        {"sid": "E1", "userId": "S1", "tid": "C1", "payAmount": "300.00",
         "payTime": _ms(pay_time(1, 10)), "updTime": _ms(pay_time(1, 11)),
         "orders": [{"oid": "E1-1", "tid": "C1", "itemSysId": "P_A", "num": "2",
                      "payAmount": "200.00"},
                     {"oid": "E1-2", "tid": "C1", "itemSysId": "P_B", "num": "1",
                      "payAmount": "100.00"}]},
        # C2 09-01 200：A 2件200
        {"sid": "E2", "userId": "S1", "tid": "C2", "payAmount": "200.00",
         "payTime": _ms(pay_time(1, 15)), "updTime": _ms(pay_time(1, 16)),
         "orders": [{"oid": "E2-1", "tid": "C2", "itemSysId": "P_A", "num": "2",
                      "payAmount": "200.00"}]},
        # C3 09-02 100：拆为E3/E4
        {"sid": "E3", "userId": "S1", "tid": "C3", "payAmount": "40.00",
         "payTime": _ms(pay_time(2, 11)), "updTime": _ms(pay_time(2, 12)),
         "orders": [{"oid": "E3-1", "tid": "C3", "itemSysId": "P_A", "num": "1",
                      "payAmount": "40.00"}]},
        {"sid": "E4", "userId": "S1", "tid": "C3", "payAmount": "60.00",
         "payTime": _ms(pay_time(2, 11)), "updTime": _ms(pay_time(2, 12)),
         "orders": [{"oid": "E4-1", "tid": "C3", "itemSysId": "P_B", "num": "1",
                      "payAmount": "60.00"}]},
        # C4/C5 09-03：合入E5
        {"sid": "E5", "userId": "S1", "tid": "C4", "tids": "C4,C5",
         "payAmount": "200.00", "payTime": _ms(pay_time(3, 9)),
         "updTime": _ms(pay_time(3, 10)),
         "orders": [{"oid": "E5-1", "tid": "C4", "itemSysId": "P_A", "num": "1",
                      "payAmount": "80.00"},
                     {"oid": "E5-2", "tid": "C5", "itemSysId": "P_B", "num": "1",
                      "payAmount": "120.00"}]},
        # C6 09-05 200：A 1件80 + B 1件120
        {"sid": "E6", "userId": "S1", "tid": "C6", "payAmount": "200.00",
         "payTime": _ms(pay_time(5, 20)), "updTime": _ms(pay_time(5, 21)),
         "orders": [{"oid": "E6-1", "tid": "C6", "itemSysId": "P_A", "num": "1",
                      "payAmount": "80.00"},
                     {"oid": "E6-2", "tid": "C6", "itemSysId": "P_B", "num": "1",
                      "payAmount": "120.00"}]},
    ]
    from bi_agent.sync import apply_trade, normalise_trade

    for raw in trades:
        trade = normalise_trade(raw)
        assert trade["normalization_status"] == "normal", raw
        if not apply_trade(conn, trade, batch_id="seed"):
            # 同版本幂等重放：确认记录已存在
            assert conn.execute(
                "SELECT 1 FROM bi.orders WHERE shop_id=%s AND erp_id=%s",
                (trade["shop_id"], trade["erp_id"])).fetchone()

    from bi_agent.sync import apply_aftersale, normalise_aftersale

    def refund(aid: str, tid: str | None, refund_id: str, amount: str,
               complete: datetime | None, online_status: int, status: int,
               modified: datetime) -> None:
        raw = {"aftersaleId": aid, "userId": "S1", "rawRefundMoney": amount,
               "onlineStatus": online_status, "status": status, "modified": _ms(modified)}
        if tid:
            raw["tid"] = tid
        if refund_id:
            raw["refundId"] = refund_id
        if complete is not None:
            raw["platformCompleteTime"] = _ms(complete)
        record = normalise_aftersale(raw)
        if not apply_aftersale(conn, record, batch_id="seed"):
            assert conn.execute(
                "SELECT 1 FROM bi.aftersales WHERE shop_id='S1' AND aftersale_id=%s",
                (record["aftersale_id"],)).fetchone()

    refund("R1", "C1", "PR1", "30.00", pay_time(2, 8), 7, 9, pay_time(2, 8))
    refund("R2", "C1", "PR2", "20.00", pay_time(4, 8), 7, 9, pay_time(4, 8))
    refund("R3", "C0", "PR3", "50.00", pay_time(3, 8), 7, 9, pay_time(3, 8))
    # R4 09-09超过本次截止，不能计入
    refund("R4", "C2", "PR4", "40.00",
           datetime(2026, 9, 9, 8, tzinfo=BEIJING), 7, 9,
           datetime(2026, 9, 9, 8, tzinfo=BEIJING))
    # R5 待处理退款10；R6 工单已解决但线上退款关闭20：均不计
    refund("R5", "C3", "PR5", "10.00", None, 2, 2, pay_time(5, 8))
    refund("R6", "C2", "PR6", "20.00", None, 6, 9, pay_time(6, 8))

    for entity, source in (("orders", "erp.trade.list.query"),
                           ("aftersales_occurrence", "erp.aftersale.list.query"),
                           ("aftersales_cohort", "erp.aftersale.list.query")):
        conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
            "data_as_of, quality_ok) VALUES (%s, %s, 'S1', %s, "
            "tstzmultirange(tstzrange(%s, %s, '[)')), %s, true) "
            "ON CONFLICT (source, entity, shop_id) DO UPDATE SET covered = "
            "EXCLUDED.covered, data_as_of = EXCLUDED.data_as_of, quality_ok = true",
            (source, entity, COVERAGE_END, COVERAGE_START, COVERAGE_END, FROZEN_CUTOFF))


class MetricsTests(unittest.TestCase):
    """5.2/5.6：人工金额断言与业务风险检查，reader角色只读执行。"""

    def setUp(self):
        if not os.getenv("BI_TEST_ADMIN_DSN"):
            self.skipTest("未配置独立测试数据库")
        self.conn = psycopg.connect(os.environ["BI_TEST_ADMIN_DSN"])
        if not self.conn.info.dbname.endswith("_test"):
            self.fail(f"测试必须连接 *_test 数据库，实际 {self.conn.info.dbname}")
        seed_business_case(self.conn)
        self.conn.execute("SET LOCAL ROLE bi_reader")

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def _query(self, **overrides):
        import time as time_module

        from bi_agent.metrics import QueryRequest, query_business

        defaults = dict(start="2026-09-01", end="2026-09-08", shop_ids=["S1"],
                        metrics=["paid_amount"])
        defaults.update(overrides)
        request = QueryRequest(**defaults)
        return query_business(self.conn, request, allowed_shop_ids=frozenset({"S1"}),
                              now=FROZEN_NOW, deadline=time_module.monotonic() + 30)

    def test_period_totals_match_manual_answers(self):
        result = self._query(metrics=["paid_amount", "paid_orders", "refund_amount",
                                      "cash_difference", "cohort_refund_rate"])
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual(result.coverage.status, "complete")
        row = result.data[0]
        self.assertEqual(Decimal(row["paid_amount"]), Decimal("1000"))
        self.assertEqual(row["paid_orders"], 6)
        self.assertEqual(Decimal(row["refund_amount"]), Decimal("100"))
        self.assertEqual(Decimal(row["cash_difference"]), Decimal("900"))
        self.assertEqual(Decimal(row["cohort_refund_rate"]), Decimal("0.05"))
        self.assertEqual(result.data_as_of, FROZEN_CUTOFF)

    def test_aov_uses_commercial_orders(self):
        result = self._query(metrics=["paid_amount", "paid_orders", "aov"])
        row = result.data[0]
        self.assertEqual(Decimal(row["aov"]), (Decimal("1000") / Decimal("6")))
        # ERP单据数同样是6（E1..E6），不能以此替代商业单分母检验
        self.assertNotIn("erp_documents", row)

    def test_sep02_granularity_distinct(self):
        """09-02单独看：ERP单2、商业单1，证明没有混淆粒度。"""
        result = self._query(start="2026-09-02", end="2026-09-03",
                             metrics=["paid_amount", "paid_orders", "erp_documents"],
                             group_by="day")
        row = result.data[0]
        self.assertEqual(Decimal(row["paid_amount"]), Decimal("100"))
        self.assertEqual(row["paid_orders"], 1)
        self.assertEqual(row["erp_documents"], 2)

    def test_day_trend_zero_fills_only_covered_days(self):
        result = self._query(metrics=["paid_amount"], group_by="day")
        self.assertEqual(result.status, "ok", result.limitations)
        by_day = {row["day"]: Decimal(row["paid_amount"]) for row in result.data}
        expected = {"2026-09-01": "500", "2026-09-02": "100", "2026-09-03": "200",
                    "2026-09-04": "0", "2026-09-05": "200", "2026-09-06": "0",
                    "2026-09-07": "0"}
        self.assertEqual(len(result.data), 7)
        for day, value in expected.items():
            self.assertEqual(by_day[day], Decimal(value))

    def test_product_ranking(self):
        result = self._query(metrics=["product_paid_amount", "quantity"],
                             group_by="product", top_n=2)
        self.assertEqual(result.status, "ok", result.limitations)
        rows = {row["product_id"]: row for row in result.data}
        self.assertEqual(Decimal(rows["P_A"]["product_paid_amount"]), Decimal("600"))
        self.assertEqual(Decimal(rows["P_A"]["quantity"]), Decimal("7"))
        self.assertEqual(Decimal(rows["P_B"]["product_paid_amount"]), Decimal("400"))
        self.assertEqual(Decimal(rows["P_B"]["quantity"]), Decimal("4"))
        self.assertTrue(all(row["allocation_verified"] == 1 for row in rows.values()))

    def test_compare_previous_period(self):
        result = self._query(metrics=["paid_amount"], compare="previous_period")
        row = result.data[0]
        self.assertEqual(Decimal(row["paid_amount"]), Decimal("1000"))
        self.assertEqual(Decimal(row["paid_amount_previous"]), Decimal("500"))
        self.assertEqual(Decimal(row["paid_amount_change"]), Decimal("500"))
        self.assertEqual(Decimal(row["paid_amount_change_ratio"]), Decimal("1"))

    def test_cross_period_refund_and_partial_refunds_counted(self):
        """R3跨期退款计入期间退款发生；C1两次部分退款都计入。"""
        result = self._query(metrics=["refund_amount"])
        self.assertEqual(Decimal(result.data[0]["refund_amount"]), Decimal("100"))

    def test_refund_after_cutoff_excluded(self):
        """R4于09-09退款：窗口与截止都不含。"""
        result = self._query(start="2026-09-01", end="2026-09-10",
                             metrics=["refund_amount"])
        self.assertEqual(result.status, "missing_data")

    def test_pending_and_closed_refunds_not_counted(self):
        result = self._query(metrics=["refund_amount", "cash_difference"])
        self.assertEqual(Decimal(result.data[0]["refund_amount"]), Decimal("100"))

    def test_unmatched_refund_degrades_to_missing_data(self):
        """另加一条未匹配成功退款：退款归属未确认，返回缺数据并显示数量。"""
        from bi_agent.sync import apply_aftersale, normalise_aftersale

        self.conn.execute("RESET ROLE")
        record = normalise_aftersale({
            "aftersaleId": "R7", "userId": "S1", "refundId": "PR7",
            "rawRefundMoney": "25.00", "onlineStatus": 7, "status": 9,
            "platformCompleteTime": _ms(datetime(2026, 9, 5, 8, tzinfo=BEIJING)),
            "modified": _ms(datetime(2026, 9, 5, 8, tzinfo=BEIJING)),
        })
        self.assertTrue(apply_aftersale(self.conn, record, batch_id="seed2"))
        self.conn.execute("SET LOCAL ROLE bi_reader")
        result = self._query(metrics=["refund_amount", "cohort_refund_rate"])
        self.assertEqual(result.status, "missing_data")
        self.assertTrue(any("未匹配" in item for item in result.limitations))
        # 纯支付指标不受影响
        paid = self._query(metrics=["paid_amount"])
        self.assertEqual(paid.status, "ok")
        self.assertEqual(Decimal(paid.data[0]["paid_amount"]), Decimal("1000"))

    def test_unauthorized_shop_forbidden(self):
        result = self._query(shop_ids=["S1", "S2"], metrics=["paid_amount"])
        self.assertEqual(result.status, "forbidden")

    def test_injection_style_shop_id_rejected(self):
        result = self._query(shop_ids=["S1; DROP TABLE bi.orders; --"],
                             metrics=["paid_amount"])
        self.assertEqual(result.status, "forbidden")
        self.conn.execute("RESET ROLE")
        remaining = self.conn.execute(
            "SELECT count(*) FROM bi.orders").fetchone()[0]
        self.assertGreater(remaining, 0)

    def test_true_zero_vs_missing_day(self):
        """09-04覆盖完整且无支付：真实0；超出覆盖的日期：missing_data。"""
        covered = self._query(start="2026-09-04", end="2026-09-05",
                              metrics=["paid_amount"], group_by="day")
        self.assertEqual(covered.status, "ok")
        self.assertEqual(Decimal(covered.data[0]["paid_amount"]), Decimal("0"))
        beyond = self._query(start="2026-09-09", end="2026-09-10",
                             metrics=["paid_amount"])
        self.assertEqual(beyond.status, "missing_data")
        self.assertEqual(beyond.coverage.status, "missing")

    def test_zero_denominator_not_computable(self):
        result = self._query(start="2026-09-06", end="2026-09-07",
                             metrics=["aov", "cohort_refund_rate"])
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.data[0]["aov"])
        self.assertIsNone(result.data[0]["cohort_refund_rate"])
        self.assertTrue(any("不可计算" in item for item in result.limitations))

    def test_fan_out_guard_amounts_not_inflated(self):
        """多商品行+多笔退款+同日多单：各自聚合，金额不被连接放大。"""
        result = self._query(metrics=["paid_amount", "paid_orders", "refund_amount",
                                      "cohort_refund_rate", "erp_documents"])
        row = result.data[0]
        self.assertEqual(Decimal(row["paid_amount"]), Decimal("1000"))
        self.assertEqual(row["paid_orders"], 6)
        self.assertEqual(Decimal(row["refund_amount"]), Decimal("100"))
        self.assertEqual(row["erp_documents"], 6)


if __name__ == "__main__":
    unittest.main()
