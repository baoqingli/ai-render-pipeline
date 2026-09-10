# app/agents/vision/gated_analyze.py
"""门禁式识图：analyze → 验证 → 定向修正 → 再验证 → 达标才放行。

闭环设计（用户架构）：识图 Agent 输出必须过验证 Agent 门禁，
不通过的项触发定向修正（确定性规则），循环直到达标或迭代上限。
输出 registry + 逐轮验证记录（可追溯），供下游按置信度消费。
"""
import math
from pathlib import Path

from app.models.tooling import Metrics, ToolError, ToolResult
from app.models.vision import ElementRegistry, TileElement
from app.tools.tri_validate import registry_health, validate_registry

MAX_ITERS = 3
# 门禁阈值：墙碎片率（<800mm 占比）、房间数上限
_WALL_FRAG_MAX = 0.35
_ROOM_MAX = 9


# ── 定向修正（按验证发现的问题类型） ──────────────────────────────────────────

def _fix_wall_fragmentation(registry: ElementRegistry, boost: float) -> int:
    """墙碎片化修正：提高合并容差重合并中心线。

    boost 每轮递增（1.0→2.0→3.0 倍容差），合并率单调上升。
    返回移除的碎片数。
    """
    from app.tools.cad.walls import merge_collinear, snap_endpoints
    walls = [e for e in registry.elements
             if e.category == "墙" and e.world_bbox]
    if not walls:
        return 0
    # bbox → 中心线段
    segs = []
    for e in walls:
        wb = e.world_bbox
        assert wb is not None
        x0, y0, x1, y1 = wb
        if x1 - x0 >= y1 - y0:
            segs.append(((x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2)))
        else:
            segs.append((((x0 + x1) / 2, y0), ((x0 + x1) / 2, y1)))
    tol = 300.0 * boost
    snapped = snap_endpoints(segs, tol=min(tol, 1500.0)) or []
    merged = merge_collinear(snapped,
        lateral_tol=150.0 * boost, gap_tol=300.0 * boost)
    n_before = len(walls)
    # 用合并结果替换墙元素
    xs = [c for e in registry.elements if e.world_bbox
          for c in (e.world_bbox[0], e.world_bbox[2])]
    ys = [c for e in registry.elements if e.world_bbox
          for c in (e.world_bbox[1], e.world_bbox[3])]
    x0, y0 = min(xs), min(ys)
    W = max(max(xs) - x0, 1)
    H = max(max(ys) - y0, 1)
    new_walls = []
    for p1, p2 in merged:
        if math.hypot(p2[0] - p1[0], p2[1] - p1[1]) < 200:
            continue
        b = [float(min(p1[0], p2[0])), float(min(p1[1], p2[1])),
             float(max(p1[0], p2[0])), float(max(p1[1], p2[1]))]
        new_walls.append(TileElement(
            category="墙", item="wall", count=1,
            bbox_pct=[round((b[0] - x0) / W, 3), round((b[1] - y0) / H, 3),
                      round((b[2] - x0) / W, 3), round((b[3] - y0) / H, 3)],
            confidence=0.9, tile=1, world_bbox=b))
    registry.elements = ([e for e in registry.elements if e.category != "墙"]
                         + new_walls)
    return n_before - len(new_walls)


def _fix_room_overcount(registry: ElementRegistry) -> int:
    """房间超量修正：bbox IoU/包含去重 + 小房间并入大房间。

    返回移除的房间数。
    """
    room_items = [(e, e.world_bbox) for e in registry.elements
                  if e.category == "房间边界" and e.world_bbox]
    rooms = sorted(room_items,
                   key=lambda pair: -((pair[1][2] - pair[1][0])
                                      * (pair[1][3] - pair[1][1])))
    kept: list[list[float]] = []
    kept_elems: list = []
    removed = 0
    for r_elem, b in rooms:
        area = (b[2] - b[0]) * (b[3] - b[1])
        dup = False
        for kb in kept:
            ix0, iy0 = max(b[0], kb[0]), max(b[1], kb[1])
            ix1, iy1 = min(b[2], kb[2]), min(b[3], kb[3])
            inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
            karea = (kb[2] - kb[0]) * (kb[3] - kb[1])
            if inter > 0 and (inter / min(area, karea) > 0.4):
                dup = True
                break
        if dup:
            removed += 1
            continue
        kept.append(list(b))
        kept_elems.append(r_elem)
    # 超出上限时：最小面积房间并入最近邻（面积合并，标注 merged）
    while len(kept) > _ROOM_MAX:
        smallest_i = min(range(len(kept)),
                         key=lambda i: (kept[i][2] - kept[i][0])
                         * (kept[i][3] - kept[i][1]))
        # 并入与它重叠最大的保留房间
        best_j, best_ov = None, 0.0
        b = kept[smallest_i]
        for j, kb in enumerate(kept):
            if j == smallest_i:
                continue
            ix0, iy0 = max(b[0], kb[0]), max(b[1], kb[1])
            ix1, iy1 = min(b[2], kb[2]), min(b[3], kb[3])
            ov = max(0, ix1 - ix0) * max(0, iy1 - iy0)
            if ov > best_ov:
                best_ov, best_j = ov, j
        if best_j is None or best_ov <= 0:
            # 无重叠邻居：直接放弃最小房间（开敞区误判）
            kept.pop(smallest_i)
            removed += 1
        else:
            kept[best_j] = [min(kept[best_j][0], b[0]),
                            min(kept[best_j][1], b[1]),
                            max(kept[best_j][2], b[2]),
                            max(kept[best_j][3], b[3])]
            kept.pop(smallest_i)
            removed += 1
    registry.elements = ([e for e in registry.elements
                         if e.category != "房间边界"]
                        + kept_elems[:_ROOM_MAX])
    return removed


# ── 门禁主循环 ────────────────────────────────────────────────────────────────

def analyze_with_gate(dxf_path: str | Path, standard_png: str | Path, *,
                      vlm_model: str | None = None,
                      out_dir: str | Path | None = None) -> ToolResult[dict]:
    """识图 + 验证闭环：达标（或迭代上限）后输出 registry + 验证记录。

    返回 data = {registry, rounds: [{iter, health, vlm, fixes}],
                 passed: bool, final_counts}
    """
    from app.agents.vision.agent_v3 import analyze_dwg

    r = analyze_dwg(dxf_path)
    if not r.ok or r.data is None:
        return ToolResult(ok=False, error=r.error or ToolError(
            code="ANALYZE_FAILED", message="analyze_dwg failed"))
    registry = r.data
    rounds: list[dict] = []
    boost = 1.0

    for it in range(1, MAX_ITERS + 1):
        health = registry_health(registry)
        use_vlm = it == MAX_ITERS or not health["issues"]  # 末轮或干净时 VLM 终检
        vlm = validate_registry(registry, str(standard_png),
                                model=vlm_model) if use_vlm else None
        frag_bad = any(i["kind"] == "fragmentation" for i in health["issues"])
        room_bad = any(i["kind"] == "over_count" for i in health["issues"])
        fixes: list[str] = []
        if frag_bad and it < MAX_ITERS:
            removed = _fix_wall_fragmentation(registry, boost)
            boost += 1.0
            fixes.append(f"wall_merge(-{removed})")
        if room_bad and it < MAX_ITERS:
            removed_r = _fix_room_overcount(registry)
            fixes.append(f"room_merge(-{removed_r})")
        rounds.append({"iter": it,
                      "walls": health["wall_count"],
                      "rooms": health["room_count"],
                      "health_issues": len(health["issues"]),
                      "fixes": fixes,
                      "vlm_verdict": (vlm or {}).get("verdict", "")[:60] if vlm else "-"})
        if not fixes and (vlm is not None):
            break   # 无可修项且已 VLM 终检 → 退出

    final_health = registry_health(registry)
    walls = [e for e in registry.elements if e.category == "墙"]
    frag_ratio = (sum(1 for e in walls if e.world_bbox
                      and max(e.world_bbox[2] - e.world_bbox[0],
                             e.world_bbox[3] - e.world_bbox[1]) < 800)
                  / max(len(walls), 1))
    passed = (frag_ratio <= _WALL_FRAG_MAX
              and final_health["room_count"] <= _ROOM_MAX)
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "element_registry.json").write_text(
            registry.model_dump_json(indent=1), encoding="utf-8")
        import json as _json
        (out / "gate_report.json").write_text(_json.dumps(
            {"rounds": rounds, "passed": passed,
             "final": final_health}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    return ToolResult(ok=True, data={
        "registry": registry, "rounds": rounds, "passed": passed,
        "final_counts": {"walls": len(walls),
                         "rooms": final_health["room_count"],
                         "furniture": sum(1 for e in registry.elements
                                          if e.category == "家具"),
                         "doors": sum(1 for e in registry.elements
                                      if e.category == "门"),
                         "windows": sum(1 for e in registry.elements
                                        if e.category == "窗")}},
        metrics=Metrics())
