"""白模纯几何层：归一化、墙体矩形化、开洞分段、机位规划、BuildPlan 组装。全程 mm。

设计见 docs/white-model-fix-plan-2026-09.md §4 P0-2/P0-5：
- 墙单一路径（scene.walls 统一列表，parse 已保证房间边+条带缝合），全高 + 门窗开洞；
- view_iso 改正交俯视（对齐验收基准图）；窗洞生成棂条（kind="frame"）；
- 家具带 label（规范类型），scene_builder 据此选低模套件。
"""
from typing import NamedTuple

from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from app.models.build_plan import BuildPlan, PlanBox, PlanCamera
from app.models.scene import SceneJSON, Wall
from app.tools.cad.rules import WINDOW_MULLION_INTERVAL


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


def window_frames(box: Box, opening: Opening) -> list[Box]:
    """窗洞棂条：开洞宽度内按 WINDOW_MULLION_INTERVAL 均布竖向细框（kind=frame）。"""
    lo, hi = opening.u - opening.width / 2.0, opening.u + opening.width / 2.0
    n = max(1, int(opening.width // WINDOW_MULLION_INTERVAL))
    frames: list[Box] = []
    for k in range(1, n + 1):
        u = lo + (hi - lo) * k / (n + 1)
        b = _local_box(box, u, 40.0, opening.z_bot, opening.z_top)
        frames.append(Box(center=b.center, size=(b.size[0], b.size[1] * 0.5, b.size[2]),
                          rot_z=b.rot_z))
    return frames


class CameraPose(NamedTuple):
    view_id: str
    position: tuple[float, float, float]
    target: tuple[float, float, float]


SHRINK = 400.0


def plan_views(scene: SceneJSON, per_room: int = 2) -> list[CameraPose]:
    """机位规划（数量与名称被单测锁定：iso + interior 共 2）：
    - view_iso：正交俯视（对齐验收基准图的"模型俯瞰"形态），相机在中心上方轻微倾斜
    - view_interior：最大房间内角透视（全高墙下 eye 1600mm）
    """
    import math
    scene = normalize_scene(scene)
    poses: list[CameraPose] = []

    all_polys = ([Polygon(r.polygon) for r in scene.rooms]
                 or [Polygon(w.polygon) for w in scene.walls])
    if not all_polys:
        return []

    merged = unary_union(all_polys)
    minx, miny, maxx, maxy = merged.bounds
    cx = (minx + maxx) / 2.0
    cy = (miny + maxy) / 2.0
    diag = math.hypot(maxx - minx, maxy - miny)
    fh = scene.floor_height

    # 视角1：正交俯视——正上方零旋转（与 CAD 截图轴对齐，便于逐项对比）
    poses.append(CameraPose(
        view_id="view_iso",
        position=(cx, cy, fh + diag * 0.9),
        target=(cx, cy, 0.0),
    ))

    # 视角2：室内透视——最大房间缩进多边形顶点作机位（bbox 角点对 L 形房间可能落在
    # 房间外/墙内），朝最远顶点看（小房间质心太近，画面被近处地面填满）
    if scene.rooms:
        largest = max((Polygon(r.polygon) for r in scene.rooms), key=lambda p: p.area)
        shrunk = largest.buffer(-SHRINK)
        if not shrunk.is_empty:
            if shrunk.geom_type == "MultiPolygon":
                shrunk = max(shrunk.geoms, key=lambda g: g.area)
            bx1, by1, bx2, by2 = shrunk.bounds
            anchor = (bx1 + (bx2 - bx1) * 0.15, by1 + (by2 - by1) * 0.15)
            coords = list(shrunk.exterior.coords)
            vtx_near = min(coords, key=lambda p: math.hypot(p[0] - anchor[0],
                                                            p[1] - anchor[1]))
            vtx_far = max(coords, key=lambda p: math.hypot(p[0] - vtx_near[0],
                                                           p[1] - vtx_near[1]))
            poses.append(CameraPose(
                view_id="view_interior",
                position=(vtx_near[0], vtx_near[1], fh * 0.57),
                target=(vtx_far[0], vtx_far[1], fh * 0.38),
            ))

    return poses


def plan_interior_candidates(scene: SceneJSON, max_rooms: int = 1) -> list[CameraPose]:
    """内视相机候选：最大房间（面积最大）一对对角机位。

    只取最大房间——深度方差指标会偏向小房间（小空间深度变化大），
    但内视图的价值是展示主生活空间。候选须经 runner 的深度校验
    在两个对角机位间择优，避免单机位贴墙/空白。
    """
    import math
    scene = normalize_scene(scene)
    out: list[CameraPose] = []
    rooms = sorted(scene.rooms, key=lambda r: -Polygon(r.polygon).area)[:max_rooms]
    for r in rooms:
        poly = Polygon(r.polygon).buffer(-SHRINK)
        if poly.is_empty:
            continue
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        coords = list(poly.exterior.coords)[:-1]
        if len(coords) < 3:
            continue
        best = None
        for i in range(len(coords)):
            for j in range(i + 1, len(coords)):
                d = math.hypot(coords[i][0] - coords[j][0], coords[i][1] - coords[j][1])
                if best is None or d > best[0]:
                    best = (d, coords[i], coords[j])
        if best is None:
            continue
        _, a, b = best
        k = len(out)
        out.append(CameraPose(f"view_int_{k}a",
                              (a[0], a[1], 1600.0), (b[0], b[1], 1100.0)))
        out.append(CameraPose(f"view_int_{k}b",
                              (b[0], b[1], 1600.0), (a[0], a[1], 1100.0)))
    return out


def _aabb(box: PlanBox) -> tuple[float, float, float, float]:
    """旋转盒的世界轴对齐包围盒 (minx, maxx, miny, maxy)——按角点投影范围。"""
    import math
    c, s = math.cos(box.rot_z), math.sin(box.rot_z)
    sx, sy = box.size[0] / 2.0, box.size[1] / 2.0
    hx = abs(sx * c) + abs(sy * s)
    hy = abs(sx * s) + abs(sy * c)
    return (box.center[0] - hx, box.center[0] + hx, box.center[1] - hy, box.center[1] + hy)


def build_plan(scene: SceneJSON, output_dir: str) -> BuildPlan:
    import math
    scene = normalize_scene(scene)
    fh = scene.floor_height
    boxes: list[PlanBox] = []

    # 统一墙路径（唯一）：scene.walls 已由 parse 缝合（房间边+双线条带），
    # 全高 + 门窗开洞 + 窗棂
    walls_poly: list[Polygon] = [Polygon(w.polygon) for w in scene.walls
                                 if len(w.polygon) >= 4]
    door_by_wall: dict[str, list] = {}
    win_by_wall: dict[str, list] = {}
    for d in scene.doors:
        door_by_wall.setdefault(d.wall_id or "", []).append(d)
    for w in scene.windows:
        win_by_wall.setdefault(w.wall_id or "", []).append(w)
    for wall in scene.walls:
        base = wall_box(wall, fh)
        # 端部延伸半个墙厚：保证 L/T 交角处墙盒搭接，消除接缝黑缝（光照死角）
        base = base._replace(size=(base.size[0] + base.size[1], base.size[1], base.size[2]))
        openings = []
        for d in door_by_wall.get(wall.id, []):
            openings.append(project_opening(base, d.position, d.width, 0.0, d.height))
        wins = win_by_wall.get(wall.id, [])
        for w in wins:
            openings.append(project_opening(base, w.position, w.width,
                                            w.sill_height, w.sill_height + w.height))
        for seg in segment_wall(base, openings):
            boxes.append(PlanBox(center=list(seg.center), size=list(seg.size),
                                 rot_z=seg.rot_z, kind="wall"))
        # 门洞地面标记条（高 50mm 橙色）：俯视图过梁会盖住门洞，
        # 地面标记让门洞位置在俯视校验中可见
        for d in door_by_wall.get(wall.id, []):
            op = project_opening(base, d.position, d.width, 0.0, d.height)
            lo, hi = op.u - op.width / 2, op.u + op.width / 2
            lo_c, hi_c = max(lo, -base.size[0] / 2), min(hi, base.size[0] / 2)
            if hi_c - lo_c > 100:
                thr = _local_box(base, (lo_c + hi_c) / 2, hi_c - lo_c, 0, 50)
                boxes.append(PlanBox(center=list(thr.center),
                                     size=[thr.size[0], thr.size[1] * 0.4, 50],
                                     rot_z=thr.rot_z, kind="frame", label="door"))
        for w in wins:
            op = project_opening(base, w.position, w.width,
                                 w.sill_height, w.sill_height + w.height)
            # 玻璃面板填满窗洞：避免俯视看进洞内过梁底面的无光黑腔（对齐基准图窗棂形态）
            glass = _local_box(base, op.u, op.width - 40.0, op.z_bot + 20.0, op.z_top - 20.0)
            boxes.append(PlanBox(center=list(glass.center),
                                 size=[glass.size[0], 10.0, glass.size[2]],
                                 rot_z=glass.rot_z, kind="frame", label="glass"))
            for fr in window_frames(base, op):
                boxes.append(PlanBox(center=list(fr.center), size=list(fr.size),
                                     rot_z=fr.rot_z, kind="frame"))

    for f in scene.furniture:
        sx, sy, sz = f.size
        rot = f.rotation
        if f.type in ("bed", "sofa") and walls_poly:
            # 床头/沙发背贴最近墙：比较家具两端到最近墙面的距离，远端翻转 180°
            L = sy / 2.0
            c, s = math.cos(rot), math.sin(rot)
            pt_m = Point(f.position[0] + L * s, f.position[1] - L * c)   # local -y 端（床头/靠背）
            pt_p = Point(f.position[0] - L * s, f.position[1] + L * c)   # local +y 端
            dm = min(pg.distance(pt_m) for pg in walls_poly)
            dp = min(pg.distance(pt_p) for pg in walls_poly)
            if dp < dm:
                rot += math.pi
        boxes.append(PlanBox(center=[f.position[0], f.position[1], sz / 2.0],
                             size=[sx, sy, sz], rot_z=rot, kind="furniture",
                             label=f.type))
    aabbs = [_aabb(b) for b in boxes]
    minx = min(a[0] for a in aabbs) if aabbs else 0.0
    maxx = max(a[1] for a in aabbs) if aabbs else 0.0
    miny = min(a[2] for a in aabbs) if aabbs else 0.0
    maxy = max(a[3] for a in aabbs) if aabbs else 0.0
    floor = PlanBox(center=[(minx + maxx) / 2.0, (miny + maxy) / 2.0, -50.0],
                    size=[(maxx - minx) + 1000.0, (maxy - miny) + 1000.0, 100.0],
                    kind="floor")
    boxes.append(floor)
    cameras = []
    for p in plan_views(scene):
        ortho = p.view_id == "view_iso"
        scale = max(maxx - minx, maxy - miny) * 1.15 if ortho else 0.0
        cameras.append(PlanCamera(view_id=p.view_id, position=list(p.position),
                                  target=list(p.target), ortho=ortho,
                                  ortho_scale=scale))
    return BuildPlan(floor_height=fh, boxes=boxes, cameras=cameras, output_dir=output_dir)
