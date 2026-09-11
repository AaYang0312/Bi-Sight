"""API 侧目录投影：只用 reporting 视图与纯函数得到引用与展示名。

聊天 API 以 bi_app 身份运行，只能读 reporting 视图；所以这里不碰 bi.entity_refs，
引用一律用 ref_for_key 纯派生（与同步写入表里的值同源），展示名来自查询结果本身
带的档案名 / 成交快照与 reporting.v_shops。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .models import (
    DisplayEntity,
    EntityKind,
    is_safe_display_name,
    pick_display_name,
    pick_sku_label,
    ref_for_key,
    shop_display_labels,
)


class CatalogUnauthorized(ValueError):
    """结果行里出现未授权店铺：宁可不返回数字，也不能把未知主体的数据发出去。"""


@dataclass(frozen=True)
class Catalog:
    """一次查询结果的引用与展示名快照；投影层只读它，不再访问数据库。"""

    shop_refs: Mapping[str, str]
    product_refs: Mapping[str, str]
    entities: tuple[DisplayEntity, ...]
    catalog_version: int

    def shop_ref(self, shop_id: str) -> str:
        try:
            return self.shop_refs[shop_id]
        except KeyError:
            raise CatalogUnauthorized("shop_not_authorized") from None

    def product_ref(self, product_id: str) -> str:
        try:
            return self.product_refs[product_id]
        except KeyError:
            raise CatalogUnauthorized("product_not_resolved") from None

    @property
    def display_by_ref(self) -> Mapping[str, str | None]:
        return MappingProxyType({entity.ref: entity.display_name for entity in self.entities})

    def entities_payload(self) -> list[dict[str, Any]]:
        return [entity.model_dump(mode="json") for entity in self.entities]


def _shop_rows(conn) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT shop_id, platform, display_name FROM reporting.v_shops").fetchall()
    return [{"shop_id": str(row[0]), "platform": str(row[1] or ""),
             "display_name": str(row[2] or "")} for row in rows]


def _catalog_version(conn) -> int:
    row = conn.execute("SELECT version FROM reporting.v_catalog_version").fetchone()
    return int(row[0]) if row is not None else 0


def _row_values(data: Iterable[Mapping[str, Any]], key: str) -> list[str]:
    values: list[str] = []
    for row in data:
        value = row.get(key)
        if isinstance(value, str) and value.strip() and value not in values:
            values.append(value)
    return values


def build_catalog(conn, result, *, allowed_shop_ids: frozenset[str]) -> Catalog:
    """为一次工具结果建目录：校验授权、派生引用、解析展示名。"""
    data = [row for row in getattr(result, "data", []) if isinstance(row, Mapping)]
    shop_ids = _row_values(data, "shop_id")
    filter_shop_ids = [str(value)
                       for value in (getattr(result, "filters", {}) or {}).get("shop_ids", [])]
    offending = [shop_id for shop_id in [*shop_ids, *filter_shop_ids]
                 if shop_id not in allowed_shop_ids]
    if offending:
        raise CatalogUnauthorized("shop_not_authorized")

    labels = shop_display_labels(_shop_rows(conn))
    entities: list[DisplayEntity] = []
    shop_refs: dict[str, str] = {}
    for shop_id in sorted({*shop_ids, *filter_shop_ids}):
        ref = ref_for_key(EntityKind.SHOP.value, shop_id)
        shop_refs[shop_id] = ref
        label = labels.get(shop_id)
        entities.append(DisplayEntity(
            ref=ref, kind=EntityKind.SHOP,
            display_name=label if is_safe_display_name(label) else None,
            name_source="shop_profile" if is_safe_display_name(label) else "unresolved"))

    product_refs: dict[str, str] = {}
    for product_id in _row_values(data, "product_id"):
        ref = ref_for_key(EntityKind.PRODUCT.value, product_id)
        product_refs[product_id] = ref
        rows = [row for row in data if row.get("product_id") == product_id]
        archive = next((row.get("product_name") for row in rows
                        if is_safe_display_name(row.get("product_name"))), None)
        snapshot = next((row.get("product_name_snapshot") for row in rows
                         if is_safe_display_name(row.get("product_name_snapshot"))), None)
        name, source = pick_display_name(archive, snapshot)
        # 规格走另一条规则：多行不一致或任一缺规格就置空，不沿用名称的“先拿到的算”。
        entities.append(DisplayEntity(
            ref=ref, kind=EntityKind.PRODUCT, display_name=name, name_source=source,
            sku_label=pick_sku_label([row.get("sku_label") for row in rows])))

    return Catalog(
        shop_refs=MappingProxyType(shop_refs),
        product_refs=MappingProxyType(product_refs),
        entities=tuple(entities),
        catalog_version=_catalog_version(conn),
    )
