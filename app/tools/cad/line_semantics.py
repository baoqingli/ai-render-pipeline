# app/tools/cad/line_semantics.py
"""线条语义分类器：多信号投票判定 DXF 线条的建筑语义。

解决的核心问题：DWG 中的线条是语义歧义的——同一根线可能是墙边/家具轮廓/
标注辅助线/管网线。仅靠图层名正则会误判（非墙图层的线被当墙、家具轮廓
被当隔墙）。本模块用多信号投票制：墙的判定需要 ≥2 个独立信号交叉确认。

信号清单（强度降序）：
  LAYER_SEMANTICS  图层语义（VLM 判定或正则命中墙体关键词）
  CLOSED_BAND      闭合细长条带（厚 40-600，长 ≥400，矩形度 ≥0.5）
  PARALLEL_PAIR    平行线对（间距 80-600，重叠率 ≥0.5，同层或邻层）
  BLOCK_TAG        所在块名含墙体关键词
  HATCH_FILL       HATCH 填充且图案为实体色/斜线（墙的典型画法）
反信号（一票否决）：
  IN_FURNITURE_BLOCK  实体在家具语义块内部
  ON_ANNOTATION_LAYER 图层名为标注/文字/尺寸
  ISOLATED_SHORT      孤立短线（<400）且无平行伴线
"""
import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LineFeature:
    """单条线条的几何与语义特征。"""
    entity: Any                # ezdxf 实体引用
    layer: str
    bbox: tuple[float, float, float, float]  # minx, miny, maxx, maxy
    length: float              # 线长或周长
    thickness: float           # 闭合轮廓的短边（开放线=0）
    is_closed: bool
    signals: set[str] = field(default_factory=set)
    role: str = "unknown"      # wall|furniture|annotation|grid|opening|pipe|unknown
    confidence: float = 0.0


def _bbox_of(e) -> tuple[float, float, float, float] | None:
    """实体的世界坐标 bbox（含 LWPOLYLINE/LINE）。"""
    with contextlib_a():
        if e.dxftype() == "LINE":
            s_, en = e.dxf.start, e.dxf.end
            return (min(s_.x, en.x), min(s_.y, en.y),
                    max(s_.x, en.x), max(s_.y, en.y))
        if e.dxftype() == "LWPOLYLINE":
            pts = list(e.get_points())
            if not pts:
                return None
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            return (min(xs), min(ys), max(xs), max(ys))
    return None


def contextlib_a():
    return __import__("contextlib").suppress(Exception)


def _seg_angle(seg: tuple) -> float:
    """线段角度（0-180°）。"""
    dx = seg[1][0] - seg[0][0]
    dy = seg[1][1] - seg[0][1]
    return math.degrees(math.atan2(dy, dx)) % 180.0



def detect_parallel_pairs(
    lines: list[tuple[float, float, float, float, Any]],
    min_gap: float = 80.0, max_gap: float = 600.0) -> list[tuple]:
    """检测平行线对（墙体双线的确定性特征）。

    lines: [(x1, y1, x2, y2, 引用)] 列表。
    返回 [(线段1, 线段2, 间距)]。
    """
    import math as _m
    pairs = []
    n = len(lines)
    for i in range(n):
        x1a, y1a, x2a, y2a, _ref_a = lines[i]
        dx1, dy1 = x2a - x1a, y2a - y1a
        La = _m.hypot(dx1, dy1)
        if La < 200:
            continue
        for j in range(i + 1, n):
            x1b, y1b, x2b, y2b, _ref_b = lines[j]
            dx2, dy2 = x2b - x1b, y2b - y1b
            Lb = _m.hypot(dx2, dy2)
            if Lb < 200:
                continue
            cross = dx1 * dy2 - dy1 * dx2
            sin_ang = abs(cross) / (La * Lb) if La * Lb > 0 else 1.0
            if sin_ang > 0.09:           # 夹角 > ~5°
                continue
            # b 中点到 a 所在直线的法向距离
            mx, my = (x1b + x2b) / 2, (y1b + y2b) / 2
            nx, ny = -dy1 / La, dx1 / La
            gap = abs((mx - x1a) * nx + (my - y1a) * ny)
            if min_gap <= gap <= max_gap:
                pairs.append((lines[i], lines[j], gap))
    return pairs


