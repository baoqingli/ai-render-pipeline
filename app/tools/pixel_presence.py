# app/tools/pixel_presence.py
"""像素级家具存在检查（确定性）——补 VLM 校验的小件盲区。

VLM 对 <20px 的小家具（椅子/TV）常误报"缺失"；本模块把场景家具坐标
映射到渲染像素，采样窗口与地板色对比，确定性地判定"是否渲染了几何体"。
用法：与 validation_loop 的 VLM 报告合并——pixel_present=True 的
missing_furniture 项判为误报剔除。
"""
import json
from pathlib import Path

from app.tools.blender.imgstat import decode_png

# 材质基色（scene_builder emissive 值 ×255，容差 40）
_FLOOR_RGB = (224, 224, 224)      # mat_floor 0.88
_BG_RGB = (255, 255, 255)         # 世界背景
_TOL = 40
_WINDOW = 6                        # 采样窗口半径（px）


def _world_to_px_factory(scene: dict, img_w: int, img_h: int):
    """场景归一化坐标 → 渲染像素映射（复现 geom.build_plan 的 iso 相机）。"""
    import math
    xs = [p[0] for w in scene.get("walls", []) for p in w["polygon"]]
    ys = [p[1] for w in scene.get("walls", []) for p in w["polygon"]]
    for r in scene.get("rooms", []):
        xs += [p[0] for p in r["polygon"]]
        ys += [p[1] for p in r["polygon"]]
    if not xs:
        return None
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    diag = math.hypot(x1 - x0, y1 - y0)
    scale = diag * 1.15                      # geom.py: ortho_scale
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    # 正交相机俯视：ortho_scale 为视口宽（对应分辨率长边），
    # 高按分辨率比例 900/1200
    half_w = scale / 2
    half_h = scale / 2 * (img_h / img_w)

    def to_px(wx: float, wy: float) -> tuple[int, int]:
        fx = (wx - (cx - half_w)) / scale
        fy = ((cy + half_h) - wy) / (scale * img_h / img_w)
        return int(fx * img_w), int(fy * img_h)
    return to_px


def check_furniture_presence(render_png: str, scene_json: str) -> dict:
    """每件家具 → 是否在渲染图对应像素渲染了几何体（非地板/背景色）。"""
    scene = json.loads(Path(scene_json).read_text(encoding="utf-8"))
    w, h, bpp, pix = decode_png(render_png)
    to_px = _world_to_px_factory(scene, w, h)
    if to_px is None:
        return {"items": [], "note": "场景无几何"}

    def _near(rgb, ref):
        return all(abs(a - b) <= _TOL for a, b in zip(rgb, ref))

    def _is_meaningful(rgb):
        # 语义配色：墙(黑) 或 饱和家具色(通道差大)
        r, g, b = rgb
        if r < 100 and g < 100 and b < 100:
            return True
        return max(r, g, b) - min(r, g, b) > 60

    items = []
    for f in scene.get("furniture", []):
        px, py = f["position"]
        fw, fd = f["size"][0], f["size"][1]
        ix, iy = to_px(px, py)
        found = False
        oob = not (0 <= ix < w and 0 <= iy < h)
        if not oob:
            for dy in range(-_WINDOW, _WINDOW + 1, 2):
                for dx in range(-_WINDOW, _WINDOW + 1, 2):
                    x_, y_ = ix + dx, iy + dy
                    if not (0 <= x_ < w and 0 <= y_ < h):
                        continue
                    o = (y_ * w + x_) * bpp
                    rgb = (pix[o], pix[o + 1], pix[o + 2])
                    if _is_meaningful(rgb):
                        found = True
                        break
                if found:
                    break
        items.append({
            "type": f["type"], "position": f["position"],
            "pixel": (ix, iy), "rendered": found, "out_of_frame": oob,
        })
    n_ok = sum(1 for i in items if i["rendered"])
    return {"items": items, "summary": {
        "total": len(items), "rendered": n_ok,
        "missing": len(items) - n_ok,
        "out_of_frame": sum(1 for i in items if i["out_of_frame"])}}


def filter_vlm_false_negatives(vlm_issues: list[dict],
                               pixel_result: dict) -> tuple[list[dict], list[dict]]:
    """VLM 报告的 missing_furniture 项中，像素实测已渲染的 → 剔除（误报）。

    返回 (修正后 issues, 被剔除的误报列表)。
    """
    rendered_types_pos = [(i["type"], tuple(i["position"]))
                          for i in pixel_result.get("items", []) if i["rendered"]]
    kept, removed = [], []
    for issue in vlm_issues:
        is_missing_furn = "furniture" in str(issue.get("kind", "")) \
            and "missing" in str(issue.get("kind", ""))
        matched_rendered = False
        if is_missing_furn:
            desc = str(issue.get("description", ""))
            for t, pos in rendered_types_pos:
                if t in desc:
                    matched_rendered = True
                    break
        if is_missing_furn and matched_rendered:
            removed.append(issue)
        else:
            kept.append(issue)
    return kept, removed
