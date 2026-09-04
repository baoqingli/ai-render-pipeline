# app/tools/cad/inspect.py
import re
from pathlib import Path

import ezdxf

from app.models.cad_report import BlockStat, CadReport, LayerStat, TextNote

WALL_LAYER_RE = re.compile(r"(wall|墙|a-wall|arch)", re.IGNORECASE)
HEIGHT_RE = re.compile(r"层高\s*(\d{3,4})")

LINE_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE"}
TEXT_TYPES = {"TEXT", "MTEXT"}


def count_proxies(msp) -> int:
    return sum(1 for e in msp if e.dxftype() == "ACAD_PROXY_ENTITY")


def _extent(msp) -> float:
    xs, ys = [], []
    for e in msp:
        if e.dxftype() == "LINE":
            xs += [e.dxf.start.x, e.dxf.end.x]
            ys += [e.dxf.start.y, e.dxf.end.y]
    if not xs:
        return 0.0
    return max(max(xs) - min(xs), max(ys) - min(ys))


def _xy(v) -> list[float]:
    # Vec3 取 [x, y]：Cython 加速版 ezdxf.acc 的 Vec3 不支持切片下标
    return [float(v[0]), float(v[1])]


def inspect_dxf(dxf_path: str | Path) -> CadReport:
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    layers: dict[str, LayerStat] = {}
    blocks: dict[tuple[str, str], BlockStat] = {}
    notes: list[TextNote] = []
    proxies = 0

    def stat(name: str) -> LayerStat:
        return layers.setdefault(name, LayerStat(name=name))

    for e in msp:
        t, layer = e.dxftype(), e.dxf.layer
        s = stat(layer)
        if t == "ACAD_PROXY_ENTITY":
            proxies += 1
            s.other_count += 1
        elif t == "LINE":
            s.line_count += 1
        elif t in ("LWPOLYLINE", "POLYLINE"):
            s.polyline_count += 1
        elif t in TEXT_TYPES:
            s.text_count += 1
            content = e.dxf.text if t == "TEXT" else e.text  # type: ignore[attr-defined]
            # TEXT 默认对齐时 align_point 未设置（None），真实位置在 insert；
            # MTEXT 位置恒在 insert
            pos = _xy(e.dxf.insert) if t == "MTEXT" else _xy(e.dxf.align_point or e.dxf.insert)
            notes.append(TextNote(layer=layer, content=content.strip(), position=pos))
        elif t == "INSERT":
            s.insert_count += 1
            key = (e.dxf.name, layer)
            b = blocks.setdefault(key, BlockStat(name=e.dxf.name, layer=layer, insert_count=0))
            b.insert_count += 1
        else:
            s.other_count += 1

    heights = [float(m.group(1)) for n in notes if (m := HEIGHT_RE.search(n.content))]
    ext = _extent(msp)
    unit: str = "mm" if ext > 1000 or ext == 0 else "m"
    wall_candidates = [n for n, s in layers.items()
                       if WALL_LAYER_RE.search(n) and (s.line_count + s.polyline_count) >= 4]
    confidence = 1.0
    if not wall_candidates:
        confidence -= 0.4
    if not heights:
        confidence -= 0.1
    if proxies:
        confidence -= 0.3
    if ext == 0:
        confidence -= 0.2
    return CadReport(layers=sorted(layers.values(), key=lambda s: -s.line_count - s.polyline_count),
                     blocks=list(blocks.values()), proxy_entity_count=proxies,
                     text_annotations=notes, floor_height_candidates=heights,
                     unit_guess=unit,  # type: ignore[arg-type]
                     confidence=round(confidence, 2), wall_layer_candidates=wall_candidates)
