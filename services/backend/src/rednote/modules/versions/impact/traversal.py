"""影响图遍历（纯函数，不碰数据库）：分层 BFS + 访问集合 + 上限 + 环报告。

边 `E(parent -> dependency)` = 「parent 用到了 dependency」。影响图沿**反向**走：
从版本 X 出发，下一跳 = `{parent | E(parent -> X)}`。`fetch_dependents` 由调用方注入：
一次批量取一层，返回 `Edge` 序列（只含 `dependency` 在给定 frontier 内的边）。

* 终止性由**访问集合**保证（环不产生新 id）；深度 / 节点上限只限制响应规模，两者不可互相替代。
* 超限**不抛错**：返回 `complete=False` + `truncation`，节点列表是确定性前缀（每层按 id 排序）。
* 环：遍历结束后对两端都在访问集合内的边做 Kahn 拓扑排序，有剩余 ⇒ `DEPENDENCY_CYCLE`，不抛错。
  截断时「未发现环」不构成无环结论。
* `purpose` 不参与过滤：所有边一律传播，到达节点的 purpose 集合排序去重后原样透出。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence

from rednote.modules.versions.impact.types import (
    ANOMALY_CYCLE,
    MAX_TRAVERSAL_DEPTH,
    MAX_TRAVERSAL_NODES,
    TRUNCATION_DEPTH_LIMIT,
    TRUNCATION_NODE_LIMIT,
    Edge,
    TraversalNode,
    TraversalResult,
)

FetchDependents = Callable[[Sequence[uuid.UUID]], Sequence[Edge]]


def find_cycle_nodes(edges: Sequence[Edge], nodes: set[uuid.UUID]) -> int:
    """拓扑剥离：反复剥掉入度或出度为 0 的节点，剩下的就是环上（或环间）节点数。

    0 = 无环；菱形的汇聚点两端都能被剥掉，不会误报。只做单向 Kahn 会把「环下游的无辜节点」
    也算进去，所以两个方向都剥。
    """
    pairs = {(e.parent, e.dependency) for e in edges if e.parent in nodes and e.dependency in nodes}
    alive = set(nodes)
    changed = True
    while changed:
        changed = False
        indeg: dict[uuid.UUID, int] = defaultdict(int)
        outdeg: dict[uuid.UUID, int] = defaultdict(int)
        for parent, dep in pairs:
            if parent in alive and dep in alive:
                outdeg[parent] += 1
                indeg[dep] += 1
        for node in list(alive):
            if indeg[node] == 0 or outdeg[node] == 0:
                alive.discard(node)
                changed = True
    return len(alive)


def traverse_dependents(
    fetch_dependents: FetchDependents,
    start: uuid.UUID,
    *,
    max_depth: int = MAX_TRAVERSAL_DEPTH,
    max_nodes: int = MAX_TRAVERSAL_NODES,
) -> TraversalResult:
    depth_of: dict[uuid.UUID, int] = {start: 0}
    purposes: dict[uuid.UUID, set[str]] = defaultdict(set)
    edges: list[Edge] = []
    round_trips = 0
    frontier: list[uuid.UUID] = [start]
    truncation: str | None = None

    def fetch(ids: list[uuid.UUID]) -> Sequence[Edge]:
        nonlocal round_trips
        round_trips += 1
        rows = list(fetch_dependents(ids))
        edges.extend(rows)
        return rows

    finished = False
    for depth in range(1, max_depth + 1):
        rows = fetch(frontier)
        new = sorted({r.parent for r in rows} - depth_of.keys())
        taken = len(depth_of) - 1
        if taken + len(new) > max_nodes:
            for node in new[: max_nodes - taken]:
                depth_of[node] = depth
            truncation = TRUNCATION_NODE_LIMIT
            finished = True
            break
        for node in new:
            depth_of[node] = depth
        if not new:
            finished = True
            break
        frontier = new
    if not finished:
        # 走满 max_depth 仍有 frontier：多探一层，区分「恰好走完」与「被深度截断」。
        rows = fetch(frontier)
        if {r.parent for r in rows} - depth_of.keys():
            truncation = TRUNCATION_DEPTH_LIMIT

    for edge in edges:
        if edge.parent in depth_of and edge.parent != start:
            purposes[edge.parent].add(edge.purpose)

    ordered = sorted((n for n in depth_of if n != start), key=lambda n: (depth_of[n], n))
    cycle_nodes = find_cycle_nodes(edges, set(depth_of))
    return TraversalResult(
        start=start,
        nodes=tuple(
            TraversalNode(n, depth_of[n], tuple(sorted(purposes.get(n, ())))) for n in ordered
        ),
        complete=truncation is None,
        truncation=truncation,
        max_depth_reached=max(depth_of.values()),
        anomalies=(ANOMALY_CYCLE,) if cycle_nodes else (),
        cycle_node_count=cycle_nodes,
        round_trips=round_trips,
    )


__all__ = ["FetchDependents", "find_cycle_nodes", "traverse_dependents"]
