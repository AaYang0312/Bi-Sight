"""领域注册表：一个领域能写哪些节点、哪些 Artifact 类型，由这一处决定。

004 的 CHECK 约束只允许 business_query / metric_result。扩展到多领域时必须继续
"白名单可枚举"，不能因为要加领域就把约束拆成任意文本——未知领域、未知节点、
未知 Artifact 类型都要在写库之前就被拒。
"""

from __future__ import annotations

from dataclasses import dataclass

# Artifact 类型白名单（docs/superpowers/specs/2026-09-11-operator-workflows-design.md
# 第 3 节），metric_result 是既有类型，必须继续可用。
ARTIFACT_TYPES = frozenset({
    "metric_result",
    "comparison_table",
    "trend_series",
    "chart_spec",
    "price_audit",
    "inventory_alerts",
})

# 数据集与图表必须成对同版本：chart_spec 只能引用这两类数据集 Artifact。
DATASET_ARTIFACT_TYPES = frozenset({"metric_result", "comparison_table", "trend_series"})


class DomainUnknown(ValueError):
    """未登记领域：拒绝，不给它任何默认能力。"""


@dataclass(frozen=True)
class DomainSpec:
    """一个领域的合法状态节点与可产出的 Artifact 类型集合。"""

    name: str
    nodes: frozenset[str]
    artifact_types: frozenset[str]


# 节点名沿用各自状态机的 PersistenceNode 取值，注册表只列白名单不解释语义，
# 避免同一份节点集合在 SQL 约束、Pydantic 与这里各抄一遍。
_COMMERCE_NODES = frozenset({
    "received", "resolve_parameters", "validate_parameters", "authorize_scope",
    "execute_fixed_query", "classify_result", "persist_artifact", "finalize",
})
_LISTING_NODES = _COMMERCE_NODES | {"assess_readiness", "audit_prices"}
_INVENTORY_NODES = _COMMERCE_NODES | {"assess_readiness", "scan_inventory", "evaluate_rules"}

_REGISTRY: dict[str, DomainSpec] = {
    "business_query": DomainSpec(
        name="business_query",
        nodes=frozenset({
            "received", "resolve_parameters", "validate_parameters", "authorize_scope",
            "execute_fixed_query", "classify_result", "persist_artifact", "finalize",
        }),
        artifact_types=frozenset({"metric_result"}),
    ),
    # 后三个领域在此登记契约，实现按计划 Task 7–10 落地；登记即表示
    # “未实现的节点/类型不允许提前写入”，而不是允许任意 payload。
    "commerce_performance": DomainSpec(
        name="commerce_performance", nodes=_COMMERCE_NODES,
        artifact_types=frozenset({"metric_result", "comparison_table", "trend_series",
                                  "chart_spec"}),
    ),
    "listing_price_audit": DomainSpec(
        name="listing_price_audit", nodes=_LISTING_NODES,
        artifact_types=frozenset({"price_audit", "comparison_table", "chart_spec"}),
    ),
    "inventory_watch": DomainSpec(
        name="inventory_watch", nodes=_INVENTORY_NODES,
        artifact_types=frozenset({"inventory_alerts", "trend_series", "chart_spec"}),
    ),
}


def domains() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def known_domain(value: object) -> bool:
    return isinstance(value, str) and value in _REGISTRY


def spec_for(domain: str) -> DomainSpec:
    try:
        return _REGISTRY[domain]
    except KeyError:
        raise DomainUnknown(domain) from None


def allows_artifact_type(domain: str, artifact_type: str) -> bool:
    """领域能否产出这种 Artifact：未知领域与未知类型都直接否。"""
    entry = _REGISTRY.get(domain)
    return entry is not None and artifact_type in entry.artifact_types
