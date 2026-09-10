# app/tools/tri_validate.py
"""三向对账验证：ElementRegistry × 渲染图 × 标准图（CAD 真值）。

对账设计（docs/agent-optimization-plan-2026-09.md T3）：
  信号1（确定性）：registry 元素坐标 → 渲染图像素位置 → 语义配色核对
  信号2（确定性）：registry 与标准图渲染的几何 IoU 对账
  信号3（VLM）：视觉差异兜底（validation_loop）
合并输出：score + 结构化差异（kind/severity/description/action）。
"""

from pathlib import Path

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
                             window: int = 8,
                             build_plan_json: str | None = None) -> dict:
    """信号1：registry 每元素 → 渲染图对应像素 → 颜色核对（确定性）。

    build_plan_json：渲染所用 build_plan——传入则直接读真实 iso 相机参数
    （position/ortho_scale），映射与渲染完全一致；缺省按 bbox 估算。
    返回 {items: [{category, item, pixel, rendered}], summary}。
    要求渲染图为语义配色（ARP_SEMANTIC_COLORS=1）。
    """
    import json as _json

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
    if build_plan_json and Path(build_plan_json).exists():
        plan = _json.loads(Path(build_plan_json).read_text(encoding="utf-8"))
        cams = [c for c in plan.get("cameras", [])
                if c.get("view_id") == "view_iso"]
        if cams and cams[0].get("ortho"):
            cx = float(cams[0]["position"][0])
            cy = float(cams[0]["position"][1])
            scale = float(cams[0]["ortho_scale"])
    hw, hh = scale / 2, scale / 2 * (h / w)
    # 相机坐标是归一化系（0 起）；registry 是世界系——用 registry bbox 原点
    # 对齐两系（registry 由同一 scene 生成，其 bbox min = normalize 偏移）
    off_x, off_y = (x0, y0) if build_plan_json else (0.0, 0.0)

    def to_px(wx, wy):
        return (int(((wx - off_x) - (cx - hw)) / scale * w),
                int(((cy + hh) - (wy - off_y)) / (scale * h / w) * h))

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
        # 门标记条很薄（~8px 高）：门类用更大搜索窗
        win = 20 if e.category == '门' else window
        found = False
        hit_px = None
        for swx, swy in sample_pts:
            ex, ey = to_px(swx, swy)
            if not (0 <= ex < w and 0 <= ey < h):
                continue
            for dy in range(-win, win + 1, 2):
                for dx in range(-win, win + 1, 2):
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
                 vlm_model: str | None = None,
                 build_plan_json: str | None = None) -> dict:
    """三向对账主入口。

    registry: v3 输出（mm 或像素坐标，与渲染图同源即可）
    render_png: 语义配色渲染（ARP_SEMANTIC_COLORS=1 产出）
    standard_png: CAD 真值图（可选，供 VLM 兜底对比）
    返回 {signal1, vlm, combined_score, issues}
    """
    from app.tools.validation_loop import validate_render

    s1 = check_registry_in_render(registry, render_png,
                                  build_plan_json=build_plan_json)
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


# ── Registry × 标准图（CAD 真值）验证——识图 Agent 输出的第一道门禁 ────────────

_REGISTRY_CHECK_SYSTEM = (
    "你是建筑图纸审查专家。给定一张 CAD 平面布置图（真值）和一个由识图 Agent "
    "输出的结构化元素清单（JSON 摘要）。请逐类别核对清单与图的实际内容，"
    "找出清单中的错误（多报/少报/明显异常值）。"
    '只输出 JSON：{"verdict": "<一句话>", '
    '"category_checks": [{"category": "墙|房间|门|窗|家具", '
    '"listed": <清单数>, "visible_estimate": <图中目测数>, '
    '"verdict": "ok|over|under", "note": "<说明>"}], '
    '"issues": [{"kind": "over_count|under_count|fragmentation|other", '
    '"description": "<描述>", "severity": "high|medium|low", '
    '"action": "<修正建议>"}]}'
)


def _registry_summary(registry: ElementRegistry) -> str:
    from collections import Counter
    by_cat = Counter(e.category for e in registry.elements)
    lines = [f"元素总计 {len(registry.elements)}，按类别："]
    for cat, n in by_cat.most_common():
        items = Counter(e.item for e in registry.elements if e.category == cat)
        detail = ", ".join(f"{k}×{v}" for k, v in items.most_common(8))
        lines.append(f"  {cat}: {n}（{detail}）")
    # 墙的尺寸分布（碎片化检测线索）
    walls = [e for e in registry.elements if e.category == "墙" and e.world_bbox]
    if walls:
        lens = sorted(max(wb[2] - wb[0], wb[3] - wb[1]) for wb in
                      (e.world_bbox for e in walls if e.world_bbox))
        short = sum(1 for L in lens if L < 800)
        lines.append(f"  墙长分布: 最短{lens[0]:.0f}mm / 中位{lens[len(lens)//2]:.0f}mm / "
                     f"最长{lens[-1]:.0f}mm；<800mm 短段 {short} 条")
    return chr(10).join(lines)


def validate_registry(registry: ElementRegistry, standard_png: str, *,
                      model: str | None = None) -> dict:
    """识图 Agent 输出 × CAD 真值图 → 逐类别核对报告（第一道门禁）。

    返回 {verdict, category_checks, issues}。VLM 概率性结果，
    配合确定性摘要（墙长分布等）辅助判断。
    """
    import asyncio
    import base64

    from langchain_core.messages import HumanMessage, SystemMessage

    from app.core.config import get_settings
    from app.infra.llm import make_chat_model

    s = get_settings()
    use_model = model or s.vision_model or s.llm_model
    client = make_chat_model(s.model_copy(update={"llm_model": use_model}),
                             temperature=0.0)
    b64 = base64.b64encode(Path(standard_png).read_bytes()).decode()
    content = [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/png", "data": b64}}
        if "/api/anthropic" in s.llm_base_url else
        {"type": "image_url",
         "image_url": {"url": f"data:image/png;base64,{b64}"}},
        {"type": "text", "text":
            "图是 CAD 平面布置图真值。下方是识图 Agent 的元素清单摘要，"
            "请逐类别核对并输出差异 JSON。\n\n" + _registry_summary(registry)},
    ]
    resp = asyncio.run(client.ainvoke([
        SystemMessage(content=_REGISTRY_CHECK_SYSTEM),
        HumanMessage(content=content)]))  # type: ignore[arg-type]
    raw = resp.content if isinstance(resp.content, str) else         "".join(b.get("text", "") for b in resp.content if isinstance(b, dict))
    t = raw.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    import json as _json
    start = t.find("{")
    if start < 0:
        return {"verdict": "解析失败", "category_checks": [], "issues": [],
                "raw": raw[:200]}
    try:
        return _json.JSONDecoder().raw_decode(t[start:])[0]
    except (ValueError, _json.JSONDecodeError):
        return {"verdict": "解析失败", "category_checks": [], "issues": [],
                "raw": raw[:200]}


def registry_health(registry: ElementRegistry) -> dict:
    """确定性健康检查（无需 VLM）：碎片化/异常值检测。"""
    walls = [e for e in registry.elements
             if e.category == "墙" and e.world_bbox]
    issues = []
    if walls:
        lens = sorted(max(wb[2] - wb[0], wb[3] - wb[1]) for wb in
                      (e.world_bbox for e in walls if e.world_bbox))
        short = sum(1 for L in lens if L < 800)
        if short > len(walls) * 0.4:
            issues.append({"kind": "fragmentation",
                           "severity": "high",
                           "description": f"墙 {len(walls)} 条中 {short} 条 <800mm——"
                                          f"疑似多源碎片未合并",
                           "action": "墙段共线合并（merge_collinear）后再入 registry"})
    rooms = [e for e in registry.elements if e.category == "房间边界"]
    if len(rooms) > 10:
        issues.append({"kind": "over_count", "severity": "medium",
                       "description": f"房间 {len(rooms)} 个超过典型套房 6-9 分区",
                       "action": "房间去重/IoU 合并"})
    return {"wall_count": len(walls), "room_count": len(rooms),
            "issues": issues}


def check_elements_anchored(registry: ElementRegistry, standard_png: str,
                           min_ink_ratio: float = 0.04) -> dict:
    """逐元素锚定验证（确定性）：元素 bbox_pct 区域在标准图上须有图墨。

    空白区元素 = 幻影（提取器虚构）。ink = 非近白像素占比。
    返回 {items: [{item, ink_ratio, anchored}], summary}。
    """
    w, h, bpp, pix = decode_png(standard_png)
    items = []
    for e in registry.elements:
        if not e.bbox_pct or len(e.bbox_pct) != 4:
            continue
        x0 = max(int(e.bbox_pct[0] * w), 0)
        y0 = max(int(e.bbox_pct[1] * h), 0)
        x1 = min(int(e.bbox_pct[2] * w), w - 1)
        y1 = min(int(e.bbox_pct[3] * h), h - 1)
        if x1 <= x0 or y1 <= y0:
            items.append({"category": e.category, "item": e.item,
                          "ink_ratio": 0.0, "anchored": False,
                          "note": "degenerate_bbox"})
            continue
        # 窗口搜索：标准图坐标框与 registry 框有少量偏移（渲染 pad +
        # 冻结视口差异 1~4%），细线元素单点采样必 miss → 以 bbox 为核心
        # 在 ±3% 图幅邻域内取最大 ink（容忍帧偏移，仍能抓空白区幻影）
        win = int(0.03 * min(w, h))
        best_ratio = 0.0
        for oy in (0, -win, win):
            for ox in (0, -win, win):
                sx0, sy0 = max(x0 + ox, 0), max(y0 + oy, 0)
                sx1 = min(x1 + ox, w - 1)
                sy1 = min(y1 + oy, h - 1)
                if sx1 <= sx0 or sy1 <= sy0:
                    continue
                total = ink = 0
                for y in range(sy0, sy1 + 1, 2):
                    for x in range(sx0, sx1 + 1, 2):
                        o = (y * w + x) * bpp
                        total += 1
                        if not all(v > 235 for v in (pix[o], pix[o+1], pix[o+2])):
                            ink += 1
                best_ratio = max(best_ratio, ink / max(total, 1))
        items.append({"category": e.category, "item": e.item,
                      "ink_ratio": round(best_ratio, 3),
                      "anchored": best_ratio >= min_ink_ratio})
    n_ok = sum(1 for i in items if i["anchored"])
    return {"items": items, "summary": {"total": len(items),
                                        "anchored": n_ok,
                                        "phantom": len(items) - n_ok}}
