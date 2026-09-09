# app/tools/cad/walls.py
"""墙缝合纯几何层：房间边提取 → 端点吸附 → 共线聚类合并 → 双线墙条带去重。

输入输出全部为 mm 坐标段；不含任何 ezdxf/DXF 依赖，可独立单测。
设计见 docs/white-model-fix-plan-2026-09.md §4 P0-2。
"""
import math
from itertools import pairwise

from app.tools.cad.rules import (
    COLLINEAR_ANGLE_TOL_DEG,
    COLLINEAR_GAP_TOL,
    COLLINEAR_LATERAL_TOL,
    EDGE_SNAP_TOL,
)

Vec = tuple[float, float]
Seg = tuple[Vec, Vec]


def _dist(a: Vec, b: Vec) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _tup(p) -> Vec:
    return (float(p[0]), float(p[1]))


def extract_room_edges(rings: list[list[Vec]]) -> list[Seg]:
    """从房间多边形环提取边段（环不闭合时自动补闭合边；点可为 list/tuple）。"""
    segs: list[Seg] = []
    for ring in rings:
        pts = [_tup(p) for p in ring]
        if len(pts) >= 2 and _dist(pts[0], pts[-1]) > 1e-9:
            pts.append(pts[0])
        for a, b in pairwise(pts):
            if _dist(a, b) > 1.0:               # 跳过退化边
                segs.append((a, b))
    return segs


def snap_endpoints(segs: list[Seg], tol: float = EDGE_SNAP_TOL) -> list[Seg]:
    """端点网格吸附：tol 邻域内的端点统一到首个出现位置，消除轮廓间微小错位。"""
    buckets: dict[tuple[int, int], Vec] = {}
    rep: dict[Vec, Vec] = {}

    def rep_of(p: Vec) -> Vec:
        if p in rep:
            return rep[p]
        gx, gy = round(p[0] / tol), round(p[1] / tol)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                key = (gx + dx, gy + dy)
                if key in buckets and _dist(buckets[key], p) <= tol:
                    rep[p] = buckets[key]
                    return buckets[key]
        buckets[(gx, gy)] = p
        rep[p] = p
        return p

    out: list[Seg] = []
    for a, b in segs:
        na, nb = rep_of(a), rep_of(b)
        if _dist(na, nb) > 1.0:
            out.append((na, nb))
    return out


def _norm_angle_deg(seg: Seg) -> float:
    dx, dy = seg[1][0] - seg[0][0], seg[1][1] - seg[0][1]
    return math.degrees(math.atan2(dy, dx)) % 180.0


class _Cluster:
    __slots__ = ("angle", "hi", "lo", "ox", "oy", "px", "py", "s_off", "ux", "uy")

    def __init__(self, seg: Seg) -> None:
        dx, dy = seg[1][0] - seg[0][0], seg[1][1] - seg[0][1]
        L = math.hypot(dx, dy) or 1.0
        self.ux, self.uy = dx / L, dy / L
        self.px, self.py = -self.uy, self.ux
        self.ox, self.oy = seg[0]
        self.angle = _norm_angle_deg(seg)
        self.s_off = 0.0
        t = self._project(seg)
        self.lo, self.hi = t

    def _project(self, seg: Seg) -> tuple[float, float]:
        t0 = (seg[0][0] - self.ox) * self.ux + (seg[0][1] - self.oy) * self.uy
        t1 = (seg[1][0] - self.ox) * self.ux + (seg[1][1] - self.oy) * self.uy
        return (min(t0, t1), max(t0, t1))

    def lateral(self, seg: Seg) -> float:
        return (seg[0][0] - self.ox) * self.px + (seg[0][1] - self.oy) * self.py

    def accepts(self, seg: Seg, angle_tol: float, lateral_tol: float,
                gap_tol: float = COLLINEAR_GAP_TOL) -> bool:
        diff = abs(((_norm_angle_deg(seg) - self.angle + 90.0) % 180.0) - 90.0)
        if diff > angle_tol:
            return False
        if abs(self.lateral(seg) - self.s_off) > lateral_tol:
            return False
        lo, hi = self._project(seg)
        return lo <= self.hi + gap_tol and self.lo <= hi + gap_tol

    def absorb(self, seg: Seg) -> None:
        lo, hi = self._project(seg)
        self.lo, self.hi = min(self.lo, lo), max(self.hi, hi)

    def emit(self) -> Seg | None:
        if self.hi - self.lo <= 1.0:
            return None
        return ((self.ox + self.ux * self.lo, self.oy + self.uy * self.lo),
                (self.ox + self.ux * self.hi, self.oy + self.uy * self.hi))


def merge_collinear(segs: list[Seg], angle_tol_deg: float = COLLINEAR_ANGLE_TOL_DEG,
                    lateral_tol: float = COLLINEAR_LATERAL_TOL,
                    gap_tol: float = COLLINEAR_GAP_TOL) -> list[Seg]:
    """共线重叠边合并：相邻房间共享边（画两次）并为一条；≤gap_tol 的同向断缝拼回
    一段（真实门窗洞口由开洞阶段负责表达，不依赖轮廓缝隙）。"""
    clusters: list[_Cluster] = []
    for s in segs:
        for c in clusters:
            if c.accepts(s, angle_tol_deg, lateral_tol, gap_tol):
                c.absorb(s)
                break
        else:
            clusters.append(_Cluster(s))
    out: list[Seg] = []
    for c in clusters:
        seg = c.emit()
        if seg is not None:
            out.append(seg)
    return out


def point_seg_distance(p: Vec, seg: Seg) -> float:
    ax, ay = seg[0]
    bx, by = seg[1]
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return _dist(p, seg[0])
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return _dist(p, (ax + t * dx, ay + t * dy))


def strip_covered_by_edges(centerline: Seg, thickness: float, edges: list[Seg],
                           tol: float = EDGE_SNAP_TOL) -> bool:
    """双线墙条带中心线是否已被房间边覆盖：两端落在同一条边 tol 邻域内，
    或中点贴边且端点偏移在墙厚内。"""
    p1, p2 = centerline
    mid = ((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)
    for e in edges:
        if point_seg_distance(p1, e) <= tol and point_seg_distance(p2, e) <= tol:
            return True
        if point_seg_distance(mid, e) <= max(thickness / 2.0, 1.0) \
                and point_seg_distance(p1, e) <= thickness \
                and point_seg_distance(p2, e) <= thickness:
            return True
    return False
