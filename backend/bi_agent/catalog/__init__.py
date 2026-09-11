"""目录身份层：稳定引用、授权展示名与正文改写。

对外只暴露这些名字；模型侧投影用 ref，展示侧投影与正文用 display_name。
"""

from .models import (
    PLATFORM_LABELS,
    REF_RE,
    DisplayEntity,
    EntityKind,
    EntityRef,
    NameSource,
    is_safe_display_name,
    pick_display_name,
    ref_for_key,
    render_display_text,
    shop_display_labels,
)
from .projection import Catalog, CatalogUnauthorized, build_catalog
from .repository import (
    bump_catalog_version,
    catalog_version,
    ensure_refs,
    lookup_refs,
    resolve_display_entities,
)

__all__ = [
    "PLATFORM_LABELS",
    "REF_RE",
    "Catalog",
    "CatalogUnauthorized",
    "DisplayEntity",
    "EntityKind",
    "EntityRef",
    "NameSource",
    "build_catalog",
    "bump_catalog_version",
    "catalog_version",
    "ensure_refs",
    "is_safe_display_name",
    "lookup_refs",
    "pick_display_name",
    "ref_for_key",
    "render_display_text",
    "resolve_display_entities",
    "shop_display_labels",
]
