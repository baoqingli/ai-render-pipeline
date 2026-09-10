# app/tools/cad/dimension_walls.py
"""尺寸链驱动墙体放置：DIMENSION defpoints → 精确墙线。

原理：线性尺寸标注从一个墙面量到另一个墙面，defpoint2/defpoint3 落在
被测墙面上——水平尺寸给出竖向墙的 x 坐标，竖向尺寸给出横向墙的 y 坐标。
多次被引用的坐标 = 高置信墙线。这是 CAD 制图的原生读法（人跟尺寸链走）。

比图层扫描/VLM 分区矩形精确一个量级（defpoints 是毫米级设计坐标）。
"""
import contextlib
from dataclasses import dataclass

from shapely.geometry import Polygon


@dataclass
class WallLine:
    """一条轴对齐墙线（竖向 x=const 或横向 y=const）。"""
    axis: str                  # "v" | "h"
    coord: float               # x（v）或 y（h）
    refs: int = 1              # 被 defpoints 引用次数（置信度）
    span: tuple[float, float] = (0.0, 0.0)   # 沿线延伸范围（来自引用 defpoints）
    thickness: float = 200.0


def _dim_class(d) -> str:
    """标注方向：h（水平）/ v（竖向）/ x（斜向/其他）。"""
    with contextlib.suppress(Exception):
        dp2, dp3 = d.dxf.defpoint2, d.dxf.defpoint3
        dx, dy = abs(dp3.x - dp2.x), abs(dp3.y - dp2.y)
        if max(dx, dy) < 1:
            return "x"
        if dx > dy * 3:
            return "h"
        if dy > dx * 3:
            return "v"
    return "x"


def extract_wall_lines(doc, min_measure: float = 400.0,
                       cluster_tol: float = 200.0) -> tuple[list[WallLine], list[WallLine]]:
    """从 DIMENSION defpoints 提取墙线（竖向 x 线 + 横向 y 线）。

    返回 (v_lines, h_lines)，按引用次数降序。
    """
    msp = doc.modelspace()
    v_acc: list[tuple[float, float, float]] = []   # (x, y1, y2)
    h_acc: list[tuple[float, float, float]] = []   # (y, x1, x2)

    for d in msp.query("DIMENSION"):
        with contextlib.suppress(Exception):
            m = abs(d.get_measurement() or 0)
            if m < min_measure:
                continue
            # 墙厚标注（短尺寸）提供厚度证据
            dp2, dp3 = d.dxf.defpoint2, d.dxf.defpoint3
            # 过滤未设默认值（0,0）——真实图纸坐标在 22 万/负 37 万量级
            if abs(dp2.x) < 1000 and abs(dp2.y) < 1000:
                continue
            if abs(dp3.x) < 1000 and abs(dp3.y) < 1000:
                continue
            cls = _dim_class(d)
            if cls == "h":
                # 水平尺寸：两端是竖向墙 → x 墙线，span 为该尺寸的 y 邻域
                y = (dp2.y + dp3.y) / 2
                v_acc.append((dp2.x, y, y))
                v_acc.append((dp3.x, y, y))
                if m <= 800:                       # 墙厚尺寸
                    h_acc.append((dp2.y, min(dp2.x, dp3.x), max(dp2.x, dp3.x)))
            elif cls == "v":
                x = (dp2.x + dp3.x) / 2
                h_acc.append((dp2.y, x, x))
                h_acc.append((dp3.y, x, x))
                if m <= 800:
                    v_acc.append((dp2.x, min(dp2.y, dp3.y), max(dp2.y, dp3.y)))

    def _cluster(acc, tol):
        """坐标聚类 → WallLine（引用计数 = 置信度）。"""
        lines: list[WallLine] = []
        for coord, s1, s2 in sorted(acc):
            hit = None
            for ln in lines:
                if abs(ln.coord - coord) <= tol:
                    hit = ln
                    break
            if hit is None:
                lines.append(WallLine(axis="v" if coord in [a[0] for a in acc[:0]] else "?",
                                      coord=coord, refs=1,
                                      span=(min(s1, s2), max(s1, s2))))
                lines[-1].axis = "v"
            else:
                hit.refs += 1
                lo, hi = hit.span
                hit.span = (min(lo, s1, s2), max(hi, s1, s2))
        return lines

    # 分别聚类（axis 在构造后修正）
    v_lines = _cluster_with_axis("v", v_acc, cluster_tol)
    h_lines = _cluster_with_axis("h", h_acc, cluster_tol)
    v_lines.sort(key=lambda wl: -wl.refs)
    h_lines.sort(key=lambda wl: -wl.refs)
    return v_lines, h_lines


def _cluster_with_axis(axis: str, acc: list[tuple], tol: float) -> list[WallLine]:
    lines: list[WallLine] = []
    for coord, s1, s2 in sorted(acc):
        hit = None
        for ln in lines:
            if abs(ln.coord - coord) <= tol:
                hit = ln
                break
        if hit is None:
            lines.append(WallLine(axis=axis, coord=coord, refs=1,
                                  span=(min(s1, s2), max(s1, s2))))
        else:
            hit.refs += 1
            lo, hi = hit.span
            hit.span = (min(lo, s1, s2), max(hi, s1, s2))
    return lines


def build_dimension_walls(doc, min_refs: int = 2,
                          default_t: float = 200.0) -> list[Polygon]:
    """尺寸链墙线 → 墙多边形列表。

    外墙 = 引用最多的边界线（min/max 坐标）；内墙 = refs ≥ min_refs 的线。
    墙矩形范围：跨全建筑内域（保守）；外墙圈定 bbox。
    """
    v_lines, h_lines = extract_wall_lines(doc)
    if not v_lines or not h_lines:
        return []

    # 建筑内域：所有高置信线的外包
    xs = [ln.coord for ln in v_lines if ln.refs >= min_refs]
    ys = [ln.coord for ln in h_lines if ln.refs >= min_refs]
    if not xs or not ys:
        return []
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    # 外域（外墙外面）：放宽一个墙厚
    ext = 300.0

    polys: list[Polygon] = []

    def _rect(a0, a1, b0, b1):
        return Polygon([(a0, b0), (a1, b0), (a1, b1), (a0, b1)])

    # 外墙四条（全跨度，厚 default_t）
    t = default_t
    polys.append(_rect(x0 - t, x0 + t, y0 - ext, y1 + ext))                # 西外墙
    polys.append(_rect(x1 - t, x1 + t, y0 - ext, y1 + ext))                # 东外墙
    polys.append(_rect(x0 - t, x1 + t, y0 - t, y0 + t))                    # 南外墙
    polys.append(_rect(x0 - t, x1 + t, y1 - t, y1 + t))                    # 北外墙

    # 内墙：refs ≥ min_refs 且不在外墙位置（容差 400）
    for ln in v_lines:
        if ln.refs < min_refs or abs(ln.coord - x0) < 400 or abs(ln.coord - x1) < 400:
            continue
        # 竖向内墙：span 有实测范围用之，否则跨全高
        lo = ln.span[0] if ln.span[0] != ln.span[1] else y0
        hi = ln.span[1] if ln.span[0] != ln.span[1] else y1
        lo, hi = max(lo, y0), min(hi, y1)
        polys.append(_rect(ln.coord - t / 2, ln.coord + t / 2, lo, hi))
    for ln in h_lines:
        if ln.refs < min_refs or abs(ln.coord - y0) < 400 or abs(ln.coord - y1) < 400:
            continue
        lo = ln.span[0] if ln.span[0] != ln.span[1] else x0
        hi = ln.span[1] if ln.span[0] != ln.span[1] else x1
        lo, hi = max(lo, x0), min(hi, x1)
        polys.append(_rect(lo, hi, ln.coord - t / 2, ln.coord + t / 2))

    return polys
