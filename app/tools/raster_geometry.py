# app/tools/raster_geometry.py
"""栅格几何引擎：户型图片 → 墙线/房间/定标（纯 numpy，无深度学习依赖）。

用户方案（docs/agent-optimization-plan-2026-09.md）：
  带尺寸图 → 尺寸数字定标 → 精确 mm 坐标；
  无尺寸图 → 像素坐标体系 → 元素位置/大小。
墙提取用经典形态学（阈值→闭运算补门洞→连通域→矩形拟合），
参考 Raster-to-Vector (ICCV'17) 的非深度学习基线。
"""
import math
from dataclasses import dataclass, field

from app.tools.blender.imgstat import decode_png


@dataclass
class RasterScene:
    """栅格几何提取结果（像素坐标 + 可选 mm 定标）。"""
    width: int
    height: int
    wall_rects: list[tuple[int, int, int, int]] = field(default_factory=list)  # px (x0,y0,x1,y1)
    wall_mask: list[list[bool]] = field(default_factory=list)
    px_per_mm: float | None = None          # 定标比例（None=未定标）
    scale_refs: list[dict] = field(default_factory=list)  # 定标依据


# ── 基础：图存取 ──────────────────────────────────────────────────────────────

def load_gray(png_path: str) -> tuple[int, int, list[list[int]]]:
    """PNG → 灰度矩阵 (0-255)。"""
    w, h, bpp, pix = decode_png(png_path)
    gray = [[(pix[(y * w + x) * bpp]
              + pix[(y * w + x) * bpp + 1]
              + pix[(y * w + x) * bpp + min(bpp - 1, 2)]) // 3
             for x in range(w)] for y in range(h)]
    return w, h, gray


# ── 形态学（纯 numpy-free 列表实现，图幅 ≤2K 可接受） ──────────────────────────

def threshold(gray: list[list[int]], dark: int = 120) -> list[list[bool]]:
    """暗色像素 = 候选墙线（户型图墙通常为最深的粗线）。"""
    return [[px < dark for px in row] for row in gray]


def erode(mask: list[list[bool]], k: int = 2) -> list[list[bool]]:
    """腐蚀：k 邻域全真才真——去掉细线（标注/文字/家具线）。"""
    h, w = len(mask), len(mask[0])
    out = [[False] * w for _ in range(h)]
    for y in range(k, h - k):
        row = mask[y]
        for x in range(k, w - k):
            if row[x]:
                ok = True
                for dy in range(-k, k + 1):
                    if not all(mask[y + dy][x - k:x + k + 1]):
                        ok = False
                        break
                out[y][x] = ok
    return out


def dilate(mask: list[list[bool]], k: int = 2) -> list[list[bool]]:
    """膨胀：补门洞/窗洞的小缝隙（wall gap closing）。"""
    k = int(k)
    h, w = len(mask), len(mask[0])
    out = [[False] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            if mask[y][x]:
                for dy in range(-int(k), int(k) + 1):
                    yy = y + dy
                    if 0 <= yy < h:
                        for dx in range(-int(k), int(k) + 1):
                            xx = x + dx
                            if 0 <= xx < w:
                                out[yy][xx] = True
    return out


def connected_components(mask: list[list[bool]],
                         min_area: int = 200) -> list[dict]:
    """连通域（8 邻域 BFS）→ [{bbox, area}]，过滤小域（文字/噪点）。"""
    h, w = len(mask), len(mask[0])
    seen = [[False] * w for _ in range(h)]
    comps = []
    for sy in range(h):
        for sx in range(w):
            if mask[sy][sx] and not seen[sy][sx]:
                stack = [(sy, sx)]
                seen[sy][sx] = True
                x0 = x1 = sx
                y0 = y1 = sy
                area = 0
                while stack:
                    y, x = stack.pop()
                    area += 1
                    if x < x0: x0 = x
                    if x > x1: x1 = x
                    if y < y0: y0 = y
                    if y > y1: y1 = y
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            yy, xx = y + dy, x + dx
                            if 0 <= yy < h and 0 <= xx < w \
                                    and mask[yy][xx] and not seen[yy][xx]:
                                seen[yy][xx] = True
                                stack.append((yy, xx))
                if area >= min_area:
                    comps.append({"bbox": (x0, y0, x1, y1), "area": area})
    comps.sort(key=lambda c: -int(c["area"]))
    return comps


# ── 墙矩形化 ──────────────────────────────────────────────────────────────────

def wall_rects_from_mask(mask: list[list[bool]],
                         min_len: int = 30,
                         max_thick: int = 40) -> list[tuple[int, int, int, int]]:
    """连通域 → 墙矩形（轴对齐，细长判定：长≥min_len 且厚≤max_thick px）。

    非细长大域（家具色块/大面积填充）剔除。
    """
    rects = []
    for c in connected_components(mask):
        x0, y0, x1, y1 = c["bbox"]
        w_, h_ = x1 - x0 + 1, y1 - y0 + 1
        thick, long_ = min(w_, h_), max(w_, h_)
        if long_ >= min_len and thick <= max_thick:
            rects.append((x0, y0, x1, y1))
    return rects


# ── 定标：尺寸文字 → px/mm ────────────────────────────────────────────────────

def calibrate(scale_refs: list[dict]) -> float | None:
    """定标参照 → px_per_mm。

    scale_refs: [{"px_len": 两点像素距离, "mm_len": 标注真实长度}]
    取中位数（抗单点误读）。
    """
    ratios = [r["px_len"] / r["mm_len"] for r in scale_refs
              if r.get("px_len", 0) > 0 and r.get("mm_len", 0) > 0]
    if not ratios:
        return None
    ratios.sort()
    return ratios[len(ratios) // 2]


def to_mm(rect: tuple[int, int, int, int], px_per_mm: float,
          flip_y_height: int | None = None) -> tuple[float, float, float, float]:
    """像素矩形 → mm 矩形（y 可选翻转：图像 y 向下 → CAD y 向上）。"""
    x0, y0, x1, y1 = rect
    if flip_y_height:
        y0, y1 = flip_y_height - y1, flip_y_height - y0
    return (x0 / px_per_mm, y0 / px_per_mm, x1 / px_per_mm, y1 / px_per_mm)


# ── 主入口 ────────────────────────────────────────────────────────────────────

def extract_walls(png_path: str, dark: int = 120,
                  erode_k: int = 2, dilate_k: int = 2) -> RasterScene:
    """户型图片 → 墙矩形（像素坐标）。

    流程：灰度 → 暗阈值 → 腐蚀(去细线) → 膨胀(补门洞) → 连通域 → 细长矩形。
    """
    w, h, gray = load_gray(png_path)
    mask = threshold(gray, dark)
    mask = erode(mask, erode_k)
    mask = dilate(mask, dilate_k)
    rects = wall_rects_from_mask(mask)
    return RasterScene(width=w, height=h, wall_rects=rects,
                       wall_mask=mask)
