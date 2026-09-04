# app/tools/cad/parse.py
"""DXF → SceneJSON：墙体中心线缓冲成条带、polygonize 出房间、块引用成门窗、标注取名。"""
import re
from itertools import pairwise
from pathlib import Path

import ezdxf
from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize, unary_union

from app.models.cad_report import CadReport
from app.models.scene import Door, Furniture, Room, SceneJSON, Wall, Window
from app.models.tooling import Metrics, ToolError, ToolResult
from app.tools.cache import build_cache_key
from app.tools.cad.geometry import Centerline, Vec, pair_wall_segments
from app.tools.cad.inspect import inspect_dxf

WIDTH_RE = re.compile(r"(\d{3,4})")
ROOM_NAME_RE = re.compile(r"(室|厅|卧|厨|卫|阳台)")
DEFAULT_WALL_T = 200.0
MIN_ROOM_AREA_MM2 = 1_000_000      # 1 m²
MAX_ROOM_AREA_MM2 = 1_000_000_000  # 1000 m²（语料实测 8e6/16e6，均在界内）


def _segments_from(msp, layers: set[str]) -> list[tuple[Vec, Vec]]:
    segs = []
    for e in msp:
        if e.dxf.layer not in layers:
            continue
        if e.dxftype() == "LINE":
            segs.append(((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)))
        elif e.dxftype() == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in e.get_points()]
            # 闭合多段线：get_points 不重复首点，须补回末点→首点闭合边，
            # 否则中心线网络断开、polygonize 房间不闭合（ODA 转换图纸墙即此形态）
            if e.closed and len(pts) >= 3:
                pts.append(pts[0])
            segs += list(pairwise(pts))
    return segs


def _strip_polygon(c: Centerline) -> list[list[float]]:
    (x1, y1), (x2, y2), t = c
    ls = LineString([(x1, y1), (x2, y2)]).buffer(t / 2, cap_style=2, join_style=2)
    return [[float(x), float(y)] for x, y in ls.exterior.coords]


def _nearest_wall_id(
    point: Point, walls: list[Wall], strips: dict[str, BaseGeometry]
) -> str | None:
    best, best_d = None, None
    for w in walls:
        d = point.distance(strips[w.id])
        if best_d is None or d < best_d:
            best, best_d = w.id, d
    return best


def parse_scene(dxf_path: str | Path, report: CadReport | None = None) -> ToolResult[SceneJSON]:
    rep = report or inspect_dxf(dxf_path)
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    fallbacks: list[str] = []
    scale = 1000.0 if rep.unit_guess == "m" else 1.0   # 统一归一 mm

    wall_layers = set(rep.wall_layer_candidates) or {"WALL"}
    segs = [((a[0] * scale, a[1] * scale), (b[0] * scale, b[1] * scale))
            for a, b in _segments_from(msp, wall_layers)]
    centers = pair_wall_segments(segs, default_thickness=DEFAULT_WALL_T)
    walls = [Wall(id=f"wall_{i+1:03d}", polygon=_strip_polygon(c)) for i, c in enumerate(centers)]
    strips: dict[str, BaseGeometry] = {}
    for i, w in enumerate(walls):
        c = centers[i]
        strips[w.id] = LineString([(c[0][0], c[0][1]), (c[1][0], c[1][1])]).buffer(c[2] / 2)

    # 房间：中心线网络 polygonize。语料实测：内墙中心线端点落在底/顶墙中心线中段
    # （T 型 junction），直接 polygonize 只出外环 1 个面；必须先 unary_union 结点化
    # 才能切出 2 个房间。
    center_lines = [LineString([(c[0][0], c[0][1]), (c[1][0], c[1][1])]) for c in centers]
    noded = unary_union(center_lines)
    room_polys = [p for p in polygonize(getattr(noded, "geoms", [noded]))
                  if MIN_ROOM_AREA_MM2 <= p.area <= MAX_ROOM_AREA_MM2]
    rooms = []
    for i, poly in enumerate(sorted(room_polys, key=lambda p: -p.area)):
        name = None
        for note in rep.text_annotations:
            if ROOM_NAME_RE.search(note.content) and poly.contains(Point(note.position[0] * scale,
                                                                         note.position[1] * scale)):
                name = note.content
                break
        rooms.append(Room(id=f"room_{i+1:03d}", name=name,
                          polygon=[[float(x), float(y)] for x, y in poly.exterior.coords]))

    # 门窗家具：块引用。分类看图层+块名（语料窗块 C_1500 仅图层 WINDOW 可识别）
    doors: list[Door] = []
    windows: list[Window] = []
    furniture: list[Furniture] = []
    for e in msp.query("INSERT"):
        layer, name = e.dxf.layer, e.dxf.name
        pos = Point(e.dxf.insert.x * scale, e.dxf.insert.y * scale)
        m = WIDTH_RE.search(name)
        width = float(m.group(1)) if m else None
        lw = f"{layer} {name}".lower()
        if "door" in lw or "门" in name:
            doors.append(Door(id=f"door_{len(doors)+1:03d}", position=[pos.x, pos.y],
                              width=width or 900.0, wall_id=_nearest_wall_id(pos, walls, strips)))
        elif "window" in lw or "窗" in name:
            windows.append(Window(id=f"window_{len(windows)+1:03d}", position=[pos.x, pos.y],
                                  width=width or 1500.0,
                                  wall_id=_nearest_wall_id(pos, walls, strips)))
        else:
            furniture.append(Furniture(id=f"furn_{len(furniture)+1:03d}", type=name.split("_")[0],
                                       position=[pos.x, pos.y], size=[1000.0, 500.0, 500.0],
                                       source="cad"))

    floor_height = rep.floor_height_candidates[0] if rep.floor_height_candidates else 2800.0
    if not rep.floor_height_candidates:
        fallbacks.append("floor_height -> 2800 默认")
    thicknesses = [c[2] for c in centers]
    wall_t = max(set(thicknesses), key=thicknesses.count) if thicknesses else DEFAULT_WALL_T
    scene = SceneJSON(floor_height=floor_height, wall_thickness=wall_t, walls=walls,
                      doors=doors, windows=windows, furniture=furniture, rooms=rooms)
    err = ToolError(code="PARSE_LOW_CONFIDENCE", message="; ".join(fallbacks)) \
        if rep.confidence < 0.6 else None
    key = build_cache_key("parse_scene", str(dxf_path), str(sorted(wall_layers)))
    return ToolResult(ok=True, data=scene, error=err, cache_key=key, metrics=Metrics())
