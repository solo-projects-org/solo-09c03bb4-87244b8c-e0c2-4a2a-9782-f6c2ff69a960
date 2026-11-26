# -*- coding: utf-8 -*-
"""引擎对拍与边界测试（纯标准库，可直接 python tests/test_engine.py 运行）。

对拍方法：对每张单据真实撤掉后用独立 BF 重算全港（参考实现，刻意不做任何
增量优化），与引擎输出的稳健区间逐点比较，保证"避免逐单重算"的优化结果
与朴素定义完全一致。
"""

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.engine import (  # noqa: E402
    INF,
    Sheet,
    ValidationError,
    compute,
)


def brute_force_bounds(sheets, datum, span_limit=20):
    """参考实现：枚举撤掉每张单 + 不撤单，BF 求每个点的可行界。"""
    points = {datum}
    for s in sheets:
        points.add(s.point_a)
        points.add(s.point_b)
    points = sorted(points)

    def solve(active):
        arcs = []
        for s in active:
            arcs.append((s.point_a, s.point_b, s.hi, s.sid))
            arcs.append((s.point_b, s.point_a, -s.lo, s.sid))
        n = len(points)
        # 超源检测负环
        d = {p: 0 for p in points}
        for _ in range(n):
            upd = False
            for u, v, w, _ in arcs:
                if d[u] + w < d[v]:
                    d[v] = d[u] + w
                    upd = True
            if not upd:
                break
        else:
            return None  # 矛盾
        fwd = {p: INF for p in points}
        fwd[datum] = 0
        for _ in range(n - 1):
            for u, v, w, _ in arcs:
                if fwd[u] != INF and fwd[u] + w < fwd[v]:
                    fwd[v] = fwd[u] + w
        rev = {p: INF for p in points}
        rev[datum] = 0
        for _ in range(n - 1):
            for u, v, w, _ in arcs:  # 反向：原 v->u
                if rev[v] != INF and rev[v] + w < rev[u]:
                    rev[u] = rev[v] + w
        out = {}
        for p in points:
            if fwd[p] == INF or rev[p] == INF:
                out[p] = (None, None)
            else:
                out[p] = (-rev[p], fwd[p])
        return out

    base = solve(sheets)
    if base is None:
        return None
    # 有界撤单情形的极值（引擎同时把断开单号记入 unbound）
    rob = {p: [base[p][0], base[p][1]] for p in points}
    unbound = {p: [] for p in points}
    for s in sheets:
        sub = [x for x in sheets if x.sid != s.sid]
        got = solve(sub)
        assert got is not None, "撤单不应产生新矛盾"
        for p in points:
            lo, hi = got[p]
            if lo is None:
                unbound[p].append(s.sid)
            else:
                rob[p][0] = min(rob[p][0], lo)
                rob[p][1] = max(rob[p][1], hi)
    return base, rob, unbound


def random_sheets(seed, n_points=6, n_sheets=12, datum="D0"):
    """生成保证可行的随机联测单：先定各点真值高程，单据区间围绕
    h_b - h_a 的真值取；这样随机图不会必现负环。"""
    rnd = random.Random(seed)
    pts = [datum] + ["P%d" % i for i in range(1, n_points)]
    height = {datum: 0}
    for p in pts[1:]:
        height[p] = rnd.randint(-40, 120)
    sheets = []
    for i in range(n_sheets):
        a, b = rnd.sample(pts, 2)
        true_diff = height[b] - height[a]
        width = rnd.randint(0, 8)
        sid = "T%02d" % i
        sheets.append(Sheet(sid, a, b, true_diff - width, true_diff + width))
    # 保证基准连通：随机生成一棵生成树
    shuffled = pts[1:]
    rnd.shuffle(shuffled)
    connected = {datum}
    for j, p in enumerate(shuffled):
        parent = rnd.choice(sorted(connected))
        true_diff = height[p] - height[parent]
        sheets.append(Sheet("TT%02d" % j, parent, p, true_diff - 4, true_diff + 4))
        connected.add(p)
    return sheets


def test_random_against_brute_force():
    tested = 0
    for seed in range(120):
        if tested >= 60:
            break
        sheets = random_sheets(seed)
        brute0 = brute_force_bounds(sheets, "D0")
        if brute0 is None:
            continue  # 该随机图本身矛盾，矛盾检测另有专项测试
        report = compute(sheets)
        assert report.ok, "参考实现认为可行时引擎不应报矛盾"
        base, rob, unbound = brute0
        by_point = {n.point: n for n in report.nodes}
        for p, n in by_point.items():
            blo, bhi = base[p]
            assert n.cur_lo == blo and n.cur_hi == bhi, (seed, p, n, base[p])
            rlo, rhi = rob[p]
            assert n.rob_lo == rlo and n.rob_hi == rhi, (seed, p, n, rob[p])
            assert sorted(n.unbounded_on_removal) == sorted(unbound[p]), (seed, p, n, unbound[p])
            if blo is None:
                assert n.status == "PENDING"
            elif unbound[p]:
                assert n.status == "FAIL" and "断开" in n.reason
            elif rhi - rlo > 20:
                assert n.status == "FAIL" and "跨度" in n.reason
            else:
                assert n.status == "OK"
        tested += 1
    print("test_random_against_brute_force: %d 个随机图与暴力枚举对拍全部通过" % tested)


def test_parallel_resurvey_sheets_count_separately():
    # 两条同值复测单，撤任一条仍连通，稳健区间==单据本身区间
    sheets = [
        Sheet("A1", "D0", "Z", 10, 14),
        Sheet("A2", "D0", "Z", 10, 14),
    ]
    r = compute(sheets)
    n = {x.point: x for x in r.nodes}["Z"]
    assert (n.cur_lo, n.cur_hi) == (10, 14)
    assert (n.rob_lo, n.rob_hi) == (10, 14)
    assert n.unbounded_on_removal == [] and n.status == "OK"
    # 只剩一张时它就是桥
    r2 = compute(sheets[:1])
    n2 = {x.point: x for x in r2.nodes}["Z"]
    assert n2.unbounded_on_removal == ["A1"] and n2.status == "FAIL"
    print("test_parallel_resurvey_sheets_count_separately: 通过")


def test_bridge_detection():
    sheets = [
        Sheet("B1", "D0", "X", 0, 4),
        Sheet("B2", "X", "Y", -2, 2),
        Sheet("B3", "Y", "Q", 5, 9),
    ]
    r = compute(sheets)
    by = {n.point: n for n in r.nodes}
    assert by["X"].unbounded_on_removal == ["B1"]
    assert by["Y"].unbounded_on_removal == ["B1", "B2"]
    assert by["Q"].unbounded_on_removal == ["B1", "B2", "B3"]
    assert all(by[p].status == "FAIL" for p in ("X", "Y", "Q"))
    print("test_bridge_detection: 通过")


def test_span_threshold_boundary():
    # 跨度恰好 20 -> 合图；21 -> 不合图
    base = [Sheet("K1", "D0", "Z", 0, 10), Sheet("K2", "D0", "Z", 10, 20)]
    r = compute(base)
    n = {x.point: x for x in r.nodes}["Z"]
    # 现行交集 [10,10]；撤 K1 -> [10,20]，撤 K2 -> [0,10]，稳健 [0,20] 跨度20
    assert (n.rob_lo, n.rob_hi) == (0, 20) and n.status == "OK"
    base2 = [Sheet("K1", "D0", "Z", 0, 10), Sheet("K2", "D0", "Z", 10, 21)]
    r2 = compute(base2)
    n2 = {x.point: x for x in r2.nodes}["Z"]
    assert (n2.rob_lo, n2.rob_hi) == (0, 21) and n2.status == "FAIL"
    # 撤 K2 只剩 [0,10] -> 最小下界 0；撤 K1 只剩 [10,21] -> 最大上界 21
    assert n2.min_lo_sheet == "K2" and n2.max_hi_sheet == "K1"
    print("test_span_threshold_boundary: 通过")


def test_contradiction_negative_cycle():
    # D0->A [10,12], A->B [0,0](精确), B->D0 要求 h_D0-h_B ∈ [-13,-11]
    # 即 h_B ∈ [11,13]；与 h_B=h_A∈[10,12] 交集存在，不矛盾。
    # 改成强制矛盾：B->D0 lo=-9 hi=-9 => h_B=9，与 h_B>=10 冲突
    sheets = [
        Sheet("C1", "D0", "A", 10, 12),
        Sheet("C2", "A", "B", 0, 0),
        Sheet("C3", "B", "D0", -9, -9),
    ]
    r = compute(sheets)
    assert not r.ok and r.contradictions
    c = r.contradictions[0]
    assert set(c.cycle_sheet_ids) == {"C1", "C2", "C3"}
    assert c.cycle_bound < 0
    assert c.cycle_points[0] == c.cycle_points[-1]
    print("test_contradiction_negative_cycle: 通过（闭环 %s，界 %d mm）"
          % (" -> ".join(c.cycle_points), c.cycle_bound))


def test_disconnected_pending():
    sheets = [Sheet("D1", "X", "Y", 0, 4)]
    r = compute(sheets)
    by = {n.point: n for n in r.nodes}
    assert by["D0"].status == "OK" and (by["D0"].rob_lo, by["D0"].rob_hi) == (0, 0)
    assert by["X"].status == "PENDING" and by["Y"].status == "PENDING"
    print("test_disconnected_pending: 通过")


def test_validation_errors():
    for bad in [
        Sheet("", "D0", "Z", 0, 1),
        Sheet("E1", "D0", "D0", 0, 1),
        Sheet("E2", "D0", "Z", 5, 4),
    ]:
        try:
            bad.validate()
        except ValidationError:
            continue
        raise AssertionError("应拒绝: %s" % bad)
    try:
        compute([Sheet("F1", "D0", "Z", 0, 1), Sheet("F1", "D0", "Q", 0, 1)])
    except ValidationError:
        pass
    else:
        raise AssertionError("重复编号应拒绝")
    print("test_validation_errors: 通过")


def test_empty_sheets():
    r = compute([])
    assert r.ok and len(r.nodes) == 1 and r.nodes[0].point == "D0"
    assert r.nodes[0].status == "OK" and r.nodes[0].rob_lo == 0
    print("test_empty_sheets: 通过")


def test_parallel_edges_bridge_chain():
    """含平行复测单的桥链：两条平行边时无桥，撤掉一条后剩单成桥。"""
    sheets = [
        Sheet("P1", "D0", "X", 0, 4),
        Sheet("P2", "D0", "X", 1, 5),    # 与 P1 平行，二者均非桥
        Sheet("P3", "X", "Y", 2, 6),     # 唯一通道，是 X-Y 的桥
    ]
    r = compute(sheets)
    by = {n.point: n for n in r.nodes}
    assert by["X"].unbounded_on_removal == []
    assert by["Y"].unbounded_on_removal == ["P3"]
    # 撤 P1 后只剩 P2 接 X，X 的稳健区间覆盖两条单据各自的可行域
    assert by["X"].rob_lo == 0 and by["X"].rob_hi == 5
    print("test_parallel_edges_bridge_chain: 通过")


def test_extreme_sheet_witnesses():
    # 三路径，验证 min_lo_sheet / max_hi_sheet 指认真正取极值的单
    sheets = [
        Sheet("W1", "D0", "Z", 0, 100),
        Sheet("W2", "D0", "Z", 40, 60),
        Sheet("W3", "D0", "Z", 45, 55),
    ]
    r = compute(sheets)
    n = {x.point: x for x in r.nodes}["Z"]
    # 现行交集 [45,55]；撤 W1 -> [45,55]；撤 W2 -> [45,55]；撤 W3 -> [40,60]
    assert (n.cur_lo, n.cur_hi) == (45, 55)
    assert (n.rob_lo, n.rob_hi) == (40, 60)
    assert n.min_lo_sheet == "W3" and n.max_hi_sheet == "W3"
    print("test_extreme_sheet_witnesses: 通过")


if __name__ == "__main__":
    test_random_against_brute_force()
    test_parallel_resurvey_sheets_count_separately()
    test_bridge_detection()
    test_span_threshold_boundary()
    test_contradiction_negative_cycle()
    test_disconnected_pending()
    test_validation_errors()
    test_empty_sheets()
    test_parallel_edges_bridge_chain()
    test_extreme_sheet_witnesses()
    print("\n全部引擎测试通过。")
