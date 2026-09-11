# app/tools/cad_sheets.py
"""布局（图纸空间）多视口 → 多张图采集与布置图识别。

机制：CAD 布局区的多张图 = 多个 VIEWPORT 各自冻结不同图层，显示同一模型空间
的不同图层组合（家具布置图/插座布置图/天花布置图…）。
- layout_sheet_views：枚举布局视口 → [(标签, 冻结图层集)]
- render_sheet_view：按冻结图层过滤渲染该视口的图
- pick_layout_view：确定性评分选"布置图"（家具/床类实体密度最高）
"""
import contextlib
import math
from collections import Counter
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from app.tools.cad_render import Canvas, layer_rgb


@dataclass
class SheetView:
    label: str                  # VP2…
    frozen_layers: set[str]     # 该视口冻结（不显示）的图层
    paper_box: tuple[float, float, float, float]


def layout_sheet_views(doc, layout_name: str | None = None) -> list[SheetView]:
    """枚举布局视口 → SheetView 列表（含各自冻结图层集）。"""
    if layout_name:
        lay = doc.layouts.get(layout_name)
    else:
        lay = None
        for name in doc.layout_names():
            if name != "Model" and name != "模型":
                lay = doc.layouts.get(name)
                break
        if lay is None:
            lay = doc.modelspace()
    views: list[SheetView] = []
    n = 0
    for e in lay:
        if e.dxftype() != "VIEWPORT":
            continue
        n += 1
        frozen: set[str] = set()
        with contextlib.suppress(Exception):
            frozen = set(e.frozen_layers)
        d = e.dxf
        box = (d.center.x - d.width / 2, d.center.y - d.height / 2,
               d.center.x + d.width / 2, d.center.y + d.height / 2)
        views.append(SheetView(
            label=f"VP{n}", frozen_layers=frozen, paper_box=box))
    return views


def count_furniture_entities(msp, frozen: set[str]) -> Counter:
    """统计未被冻结图层上的"家具语义"实体数（图层名含家具/洁具关键词）。"""
    import re
    pat = re.compile(r"(家具|furn|洁具|床|sofa|bed)", re.IGNORECASE)
    counts: Counter = Counter()
    for e in msp:
        with contextlib.suppress(Exception):
            layer = e.dxf.layer
            if layer in frozen:
                continue
            if pat.search(layer):
                counts[layer] += 1
    return counts


def pick_layout_view(
    doc, layout_name: str | None = None) -> tuple[SheetView | None, list[SheetView]]:
    """确定性选择布置图视口：家具语义实体未被冻结最多的视口。

    返回 (最佳视口, 全部视口评分列表)。
    """
    msp = doc.modelspace()
    views = layout_sheet_views(doc, layout_name)
    scored: list[tuple[int, int, int, SheetView]] = []
    for sv in views:
        counts = count_furniture_entities(msp, sv.frozen_layers)
        total = sum(counts.values())
        scored.append((total, len(sv.frozen_layers), -len(scored), sv))
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    ranked = [sv for _, _, _, sv in scored]
    best = ranked[0] if ranked and scored[0][0] > 0 else None
    return best, ranked


def _hatch_boundary_pts(e) -> list[tuple[float, float]]:
    """提取 HATCH 图元边界折线点（支持直线段和圆弧边）。"""
    pts: list[tuple[float, float]] = []
    with contextlib.suppress(Exception):
        for pth in e.paths:
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
                        a0 = math.radians(ed.start_angle)
                        a1 = math.radians(ed.end_angle)
                        if a1 <= a0:
                            a1 += 2 * math.pi
                        steps = max(8, int(abs(a1 - a0) / (math.pi / 8)))
                        for i in range(steps + 1):
                            a = a0 + (a1 - a0) * i / steps
                            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def render_sheet_view(doc, sv: SheetView, out_png: str | Path,
                      width_px: int = 1600,
                      extra_hatch_layers: set[str] | None = None
                      ) -> tuple[float, float, float, float]:
    """渲染该视口可见内容（冻结图层过滤）→ 布置图参考图。返回实际裁剪框。

    extra_hatch_layers: 即使被视口冻结，也强制渲染这些图层的 HATCH 边界（用于
    把地面材质填充叠加到家具布置图上，以区分卫生间/卧室区域）。
    """
    msp = doc.modelspace()
    frozen = sv.frozen_layers
    _extra = extra_hatch_layers or set()

    def visible(ent) -> bool:
        with contextlib.suppress(Exception):
            lay = ent.dxf.layer
            return lay not in frozen or lay in _extra
        return True

    xs, ys = [], []
    for e in msp:
        if not visible(e):
            continue
        with contextlib.suppress(Exception):
            t = e.dxftype()
            if t == "LINE":
                xs += [e.dxf.start.x, e.dxf.end.x]
                ys += [e.dxf.start.y, e.dxf.end.y]
            elif t == "LWPOLYLINE":
                for p in e.get_points():
                    xs.append(p[0])
                    ys.append(p[1])
            elif t == "CIRCLE":
                r_ = e.dxf.radius
                xs += [e.dxf.center.x - r_, e.dxf.center.x + r_]
                ys += [e.dxf.center.y - r_, e.dxf.center.y + r_]
    if not xs:
        raise ValueError("视口无可见内容")
    # 先用线条范围作为基准，HATCH 点只在此范围 20% 余量内才纳入 extent
    bx0, by0, bx1, by1 = min(xs), min(ys), max(xs), max(ys)
    bw, bh = bx1 - bx0, by1 - by0
    margin_x, margin_y = bw * 0.2, bh * 0.2
    for e in msp:
        if not visible(e):
            continue
        if e.dxftype() != "HATCH":
            continue
        with contextlib.suppress(Exception):
            for px, py in _hatch_boundary_pts(e):
                if (bx0 - margin_x <= px <= bx1 + margin_x and
                        by0 - margin_y <= py <= by1 + margin_y):
                    xs.append(px)
                    ys.append(py)
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    pad = (x1 - x0) * 0.02
    cv = Canvas(x0 - pad, y0 - pad, x1 + pad, y1 + pad, width_px)
    for e in msp:
        if not visible(e):
            continue
        t = e.dxftype()
        rgb = layer_rgb(e.dxf.layer)
        with contextlib.suppress(Exception):
            if t == "LINE":
                s_, en = e.dxf.start, e.dxf.end
                cv.line(s_.x, s_.y, en.x, en.y, rgb)
            elif t == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in e.get_points()]
                if e.closed and len(pts) >= 3:
                    pts.append(pts[0])
                for a, b in pairwise(pts):
                    cv.line(a[0], a[1], b[0], b[1], rgb)
            elif t == "INSERT":
                blk = doc.blocks.get(e.dxf.name)
                if blk is None:
                    continue
                with contextlib.suppress(Exception):
                    m = e.matrix44()
                for sub in blk:
                    st = sub.dxftype()
                    sub_rgb = layer_rgb(sub.dxf.layer)
                    with contextlib.suppress(Exception):
                        if st == "LINE":
                            s_, en = sub.dxf.start, sub.dxf.end
                            a, b = m.transform((s_.x, s_.y, 0)), m.transform((en.x, en.y, 0))
                            cv.line(a.x, a.y, b.x, b.y, sub_rgb)
                        elif st == "LWPOLYLINE":
                            pts = [(p[0], p[1]) for p in sub.get_points()]
                            if sub.closed and len(pts) >= 3:
                                pts.append(pts[0])
                            w = [m.transform((p[0], p[1], 0)) for p in pts]
                            for a, b in pairwise([(v.x, v.y) for v in w]):
                                cv.line(a[0], a[1], b[0], b[1], sub_rgb)
                        elif st == "CIRCLE":
                            r_ = sub.dxf.radius
                            c0 = m.transform((sub.dxf.center.x, sub.dxf.center.y, 0))
                            steps = max(16, int(r_ / 50))
                            pts = [(c0.x + r_ * math.cos(2 * math.pi * i / steps),
                                    c0.y + r_ * math.sin(2 * math.pi * i / steps))
                                   for i in range(steps + 1)]
                            for a, b in pairwise(pts):
                                cv.line(a[0], a[1], b[0], b[1], sub_rgb)
            elif t == "CIRCLE":
                r_ = e.dxf.radius
                cx_, cy_ = e.dxf.center.x, e.dxf.center.y
                steps = max(16, int(r_ / 50))
                pts = [(cx_ + r_ * math.cos(2 * math.pi * i / steps),
                        cy_ + r_ * math.sin(2 * math.pi * i / steps))
                       for i in range(steps + 1)]
                for a, b in pairwise(pts):
                    cv.line(a[0], a[1], b[0], b[1], rgb)
            elif t == "HATCH":
                hpts = [
                    (px, py) for px, py in _hatch_boundary_pts(e)
                    if (x0 - (x1-x0)*0.2 <= px <= x1 + (x1-x0)*0.2 and
                        y0 - (y1-y0)*0.2 <= py <= y1 + (y1-y0)*0.2)
                ]
                if len(hpts) >= 2:
                    closed_pts = hpts + [hpts[0]]
                    for a, b in pairwise(closed_pts):
                        cv.line(a[0], a[1], b[0], b[1], rgb)
    cv.save(str(out_png))
    return (x0, y0, x1, y1)
