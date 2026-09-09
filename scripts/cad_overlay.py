# scripts/cad_overlay.py
"""CAD 平面 vs 解析结果 叠加对照图（白模正确性的可证伪检验，§4.5 维护纪律）。

用法:
  uv run python scripts/cad_overlay.py --dxf <converted.dxf> [--scene scene.json] --out overlay.png

输出两张:
  <out>          CAD 原图（按图层着色，含块展开）+ 解析结果叠加（红=墙 蓝=房间 绿=门 紫=窗）
  <out>.plan.png 仅 CAD 原图
"""
import argparse
import contextlib
import struct
import sys
import zlib
from itertools import pairwise
from pathlib import Path

import ezdxf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools.cad import rules as R
from app.tools.cad.parse import parse_scene


def _chunk(typ: bytes, data: bytes) -> bytes:
    c = struct.pack(">I", len(data)) + typ + data
    return c + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)


class Canvas:
    def __init__(self, x0, y0, x1, y1, width_px=1600):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.w = width_px
        self.h = max(int(width_px * (y1 - y0) / max(x1 - x0, 1e-6)), 1)
        self.img = bytearray(b"\xff\xff\xff" * self.w * self.h)

    def _put(self, x, y, rgb):
        px = int((x - self.x0) / (self.x1 - self.x0) * (self.w - 1))
        py = int((self.y1 - y) / (self.y1 - self.y0) * (self.h - 1))
        if 0 <= px < self.w and 0 <= py < self.h:
            o = (py * self.w + px) * 3
            self.img[o:o + 3] = bytes(rgb)

    def line(self, x1, y1, x2, y2, rgb, step=15.0, bold=1):
        n = max(int(max(abs(x2 - x1), abs(y2 - y1)) / step), 1)
        for i in range(n + 1):
            t = i / n
            x, y = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
            for dx in range(bold):
                for dy in range(bold):
                    self._put(x + dx * 30, y + dy * 30, rgb)

    def save(self, path: str) -> None:
        raw = b"".join(b"\x00" + bytes(self.img[y * self.w * 3:(y + 1) * self.w * 3])
                       for y in range(self.h))
        png = (b"\x89PNG\r\n\x1a\n"
               + _chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
               + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b""))
        Path(path).write_bytes(png)


def _layer_rgb(name: str):
    if R.WALL_LAYER_RE.search(name) or R.WALL_FILL_LAYER_RE.search(name):
        return (180, 0, 0)
    if R.CEILING_LAYER_RE.search(name):
        return (0, 150, 0)
    if R.FURNITURE_LAYER_RE.search(name):
        return (30, 80, 230)
    if R.DOOR_HINT_RE.search(name) or R.WINDOW_HINT_RE.search(name):
        return (200, 60, 160)
    if R.ROOM_LAYER_RE.search(name):
        return (0, 150, 150)
    return (185, 185, 185)


def _hatch_pts(h):
    import math
    pts = []
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
                    a0, a1 = ed.start_angle, ed.end_angle
                    n = max(int(abs(a1 - a0) / 0.25), 2)
                    for k in range(n + 1):
                        a = math.radians(a0 + (a1 - a0) * k / n)
                        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _walk(e, cv: Canvas, depth: int = 0) -> None:
    if depth > 6:
        return
    t = e.dxftype()
    if t == "INSERT":
        with contextlib.suppress(Exception):
            for sub in e.virtual_entities():
                _walk(sub, cv, depth + 1)
    elif t == "LINE":
        s, en = e.dxf.start, e.dxf.end
        cv.line(s.x, s.y, en.x, en.y, _layer_rgb(e.dxf.layer))
    elif t == "LWPOLYLINE":
        rgb = _layer_rgb(e.dxf.layer)
        pts = [(p[0], p[1]) for p in e.get_points()]
        if e.closed and len(pts) >= 3:
            pts.append(pts[0])
        for a, b in pairwise(pts):
            cv.line(a[0], a[1], b[0], b[1], rgb)
    elif t == "HATCH":
        rgb = _layer_rgb(e.dxf.layer)
        pts = _hatch_pts(e)
        for a, b in pairwise(pts):
            cv.line(a[0], a[1], b[0], b[1], rgb)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dxf", required=True)
    ap.add_argument("--scene", help="scene.json；缺省时重新解析")
    ap.add_argument("--out", default="overlay.png")
    args = ap.parse_args()

    doc = ezdxf.readfile(args.dxf)
    msp = doc.modelspace()
    if args.scene:
        import json
        with Path(args.scene).open(encoding="utf-8") as fh:
            scene = json.load(fh)
        geo_walls = scene["walls"]
        geo_rooms = scene["rooms"]
        geo_doors = [(d["position"],) for d in scene["doors"]]
        geo_windows = [(w["position"],) for w in scene["windows"]]
        pad = 0.0
    else:
        raw = parse_scene(args.dxf).data
        assert raw is not None, "解析失败"
        geo_walls = [{"polygon": w.polygon} for w in raw.walls]
        geo_rooms = [{"polygon": r.polygon} for r in raw.rooms]
        geo_doors = [(d.position,) for d in raw.doors]
        geo_windows = [(w.position,) for w in raw.windows]
        pad = 3000.0

    xs = [p[0] for w in geo_walls for p in w["polygon"]] \
        + [p[0] for r in geo_rooms for p in r["polygon"]]
    ys = [p[1] for w in geo_walls for p in w["polygon"]] \
        + [p[1] for r in geo_rooms for p in r["polygon"]]
    x0, y0, x1, y1 = min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad

    plan = Canvas(x0, y0, x1, y1)
    for e in msp:
        _walk(e, plan)
    plan.save(str(Path(args.out).with_suffix("").__str__() + ".plan.png"))

    both = Canvas(x0, y0, x1, y1)
    for e in msp:
        _walk(e, both)
    for r in geo_rooms:
        pts = r["polygon"] + [r["polygon"][0]]
        for a, b in pairwise(pts):
            both.line(a[0], a[1], b[0], b[1], (30, 80, 230), bold=3)
    for w in geo_walls:
        pts = w["polygon"] + [w["polygon"][0]]
        for a, b in pairwise(pts):
            both.line(a[0], a[1], b[0], b[1], (230, 30, 30), bold=2)
    for (pos,) in geo_doors:
        both.line(pos[0] - 200, pos[1], pos[0] + 200, pos[1], (0, 160, 0), bold=4)
    for (pos,) in geo_windows:
        both.line(pos[0] - 250, pos[1], pos[0] + 250, pos[1], (160, 0, 200), bold=4)
    both.save(args.out)
    print(f"written: {args.out} / {Path(args.out).with_suffix('').__str__()}.plan.png")


if __name__ == "__main__":
    main()
