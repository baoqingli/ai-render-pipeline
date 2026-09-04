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


class Opening(NamedTuple):
    u: float
    width: float
    z_bot: float
    z_top: float


def project_opening(box: Box, position: tuple[float, float], width: float,
                    z_bot: float, z_top: float) -> Opening:
    import math
    dx, dy = position[0] - box.center[0], position[1] - box.center[1]
    cos, sin = math.cos(-box.rot_z), math.sin(-box.rot_z)
    u = dx * cos - dy * sin
    return Opening(u=u, width=width, z_bot=z_bot, z_top=z_top)


def _merged_intervals(openings: list[Opening]) -> list[tuple[float, float, float, float]]:
    """按 u 排序合并重叠区间；z 范围取并集区间的包围（分段法 MVP：同墙多洞按各自 z 独立处理）。"""
    ivs = sorted((o.u - o.width / 2.0, o.u + o.width / 2.0, o.z_bot, o.z_top)
                 for o in openings)
    merged: list[tuple[float, float, float, float]] = []
    for lo, hi, zb, zt in ivs:
        if merged and lo <= merged[-1][1]:
            m = merged[-1]
            merged[-1] = (m[0], max(m[1], hi), min(m[2], zb), max(m[3], zt))
        else:
            merged.append((lo, hi, zb, zt))
    return merged


def _local_box(box: Box, u_center: float, du: float, z_bot: float, z_top: float) -> Box:
    """墙局部坐标 → 全局 Box（rot/thickness 继承墙）。"""
    import math
    cos, sin = math.cos(box.rot_z), math.sin(box.rot_z)
    gx = box.center[0] + u_center * cos
    gy = box.center[1] + u_center * sin
    zc = (z_bot + z_top) / 2.0
    return Box(center=(gx, gy, zc),
               size=(du, box.size[1], z_top - z_bot), rot_z=box.rot_z)


def segment_wall(box: Box, openings: list[Opening]) -> list[Box]:
    if not openings:
        return [box]
    half_l = box.size[0] / 2.0
    fh = box.size[2]
    merged = _merged_intervals(openings)
    segs: list[Box] = []
    cursor = -half_l
    for lo, hi, zb, zt in merged:
        lo_c, hi_c = max(lo, -half_l), min(hi, half_l)
        if hi_c - lo_c <= 1.0:
            continue
        if lo_c - cursor > 1.0:                                    # 洞前全高段
            segs.append(_local_box(box, (cursor + lo_c) / 2.0, lo_c - cursor, 0.0, fh))
        if zb > 1.0:                                               # 下段（窗台/门无）
            segs.append(_local_box(box, (lo_c + hi_c) / 2.0, hi_c - lo_c, 0.0, zb))
        if fh - zt > 1.0:                                          # 上段（过梁/窗上）
            segs.append(_local_box(box, (lo_c + hi_c) / 2.0, hi_c - lo_c, zt, fh))
        cursor = hi_c
    if half_l - cursor > 1.0:                                      # 尾部全高段
        segs.append(_local_box(box, (cursor + half_l) / 2.0, half_l - cursor, 0.0, fh))
    return segs
