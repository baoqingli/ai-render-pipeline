"""白模纯几何层：归一化、墙体矩形化、开洞分段、机位规划、BuildPlan 组装。全程 mm。"""
from typing import NamedTuple

from shapely.geometry import Polygon

from app.models.scene import SceneJSON, Wall


class Box(NamedTuple):
    center: tuple[float, float, float]
    size: tuple[float, float, float]
    rot_z: float


def _bbox_min(scene: SceneJSON) -> tuple[float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for w in scene.walls:
        xs += [p[0] for p in w.polygon]
        ys += [p[1] for p in w.polygon]
    for r in scene.rooms:
        xs += [p[0] for p in r.polygon]
        ys += [p[1] for p in r.polygon]
    if not xs:
        return (0.0, 0.0)
    return (min(xs), min(ys))


def scene_offset(scene: SceneJSON) -> tuple[float, float]:
    return _bbox_min(scene)


def normalize_scene(scene: SceneJSON) -> SceneJSON:
    dx, dy = _bbox_min(scene)
    data = scene.model_dump()
    for w in data["walls"]:
        w["polygon"] = [[p[0] - dx, p[1] - dy] for p in w["polygon"]]
    for r in data["rooms"]:
        r["polygon"] = [[p[0] - dx, p[1] - dy] for p in r["polygon"]]
    for d in data["doors"]:
        d["position"] = [d["position"][0] - dx, d["position"][1] - dy]
    for w in data["windows"]:
        w["position"] = [w["position"][0] - dx, w["position"][1] - dy]
    for f in data["furniture"]:
        f["position"] = [f["position"][0] - dx, f["position"][1] - dy]
    return SceneJSON.model_validate(data)


def wall_box(wall: Wall, floor_height: float) -> Box:
    rect = Polygon(wall.polygon).minimum_rotated_rectangle
    c = list(rect.exterior.coords)          # 5 点闭合
    ax, ay = c[1][0] - c[0][0], c[1][1] - c[0][1]
    bx, by = c[3][0] - c[0][0], c[3][1] - c[0][1]
    la, lb = (ax * ax + ay * ay) ** 0.5, (bx * bx + by * by) ** 0.5
    # 取长边为局部 x 轴
    if la >= lb:
        length, width, angle = la, lb, _angle(ax, ay)
    else:
        length, width, angle = lb, la, _angle(bx, by)
    cx = sum(p[0] for p in c[:4]) / 4.0
    cy = sum(p[1] for p in c[:4]) / 4.0
    return Box(center=(cx, cy, floor_height / 2.0),
               size=(length, width, floor_height), rot_z=angle)


def _angle(dx: float, dy: float) -> float:
    import math
    a = math.atan2(dy, dx)
    if a < -math.pi / 2:      # 归一到 (-π/2, π/2]，使长边方向稳定
        a += math.pi
    elif a > math.pi / 2:
        a -= math.pi
    return a
