# app/tools/multi_signal_validate.py
"""多信号融合校验：确定性几何对账 + 尺寸链验证 + 连通性检查。"""
from collections import deque
from dataclasses import dataclass, field

from shapely.geometry import Polygon


@dataclass
class SignalResult:
    signal: str
    passed: bool
    detail: str
    data: dict = field(default_factory=dict)


def _point_in_bbox(p, bbox):
    return bbox[0] <= p[0] <= bbox[2] and bbox[1] <= p[1] <= bbox[3]


def _poly_overlaps_bbox(poly_pts, bbox):
    if not poly_pts:
        return False
    xs = [p[0] for p in poly_pts]
    ys = [p[1] for p in poly_pts]
    return not (max(xs) < bbox[0] or min(xs) > bbox[2] or max(ys) < bbox[1] or min(ys) > bbox[3])


_TYPE_COMPAT = {
    "bed": {"bed"}, "sofa": {"sofa"}, "table": {"table", "cabinet"},
    "chair": {"chair"}, "wardrobe": {"wardrobe", "cabinet"},
    "cabinet": {"cabinet", "wardrobe"}, "tv": {"tv"},
    "toilet": {"toilet"}, "sink": {"sink"},
    "shower": {"shower", "cabinet"}, "bathtub": {"bathtub"},
    "plant": {"plant"}, "appliance": {"appliance", "cabinet"},
}


def signal1_geometry(registry, scene):
    """逐元素对账：每个识别元素在其 world_bbox 内是否有匹配的 SceneJSON 实体。"""
    matched = []
    missing = []
    for el in registry.elements:
        if not el.world_bbox or len(el.world_bbox) != 4:
            continue
        bbox = el.world_bbox
        cat = el.category
        hits = []
        if cat in ("家具", "固定柜"):
            compat = _TYPE_COMPAT.get(el.item, set()) | {el.item}
            for f in scene.furniture:
                if f.type in compat and _point_in_bbox(tuple(f.position), bbox):
                    hits.append(f.id)
        elif cat in ("墙",):
            for w in scene.walls:
                if _poly_overlaps_bbox(w.polygon, bbox):
                    hits.append(w.id)
        elif cat in ("门",):
            for d in scene.doors:
                if _point_in_bbox(tuple(d.position), bbox):
                    hits.append(d.id)
        elif cat in ("窗",):
            for w in scene.windows:
                if _point_in_bbox(tuple(w.position), bbox):
                    hits.append(w.id)
        if hits:
            matched.append({"category": cat, "item": el.item, "hits": hits[:3]})
        else:
            missing.append({"category": cat, "item": el.item, "bbox": bbox})
    total = len(matched) + len(missing)
    ratio = len(matched) / total if total else 1.0
    return SignalResult(
        signal="geometry_match", passed=ratio >= 0.7,
        detail=f"匹配 {len(matched)}/{total}（{ratio:.0%}）",
        data={"matched": matched, "missing": missing, "ratio": round(ratio, 2)},
    )


def signal2_dimensions(dimensions, scene, min_mm=500):
    """验证：每个 ≥min_mm 的标注应有对应长度的墙段。"""
    verified = 0
    unverified = 0
    for dim in dimensions:
        m = dim.get("measure", 0)
        if m < min_mm:
            continue
        found = any(
            abs(max(p[0] for p in w.polygon) - min(p[0] for p in w.polygon) - m) < 100
            or abs(max(p[1] for p in w.polygon) - min(p[1] for p in w.polygon) - m) < 100
            for w in scene.walls
        )
        if found:
            verified += 1
        else:
            unverified += 1
    total = verified + unverified
    ratio = verified / total if total else 1.0
    return SignalResult(
        signal="dimension_verify", passed=ratio >= 0.7,
        detail=f"尺寸验证 {verified}/{total}（{ratio:.0%}）",
        data={"verified": verified, "unverified": unverified},
    )


def signal3_connectivity(scene, step=100.0):
    """BFS 连通性检查：自由空间应为单一连通区域。"""
    from shapely.geometry import Point as ShPt
    wall_polys = []
    for w in scene.walls:
        try:
            wp = Polygon(w.polygon)
            if wp.is_valid and wp.area > 0:
                wall_polys.append(wp)
        except Exception:  # noqa: BLE001
            pass

    all_pts = [p for w in scene.walls for p in w.polygon]
    if not all_pts:
        return SignalResult(signal="connectivity", passed=True, detail="无墙",
                            data={"free_total": 0, "largest_component": 0, "disconnected": 0})
    minx = min(p[0] for p in all_pts) - 500
    miny = min(p[1] for p in all_pts) - 500
    maxx = max(p[0] for p in all_pts) + 500
    maxy = max(p[1] for p in all_pts) + 500

    # 网格采样：排除墙覆盖点
    free = set()
    x = minx
    while x <= maxx:
        y = miny
        while y <= maxy:
            p = ShPt(x, y)
            if not any(wp.contains(p) for wp in wall_polys):
                free.add((round(x), round(y)))
            y += step
        x += step

    # BFS 最大连通分量
    if not free:
        return SignalResult(signal="connectivity", passed=False,
                            detail="无自由空间", data={"components": 0})
    visited = set()
    largest = 0
    fp_list = sorted(free)
    for start in fp_list:
        if start in visited:
            continue
        component = 0
        queue = deque([start])
        visited.add(start)
        while queue:
            cx, cy = queue.popleft()
            component += 1
            for dx, dy in ((0, step), (0, -step), (step, 0), (-step, 0)):
                np_ = (round(cx + dx), round(cy + dy))
                if np_ in free and np_ not in visited:
                    visited.add(np_)
                    queue.append(np_)
        largest = max(largest, component)

    disconnected = len(free) - largest
    ratio = largest / max(len(free), 1)
    return SignalResult(
        signal="connectivity",
        passed=disconnected <= len(free) * 0.02,
        detail=f"最大连通分量占 {ratio:.0%}，离散点 {disconnected}/{len(free)}",
        data={"free_total": len(free), "largest_component": largest,
              "disconnected": disconnected},
    )
