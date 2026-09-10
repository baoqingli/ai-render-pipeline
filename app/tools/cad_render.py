# app/tools/cad_render.py
"""DXF 模型空间 → PNG 渲染（纯标准库，无 matplotlib 依赖）。

供 VLM 识别 Agent 与叠加对照工具共用：
- Canvas：世界坐标 → 像素画板
- 按图层语义着色（墙红/天花绿/家具蓝/门窗紫/完成面青/其他灰）
- 块遍历走手动矩阵链（virtual_entities 对含 HATCH 块静默失败，见白模修正方案 §7）
"""
import contextlib
import math
import re
import struct
import zlib
from itertools import pairwise
from pathlib import Path


def _chunk(typ: bytes, data: bytes) -> bytes:
    c = struct.pack(">I", len(data)) + typ + data
    return c + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)


class Canvas:
    def __init__(self, x0: float, y0: float, x1: float, y1: float, width_px: int = 1600):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.w = width_px
        self.h = max(int(width_px * (y1 - y0) / max(x1 - x0, 1e-6)), 1)
        self.img = bytearray(b"\xff\xff\xff" * self.w * self.h)

    def _put(self, x: float, y: float, rgb: tuple) -> None:
        px = int((x - self.x0) / (self.x1 - self.x0) * (self.w - 1))
        py = int((self.y1 - y) / (self.y1 - self.y0) * (self.h - 1))
        if 0 <= px < self.w and 0 <= py < self.h:
            o = (py * self.w + px) * 3
            self.img[o:o + 3] = bytes(rgb)

    def line(self, x1: float, y1: float, x2: float, y2: float,
             rgb: tuple, step: float = 15.0, bold: int = 1) -> None:
        n = max(int(max(abs(x2 - x1), abs(y2 - y1)) / step), 1)
        for i in range(n + 1):
            t = i / n
            x, y = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
            for dx in range(bold):
                for dy in range(bold):
                    self._put(x + dx * 30, y + dy * 30, rgb)

    def save(self, path: str | Path) -> None:
        raw = b"".join(b"\x00" + bytes(self.img[y * self.w * 3:(y + 1) * self.w * 3])
                       for y in range(self.h))
        png = (b"\x89PNG\r\n\x1a\n"
               + _chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
               + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b""))
        Path(path).write_bytes(png)


def layer_rgb(name: str) -> tuple:
    if re.search(r"(墙|承重|柱|砌筑|墙体)", name):
        return (180, 0, 0)
    if re.search(r"(天花|ceiling)", name, re.IGNORECASE):
        return (0, 150, 0)
    if re.search(r"(家具|furn|洁具)", name, re.IGNORECASE):
        return (30, 80, 230)
    if re.search(r"(门|窗|GL-)", name):
        return (200, 60, 160)
    if re.search(r"(完成面|木饰面|台面)", name):
        return (0, 150, 150)
    return (185, 185, 185)


def _hatch_pts(h) -> list[tuple[float, float]]:
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


def _xf(chain: tuple, p) -> tuple[float, float]:
    px, py = p[0], p[1]
    for m in reversed(chain):
        v = m.transform((px, py, 0.0))
        px, py = v.x, v.y
    return px, py


def _text_marker(cv: Canvas, center: tuple, size: float, rgb: tuple) -> None:
    """文字标注渲染为小方框标记（视觉锚点；文字内容经提示词表格提供给 VLM）。"""
    px, py = center
    w = max(size * 0.6, 20.0)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if abs(dx) != 1 and abs(dy) != 1:
                continue
            cv._put(px + dx * w, py + dy * w, rgb)


def _walk_block(doc, blk, cv: Canvas, chain: tuple, depth: int,
                inherited: str) -> None:
    """遍历块定义实体，手动矩阵链变换后绘制（含嵌套块与文字标记）。"""
    if depth > 6:
        return
    for sub in blk:
        t = sub.dxftype()
        layer = sub.dxf.layer if sub.dxf.layer != "0" else inherited
        if t == "INSERT":
            sub_blk = doc.blocks.get(sub.dxf.name)
            if sub_blk is not None:
                with contextlib.suppress(Exception):
                    sub_m = sub.matrix44()
                _walk_block(doc, sub_blk, cv, (*chain, sub_m), depth + 1, layer)
            continue
        if t in ("TEXT", "MTEXT"):
            with contextlib.suppress(Exception):
                ins = sub.dxf.insert
                w = _xf(chain, (ins.x, ins.y))
                hgt = float(sub.dxf.height) if sub.dxf.hasattr("height") else 100.0
                _text_marker(cv, w, hgt * 0.6, (60, 60, 220))
            continue
        rgb = layer_rgb(layer)
        with contextlib.suppress(Exception):
            if t == "LINE":
                s_, en = sub.dxf.start, sub.dxf.end
                a, b = _xf(chain, (s_.x, s_.y)), _xf(chain, (en.x, en.y))
                cv.line(a[0], a[1], b[0], b[1], rgb)
            elif t == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in sub.get_points()]
                if sub.closed and len(pts) >= 3:
                    pts.append(pts[0])
                for a, b in pairwise(pts):
                    a2, b2 = _xf(chain, a), _xf(chain, b)
                    cv.line(a2[0], a2[1], b2[0], b2[1], rgb)
            elif t == "HATCH":
                pts = _hatch_pts(sub)
                if len(pts) >= 3:
                    pts.append(pts[0])
                    for a, b in pairwise(pts):
                        a2, b2 = _xf(chain, a), _xf(chain, b)
                        cv.line(a2[0], a2[1], b2[0], b2[1], rgb)


def _walk_entity(e, cv: Canvas) -> None:
    t = e.dxftype()
    if t in ("TEXT", "MTEXT"):
        with contextlib.suppress(Exception):
            ins = e.dxf.insert
            hgt = float(e.dxf.height) if e.dxf.hasattr("height") else 100.0
            c = _xf((), (ins.x, ins.y))
            _text_marker(cv, c, hgt * 0.6, (60, 60, 220))
        return
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
        elif t == "HATCH":
            pts = _hatch_pts(e)
            if len(pts) >= 3:
                pts.append(pts[0])
                for a, b in pairwise(pts):
                    cv.line(a[0], a[1], b[0], b[1], rgb)
        elif t == "CIRCLE":
            r_ = e.dxf.radius
            cx_, cy_ = e.dxf.center.x, e.dxf.center.y
            steps = max(16, int(r_ / 50))
            pts = [(cx_ + r_ * math.cos(2 * math.pi * i / steps),
                    cy_ + r_ * math.sin(2 * math.pi * i / steps))
                   for i in range(steps + 1)]
            for a, b in pairwise(pts):
                cv.line(a[0], a[1], b[0], b[1], rgb)


def render_modelspace(doc, view: tuple[float, float, float, float],
                      width_px: int, out_path: str | Path) -> None:
    """渲染模型空间指定裁剪框 → PNG（含块展开）。"""
    msp = doc.modelspace()
    x0, y0, x1, y1 = view
    pad = (x1 - x0) * 0.02
    cv = Canvas(x0 - pad, y0 - pad, x1 + pad, y1 + pad, width_px)
    for e in msp:
        if e.dxftype() == "INSERT":
            blk = doc.blocks.get(e.dxf.name)
            if blk is not None:
                with contextlib.suppress(Exception):
                    m = e.matrix44()
                _walk_block(doc, blk, cv, (m,), 0, e.dxf.layer)
            continue
        _walk_entity(e, cv)
    cv.save(str(out_path))


def model_extent(doc) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for e in doc.modelspace():
        with contextlib.suppress(Exception):
            if e.dxftype() == "LINE":
                xs += [e.dxf.start.x, e.dxf.end.x]
                ys += [e.dxf.start.y, e.dxf.end.y]
            elif e.dxftype() == "LWPOLYLINE":
                for p in e.get_points():
                    xs.append(p[0])
                    ys.append(p[1])
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))
