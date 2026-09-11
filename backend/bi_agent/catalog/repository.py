"""目录身份层的数据库操作：引用落表、目录版本、按授权解析展示名。

引用不是授权凭证：解析展示名时必须再按 allowed_shop_ids 复核一次，
越权引用一个名称都不返回。
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from .models import (
    DisplayEntity,
    EntityKind,
    is_safe_display_name,
    pick_display_name,
    ref_for_key,
    shop_display_labels,
)

_KINDS = {kind.value for kind in EntityKind}


def ensure_refs(conn, kind: str, natural_keys: Iterable[str]) -> dict[str, str]:
    """幂等取/建 (kind, ERP主键) → 引用；返回 主键→引用。"""
    if kind not in _KINDS:
        raise ValueError("unknown_entity_kind")
    keys = sorted({str(key) for key in natural_keys if str(key or "").strip()})
    if not keys:
        return {}
    placeholders = ", ".join(["(%s, %s, %s)"] * len(keys))
    params: list[str] = []
    for key in keys:
        params.extend([kind, key, ref_for_key(kind, key)])
    conn.execute(
        f"INSERT INTO bi.entity_refs(kind, natural_key, ref) VALUES {placeholders} "
        "ON CONFLICT (kind, natural_key) DO NOTHING",
        tuple(params),
    )
    stored = conn.execute(
        "SELECT natural_key, ref FROM bi.entity_refs WHERE kind=%s AND natural_key = ANY(%s)",
        (kind, keys),
    ).fetchall()
    resolved = {str(row[0]): str(row[1]) for row in stored}
    expected = {key: ref_for_key(kind, key) for key in keys}
    if resolved != expected:
        # 哈希截断撞车或表被外部改写：宁可拒绝出数，也不能两个实体共用一个引用。
        raise ValueError("ref_conflict")
    return resolved


def lookup_refs(conn, refs: Sequence[str]) -> dict[str, tuple[str, str]]:
    """引用反查 (kind, ERP主键)；不存在的引用直接缺失，不回退猜测。"""
    wanted = [str(ref) for ref in refs if str(ref or "").strip()]
    if not wanted:
        return {}
    rows = conn.execute(
        "SELECT ref, kind, natural_key FROM bi.entity_refs WHERE ref = ANY(%s)",
        (wanted,),
    ).fetchall()
    return {str(row[0]): (str(row[1]), str(row[2])) for row in rows}


def catalog_version(conn) -> int:
    row = conn.execute("SELECT version FROM bi.catalog_state WHERE id = 1").fetchone()
    return int(row[0]) if row is not None else 0


def bump_catalog_version(conn) -> bool:
    """名称目录发生变化时递增；行缺失时按幂等 upsert 重建。"""
    row = conn.execute(
        "INSERT INTO bi.catalog_state(id, version, updated_at) VALUES (1, 1, now()) "
        "ON CONFLICT (id) DO UPDATE SET version = bi.catalog_state.version + 1, "
        "updated_at = now() RETURNING version").fetchone()
    return row is not None


def _shop_labels(conn) -> dict[str, str | None]:
    rows = conn.execute(
        "SELECT shop_id, platform, display_name FROM bi.shops").fetchall()
    return shop_display_labels([
        {"shop_id": row[0], "platform": row[1], "display_name": row[2]} for row in rows])


def _archive_titles(conn, product_ids: Sequence[str]) -> dict[str, str]:
    if not product_ids:
        return {}
    rows = conn.execute(
        "SELECT product_id, title FROM bi.products WHERE product_id = ANY(%s)",
        (list(product_ids),),
    ).fetchall()
    return {str(row[0]): str(row[1] or "").strip() for row in rows}


def resolve_display_entities(
    conn,
    refs: Sequence[str],
    *,
    allowed_shop_ids: frozenset[str],
    snapshot_names: Mapping[str, str | None] | None = None,
    sku_labels: Mapping[str, str | None] | None = None,
) -> list[DisplayEntity]:
    """按授权把引用换成展示名；顺序与入参一致，越权与未知引用直接不返回。"""
    if not refs:
        return []
    targets = lookup_refs(conn, refs)
    shop_keys = sorted({key for kind, key in targets.values() if kind == EntityKind.SHOP})
    product_keys = sorted({key for kind, key in targets.values()
                           if kind in {EntityKind.PRODUCT, EntityKind.SKU}})
    labels = _shop_labels(conn) if shop_keys else {}
    titles = _archive_titles(conn, product_keys) if product_keys else {}

    entities: list[DisplayEntity] = []
    for ref in refs:
        target = targets.get(ref)
        if target is None:
            continue
        kind, natural_key = target
        if kind == EntityKind.SHOP:
            if natural_key not in allowed_shop_ids:
                continue
            label = labels.get(natural_key)
            entities.append(DisplayEntity(
                ref=ref, kind=EntityKind.SHOP,
                display_name=label if is_safe_display_name(label) else None,
                name_source="shop_profile" if is_safe_display_name(label) else "unresolved"))
            continue
        archive = titles.get(natural_key)
        name, source = pick_display_name(archive, (snapshot_names or {}).get(natural_key))
        entities.append(DisplayEntity(
            ref=ref, kind=EntityKind(kind), display_name=name,
            sku_label=_label(sku_labels, natural_key), name_source=source))
    return entities


def _label(labels: Mapping[str, str | None] | None, key: str) -> str | None:
    value = (labels or {}).get(key)
    return str(value).strip() if is_safe_display_name(value) else None
