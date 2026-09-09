# app/agents/vision/reconcile.py
"""ElementRegistry（VLM 语义）× SceneJSON（确定性提取）交叉对账。

对登记簿中每个元素：在其 world_bbox 内查找 SceneJSON 实体，
判定 matched / missing，产出差异清单——"供后续模块使用"的第一消费者。
"""
from typing import Any

from shapely.geometry import Point, Polygon

from app.models.vision import ElementRegistry

# VLM 家具类型 → SceneJSON 家具类型（对账兼容映射）
_TYPE_COMPAT: dict[str, set[str]] = {
    "bed": {"bed"},
    "sofa": {"sofa"},
    "table": {"table", "cabinet"},
    "chair": {"chair"},
    "wardrobe": {"wardrobe", "cabinet"},
    "cabinet": {"cabinet", "wardrobe"},
    "tv": {"tv"},
    "toilet": {"toilet"},
    "sink": {"sink"},
    "shower": {"shower", "cabinet"},
    "bathtub": {"bathtub"},
    "plant": {"plant"},
    "appliance": {"appliance", "cabinet"},
}

# 墙类元素 → 与墙几何对账
_WALL_CATS = {"墙", "房间边界"}
_FURN_CATS = {"家具", "固定柜", "卫浴", "厨房"}
_OPENING_CATS = {"门", "窗"}


def _point_in_bbox(p: tuple[float, float], bbox: list[float]) -> bool:
    return bbox[0] <= p[0] <= bbox[2] and bbox[1] <= p[1] <= bbox[3]


def _poly_intersects_bbox(poly_pts: list[list[float]], bbox: list[float]) -> bool:
    if not poly_pts:
        return False
    xs = [p[0] for p in poly_pts]
    ys = [p[1] for p in poly_pts]
    return not (max(xs) < bbox[0] or min(xs) > bbox[2]
                or max(ys) < bbox[1] or min(ys) > bbox[3])


def reconcile(registry: ElementRegistry, scene: Any) -> dict:
    """对账 → {verdicts: [...], summary: {...}}。

    verdicts 项：{category, item, world_bbox, verdict: matched|missing|annotation,
                  matched_entities: [...]}
    annotation 类（尺寸标注/房间名称）不要求确定性对应物，直接跳过。
    """
    from app.models.scene import SceneJSON

    assert isinstance(scene, SceneJSON)
    walls = scene.walls
    furniture = scene.furniture
    doors = scene.doors
    windows = scene.windows

    verdicts: list[dict] = []
    counts = {"matched": 0, "missing": 0, "annotation": 0}

    for el in registry.elements:
        cat = el.category
        if cat in ("尺寸标注", "房间名称", "annotation"):
            counts["annotation"] += 1
            verdicts.append({"category": cat, "item": el.item,
                             "verdict": "annotation_skip"})
            continue
        bbox = el.world_bbox
        if not bbox or len(bbox) != 4:
            verdicts.append({"category": cat, "item": el.item,
                             "verdict": "no_location"})
            counts["missing"] += 1
            continue

        if cat in _WALL_CATS:
            hit = any(_poly_intersects_bbox(w.polygon, bbox) for w in walls)
            matched_entities = [w.id for w in walls
                                if _poly_intersects_bbox(w.polygon, bbox)][:5]
            verdict = "matched" if hit else "missing"
        elif cat in _OPENING_CATS:
            # 门/窗：对账场景 doors/windows（位置点入 bbox 即命中）
            pts = ([d.position for d in doors] if cat == "门"
                   else [w.position for w in windows])
            hit = any(_point_in_bbox((p[0], p[1]), bbox) for p in pts)
            matched_entities = []
            verdict = "matched" if hit else "missing"
        elif cat in _FURN_CATS:
            compat = set()
            for c in (cat, el.item):
                compat |= _TYPE_COMPAT.get(c, set())
            box_poly = Polygon([(bbox[0], bbox[1]), (bbox[2], bbox[1]),
                                (bbox[2], bbox[3]), (bbox[0], bbox[3])])
            hit_f = [f for f in furniture
                     if compat & {f.type} and box_poly.contains(Point(f.position))]
            hit = bool(hit_f)
            matched_entities = [f"{f.type}@({f.position[0]:.0f},{f.position[1]:.0f})"
                                for f in hit_f[:5]]
            # 家具缺失只在其类型确属该区（VLM item 非空）时判 missing
            verdict = "matched" if hit else "missing"
        else:
            # 设备/天花/灯具/烟感喷淋等：当前 SceneJSON 无对应实体，标记 n/a
            verdicts.append({"category": cat, "item": el.item,
                             "verdict": "no_deterministic_counterpart"})
            continue

        counts["matched" if verdict == "matched" else "missing"] += 1
        verdicts.append({"category": cat, "item": el.item, "verdict": verdict,
                         "matched_entities": matched_entities})

    return {"verdicts": verdicts, "summary": dict(counts)}
