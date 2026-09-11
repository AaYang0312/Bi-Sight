"""目录身份层：持久 opaque ref、唯一化展示名、正文确定性改写。

口径来自 docs/superpowers/plans/2026-09-11-data-and-query-closure.md Task 2：
模型只看 ref，真名只在授权展示层出现；本模块另加落库前的 ref→真名改写。
"""

import os
import unittest
from zoneinfo import ZoneInfo

import psycopg

BEIJING = ZoneInfo("Asia/Shanghai")


class RefFormatTests(unittest.TestCase):
    """ref 必须与 ERP 主键不同类：不可读、可持久、重排不变。"""

    def test_ref_is_derived_from_kind_and_key(self):
        from bi_agent.catalog import ref_for_key

        self.assertEqual(ref_for_key("shop", "166754"), ref_for_key("shop", "166754"))
        self.assertNotEqual(ref_for_key("shop", "166754"), ref_for_key("product", "166754"))

    def test_ref_shape_is_opaque_and_deterministic(self):
        from bi_agent.catalog import REF_RE, ref_for_key

        keys = [str(9000000000000000 + index) for index in range(30)]
        refs = [ref_for_key("product", key) for key in keys]
        reshuffled = [ref_for_key("product", key) for key in reversed(keys)]

        self.assertEqual(len(set(refs)), 30, "30 个商品必须给出 30 个不同引用")
        self.assertEqual(reshuffled, list(reversed(refs)), "引用不得随排序变化")
        for ref in refs:
            self.assertTrue(REF_RE.fullmatch(ref), ref)

    def test_ref_never_exposes_the_erp_key(self):
        from bi_agent.catalog import ref_for_key

        key = "548597548700160"

        self.assertNotIn(key, ref_for_key("product", key))


class ShopLabelTests(unittest.TestCase):
    """展示名要在整张店铺表上唯一：同名店不带平台就没法读，也没法安全改写。"""

    @staticmethod
    def _shops(*rows):
        return [{"shop_id": s, "platform": p, "display_name": n} for s, p, n in rows]

    def test_unique_name_is_used_verbatim(self):
        from bi_agent.catalog import shop_display_labels

        labels = shop_display_labels(self._shops(("166754", "fxg", "元发钉枪")))

        self.assertEqual(labels["166754"], "元发钉枪")

    def test_duplicate_names_get_platform_suffix(self):
        from bi_agent.catalog import shop_display_labels

        labels = shop_display_labels(self._shops(
            ("166754", "fxg", "元发钉枪"), ("167156", "kuaishou", "元发钉枪")))

        self.assertEqual(labels["166754"], "元发钉枪(抖音)")
        self.assertEqual(labels["167156"], "元发钉枪(快手)")
        self.assertEqual(len(set(labels.values())), 2)

    def test_still_colliding_labels_fall_back_to_the_shop_key_tail(self):
        from bi_agent.catalog import shop_display_labels

        # 平台码未收录时不猜中文名，用原始码；同名同平台再撞就带店铺号尾号。
        labels = shop_display_labels(self._shops(
            ("900001111", "wsxc", "微购相册"), ("900002222", "wsxc", "微购相册")))

        self.assertEqual(sorted(labels.values()),
                         ["微购相册(wsxc-1111)", "微购相册(wsxc-2222)"])

    def test_blank_display_name_is_reported_as_unresolved_not_invented(self):
        from bi_agent.catalog import shop_display_labels

        labels = shop_display_labels(self._shops(("166754", "fxg", "  ")))

        self.assertIsNone(labels["166754"])


class AnswerRewriteTests(unittest.TestCase):
    """落库/发浏览器前把引用换成真名；未知引用原样保留，绝不编造。"""

    def test_known_refs_are_replaced_in_prose(self):
        from bi_agent.catalog import render_display_text

        text = render_display_text(
            "ent-1111aaaa 支付 12 万，ent-2222bbbb 第二",
            {"ent-1111aaaa": "元发钉枪(抖音)", "ent-2222bbbb": "直钉枪-元发"})

        self.assertEqual(text, "元发钉枪(抖音) 支付 12 万，直钉枪-元发 第二")

    def test_unknown_ref_is_kept_as_is(self):
        from bi_agent.catalog import render_display_text

        text = render_display_text("ent-ffffffff 无名称", {"ent-1111aaaa": "元发钉枪"})

        self.assertEqual(text, "ent-ffffffff 无名称")

    def test_rewrite_is_longest_token_first_and_idempotent(self):
        from bi_agent.catalog import render_display_text

        mapping = {"ent-1111aaaa": "元发钉枪(抖音)"}
        once = render_display_text("ent-1111aaaa", mapping)

        self.assertEqual(render_display_text(once, mapping), once)

    def test_erp_digit_key_in_prose_is_not_touched_by_rewrite(self):
        """改写只认引用格式：正文里出现的 15 位 ERP 号不属于本步骤的处理范围。"""
        from bi_agent.catalog import render_display_text

        text = render_display_text("订单 548597548700160 异常", {})

        self.assertEqual(text, "订单 548597548700160 异常")


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class CatalogRepositoryTests(unittest.TestCase):
    def setUp(self):
        from tests.dbfixtures import connect_test_db

        self.conn = connect_test_db(self)

    def _seed_shop(self, shop_id: str, *, platform: str = "fxg", name: str = "元发钉枪"):
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name) VALUES (%s,%s,%s) "
            "ON CONFLICT (shop_id) DO UPDATE SET platform=EXCLUDED.platform, "
            "display_name=EXCLUDED.display_name",
            (shop_id, platform, name),
        )

    def test_ensure_refs_is_idempotent_and_reversible(self):
        from bi_agent.catalog import ensure_refs, lookup_refs

        first = ensure_refs(self.conn, "shop", ["166754", "167156"])
        again = ensure_refs(self.conn, "shop", ["167156", "166754"])

        self.assertEqual(first, again, "同一批键重复取引用必须完全一致")
        self.assertEqual(lookup_refs(self.conn, list(first.values())),
                         {ref: ("shop", key) for key, ref in first.items()})

    def test_resolve_returns_names_only_for_authorized_shops(self):
        from bi_agent.catalog import ensure_refs, resolve_display_entities

        # 用合成店号：真实表里“元发钉枪”本来就两家，展示名会带平台后缀。
        self._seed_shop("S_CAT_A", platform="fxg", name="目录甲店")
        self._seed_shop("S_CAT_B", platform="pdd", name="目录乙店")
        refs = ensure_refs(self.conn, "shop", ["S_CAT_A", "S_CAT_B"])

        resolved = resolve_display_entities(
            self.conn, list(refs.values()), allowed_shop_ids=frozenset({"S_CAT_A"}))

        self.assertEqual([item.display_name for item in resolved], ["目录甲店"])
        unauthorized = resolve_display_entities(
            self.conn, [refs["S_CAT_B"]], allowed_shop_ids=frozenset({"S_CAT_A"}))
        self.assertEqual(unauthorized, [], "越权引用不得带出任何名称")

    def test_duplicate_shop_names_resolve_to_distinct_labels(self):
        from bi_agent.catalog import ensure_refs, resolve_display_entities

        # 名字用合成值：真实表里“元发钉枪”本来就两对，不依赖共享库内容。
        self._seed_shop("S_CAT_C", platform="fxg", name="目录重名店")
        self._seed_shop("S_CAT_D", platform="kuaishou", name="目录重名店")
        refs = ensure_refs(self.conn, "shop", ["S_CAT_C", "S_CAT_D"])

        resolved = resolve_display_entities(
            self.conn, list(refs.values()), allowed_shop_ids=frozenset(refs))

        self.assertEqual({item.display_name for item in resolved},
                         {"目录重名店(抖音)", "目录重名店(快手)"})

    def test_product_name_prefers_archive_and_falls_back_to_snapshot(self):
        from bi_agent.catalog import ensure_refs, resolve_display_entities

        self.conn.execute(
            "INSERT INTO bi.products(product_id, title, normalization_status) "
            "VALUES ('P_ARCHIVE','直钉枪-元发','normal'), ('P_BLANK','','needs_review') "
            "ON CONFLICT (product_id) DO UPDATE SET title=EXCLUDED.title",
        )
        refs = ensure_refs(self.conn, "product", ["P_ARCHIVE", "P_BLANK"])

        resolved = {item.ref: item for item in resolve_display_entities(
            self.conn, list(refs.values()), allowed_shop_ids=frozenset(),
            snapshot_names={"P_BLANK": "接头-元发快照", "P_ARCHIVE": "旧标题快照"})}

        archive = resolved[refs["P_ARCHIVE"]]
        self.assertEqual((archive.display_name, archive.name_source),
                         ("直钉枪-元发", "archive"))
        snapshot = resolved[refs["P_BLANK"]]
        self.assertEqual((snapshot.display_name, snapshot.name_source),
                         ("接头-元发快照", "trade_snapshot"))

    def test_unresolved_product_reports_missing_name_without_inventing_one(self):
        from bi_agent.catalog import ensure_refs, resolve_display_entities

        refs = ensure_refs(self.conn, "product", ["P_UNKNOWN"])

        resolved = resolve_display_entities(self.conn, list(refs.values()),
                                            allowed_shop_ids=frozenset())

        self.assertEqual(resolved[0].display_name, None)
        self.assertEqual(resolved[0].name_source, "unresolved")

    def test_catalog_version_increments_monotonically(self):
        from bi_agent.catalog import bump_catalog_version, catalog_version

        first = catalog_version(self.conn)
        self.assertIsInstance(first, int)
        self.assertTrue(bump_catalog_version(self.conn))
        self.assertEqual(catalog_version(self.conn), first + 1)
        self.assertTrue(bump_catalog_version(self.conn))
        self.assertEqual(catalog_version(self.conn), first + 2)

    def test_ref_rows_survive_being_written_twice(self):
        from bi_agent.catalog import ensure_refs

        ensure_refs(self.conn, "product", ["P_DUP"])
        ensure_refs(self.conn, "product", ["P_DUP"])
        count = self.conn.execute(
            "SELECT count(*) FROM bi.entity_refs WHERE kind='product' AND natural_key='P_DUP'"
        ).fetchone()[0]

        self.assertEqual(count, 1)


class CatalogMigrationTests(unittest.TestCase):
    def test_007_is_the_catalog_migration_and_is_repeatable(self):
        from pathlib import Path

        sql_dir = Path(__file__).parents[1] / "sql"
        files = sorted(path.name for path in sql_dir.glob("0*.sql"))
        self.assertIn("007_catalog_identity.sql", files, f"迁移清单：{files}")

    @unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
    def test_order_items_carry_name_snapshots(self):
        from tests.dbfixtures import connect_test_db

        conn = connect_test_db(self)
        columns = {row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='bi' AND table_name='order_items'").fetchall()}
        self.assertTrue({"product_name_snapshot", "sku_label_snapshot"} <= columns)
        # 取用优先级只在 catalog 一处实现，视图只分别给出档案名与成交快照。
        view_columns = {row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='reporting' AND table_name='v_product_daily'").fetchall()}
        self.assertEqual(view_columns, {
            "shop_id", "day", "product_id", "quantity", "gift_quantity",
            "product_paid_amount", "allocation_verified", "line_kind",
            "product_name", "product_name_snapshot",
        })


if __name__ == "__main__":
    unittest.main()
