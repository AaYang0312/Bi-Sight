"""测试替身：目录投影（bi_agent.catalog.projection）只读这两条 reporting 视图。

指标查询在各测试文件里都被替换掉，所以这里只需要回答：
- 全表店铺档案 → 引用展示名
- 目录版本 → Artifact 记录它解析名称时用的版本

真实数据库用例（tests.test_catalog / test_api）走真视图，不用本替身。
"""

from __future__ import annotations

from typing import Sequence

from bi_agent.catalog import ref_for_key

# 引用由 (kind, ERP主键) 纯派生；测试用同源常量，不手抄哈希。
S1_REF = ref_for_key("shop", "S1")
S2_REF = ref_for_key("shop", "S2")
P1_REF = ref_for_key("product", "P1")

# 默认档案：店名可读、不含长数字主键，能通过展示名内容白名单。
DEFAULT_SHOPS: tuple[tuple[str, str, str], ...] = (("S1", "fxg", "钉枪工厂店"),)
DEFAULT_VERSION = 7


class Rows:
    """psycopg 结果对象的最小替身。"""

    def __init__(self, rows: Sequence[object]):
        self._rows = list(rows)

    def fetchall(self) -> list[object]:
        return list(self._rows)

    def fetchone(self) -> object:
        return self._rows[0] if self._rows else None


def catalog_rows(sql: str, *, shops: Sequence[object] = DEFAULT_SHOPS,
                 version: int = DEFAULT_VERSION) -> Rows | None:
    """命中目录投影的读取就返回结果，否则返回 None 交给调用方原本的分支。"""
    if "shop_id, platform, display_name" in sql:
        return Rows(shops)
    if "version FROM reporting.v_catalog_version" in sql:
        return Rows([(version,)])
    return None


class CatalogConn:
    """只服务目录投影的连接替身：其他 SQL 一律显式报错，不静默返回空。"""

    def __init__(self, *, shops: Sequence[object] = DEFAULT_SHOPS,
                 version: int = DEFAULT_VERSION):
        self.shops = list(shops)
        self.version = version

    def execute(self, sql: str, params: object = None) -> Rows:
        rows = catalog_rows(sql, shops=self.shops, version=self.version)
        if rows is None:
            raise AssertionError(f"未预期的SQL：{sql}")
        return rows


class ShopCatalogConn(CatalogConn):
    """Agent 侧替身：兼顾 _fetch_shops 的两列读取与目录投影的三列读取。

    两边都只读 reporting.v_shops，列数不同，所以按列名分支，不猜顺序。
    """

    def __init__(self, shops: Sequence[tuple[str, str]] = (("S1", "店铺A"),),
                 *, version: int = DEFAULT_VERSION,
                 platform: str = "fxg"):
        super().__init__(shops=[(shop_id, platform, name)
                                for shop_id, name in shops], version=version)
        self.profiles = [(shop_id, platform, name)
                         for shop_id, name in shops]

    def execute(self, sql: str, params: object = None) -> Rows:
        text = " ".join(sql.split())
        if "shop_id, platform, display_name" in text:
            return Rows(self.profiles)
        if "version FROM reporting.v_catalog_version" in text:
            return Rows([(self.version,)])
        if "FROM reporting.v_shops" in text:
            wanted = list(params[0]) if params else None
            return Rows([(shop_id, name) for shop_id, _, name in self.profiles
                         if wanted is None or shop_id in wanted])
        raise AssertionError(f"未预期的SQL：{text}")
