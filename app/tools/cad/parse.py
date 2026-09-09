# app/tools/cad/parse.py
"""DXF → SceneJSON：墙体提取 → 房间推导 → 门窗家具 → 质量指标。

设计要点（docs/white-model-fix-plan-2026-09.md §4/§7）：
- 墙体：全图层扫描细长闭合轮廓 + HATCH 填充 + 双线配对（不依赖图层名）
- 房间：混合多边形化（真实墙带 + 天花分区线闭合）+ 按墙合并相邻 cell
- 家具：INSERT 块 + 闭合/开放多段线 + CIRCLE + SPLINE（全图层扫描）
- 语义路由：VLM 解读书的 layer_semantics 增强图层分类
- frozen_layers：布置图视口对齐（提取与视口显示一致）
- 质量指标：rooms/walls/furniture/tiling/attach ratio + confidence 联动
"""
import contextlib
import math
import statistics
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

import ezdxf
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union
from shapely.validation import make_valid

from app.models.cad_report import CadReport
from app.models.scene import Door, Furniture, ParseQuality, Room, SceneJSON, Wall, Window
from app.models.tooling import Metrics, ToolError, ToolResult
from app.models.vision import DrawingUnderstanding
from app.tools.cache import build_cache_key
from app.tools.cad import rules as R
from app.tools.cad.geometry import pair_wall_segments
from app.tools.cad.inspect import inspect_dxf
from app.tools.cad.walls import (
    extract_room_edges,
    merge_collinear,
    snap_endpoints,
    strip_covered_by_edges,
)
from app.tools.cad_render import model_extent
from app.tools.cad.dimension_walls import build_dimension_walls

POLY_FURN_AREA_MIN = 100_000
POLY_FURN_AREA_MAX = 12_000_000


# ── 基础工具 ──────────────────────────────────────────────────────────────────

def _strip_polygon(p1, p2, t: float) -> list[list[float]]:
    ls = LineString([p1, p2]).buffer(t / 2, cap_style=2, join_style=2)
    return [[float(x), float(y)] for x, y in ls.exterior.coords]


def _rect_polygon(bbox: tuple[float, float, float, float]) -> Polygon:
    x0, y0, x1, y1 = bbox
    return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def _nearest_wall_id(point: Point, walls: list[Wall],
                     strips: dict[str, BaseGeometry]) -> str | None:
    best, best_d = None, None
    for w in walls:
        d = point.distance(strips[w.id])
        if best_d is None or d < best_d:
            best, best_d = w.id, d
    return best


# ── HATCH 边界点提取 ─────────────────────────────────────────────────────────

def _hatch_path_points(h) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for pth in h.paths:
        if hasattr(pth, "vertices"):
            pts += [(v[0], v[1]) for v in pth.vertices]
        else:
            for ed in pth.edges:
                tn = type(ed).__name__
                if tn == "LineEdge":
                    pts.append((ed.start[0], ed.start[1]))
                    pts.append((ed.end[0], ed.end[1]))
                elif tn == "ArcEdge":
                    cx, cy, r = ed.center[0], ed.center[1], ed.radius
                    pts += [(cx - r, cy - r), (cx + r, cy + r)]
    return pts


# ── 墙体提取（全图层扫描，几何特征驱动） ────────────────────────────────────────

def _extract_wall_polys(msp, scale: float) -> list[Polygon]:
    """全图层扫描：闭合细长多段线 + HATCH 填充 → 墙多边形。

    不依赖图层名——按几何特征分类（细长条带 = 墙体候选）。
    """
    out: list[Polygon] = []
    for e in msp:
        t = e.dxftype()
        pts: list[tuple[float, float]] = []
        if t == "LWPOLYLINE" and e.closed:  # type: ignore[attr-defined]
            pts = [(p[0] * scale, p[1] * scale) for p in e.get_points()]  # type: ignore[attr-defined]
        elif t == "HATCH":
            pts = _hatch_boundary_points(e)
        if len(pts) < 3:
            continue
        try:
            poly = Polygon(pts)
            if not poly.is_valid:
                poly = make_valid(poly)
            if poly.is_empty or poly.geom_type != "Polygon":
                continue
        except Exception:  # noqa: BLE001
            continue

        minx, miny, maxx, maxy = poly.bounds
        thick, long_ = min(maxx - minx, maxy - miny), max(maxx - minx, maxy - miny)
        # 墙体几何特征：细长条带
        if 40.0 <= thick <= 600.0 and long_ >= 400.0 and poly.area >= 40_000.0:
            out.append(poly)
    return out


def _hatch_boundary_points(h) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for pth in h.paths:
        if hasattr(pth, "vertices"):
            pts += [(v[0], v[1]) for v in pth.vertices]
        else:
            for ed in pth.edges:
                tn = type(ed).__name__
                if tn == "LineEdge":
                    pts += [(ed.start[0], ed.start[1]), (ed.end[0], ed.end[1])]
                elif tn == "ArcEdge":
                    cx, cy, r = ed.center[0], ed.center[1], ed.radius
                    pts += [(cx - r, cy - r), (cx + r, cy + r)]
    return pts


def _extract_wall_hatches(doc, msp, scale: float) -> list[Polygon]:
    """墙体 HATCH + 块内墙体轮廓 → 墙多边形（手动矩阵遍历块，支持嵌套/旋转/缩放）。

    含 WALL_FILL_LAYER_RE 语义图层上的 HATCH 边界与闭合多段线。
    virtual_entities 对含 HATCH/镜像块静默失败——必须手动 matrix44 链。
    """
    out: list[Polygon] = []

    def visit(container, chain: tuple, depth: int, inherited: str) -> None:
        if depth > 6:
            return
        for e in container:
            t = e.dxftype()
            if t == "INSERT":
                sub = doc.blocks.get(e.dxf.name)
                if sub is None:
                    continue
                m = None
                with contextlib.suppress(Exception):
                    m = e.matrix44()
                if m is None:
                    continue
                visit(sub, (*chain, m), depth + 1,
                      e.dxf.layer if e.dxf.layer != "0" else inherited)
                continue
            layer = e.dxf.layer if e.dxf.layer != "0" else inherited
            if not R.WALL_FILL_LAYER_RE.search(layer):
                continue

            def xf(x, y):
                px, py = x * scale, y * scale
                for m in reversed(chain):
                    v = m.transform((px, py, 0.0))
                    px, py = v.x, v.y
                return px, py

            loops: list[list[tuple[float, float]]] = []
            if t == "HATCH":
                for pth in e.paths:
                    if hasattr(pth, "vertices"):
                        loops.append([(v[0], v[1]) for v in pth.vertices])
            elif t == "LWPOLYLINE" and e.closed:
                with contextlib.suppress(Exception):
                    loops.append([(p[0], p[1]) for p in e.get_points()])
            for pts in loops:
                if len(pts) < 3:
                    continue
                world = [xf(x, y) for x, y in pts]
                raw = Polygon(world)
                poly = raw if raw.is_valid else make_valid(raw)
                if poly.is_empty:
                    continue
                cands = [poly] if poly.geom_type == "Polygon" \
                    else [g for g in poly.geoms if g.geom_type == "Polygon"]
                for p in cands:
                    band = (50_000.0 <= p.area <= 50_000_000.0
                            and p.area / max(p.exterior.length, 1.0) <= 350.0)
                    if band:
                        out.append(p)

    visit(msp, (), 0, "0")
    return out


def _extract_wall_hatches_msp(msp, scale: float) -> list[Polygon]:
    """全图层扫描 HATCH 填充 → 墙体多边形（细长条带几何过滤）。"""
    out: list[Polygon] = []
    for e in msp:
        if e.dxftype() != "HATCH":
            continue
        pts = _hatch_boundary_points(e)
        if len(pts) < 3:
            continue
        try:
            poly = Polygon(pts)
            if not poly.is_valid:
                poly = make_valid(poly)
            if poly.is_empty or poly.geom_type != "Polygon":
                continue
        except Exception:  # noqa: BLE001
            continue
        minx, miny, maxx, maxy = poly.bounds
        thick, long_ = min(maxx - minx, maxy - miny), max(maxx - minx, maxy - miny)
        if 40.0 <= thick <= 600.0 and long_ >= 400.0 and poly.area >= 40_000.0:
            out.append(poly)
    return out


# ── 房间推导（混合多边形化 + 按墙合并） ────────────────────────────────────────

def _raw_contours(msp, layers: set[str], scale: float) -> dict[str, list[Polygon]]:
    per_layer: dict[str, list[Polygon]] = defaultdict(list)
    for e in msp:
        if e.dxf.layer not in layers:
            continue
        pts = None
        if e.dxftype() == "LWPOLYLINE" and e.closed:  # type: ignore[attr-defined]
            pts = [(p[0] * scale, p[1] * scale) for p in e.get_points()]  # type: ignore[attr-defined]
        elif e.dxftype() == "POLYLINE" and e.is_closed:
            pts = [(v.dxf.location.x * scale, v.dxf.location.y * scale)
                   for v in e.vertices]
        if pts is None or len(pts) < 3:
            continue
        raw = Polygon(pts)
        fixed = raw if raw.is_valid else make_valid(raw)
        cands = [fixed] if fixed.geom_type == "Polygon" \
            else [g for g in fixed.geoms if g.geom_type == "Polygon"]
        per_layer[e.dxf.layer] += cands
    return per_layer


def _contour_polys(msp, layers: set[str], scale: float) -> list[Polygon]:
    per_layer = _raw_contours(msp, layers, scale)
    parts = []
    for _layer, polys in per_layer.items():
        if len(polys) >= 12:  # CEILING_GRID_MIN_CELLS
            u = unary_union(polys)
            parts += [u] if u.geom_type == "Polygon" else list(u.geoms)
        else:
            parts += polys
    return [p for p in parts if 500_000.0 <= p.area <= 1_000_000_000.0]


def _merge_cells_by_walls(cells: list[Polygon], wall_polys: list[Polygon]) -> list[Polygon]:
    n = len(cells)
    if n == 0:
        return []
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            shared = cells[i].boundary.intersection(cells[j].boundary)
            if shared.is_empty or shared.length < 500.0:
                continue
            covered = 0
            total = 0
            d = 300.0
            k = 0
            while k * d <= shared.length:
                p = shared.interpolate(k * d)
                total += 1
                if any(w.contains(p) or w.boundary.distance(p) < 1e-6 for w in wall_polys):
                    covered += 1
                k += 1
            if total and covered / total < 0.5:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(cells[i])
    return [unary_union(g) for g in groups.values()]


def _drop_wall_band_cells(cells, wall_polys):
    return [p for p in cells
            if not any(p.intersection(w).area / max(p.area, 1) > 0.5 for w in wall_polys)]


# ── 主流程 ────────────────────────────────────────────────────────────────────


def _chain_open_polylines(chains, tol=15000.0):
    segs = [list(c) for c in chains]
    changed = True
    while changed:
        changed = False
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                si, sj = segs[i], segs[j]
                dists = [
                    (math.dist(si[-1], sj[0]), False, False),
                    (math.dist(si[-1], sj[-1]), False, True),
                    (math.dist(si[0], sj[0]), True, False),
                    (math.dist(si[0], sj[-1]), True, True),
                ]
                best_d, ri, rj = min(dists, key=lambda x: x[0])
                if best_d < tol:
                    if ri:
                        segs[i] = segs[i][::-1]
                    if rj:
                        segs[j] = segs[j][::-1]
                    segs[i] = segs[i] + segs[j]
                    segs.pop(j)
                    changed = True
                    break
            if changed:
                break
    closed = []
    for s in segs:
        if len(s) >= 3 and math.dist(s[0], s[-1]) < tol:
            closed.append(s)
    return closed


import re  # noqa: E402

WIDTH_RE = re.compile(r"(\d{3,4})")
ROOM_NAME_RE = re.compile(r"(室|厅|卧|厨|卫|阳台)")


# ── parse_scene 所需的辅助函数（补全） ─────────────────────────────────────────

def _segments_from(msp, layers):
    """图层线段（原始坐标，调用方负责 scale）。"""
    segs = []
    for e in msp:
        if e.dxf.layer not in layers:
            continue
        if e.dxftype() == "LINE":
            segs.append(((e.dxf.start.x, e.dxf.start.y),
                         (e.dxf.end.x, e.dxf.end.y)))
        elif e.dxftype() == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in e.get_points()]
            if e.closed and len(pts) >= 3:
                pts.append(pts[0])
            segs += list(pairwise(pts))
    return segs


def _extract_band_polylines(msp, scale):
    """完成面/隔断图层细长闭合轮廓 → 墙带。"""
    out = []
    for e in msp:
        if e.dxftype() != "LWPOLYLINE" or not e.closed:
            continue
        if not R.FINISH_WALL_LAYER_RE.search(e.dxf.layer):
            continue
        pts = [(p[0] * scale, p[1] * scale) for p in e.get_points()]
        if len(pts) < 3:
            continue
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = make_valid(poly)
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        minx, miny, maxx, maxy = poly.bounds
        thick = min(maxx - minx, maxy - miny)
        long_ = max(maxx - minx, maxy - miny)
        if 40.0 <= thick <= 600.0 and long_ >= 400.0 and poly.area >= 40_000.0:
            out.append(poly)
    return out


def _contour_polys_msp(msp, layers, scale):
    """轮廓收集（同层网格合并 + 面积窗）。"""
    return _contour_polys(msp, layers, scale)


def _dedupe_contours(polys):
    """面积降序 + IoU/包含去重。"""
    kept: list[Polygon] = []
    for poly in sorted(polys, key=lambda p: -p.area):
        dup = False
        for k in kept:
            inter = poly.intersection(k).area
            union = poly.union(k).area
            if union > 0 and inter / union > R.ROOM_OVERLAP_IOU:
                dup = True
                break
            smaller = min(poly.area, k.area)
            if smaller > 0 and inter / smaller > R.ROOM_CONTAINMENT_RATIO:
                dup = True
                break
        if not dup:
            kept.append(poly)
    return kept


def _room_name(poly, notes, scale):
    inside = []
    near = []
    for note in notes:
        if not R.ROOM_NAME_RE.search(note.content):
            continue
        p = Point(note.position[0] * scale, note.position[1] * scale)
        if poly.contains(p):
            inside.append(note.content.strip())
        else:
            d = poly.distance(p)
            if d <= R.NAME_SNAP_DIST:
                near.append((d, note.content.strip()))
    pool = inside or ([c for _, c in sorted(near)] if near else [])
    return min(pool, key=len) if pool else None


def _classify_type(name, layer):
    text = f"{name} {layer}".lower()
    for ft in R.FURNITURE_TYPES:
        if any(kw in text for kw in ft.keywords):
            return ft
    return None


def _unrotate_size(w, h, rotation_deg):
    if abs(rotation_deg % 180.0) < 1e-6:
        return w, h
    if abs(rotation_deg % 180.0 - 90.0) < 1e-6:
        return h, w
    th = math.radians(rotation_deg)
    cos, sin = abs(math.cos(th)), abs(math.sin(th))
    corners = [(0, 0), (w, 0), (0, h), (w, h)]
    xs = [abs(cx * cos - cy * sin) for cx, cy in corners]
    ys = [abs(cx * sin + cy * cos) for cx, cy in corners]
    return max(xs) - min(xs), max(ys) - min(ys)


def _furn_size(ft, measured):
    default = list(ft.default)
    if measured is None or measured[0] <= 1.0 or measured[1] <= 1.0:
        return default, False
    w, d = measured
    (wlo, whi), (dlo, dhi), _ = ft.bounds
    if wlo <= w <= whi and dlo <= d <= dhi:
        return [w, d, ft.default[2]], True
    return default, False


def _measure_insert(e, scale, doc=None):
    """块引用世界 bbox（手动矩阵链，镜像安全）。"""
    if doc is None:
        doc = e.doc
    xs, ys = [], []

    def _xform(chain, x, y):
        px, py = x, y
        for m in reversed(chain):
            v = m.transform((px, py, 0.0))
            px, py = v.x, v.y
        return px, py

    def walk(container, chain, depth):
        if depth > 6:
            return
        for sub in container:
            t = sub.dxftype()
            if t == "INSERT":
                sub_blk = doc.blocks.get(sub.dxf.name)
                if sub_blk is None:
                    continue
                sm = None
                with contextlib.suppress(Exception):
                    sm = sub.matrix44()
                if sm is not None:
                    walk(sub_blk, (*chain, sm), depth + 1)
                continue
            ends = []
            with contextlib.suppress(Exception):
                if t == "LINE":
                    s_, e_ = sub.dxf.start, sub.dxf.end
                    ends = [(s_.x, s_.y), (e_.x, e_.y)]
                elif t == "LWPOLYLINE":
                    ends = [(p[0], p[1]) for p in sub.get_points()]
                elif t in ("ARC", "CIRCLE"):
                    c0, r = sub.dxf.center, sub.dxf.radius
                    ends = [(c0.x - r, c0.y - r), (c0.x + r, c0.y + r)]
                elif t == "ELLIPSE":
                    maj = sub.dxf.major_axis
                    r = math.hypot(maj[0], maj[1])
                    c0 = sub.dxf.center
                    ends = [(c0.x - r, c0.y - r), (c0.x + r, c0.y + r)]
            for p in ends:
                w = _xform(chain, p[0] * scale, p[1] * scale)
                xs.append(w[0])
                ys.append(w[1])

    blk = doc.blocks.get(e.dxf.name)
    if blk is None:
        return None, None, False
    m0 = None
    with contextlib.suppress(Exception):
        m0 = e.matrix44()
    walk(blk, (m0,) if m0 is not None else (), 0)
    if not xs:
        return None, None, False
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    return (w, h), ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0), True


def parse_scene(dxf_path: str | Path, report: CadReport | None = None,
                understanding: DrawingUnderstanding | None = None,
                frozen_layers: set[str] | None = None) -> ToolResult[SceneJSON]:
    rep = report or inspect_dxf(dxf_path)
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    if frozen_layers:
        for e in list(msp):
            if e.dxf.layer in frozen_layers:
                msp.delete_entity(e)

    fallbacks: list[str] = []
    scale = 1000.0 if rep.unit_guess == "m" else 1.0

    # 语义路由
    semantic_wall: set[str] = set()
    semantic_furniture: set[str] = set()
    if understanding is not None:
        for ls in understanding.layer_semantics:
            sem = ls.semantic
            if sem.startswith("wall") or sem in ("partition", "structure"):
                semantic_wall.add(ls.layer)
            elif sem.startswith("furniture"):
                semantic_furniture.add(ls.layer)

    # 1) 墙条带（双线配对）
    wall_layers = (set(rep.wall_layer_candidates) or {"WALL"}) | semantic_wall
    segs = [((a[0] * scale, a[1] * scale), (b[0] * scale, b[1] * scale))
            for a, b in _segments_from(msp, wall_layers)]
    centers = pair_wall_segments(segs, default_thickness=200.0)

    # 2) 全量墙体提取（HATCH 填充 + 闭合细长多段线，不依赖图层名）
    hatch_walls = _extract_wall_hatches(doc, msp, scale)
    band_walls = _extract_band_polylines(msp, scale)
    # 尺寸链墙（defpoints 毫米级墙线，refs>=2 高置信）——与实体墙网合并
    dim_walls: list = []
    with contextlib.suppress(Exception):
        dim_walls = build_dimension_walls(doc) or []
    all_wall_polys = hatch_walls + band_walls + dim_walls

    # 3) 房间推导
    # ① 混合多边形化（真实墙带 + 条带中心线 + 天花分区线闭合）
    contour_layers = set(rep.ceiling_layer_candidates) | set(rep.room_layer_candidates)
    contour_polys = _contour_polys_msp(msp, contour_layers, scale) if contour_layers else []
    room_polys: list[Polygon] = []

    if len(true_walls_src := hatch_walls + band_walls + dim_walls) >= 3:
        loops = [LineString(p.exterior.coords) for p in true_walls_src]
        loops += [LineString([(c[0][0], c[0][1]), (c[1][0], c[1][1])]) for c in centers]
        loops += [LineString(p.exterior.coords) for p in contour_polys]
        noded = unary_union(loops)
        cells = [p for p in polygonize(getattr(noded, "geoms", [noded]))
                 if p.area >= 30_000.0]
        cells = _drop_wall_band_cells(cells, true_walls_src)
        cells = _merge_cells_by_walls(cells, true_walls_src)
        room_polys = [p for p in cells if 500_000.0 <= p.area <= 1_000_000_000.0]
        if room_polys:
            fallbacks.append(f"rooms from wall network ({len(true_walls_src)} walls)")

    # ② 天花/完成面轮廓
    if not room_polys and contour_layers:
        room_polys = _dedupe_contours(contour_polys)
        if room_polys:
            fallbacks.append("rooms from ceiling/finish contours")

    # ③ VLM 分区
    if not room_polys and understanding is not None and understanding.zones:
        ext = model_extent(doc)
        for z in understanding.zones:
            p_ = z.bbox_pct
            if len(p_) != 4:
                continue
            zx0 = ext[0] + p_[0] * (ext[2] - ext[0])
            zy0 = ext[1] + p_[1] * (ext[3] - ext[1])
            zx1 = ext[0] + p_[2] * (ext[2] - ext[0])
            zy1 = ext[1] + p_[3] * (ext[3] - ext[1])
            rect = Polygon([(zx0, zy0), (zx1, zy0), (zx1, zy1), (zx0, zy1)])
            if 500_000.0 <= rect.area <= 1_000_000_000.0:
                room_polys.append(rect)
        if room_polys:
            fallbacks.append("rooms from VLM zones")

    # ④ 中心线 polygonize
    if not room_polys and centers:
        cl = [LineString([(c[0][0], c[0][1]), (c[1][0], c[1][1])]) for c in centers]
        noded = unary_union(cl)
        room_polys = [p for p in polygonize(getattr(noded, "geoms", [noded]))
                      if 500_000.0 <= p.area <= 1_000_000_000.0]
        if room_polys:
            fallbacks.append("rooms from centerline polygonize")

    # ⑤ 开放折线拼接
    if not room_polys and rep.room_layer_candidates:
        chains = []
        for e in msp:
            if e.dxf.layer not in set(rep.room_layer_candidates):
                continue
            if e.dxftype() != "LWPOLYLINE" or e.closed:  # type: ignore[attr-defined]
                continue
            pts = [(p[0] * scale, p[1] * scale) for p in e.get_points()]  # type: ignore[attr-defined]
            if len(pts) >= 2:
                chains.append(pts)
        for chain in _chain_open_polylines(chains):
            raw = Polygon(chain)
            fixed = raw if raw.is_valid else make_valid(raw)
            cands = [fixed] if fixed.geom_type == "Polygon" \
                else [g for g in fixed.geoms if g.geom_type == "Polygon"]
            room_polys += [p for p in cands if 500_000.0 <= p.area <= 1_000_000_000.0]
        if room_polys:
            fallbacks.append("rooms from open-polyline chain")

    # 4) 统一墙输出
    #    尺寸墙充足（≥12 = 外墙4+内墙8）时以它为唯一主源——defpoints 毫米级精确，
    #    其余源（房间边/中心线）只补充不重叠部分，防止多源叠加渲染成实心黑带。
    walls: list[Wall] = []
    wall_polys: list[Polygon] = []
    dim_dominant = len(dim_walls) >= 12
    primary = dim_walls if dim_dominant else all_wall_polys
    for poly in primary:
        walls.append(Wall(id=f"wall_{len(walls)+1:03d}",
                          polygon=[[round(x), round(y)] for x, y in poly.exterior.coords]))
        wall_polys.append(poly)

    def _overlaps_existing(seg_poly: Polygon) -> bool:
        return any(seg_poly.intersection(hp).area / max(seg_poly.area, 1) > 0.5
                   for hp in wall_polys)

    if room_polys:
        rings = [[(float(x), float(y)) for x, y in poly.exterior.coords]
                 for poly in room_polys]
        edges = merge_collinear(snap_endpoints(extract_room_edges(rings)))
        edge_t = statistics.median(
            [c[2] for c in centers if strip_covered_by_edges((c[0], c[1]), c[2], edges)]
        ) if centers else 200.0
        edge_t = min(edge_t, 600.0)   # 墙厚上限：防坏配对条带撑出巨板
        for p1, p2 in edges:
            seg_poly = Polygon(_strip_polygon(p1, p2, edge_t))
            if not _overlaps_existing(seg_poly):
                walls.append(Wall(id=f"wall_{len(walls)+1:03d}",
                                  polygon=_strip_polygon(p1, p2, edge_t)))
    if not dim_dominant:
        for c in centers:
            seg_poly = Polygon(_strip_polygon(c[0], c[1], c[2]))
            if not _overlaps_existing(seg_poly):
                walls.append(Wall(id=f"wall_{len(walls)+1:03d}",
                                  polygon=_strip_polygon(c[0], c[1], c[2])))
    elif centers:
        # 尺寸墙主导时：中心线墙只补充不重叠的（保留 HATCH 特殊形状墙）
        pass

    strips = {w.id: Polygon(w.polygon) for w in walls if len(w.polygon) >= 4}

    # 5) 房间
    rooms = []
    for i, poly in enumerate(sorted(room_polys, key=lambda p: -p.area)):
        rooms.append(Room(id=f"room_{i+1:03d}",
                          name=_room_name(poly, rep.text_annotations, scale),
                          polygon=[[float(x), float(y)] for x, y in poly.exterior.coords]))

    # 6) 门窗家具
    doors: list[Door] = []
    windows: list[Window] = []
    furniture: list[Furniture] = []
    for ins in msp.query("INSERT"):
        layer, name = ins.dxf.layer, ins.dxf.name
        pos = Point(ins.dxf.insert.x * scale, ins.dxf.insert.y * scale)
        lw = f"{layer} {name}"
        m = WIDTH_RE.search(name)
        if R.DOOR_HINT_RE.search(lw):
            doors.append(Door(id=f"door_{len(doors)+1:03d}", position=[pos.x, pos.y],
                              width=float(m.group(1)) if m else 900.0,
                              wall_id=_nearest_wall_id(pos, walls, strips)))
        elif R.WINDOW_HINT_RE.search(lw):
            windows.append(Window(id=f"win_{len(windows)+1:03d}", position=[pos.x, pos.y],
                                  width=float(m.group(1)) if m else 1500.0,
                                  wall_id=_nearest_wall_id(pos, walls, strips)))

    # 家具
    layer_names = [lay.dxf.name for lay in doc.layers]
    strict_furniture = any(R.FURNITURE_LAYER_RE.search(n) for n in layer_names)

    def _accept(layer):
        if R.FURNITURE_LAYER_RE.search(layer):
            return True
        return not strict_furniture and not R.MEP_LAYER_RE.search(layer)

    for ins in msp.query("INSERT"):
        layer, name = ins.dxf.layer, ins.dxf.name
        if not _accept(layer) or R.NOISE_BLOCK_RE.match(name) \
                or R.MEP_BLOCK_RE.search(name):
            continue
        ftype = _classify_type(name, layer)
        if ftype is None:
            continue
        world_size, center, ok = _measure_insert(ins, scale, doc)
        if ok and world_size is not None:
            local = _unrotate_size(world_size[0], world_size[1], ins.dxf.rotation)
        else:
            local = None
        fsize, measured = _furn_size(ftype, local)
        pos = list(center) if center else [ins.dxf.insert.x * scale, ins.dxf.insert.y * scale]
        furniture.append(Furniture(
            id=f"furn_{len(furniture)+1:03d}", type=ftype.type,
            position=pos, size=fsize,
            rotation=math.radians(ins.dxf.rotation), source="cad", measured=measured))

    # 6.1) 多段线/圆/SPLINE 家具（精装图固定家具常以轮廓绘制，非块）
    if strict_furniture:
        # 场景范围（中心校验）
        _bx = [p[0] for w in walls for p in w.polygon]
        _by = [p[1] for w in walls for p in w.polygon]
        for r in rooms:
            _bx += [p[0] for p in r.polygon]
            _by += [p[1] for p in r.polygon]
        _sx0, _sx1 = (min(_bx) - 2000, max(_bx) + 2000) if _bx else (-1e18, 1e18)
        _sy0, _sy1 = (min(_by) - 2000, max(_by) + 2000) if _by else (-1e18, 1e18)

        def _in_scene(px, py):
            return _sx0 <= px <= _sx1 and _sy0 <= py <= _sy1

        def _furn_layer_ok(layer):
            return R.FURNITURE_LAYER_RE.search(layer) or layer in semantic_furniture

        # 闭合多段线家具
        for e in msp:
            if e.dxftype() != "LWPOLYLINE" or not e.closed:  # type: ignore[attr-defined]
                continue
            if not _furn_layer_ok(e.dxf.layer):
                continue
            pts = [(p[0] * scale, p[1] * scale) for p in e.get_points()]  # type: ignore[attr-defined]
            if len(pts) < 3:
                continue
            poly = Polygon(pts)
            if not (POLY_FURN_AREA_MIN <= poly.area <= POLY_FURN_AREA_MAX):
                continue
            minx, miny, maxx, maxy = poly.bounds
            w, d = maxx - minx, maxy - miny
            ft2 = _classify_type("", e.dxf.layer) or R.FURNITURE_TYPES[-1]
            fsize2, measured2 = _furn_size(ft2, (w, d))
            cxy = ((minx + maxx) / 2.0, (miny + maxy) / 2.0)
            if not _in_scene(*cxy):
                continue
            furniture.append(Furniture(
                id=f"furn_{len(furniture)+1:03d}", type=ft2.type,
                position=[cxy[0], cxy[1]], size=fsize2, rotation=0.0,
                source="cad", measured=measured2))
        # 开放折线家具（dashed 床/沙发轮廓）
        for e in msp:
            if e.dxftype() != "LWPOLYLINE" or e.closed:  # type: ignore[attr-defined]
                continue
            if not _furn_layer_ok(e.dxf.layer):
                continue
            pts = [(p[0] * scale, p[1] * scale) for p in e.get_points()]  # type: ignore[attr-defined]
            if len(pts) < 3:
                continue
            try:
                poly = Polygon(pts)
            except Exception:  # noqa: BLE001
                continue
            if not poly.is_valid or not (POLY_FURN_AREA_MIN <= poly.area
                                         <= POLY_FURN_AREA_MAX):
                continue
            minx, miny, maxx, maxy = poly.bounds
            w_, d_ = maxx - minx, maxy - miny
            if max(w_, d_) > 0 and min(w_, d_) / max(w_, d_) < 0.12:
                continue
            ft2 = _classify_type("", e.dxf.layer) or R.FURNITURE_TYPES[-1]
            fsize2, measured2 = _furn_size(ft2, (w_, d_))
            cxy = ((minx + maxx) / 2.0, (miny + maxy) / 2.0)
            if not _in_scene(*cxy):
                continue
            if any(math.hypot(f.position[0] - cxy[0], f.position[1] - cxy[1]) < 300
                   for f in furniture):
                continue
            furniture.append(Furniture(
                id=f"furn_{len(furniture)+1:03d}", type=ft2.type,
                position=[cxy[0], cxy[1]], size=fsize2, rotation=0.0,
                source="cad", measured=measured2))
        # SPLINE 家具（弧线轮廓沙发/转椅）
        for spl in msp.query("SPLINE"):  # type: ignore[union-attr]
            if not _furn_layer_ok(spl.dxf.layer):
                continue
            pts = [(p[0] * scale, p[1] * scale) for p in spl.control_points]  # type: ignore[attr-defined]
            if len(pts) < 3:
                continue
            poly = Polygon(pts).convex_hull
            if not (POLY_FURN_AREA_MIN <= poly.area <= POLY_FURN_AREA_MAX):
                continue
            minx, miny, maxx, maxy = poly.bounds
            w_, d_ = maxx - minx, maxy - miny
            ft2 = _classify_type("", spl.dxf.layer) or R.FURNITURE_TYPES[-1]
            fsize2, measured2 = _furn_size(ft2, (w_, d_))
            cxy = ((minx + maxx) / 2.0, (miny + maxy) / 2.0)
            if not _in_scene(*cxy):
                continue
            if any(math.hypot(f.position[0] - cxy[0], f.position[1] - cxy[1]) < 300
                   for f in furniture):
                continue
            furniture.append(Furniture(
                id=f"furn_{len(furniture)+1:03d}", type=ft2.type,
                position=[cxy[0], cxy[1]], size=fsize2, rotation=0.0,
                source="cad", measured=measured2))
        # 圆形家具（茶几/圆桌）
        for cir in msp.query("CIRCLE"):
            if not _furn_layer_ok(cir.dxf.layer):
                continue
            r_ = cir.dxf.radius * scale
            if not (150.0 <= r_ <= 900.0):
                continue
            cx_ = cir.dxf.center.x * scale
            cy_ = cir.dxf.center.y * scale
            if not _in_scene(cx_, cy_):
                continue
            if any(math.hypot(f.position[0] - cx_, f.position[1] - cy_) < 300
                   for f in furniture):
                continue
            furniture.append(Furniture(
                id=f"furn_{len(furniture)+1:03d}", type="table",
                position=[cx_, cy_], size=[r_ * 2, r_ * 2, 450.0],
                rotation=0.0, source="cad", measured=True))

    # 6.5) 层高与墙厚
    floor_height = rep.floor_height_candidates[0] if rep.floor_height_candidates else 2800.0
    if not rep.floor_height_candidates:
        fallbacks.append("floor_height -> 2800 默认")
    thicknesses = [c[2] for c in centers]
    wall_t = max(set(thicknesses), key=thicknesses.count) if thicknesses else R.DEFAULT_WALL_T

    # 7) 质量指标
    room_union = unary_union([Polygon(r.polygon) for r in rooms]) if rooms else None
    if room_union is not None:
        hull = room_union.convex_hull
        tiling = room_union.area / hull.area if hull.area > 0 else 0.0
    else:
        tiling = 0.0
    open_total = len(doors) + len(windows)
    open_attached = (sum(1 for d in doors if d.wall_id)
                     + sum(1 for w in windows if w.wall_id))
    in_room = sum(1 for f in furniture
                  if any(Polygon(r.polygon).contains(Point(f.position)) for r in rooms))
    measured_n = sum(1 for f in furniture if f.measured)
    # 家具稀少提示：输入疑似系统图（精装图家具多为多段线，块提取少）
    if strict_furniture and 0 < len(furniture) < 15:
        fallbacks.append(f'家具实体稀少({len(furniture)}件)：输入可能为系统图，'
                         '完整布局请提供平面布置图/建筑平面图')

    quality = ParseQuality(
        rooms=len(rooms), tiling_ratio=round(tiling, 3), wall_segments=len(walls),
        openings_attached_ratio=round(open_attached / open_total, 3) if open_total else 1.0,
        furniture_count=len(furniture),
        furniture_measured_ratio=round(measured_n / len(furniture), 3) if furniture else 0.0,
        furniture_in_room_ratio=round(in_room / len(furniture), 3) if furniture else 0.0,
        notes=fallbacks)

    confidence = rep.confidence
    if not rooms:
        confidence -= 0.3
    if rooms and tiling < 0.5:
        confidence -= 0.1

    scene = SceneJSON(floor_height=floor_height, wall_thickness=wall_t,
                      walls=walls, doors=doors, windows=windows,
                      furniture=furniture, rooms=rooms, quality=quality)
    err = ToolError(code="PARSE_LOW_CONFIDENCE",
                    message="; ".join(fallbacks)) if confidence < 0.6 else None
    key = build_cache_key("parse_scene", str(dxf_path), str(sorted(wall_layers)))
    return ToolResult(ok=True, data=scene, error=err, cache_key=key, metrics=Metrics())
