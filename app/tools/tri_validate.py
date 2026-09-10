# app/tools/tri_validate.py
"""三向对账验证：ElementRegistry × 渲染图 × 标准图（CAD 真值）。

对账设计（docs/agent-optimization-plan-2026-09.md T3）：
  信号1（确定性）：registry 元素坐标 → 渲染图像素位置 → 语义配色核对
  信号2（确定性）：registry 与标准图渲染的几何 IoU 对账
  信号3（VLM）：视觉差异兜底（validation_loop）
合并输出：score + 结构化差异（kind/severity/description/action）。
"""

from app.models.vision import ElementRegistry
from app.tools.blender.imgstat import decode_png

# 语义配色期望（scene_builder sv_* ×255，宽容差）
_SEM_COLORS = {
    "墙": ((0, 0, 0), (90, 90, 90), 1),
    "床": ((150, 0, 0), (255, 100, 100), 0),
    "卫浴": ((0, 150, 0), (100, 255, 100), 0),
    "家具": ((0, 0, 150), (100, 100, 255), 0),
    "固定柜": ((0, 0, 150), (100, 100, 255), 0),
    "椅": ((150, 150, 0), (255, 255, 100), 0),
    "桌": ((150, 0, 150), (255, 100, 255), 0),
    "门": ((180, 80, 0), (255, 180, 80), 1),
}


def _expected_rgb(category: str, item: str):
    """item 优先（与 scene_builder._furniture_kit 材质分支一一对应）：
    bed红 / toilet·sink·shower·bathtub绿 / wardrobe·cabinet蓝 /
    sofa·armchair·chair黄 / 其余家具(含 table·tv)品红 / 墙黑 / 门橙。
    """
    if item in ("bed",):
        return _SEM_COLORS["床"]
    if item in ("toilet", "sink", "bathtub", "shower"):
        return _SEM_COLORS["卫浴"]
    if item in ("wardrobe", "cabinet"):
        return _SEM_COLORS["固定柜"]
    if item in ("sofa", "armchair", "chair"):
        return _SEM_COLORS["椅"]
    if item in ("table", "tv"):
        return _SEM_COLORS["桌"]
    if category == "家具":
        return _SEM_COLORS["桌"]      # 默认材质=品红
    if category in _SEM_COLORS:
        return _SEM_COLORS[category]
    return None


def check_registry_in_render(registry: ElementRegistry, render_png: str,
                             window: int = 8) -> dict:
    """信号1：registry 每元素 → 渲染图对应像素 → 颜色核对（确定性）。

    返回 {items: [{category, item, pixel, rendered}], summary}。
    要求渲染图为语义配色（ARP_SEMANTIC_COLORS=1）。
    """
    w, h, bpp, pix = decode_png(render_png)
    boxes = [e.world_bbox for e in registry.elements if e.world_bbox]
    if not boxes:
        return {"items": [], "summary": {"total": 0, "rendered": 0}}
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    scale = max(x1 - x0, y1 - y0) * 1.15
    hw, hh = scale / 2, scale / 2 * (h / w)

    def to_px(wx, wy):
        return (int((wx - (cx - hw)) / scale * w),
                int(((cy + hh) - wy) / (scale * h / w) * h))

    items = []
    for e in registry.elements:
        if not e.world_bbox:
            continue
        bx0, by0, bx1, by1 = e.world_bbox
        # 多点位采样：中心 + 四分位（L 形/裁剪墙的 bbox 中心可能落在带外）
        sample_pts = [((bx0 + bx1) / 2, (by0 + by1) / 2),
                      (bx0 + (bx1 - bx0) * 0.2, (by0 + by1) / 2),
                      (bx0 + (bx1 - bx0) * 0.8, (by0 + by1) / 2),
                      ((bx0 + bx1) / 2, by0 + (by1 - by0) * 0.2),
                      ((bx0 + bx1) / 2, by0 + (by1 - by0) * 0.8)]
        want = _expected_rgb(e.category, e.item)
        found = False
        hit_px = None
        for swx, swy in sample_pts:
            ex, ey = to_px(swx, swy)
            if not (0 <= ex < w and 0 <= ey < h):
                continue
            for dy in range(-window, window + 1, 2):
                for dx in range(-window, window + 1, 2):
                    x_, y_ = ex + dx, ey + dy
                    if not (0 <= x_ < w and 0 <= y_ < h):
                        continue
                    o = (y_ * w + x_) * bpp
                    rgb = (pix[o], pix[o + 1], pix[o + 2])
                    if want is None:
                        if not all(v > 235 for v in rgb):
                            found, hit_px = True, (ex, ey)
                            break
                    else:
                        lo, hi = want[0], want[1]
                        if all(lo[i] <= rgb[i] <= hi[i] for i in range(3)):
                            found, hit_px = True, (ex, ey)
                            break
                if found:
                    break
            if found:
                break
        ex, ey = to_px((bx0 + bx1) / 2, (by0 + by1) / 2)
        items.append({"category": e.category, "item": e.item,
                      "pixel": hit_px or (ex, ey), "rendered": found})
    n_ok = sum(1 for i in items if i["rendered"])
    return {"items": items, "summary": {"total": len(items), "rendered": n_ok,
                                        "missing": len(items) - n_ok}}


def score(ratio: float, penalty_per_missing: float = 6.0) -> int:
    """元素渲染率 → 分数。"""
    return max(0, min(100, round(ratio * 100)))


def tri_validate(registry: ElementRegistry, render_png: str,
                 standard_png: str | None = None, *,
                 vlm_model: str | None = None) -> dict:
    """三向对账主入口。

    registry: v3 输出（mm 或像素坐标，与渲染图同源即可）
    render_png: 语义配色渲染（ARP_SEMANTIC_COLORS=1 产出）
    standard_png: CAD 真值图（可选，供 VLM 兜底对比）
    返回 {signal1, vlm, combined_score, issues}
    """
    from app.tools.validation_loop import validate_render

    s1 = check_registry_in_render(registry, render_png)
    ratio = s1["summary"]["rendered"] / max(s1["summary"]["total"], 1)
    result: dict = {"signal1": s1, "combined_score": score(ratio)}

    if standard_png:
        vlm = validate_render(standard_png, render_png, model=vlm_model)
        if vlm.ok and vlm.data is not None:
            result["vlm"] = {"score": vlm.data.score,
                              "summary": vlm.data.summary,
                              "issues": [i.model_dump() for i in vlm.data.issues]}
            # 合并分：信号1（确定性）占 60%，VLM 占 40%
            result["combined_score"] = round(0.6 * result["combined_score"]
                                             + 0.4 * vlm.data.score)
        else:
            result["vlm"] = {"error": vlm.error.message[:150] if vlm.error else "?"}

    issues = []
    for it in s1["items"]:
        if not it["rendered"]:
            issues.append({
                "kind": "missing_element",
                "severity": "medium" if it["category"] in ("家具", "固定柜") else "high",
                "description": f"{it['category']}/{it['item']} 在渲染图 "
                               f"{it['pixel']} 处未检出预期颜色",
                "action": f"核查 {it['item']} 的坐标或渲染分支",
            })
    result["issues"] = issues
    return result
