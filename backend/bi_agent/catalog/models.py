"""目录身份层的纯函数：不透明引用、唯一展示名、正文确定性改写。

模型只看 ref，真名只在授权展示层与最终正文里出现。名称永远不编造：档案和快照
都给不出名字时返回 None，由展示层渲染成“名称未取得”。
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from enum import StrEnum
from typing import Any, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict

# 引用格式：kind+ERP主键 的 sha256 前 8 位，稳定可持久，重排/重跑都不变。
REF_RE = re.compile(r"^ent-[0-9a-z]{8}$")
_REF_IN_TEXT_RE = re.compile(r"ent-[0-9a-z]{8}")

NameSource = Literal["archive", "trade_snapshot", "shop_profile", "unresolved"]
_DISPLAY_NAME_MAX = 80
# 展示名内容白名单：ERP/平台长数字主键、标记符号与控制字符一律不进展示名。
_UNSAFE_NAME_RE = re.compile(r"[0-9]{10,}|[<>&\"']|[\x00-\x1f\x7f]")

# 只收录已实测确认的平台码；未收录的原样输出，不猜中文名。
PLATFORM_LABELS = {
    "fxg": "抖音",
    "tb": "淘宝",
    "tm": "天猫",
    "pdd": "拼多多",
    "jd": "京东",
    "kuaishou": "快手",
    "wxsph": "视频号",
    "1688": "1688",
}


class EntityKind(StrEnum):
    SHOP = "shop"
    PRODUCT = "product"
    SKU = "sku"


class EntityRef(BaseModel):
    """一次查询里某个实体的稳定引用；catalog_version 记录解析时的目录版本。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EntityKind
    ref: str
    catalog_version: int


class DisplayEntity(BaseModel):
    """授权展示实体：引用 + 可读名称 + 名称来源。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str
    kind: EntityKind
    display_name: str | None = None
    sku_label: str | None = None
    name_source: NameSource = "unresolved"


def ref_for_key(kind: str, natural_key: str) -> str:
    """由 (kind, ERP主键) 稳定派生引用；不同 kind 的同号主键不会撞车。"""
    digest = hashlib.sha256(f"{kind}:{natural_key}".encode("utf-8")).hexdigest()
    return f"ent-{digest[:8]}"


def is_safe_display_name(value: Any) -> bool:
    """展示名必须是可读短文本：不夹带长数字主键、标记字符或控制字符。"""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or len(text) > _DISPLAY_NAME_MAX:
        return False
    return _UNSAFE_NAME_RE.search(text) is None


def _platform_tag(platform: str) -> str:
    """同名店的第一层区分用平台；未收录的平台码原样输出，不猜中文名。"""
    return PLATFORM_LABELS.get(platform, platform or "未知平台")


def shop_display_labels(shops: Iterable[Mapping[str, Any]]) -> dict[str, str | None]:
    """给整张店铺表算出唯一展示名；只在整表上唯一，改写和阅读才不会歧义。"""
    rows = [dict(row) for row in shops]
    by_id = {str(row.get("shop_id") or ""): row for row in rows}
    by_id.pop("", None)
    names = {shop_id: str(row.get("display_name") or "").strip()
             for shop_id, row in by_id.items()}
    name_counts = Counter(name for name in names.values() if name)

    labels: dict[str, str | None] = {}
    ambiguous: dict[str, tuple[str, str]] = {}
    for shop_id, name in names.items():
        if not name:
            labels[shop_id] = None           # 档案没名字：不编造
        elif name_counts[name] == 1:
            labels[shop_id] = name
        else:
            ambiguous[shop_id] = (name, str(by_id[shop_id].get("platform") or "").strip())

    tagged = {shop_id: f"{name}({_platform_tag(platform)})"
              for shop_id, (name, platform) in ambiguous.items()}
    tagged_counts = Counter(tagged.values())
    for shop_id, label in tagged.items():
        if tagged_counts[label] == 1:
            labels[shop_id] = label
            continue
        name, platform = ambiguous[shop_id]
        labels[shop_id] = f"{name}({_platform_tag(platform)}-{shop_id[-4:]})"

    assert set(labels) == set(by_id)
    resolved = [label for label in labels.values() if label]
    assert len(set(resolved)) == len(resolved), "店铺展示名必须整表唯一"
    return labels


def pick_display_name(archive: object, snapshot: object) -> tuple[str | None, NameSource]:
    """商品名取用优先级只这一处：档案名 > 成交快照 > 未取得。

    两者都拿不出可安全展示的名称时返回 None，由展示层渲染“名称未取得”，绝不编造。
    """
    if is_safe_display_name(archive):
        return str(archive).strip(), "archive"
    if is_safe_display_name(snapshot):
        return str(snapshot).strip(), "trade_snapshot"
    return None, "unresolved"


def render_display_text(text: str, display_by_ref: Mapping[str, str | None]) -> str:
    """把正文里的引用换成展示名；未知引用与无名称引用原样保留。

    这一步在保存消息和发浏览器之前执行：模型只可能见过引用，用户读到的是名字。
    改写不做任何数字计算，也不补造名称。
    """
    if not text or not display_by_ref:
        return text

    def replace(match: re.Match[str]) -> str:
        ref = match.group(0)
        name = display_by_ref.get(ref)
        return name if is_safe_display_name(name) else ref

    return _REF_IN_TEXT_RE.sub(replace, text)
