# -*- coding: utf-8 -*-
"""港区水深基准改正核算引擎（纯标准库）。

建模
----
每张联测单 ``s = (编号, 零点A, 零点B, 下界lo, 上界hi)``（单位：毫米）表示
``lo <= h_B - h_A <= hi``，其中 h_X 是零点 X 相对港区基准 D0 的改正量，
港区基准固定 h_D0 = 0。每条联测单生成两条有向差约束弧：

* A -> B，权上界 hi      （h_B <= h_A + hi）
* B -> A，权上界 -lo     （h_A <= h_B - lo）

可行域为差分约束系统：有负环即高差互相矛盾，负环即为冲突闭环。
从 D0 出发的最短路给出上界 h_v <= d(D0, v)；反向图给出下界
h_v >= -d_rev(D0, v)（即 -d(v, D0)）。

"撤掉任一张单后所有可能的改正量" 的最窄覆盖区间（稳健区间）：
rob_lo_v = min_{删除单据 e} 可行下界，rob_hi_v = max_{删除单据 e} 可行上界，
删除后与 D0 断开则该方向无界。只对真正参与某条最短路的单据重新求解，
桥（删除即断开基准的单据）直接由 Tarjan 一次扫描判定，避免逐单重算全港。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

INF = math.inf


class ValidationError(ValueError):
    """联测单输入不合法。"""


@dataclass(frozen=True)
class Sheet:
    sid: str
    point_a: str
    point_b: str
    lo: int          # 毫米，h_b - h_a 的闭区间下界
    hi: int          # 毫米，闭区间上界

    def validate(self) -> None:
        if not isinstance(self.sid, str) or not self.sid.strip():
            raise ValidationError("联测单编号不能为空")
        if not isinstance(self.point_a, str) or not self.point_a.strip():
            raise ValidationError("联测单 %s 的零点 A 不能为空" % self.sid)
        if not isinstance(self.point_b, str) or not self.point_b.strip():
            raise ValidationError("联测单 %s 的零点 B 不能为空" % self.sid)
        if self.point_a.strip() == self.point_b.strip():
            raise ValidationError(
                "联测单 %s 的两个零点不能相同：%s" % (self.sid, self.point_a)
            )
        if isinstance(self.lo, bool) or isinstance(self.hi, bool):
            raise ValidationError("联测单 %s 的高差必须为整数毫米" % self.sid)
        if not isinstance(self.lo, int) or not isinstance(self.hi, int):
            raise ValidationError("联测单 %s 的高差必须为整数毫米" % self.sid)
        if self.lo > self.hi:
            raise ValidationError(
                "联测单 %s 的高差区间下界 %d 大于上界 %d"
                % (self.sid, self.lo, self.hi)
            )


@dataclass
class Contradiction:
    """一个冲突闭环（差约束负环）。"""

    cycle_sheet_ids: List[str]
    cycle_points: List[str]
    cycle_bound: int  # 闭环推出的严格上界，负数即自相矛盾


@dataclass
class NodeResult:
    point: str
    reachable: bool                       # 现行全图是否连到基准
    # 现行（全单）可行闭区间
    cur_lo: Optional[int] = None
    cur_hi: Optional[int] = None
    # 撤单稳健区间：覆盖撤掉任一张单后所有可能改正量的最窄区间
    rob_lo: Optional[int] = None
    rob_hi: Optional[int] = None
    unbounded_on_removal: List[str] = field(default_factory=list)
    # 分别撤哪张单取到最小下界 / 最大上界
    min_lo_sheet: Optional[str] = None
    max_hi_sheet: Optional[str] = None
    # 判定
    status: str = "PENDING"               # PENDING / OK / FAIL
    reason: str = ""


@dataclass
class ComputeReport:
    datum: str
    nodes: List[NodeResult]
    contradictions: List[Contradiction]
    sheet_count: int
    point_count: int

    @property
    def ok(self) -> bool:
        return not self.contradictions


# --------------------------------------------------------------------------- #
# 图结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Arc:
    u: str
    v: str
    w: int
    sheet_id: str
    direction: str   # "ab" 表示 A->B（用上界），"ba" 表示 B->A（用负下界）


def build_graph(sheets: Sequence[Sheet], datum: str):
    """返回 (点集, 有向弧表, 邻接表, 无向边表)。"""
    points = {datum}
    arcs: List[Arc] = []
    adj: Dict[str, List[int]] = {}
    undirected: List[Tuple[str, str, str]] = []

    for s in sheets:
        a, b = s.point_a.strip(), s.point_b.strip()
        points.add(a)
        points.add(b)
        i1 = len(arcs)
        arcs.append(Arc(a, b, s.hi, s.sid, "ab"))
        i2 = len(arcs)
        arcs.append(Arc(b, a, -s.lo, s.sid, "ba"))
        adj.setdefault(a, []).append(i1)
        adj.setdefault(b, []).append(i2)
        undirected.append((a, b, s.sid))

    for p in points:
        adj.setdefault(p, [])
    return sorted(points), arcs, adj, undirected


# --------------------------------------------------------------------------- #
# 最短路（SPFA）与负环提取
# --------------------------------------------------------------------------- #
def spfa(
    points: Sequence[str],
    arcs: Sequence[Arc],
    adj: Dict[str, List[int]],
    source: str,
    disabled_sheet: Optional[str] = None,
):
    """单源 SPFA 最短路。返回 (距离表, bad_arc)。

    正常情况 bad_arc 为 None；存在可达负环时距离表为 None。
    调用前矛盾已由超源 Bellman-Ford 排除，bad_arc 仅作防御性返回。
    """
    dist = {p: INF for p in points}
    in_queue = {p: False for p in points}
    relax_count = {p: 0 for p in points}
    queue: List[str] = [source]
    dist[source] = 0
    in_queue[source] = True

    head = 0
    n = len(points)
    while head < len(queue):
        u = queue[head]
        head += 1
        in_queue[u] = False
        du = dist[u]
        for ai in adj[u]:
            arc = arcs[ai]
            if disabled_sheet is not None and arc.sheet_id == disabled_sheet:
                continue
            nd = du + arc.w
            if nd < dist[arc.v]:
                dist[arc.v] = nd
                if not in_queue[arc.v]:
                    relax_count[arc.v] += 1
                    if relax_count[arc.v] >= n:
                        return None, ai
                    queue.append(arc.v)
                    in_queue[arc.v] = True
    return dist, None


def extract_negative_cycle(
    arcs: Sequence[Arc], pred_arc: Dict[str, int], start_arc_idx: int
) -> Tuple[List[str], List[str], int]:
    """从触发第 n 次松弛的弧出发，沿前驱还原一个负环。"""
    start_arc = arcs[start_arc_idx]
    seen: Dict[str, int] = {}
    path_points: List[str] = []
    path_arcs: List[int] = []
    cur = start_arc.v
    while cur not in seen:
        seen[cur] = len(path_points)
        ai = pred_arc[cur]
        path_arcs.append(ai)
        path_points.append(cur)
        cur = arcs[ai].u
    cut = seen[cur]
    cycle_points = [cur] + list(reversed(path_points[cut:]))
    cycle_arc_ids = list(reversed(path_arcs[cut:]))
    bound = sum(arcs[ai].w for ai in cycle_arc_ids)
    return [arcs[ai].sheet_id for ai in cycle_arc_ids], cycle_points, bound


def _bellman_ford_super_source(
    points: Sequence[str],
    arcs: Sequence[Arc],
    adj: Dict[str, List[int]],
    disabled: Iterable[str] = (),
):
    """超源 Bellman-Ford：返回 (dist, pred_arc, bad_arc)。

    无负环时 bad_arc 为 None；有负环时 bad_arc 是第 n 轮仍可松弛的弧，
    可沿 pred_arc 还原闭环。图小（港内零点/单据量级），BF 实现简单且
    对负环判定严格可靠。
    """
    blocked = set(disabled)
    dist = {p: 0 for p in points}
    pred_arc: Dict[str, int] = {}
    n = len(points)
    bad_arc: Optional[int] = None
    for _ in range(n):
        bad_arc = None
        updated = False
        for u in points:
            du = dist[u]
            for ai in adj[u]:
                arc = arcs[ai]
                if arc.sheet_id in blocked:
                    continue
                nd = du + arc.w
                if nd < dist[arc.v]:
                    dist[arc.v] = nd
                    pred_arc[arc.v] = ai
                    bad_arc = ai
                    updated = True
        if not updated:
            return dist, pred_arc, None
    return dist, pred_arc, bad_arc


def find_all_contradictions(
    points: Sequence[str], arcs: Sequence[Arc], adj: Dict[str, List[int]]
) -> List[Contradiction]:
    """反复找负环并禁用环上一张单，直到系统可行，列出全部冲突闭环。

    禁用只作用于本函数的枚举过程；每个报告的闭环都确实是当前系统中的
    负环，禁用一张环上单保证过程至多 |单据| 步终止。
    """
    out: List[Contradiction] = []
    disabled: set = set()
    for _ in range(len(arcs) + 1):
        _, pred, bad_arc_idx = _bellman_ford_super_source(points, arcs, adj, disabled)
        if bad_arc_idx is None:
            break
        ids, cyc_pts, bound = extract_negative_cycle(arcs, pred, bad_arc_idx)
        if not ids:
            break
        out.append(
            Contradiction(
                cycle_sheet_ids=ids,
                cycle_points=cyc_pts,
                cycle_bound=bound,
            )
        )
        disabled.add(ids[0])
    return out


# --------------------------------------------------------------------------- #
# 无向桥（删除即断开基准的单据），平行复测单各自计边、互不成为桥
# --------------------------------------------------------------------------- #
def find_bridge_sheets(
    points: Sequence[str], undirected: Sequence[Tuple[str, str, str]], datum: str
) -> Dict[str, List[str]]:
    """Tarjan 求无向多重图桥，并给出每个点连基准路径上的桥单据列表。"""
    neigh: Dict[str, List[Tuple[str, str]]] = {p: [] for p in points}
    for a, b, sid in undirected:
        neigh[a].append((b, sid))
        neigh[b].append((a, sid))

    tin: Dict[str, int] = {}
    low: Dict[str, int] = {}
    bridges: set = set()
    timer = 0
    import sys
    sys.setrecursionlimit(max(10000, len(points) * 4 + 1000))

    def dfs(u: str, parent_edge: Optional[str]) -> None:
        nonlocal timer
        timer += 1
        tin[u] = low[u] = timer
        for v, sid in neigh[u]:
            if sid == parent_edge:
                continue
            if v in tin:
                low[u] = min(low[u], tin[v])
            else:
                dfs(v, sid)
                low[u] = min(low[u], low[v])
                if low[v] > tin[u]:
                    bridges.add(sid)

    dfs(datum, None)

    # 沿无向生成树（按边 ID 区分平行复测单）记录每个点到基准路径上的桥
    bridges_to_point: Dict[str, List[str]] = {datum: []}
    visited = {datum}
    stack: List[Tuple[str, Optional[str]]] = [(datum, None)]  # (点, 进入该点的边)
    while stack:
        u, entry_edge = stack.pop()
        for v, sid in neigh[u]:
            if sid == entry_edge or v in visited:
                continue
            visited.add(v)
            chain = list(bridges_to_point[u])
            if sid in bridges:
                chain.append(sid)
            bridges_to_point[v] = chain
            stack.append((v, sid))
    for p in points:
        bridges_to_point.setdefault(p, [])
    return bridges_to_point


# --------------------------------------------------------------------------- #
# 主核算
# --------------------------------------------------------------------------- #
def _bounds_from_dist(d_fwd, d_rev, point: str):
    """由正向/反向最短路得到 (下界, 上界)，不可达返回 None。"""
    if d_fwd is None or point not in d_fwd or d_fwd[point] == INF:
        return None, None
    hi = d_fwd[point]
    if d_rev is None or point not in d_rev or d_rev[point] == INF:
        return None, None
    lo = -d_rev[point]
    return lo, hi


def compute(sheets: Sequence[Sheet], datum: str = "D0", span_limit: int = 20) -> ComputeReport:
    sheets = list(sheets)
    seen_ids = set()
    for s in sheets:
        s.validate()
        if s.sid in seen_ids:
            raise ValidationError("联测单编号重复：%s" % s.sid)
        seen_ids.add(s.sid)

    points, arcs, adj, undirected = build_graph(sheets, datum)

    # 1) 矛盾检测：超源 Bellman-Ford + 逐环枚举
    contradictions = find_all_contradictions(points, arcs, adj)
    if contradictions:
        return ComputeReport(
            datum=datum,
            nodes=[NodeResult(point=p, reachable=False, status="PENDING",
                              reason="存在冲突闭环，暂不核算") for p in points],
            contradictions=contradictions,
            sheet_count=len(sheets),
            point_count=len(points),
        )

    if not sheets:
        nodes = [NodeResult(point=datum, reachable=True, cur_lo=0, cur_hi=0,
                           rob_lo=0, rob_hi=0, status="OK")]
        nodes += [NodeResult(point=p, reachable=False, status="PENDING",
                             reason="尚未与港区基准联测") for p in points if p != datum]
        return ComputeReport(datum, sorted(nodes, key=lambda r: r.point), [], 0, len(points))

    # 2) 现行全图：正向最短路（上界）与反向图最短路（下界）
    rev_arcs = [Arc(arc.v, arc.u, arc.w, arc.sheet_id, arc.direction) for arc in arcs]
    rev_adj: Dict[str, List[int]] = {p: [] for p in points}
    for ai, arc in enumerate(rev_arcs):
        rev_adj[arc.u].append(ai)

    d_fwd, _ = spfa(points, arcs, adj, datum)
    d_rev, _ = spfa(points, rev_arcs, rev_adj, datum)

    # 3) 桥：撤掉即断开基准的单据
    bridges_to_point = find_bridge_sheets(points, undirected, datum)

    # 4) 找出真正参与最短路（正向或反向）的单据，只对这些单据重算。
    #    一条弧 (u,v,w) 是紧弧当且仅当 d[u] + w == d[v]。
    relevant: set = set()
    for arc in arcs:
        if d_fwd[arc.u] != INF and d_fwd[arc.v] != INF and d_fwd[arc.u] + arc.w == d_fwd[arc.v]:
            relevant.add(arc.sheet_id)
    for arc in rev_arcs:
        if d_rev[arc.u] != INF and d_rev[arc.v] != INF and d_rev[arc.u] + arc.w == d_rev[arc.v]:
            relevant.add(arc.sheet_id)

    results: Dict[str, NodeResult] = {}
    for p in points:
        lo0, hi0 = _bounds_from_dist(d_fwd, d_rev, p)
        reachable = lo0 is not None
        results[p] = NodeResult(point=p, reachable=reachable,
                                cur_lo=lo0, cur_hi=hi0)

    # 基准点恒为 0
    results[datum].reachable = True
    results[datum].cur_lo = results[datum].cur_hi = 0
    results[datum].rob_lo = results[datum].rob_hi = 0
    results[datum].status = "OK"

    # 稳健区间初值 = 现行区间（无单可撤的极限情形也被覆盖）
    for p, r in results.items():
        if r.reachable:
            r.rob_lo, r.rob_hi = r.cur_lo, r.cur_hi

    # 5) 桥单据：撤掉后路径上含该桥的点与基准断开 -> 无界
    for p, r in results.items():
        if not r.reachable or p == datum:
            continue
        r.unbounded_on_removal = sorted(bridges_to_point.get(p, []))

    # 6) 非桥且相关的单据：撤单重算（每次只跑两次单源 SPFA，非全量组合）
    non_bridge_relevant = sorted(sid for sid in relevant
                                 if not any(sid in chain for chain in bridges_to_point.values()))
    # 注：桥单撤掉后连通分量改变，其影响已在 unbounded_on_removal 记录；
    # 对仍连通的点桥单不在其路径上，距离必然不变，故跳过。
    for sid in non_bridge_relevant:
        df, _ = spfa(points, arcs, adj, datum, disabled_sheet=sid)
        dr, _ = spfa(points, rev_arcs, rev_adj, datum, disabled_sheet=sid)
        for p, r in results.items():
            if not r.reachable or p == datum:
                continue
            lo_e, hi_e = _bounds_from_dist(df, dr, p)
            if lo_e is None:
                # 理论上非桥不会断开；防御性处理
                if sid not in r.unbounded_on_removal:
                    r.unbounded_on_removal.append(sid)
                    r.unbounded_on_removal.sort()
                continue
            if lo_e < r.rob_lo:
                r.rob_lo, r.min_lo_sheet = lo_e, sid
            if hi_e > r.rob_hi:
                r.rob_hi, r.max_hi_sheet = hi_e, sid

    # 7) 判定
    for p, r in results.items():
        if p == datum:
            continue
        if not r.reachable:
            r.status = "PENDING"
            r.reason = "尚未与港区基准联测，待联测"
            continue
        if r.unbounded_on_removal:
            r.status = "FAIL"
            r.reason = "撤掉单据 %s 后与港区基准断开，改正范围无界" % "、".join(
                r.unbounded_on_removal)
            continue
        span = r.rob_hi - r.rob_lo
        if span > span_limit:
            r.status = "FAIL"
            r.reason = (
                "稳健区间跨度 %d mm 超过 %d mm 限值"
                "（撤 %s 得最小下界 %d，撤 %s 得最大上界 %d）"
                % (span, span_limit,
                   r.min_lo_sheet or "—", r.rob_lo,
                   r.max_hi_sheet or "—", r.rob_hi)
            )
            continue
        r.status = "OK"
        r.reason = "可合图：稳健区间跨度 %d mm" % span

    ordered = [results[p] for p in points]
    return ComputeReport(datum, ordered, [], len(sheets), len(points))
