# app/agents/vision/agent_v3.py
"""识图 Agent v3：统一入口（DWG/DXF 走矢量路径 A，图片走分割路径 B）。

架构（docs/agent-optimization-plan-2026-09.md）：
  路径 A（DWG/DXF）：DXF 实体 + HATCH + 尺寸链 defpoints（毫米精确）
  路径 B（PNG/JPG）：CubiCasa5K 分割（像素几何）+ VLM 尺寸文字定标（px/mm）
  职责硬隔离：几何全部来自确定性引擎/预训练模型，VLM 只出语义与尺寸数字。
输出统一 ElementRegistry（world_bbox 为 mm，路径 B 未定标时为像素）。
"""
import asyncio
import contextlib
import json
import math
from pathlib import Path

from app.models.tooling import Metrics, ToolError, ToolResult
from app.models.vision import ElementRegistry, TileElement, TileReport

# CubiCasa 图标类 → 登记簿类别
_ICON_CAT = {
    "Window": "窗", "Door": "门", "Closet": "固定柜", "Toilet": "卫浴",
    "Sink": "卫浴", "Bathtub": "卫浴", "Electr. Appl.": "设备",
    "Sauna bench": "固定柜", "Fire Place": "设备", "Chimney": "设备",
}
# CubiCasa 房间类 → 登记簿类别
_ROOM_CAT = {
    "Kitchen": "厨房", "Living Room": "客厅", "Bedroom": "卧室", "Bath": "卫浴",
    "Hallway": "走廊通道", "Storage": "储物", "Garage": "其他", "Other rooms": "其他",
    "Outdoor": "阳台", "Wall": "墙",
}


# ── 路径 B：VLM 尺寸定标 ────────────────────────────────────────────────────────

_CALIB_SYSTEM = (
    "你是图纸尺寸标注读取专家。给定户型图，找出图中的尺寸标注数字"
    "（如 9600、4400、3300 等）并估计其标注线在图上的位置。"
    "只输出 JSON：{\"dims\": [{\"mm\": <数字>, \"x0\": <0-1>, \"y0\": <0-1>, "
    "\"x1\": <0-1>, \"y1\": <0-1>}, ...]}，"
    "(x0,y0)-(x1,y1) 是该标注线两端点（延伸线起点）的图面占比坐标。"
    "至少给 2 条最长的总尺寸。没有尺寸则 {\"dims\": []}。"
)


def _b64(path: Path) -> str:
    import base64
    return base64.b64encode(path.read_bytes()).decode()


def read_dimension_texts(png_path: str | Path, *,
                         model: str | None = None) -> list[dict]:
    """VLM 读图中尺寸标注（数字+端点位置）→ 定标参照列表。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.core.config import get_settings
    from app.infra.llm import make_chat_model

    s = get_settings()
    use_model = model or s.vision_model or s.llm_model
    p = Path(png_path)
    client = make_chat_model(s.model_copy(update={"llm_model": use_model}),
                             temperature=0.0)
    b64 = _b64(p)
    content = [{"type": "image",
                "source": {"type": "base64", "media_type": "image/png",
                           "data": b64}}
               if "/api/anthropic" in s.llm_base_url else
               {"type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"}},
               {"type": "text", "text": "读出图中尺寸标注。"}]
    resp = asyncio.run(client.ainvoke([
        SystemMessage(content=_CALIB_SYSTEM),
        HumanMessage(content=content)]))  # type: ignore[arg-type]
    raw = resp.content if isinstance(resp.content, str) else \
        "".join(b.get("text", "") for b in resp.content
                if isinstance(b, dict))
    t = raw.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    start = t.find("{")
    if start < 0:
        return []
    with contextlib.suppress(ValueError, json.JSONDecodeError):
        return json.JSONDecoder().raw_decode(t[start:])[0].get("dims", [])
    return []


def calibrate_from_vlm(png_path: str | Path, img_w: int, img_h: int, *,
                       model: str | None = None) -> tuple[float | None, list[dict]]:
    """VLM 尺寸文字 → px/mm 比例尺（中位数，抗单条误读）。"""
    dims = read_dimension_texts(png_path, model=model)
    ratios = []
    for d in dims:
        px = math.hypot((d["x1"] - d["x0"]) * img_w, (d["y1"] - d["y0"]) * img_h)
        mm = float(d.get("mm", 0))
        if px > 10 and mm > 500:
            ratios.append(px / mm)
    if not ratios:
        return None, dims
    ratios.sort()
    return ratios[len(ratios) // 2], dims


# ── 路径 B：CubiCasa 分割 → ElementRegistry ───────────────────────────────────

def analyze_image(png_path: str | Path, *, model: str | None = None,
                  calibrate_model: str | None = None) -> ToolResult[ElementRegistry]:
    """图片路径：CubiCasa 分割（几何）+ VLM 定标（px/mm）→ ElementRegistry。"""
    p = Path(png_path)
    if not p.exists():
        return ToolResult(ok=False, error=ToolError(
            code="INPUT_INVALID", message=f"file not found: {p}"))
    try:
        from app.tools.cubicasa_segmenter import icon_boxes, room_polygons, segment
    except (ImportError, FileNotFoundError) as e:
        return ToolResult(ok=False, error=ToolError(
            code="CUBICASA_UNAVAILABLE", message=str(e)[:200], retryable=False))

    seg = segment(str(p), size=256)
    px_per_mm, dims = calibrate_from_vlm(p, seg["size"], seg["size"],
                                          model=calibrate_model or model)

    def to_world(bbox_px):
        x0, y0, x1, y1 = bbox_px
        if px_per_mm:
            # 像素→mm（y 翻转：图像向下 → CAD 向上）
            return [round(x0 / px_per_mm), round((seg["size"] - y1) / px_per_mm),
                    round(x1 / px_per_mm), round((seg["size"] - y0) / px_per_mm)]
        return [x0, y0, x1, y1]   # 未定标：保留像素坐标

    elements: list[TileElement] = []
    for r in room_polygons(seg):
        cat = _ROOM_CAT.get(r["type"], "其他")
        elements.append(TileElement(
            category=cat, item=r["type"], count=1,
            bbox_pct=[v / seg["size"] for v in r["bbox_px"]],
            confidence=0.75, tile=1, world_bbox=to_world(r["bbox_px"])))
    for b in icon_boxes(seg):
        cat = _ICON_CAT.get(b["type"], "其他")
        elements.append(TileElement(
            category=cat, item=b["type"], count=1,
            bbox_pct=[v / seg["size"] for v in b["bbox_px"]],
            confidence=0.7, tile=1, world_bbox=to_world(b["bbox_px"])))

    reg = ElementRegistry(
        source_dxf=str(p), elements=elements,
        tiles=[TileReport(tile=1, drawing_type="furniture_layout",
                          elements=elements)],
        best_plan_view=str(p),
        cross_notes=(f"CubiCasa5K 分割 {len(elements)} 元素；"
                     + (f"VLM 定标 {px_per_mm:.4f}px/mm（{len(dims)} 条尺寸参照）"
                        if px_per_mm else "未定标（图无可读尺寸，坐标为像素）")),
    )
    return ToolResult(ok=True, data=reg, metrics=Metrics())


# ── 路径 A：DWG/DXF → ElementRegistry ────────────────────────────────────────

def analyze_dwg(dxf_path: str | Path) -> ToolResult[ElementRegistry]:
    """矢量路径：parse_scene（实体+HATCH+尺寸链）→ ElementRegistry。"""
    from app.models.scene import SceneJSON
    from app.tools.cad.parse import parse_scene

    r = parse_scene(str(dxf_path))
    if not r.ok or r.data is None:
        return ToolResult(ok=False, error=r.error or ToolError(
            code="PARSE_FAILED", message="parse_scene failed"))
    sc: SceneJSON = r.data
    return ToolResult(ok=True, data=scene_to_registry(sc, str(dxf_path)),
                      metrics=Metrics())


def scene_to_registry(sc, source: str) -> ElementRegistry:
    """SceneJSON → ElementRegistry（mm 坐标）。"""
    xs = [p[0] for w in sc.walls for p in w.polygon]
    ys = [p[1] for w in sc.walls for p in w.polygon]
    for r in sc.rooms:
        xs += [p[0] for p in r.polygon]
        ys += [p[1] for p in r.polygon]
    x0, y0 = (min(xs), min(ys)) if xs else (0, 0)
    x1, y1 = (max(xs), max(ys)) if xs else (1, 1)
    W, H = max(x1 - x0, 1), max(y1 - y0, 1)

    def wb(poly_or_pos, is_poly=True):
        if is_poly:
            px = [p[0] for p in poly_or_pos]
            py = [p[1] for p in poly_or_pos]
        else:
            px, py = [poly_or_pos[0]], [poly_or_pos[1]]
        return [round(min(px)), round(min(py)), round(max(px)), round(max(py))]

    def pct(bbox):
        return [round((bbox[0] - x0) / W, 3), round((bbox[1] - y0) / H, 3),
                round((bbox[2] - x0) / W, 3), round((bbox[3] - y0) / H, 3)]

    elements = []
    # 墙碎片合并：多边形 → 中心线段 → snap+共线合并 → 外接矩形
    # （验证 Agent 实测：不合并时 71 条中 52 条 <800mm，约为实际 2 倍）
    from shapely.geometry import Polygon as _Poly

    from app.tools.cad.walls import merge_collinear, snap_endpoints

    def _centerline(poly_pts):
        """墙多边形 → MRR 中心线段（(p1, p2)）。"""
        mrr = _Poly(poly_pts).minimum_rotated_rectangle
        c = list(mrr.exterior.coords)
        (ax, ay), (bx, by) = c[0], c[1]
        (dx_, dy_) = c[3], c[2]
        la = math.hypot(bx - ax, by - ay)
        lb = math.hypot(dx_[0] - ax, dx_[1] - ay)
        if la >= lb:   # 长边方向为中心线
            p1 = ((ax + c[3][0]) / 2, (ay + c[3][1]) / 2)
            p2 = ((bx + c[2][0]) / 2, (by + c[2][1]) / 2)
        else:
            p1 = ((ax + bx) / 2, (ay + by) / 2)
            p2 = ((c[3][0] + dx_[0]) / 2, (c[3][1] + dx_[1]) / 2)
        return p1, p2

    wall_segs = [_centerline(w.polygon) for w in sc.walls
                 if len(w.polygon) >= 4]
    wall_segs = [(p1, p2) for p1, p2 in wall_segs
                 if math.hypot(p2[0] - p1[0], p2[1] - p1[1]) > 50]
    merged_edges = merge_collinear(snap_endpoints(wall_segs))         if wall_segs else []
    n_walls = len(merged_edges)
    for p1, p2 in merged_edges:
        b = [float(min(p1[0], p2[0])), float(min(p1[1], p2[1])),
             float(max(p1[0], p2[0])), float(max(p1[1], p2[1]))]
        elements.append(TileElement(category="墙", item="wall", count=1,
                                    bbox_pct=pct(b), confidence=0.9,
                                    tile=1, world_bbox=b))
    # 房间 IoU 去重（VLM 分区矩形+细分可能产生重叠冗余）
    seen_rooms: list[tuple[float, float, float, float]] = []
    n_rooms = 0
    for r in sorted(sc.rooms, key=lambda r: -_Poly(r.polygon).area):
        b = [float(v) for v in wb(r.polygon)]
        dup = False
        for sb in seen_rooms:
            ix0, iy0 = max(b[0], sb[0]), max(b[1], sb[1])
            ix1, iy1 = min(b[2], sb[2]), min(b[3], sb[3])
            inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
            smaller = min((b[2]-b[0])*(b[3]-b[1]), (sb[2]-sb[0])*(sb[3]-sb[1]))
            if smaller > 0 and inter / smaller > 0.5:
                dup = True
                break
        if dup:
            continue
        seen_rooms.append((float(b[0]), float(b[1]), float(b[2]), float(b[3])))
        n_rooms += 1
        raw_name = r.name or "room"
        name = raw_name.replace("\\P", " ").split("\n")[0][:20] or "room"
        elements.append(TileElement(category="房间边界", item=name, count=1,
                                    bbox_pct=pct(b), confidence=0.85,
                                    tile=1, world_bbox=b))
    for d in sc.doors:
        elements.append(TileElement(category="门", item="door", count=1,
                                    bbox_pct=pct(wb(d.position, False)),
                                    confidence=0.9, tile=1,
                                    world_bbox=wb(d.position, False)))
    for w in sc.windows:
        elements.append(TileElement(category="窗", item="window", count=1,
                                    bbox_pct=pct(wb(w.position, False)),
                                    confidence=0.9, tile=1,
                                    world_bbox=wb(w.position, False)))
    for f in sc.furniture:
        fx, fy = f.position
        fw, fd = f.size[0] / 2, f.size[1] / 2
        b = [round(fx - fw), round(fy - fd), round(fx + fw), round(fy + fd)]
        elements.append(TileElement(category="家具", item=f.type, count=1,
                                    bbox_pct=pct(b), confidence=0.8, tile=1,
                                    world_bbox=b))
    return ElementRegistry(
        source_dxf=source, elements=elements,
        tiles=[TileReport(tile=1, drawing_type="furniture_layout",
                          elements=elements)],
        best_plan_view=source,
        cross_notes=f"矢量路径：墙合并后 {n_walls} 段（原始 {len(sc.walls)}）/"
                    f" 房间 {n_rooms} / {len(sc.doors)} 门 / {len(sc.windows)} 窗 / "
                    f"{len(sc.furniture)} 家具（mm 坐标）")


# ── 统一入口 ──────────────────────────────────────────────────────────────────

def analyze(file_path: str | Path, *, vlm_model: str | None = None,
            out_json: str | Path | None = None) -> ToolResult[ElementRegistry]:
    """统一识图入口：按扩展名分路，输出 ElementRegistry。"""
    p = Path(file_path)
    suffix = p.suffix.lower()
    if suffix in (".dwg", ".dxf"):
        result = analyze_dwg(p)
    elif suffix in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
        result = analyze_image(p, model=vlm_model)
    else:
        return ToolResult(ok=False, error=ToolError(
            code="INPUT_INVALID", message=f"unsupported: {suffix}"))
    if result.ok and out_json and result.data is not None:
        Path(out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(out_json).write_text(result.data.model_dump_json(indent=1),
                                  encoding="utf-8")
    return result
