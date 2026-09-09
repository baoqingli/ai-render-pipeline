# tests/unit/test_cad_walls.py
"""walls.py 纯函数单测：房间边合并 / 端点吸附 / 条带覆盖判定。"""
from app.tools.cad.walls import (
    extract_room_edges,
    merge_collinear,
    snap_endpoints,
    strip_covered_by_edges,
)


def test_extract_room_edges_closes_ring():
    ring = [[0.0, 0.0], [4000.0, 0.0], [4000.0, 3000.0], [0.0, 3000.0]]
    segs = extract_room_edges([ring])
    assert len(segs) == 4                              # 自动补闭合边


def test_merge_collinear_dedupes_shared_edges():
    # 两相邻房间共享边画两次 → 合并为一条
    a = ((4000.0, 0.0), (4000.0, 3000.0))
    b = ((4000.0, 3000.0), (4000.0, 0.0))
    merged = merge_collinear([a, b])
    assert len(merged) == 1
    assert len(merge_collinear([a])) == 1


def test_merge_collinear_joins_small_gaps():
    # 共线且缝隙 200mm（≤ COLLINEAR_GAP_TOL）→ 拼回一段
    a = ((0.0, 0.0), (4000.0, 0.0))
    b = ((4200.0, 0.0), (8000.0, 0.0))
    assert len(merge_collinear([a, b])) == 1


def test_merge_collinear_keeps_real_gaps():
    # 共线但缝隙 1000mm → 保持两段（真实洞口由开洞阶段表达）
    a = ((0.0, 0.0), (4000.0, 0.0))
    b = ((5000.0, 0.0), (8000.0, 0.0))
    assert len(merge_collinear([a, b])) == 2


def test_merge_collinear_joins_lateral_offset():
    # 不同图层画的同一道墙横向偏移 80mm（≤ 容差）→ 合并
    a = ((0.0, 0.0), (6000.0, 0.0))
    b = ((0.0, 80.0), (6000.0, 80.0))
    assert len(merge_collinear([a, b])) == 1


def test_snap_endpoints_unifies_nearby_points():
    segs = [((0.0, 0.0), (4000.0, 0.0)),
            ((4000.0, 60.0), (4000.0, 3000.0))]       # 端点错位 60mm
    snapped = snap_endpoints(segs, tol=300.0)
    assert len(snapped) == 2
    assert snapped[0][1] == snapped[1][0]             # 吸附到同一点


def test_strip_covered_by_edges_hit_and_miss():
    edges = [((0.0, 0.0), (6000.0, 0.0))]
    on = ((1000.0, 10.0), (5000.0, -10.0))            # 两端都贴边 → 覆盖
    off = ((1000.0, 3000.0), (5000.0, 3000.0))        # 远离 → 未覆盖
    assert strip_covered_by_edges(on, 200.0, edges)
    assert not strip_covered_by_edges(off, 200.0, edges)
