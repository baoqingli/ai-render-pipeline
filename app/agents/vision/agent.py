# app/agents/vision/agent.py
"""CAD 图纸识别 Agent：VLM 多视图读图 → 图纸解读书（DrawingUnderstanding）。

架构定位（用户决策 2026-09-08 + 白模修正方案 §3.3）：
- 识别 Agent 负责**语义与粗位置**（图纸类型/图层语义/房间分区/家具清单/开口）；
- 确定性解析负责**精确几何**（DXF 实体毫米级坐标）；
- 解读书作为提取配置路由确定性代码，二者交叉验证（对账不匹配 → 降级/重问）。
"""
import asyncio
import base64
import contextlib
import json
from pathlib import Path

import ezdxf
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from app.agents.vision.taxonomy import SYSTEM_PROMPT_TILE
from app.core.config import get_settings
from app.infra.llm import make_chat_model
from app.models.tooling import Metrics, ToolError, ToolResult
from app.models.vision import DrawingUnderstanding, ElementRegistry, TileElement, TileReport
from app.tools.cad.inspect import inspect_dxf
from app.tools.cad_render import model_extent, render_modelspace

PROMPT_VERSION = "1"




SYSTEM_PROMPT = (
    "你是资深建筑室内设计图纸审图师。给定一张 CAD 图纸的渲染视图（按图层语义着色："
    "红=墙类、绿=天花、蓝=家具、紫=门窗、青=完成面/饰面、灰=其他），"
    "以及该图纸的图层实体统计表。请识别并输出《图纸解读书》JSON：\n"
    '1. "drawing_type"：图纸类型（furniture_layout 布置图 / ceiling_plan 天花图 / '
    "systems 水电系统图 / architecture 建筑图 / combined 综合图）\n"
    '2. "layer_semantics"：逐图层判断语义（wall_new 新砌墙 / wall_existing 原始结构墙 / '
    "partition 隔断 / furniture_fixed 固定家具 / furniture_loose 活动家具 / ceiling 天花 / "
    "floor_finish 地面 / door 门 / window 窗 / annotation 标注 / mep 机电 / other），"
    "并给出判断依据\n"
    '3. "zones"：功能分区（name 如 客房/走廊/淋浴间/盥洗区/马桶间/阳台，'
    "bbox_pct 为图面占比 [左,上,右,下]，0-1）\n"
    '4. "furniture"：可见家具（type: bed/sofa/table/chair/wardrobe/cabinet/tv/toilet/'
    "sink/shower/bathtub/plant/appliance/other；count 数量；location 位置描述；"
    "layer_guess 图层猜测）\n"
    '5. "walls_notes"：结构墙与内隔墙位置描述；"openings_notes"：门洞/开口位置\n'
    "只输出严格 JSON，不要 markdown 代码块。识别不到或不确定的字段用空值/other，"
    "不要编造。"
)



def _annotations_table(doc, view: tuple[float, float, float, float], top: int = 30) -> str:
    """模型空间文字标注 → 图面占比坐标表（供 VLM 命名分区/定位元素）。"""
    x0, y0, x1, y1 = view
    rows = []
    for e in doc.modelspace():
        if e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        with contextlib.suppress(Exception):
            txt = (e.dxf.text if e.dxftype() == "TEXT" else e.text).strip()
            ins = e.dxf.insert
            if not txt:
                continue
            px = (ins.x - x0) / (x1 - x0)
            py = (y1 - ins.y) / (y1 - y0)
            if 0 <= px <= 1 and 0 <= py <= 1:
                rows.append(f'  "{txt}" @ ({px:.2f}, {py:.2f})')
    return "\n".join(rows[:top]) if rows else "  （无）"


def _layer_table_text(dxf_path: str | Path, top: int = 18) -> str:
    rep = inspect_dxf(dxf_path)
    rows = []
    for lay in rep.layers[:top]:
        total = (lay.line_count + lay.polyline_count + lay.insert_count
                 + lay.text_count + lay.other_count)
        if total == 0:
            continue
        rows.append(f"  {lay.name}  实体{total}（线{lay.line_count}/多段线{lay.polyline_count}/"
                    f"块{lay.insert_count}/文字{lay.text_count}）")
    return "\n".join(rows)


def _b64(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


def _image_blocks(png_paths: list[Path]) -> list[dict]:
    s = get_settings()
    blocks: list[dict] = []
    for i, p in enumerate(png_paths):
        b64 = _b64(p)
        if "/api/anthropic" in s.llm_base_url:
            blocks.append({"type": "image",
                           "source": {"type": "base64", "media_type": "image/png",
                                      "data": b64}})
        else:
            blocks.append({"type": "image_url",
                           "image_url": {"url": f"data:image/png;base64,{b64}"}})
        blocks.append({"type": "text", "text": f"视图 {i + 1}/{len(png_paths)}"})
    return blocks


def _extract_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object in response")
    return json.loads(t[start:end + 1])


def _normalize_understanding(data: dict) -> dict:
    """容错归一化：VLM 偶尔偏离 schema（layer_semantics 输出 dict 等）。"""
    ls = data.get("layer_semantics")
    if isinstance(ls, dict):
        fixed = []
        for layer, val in ls.items():
            if isinstance(val, dict):
                fixed.append({"layer": layer, **val})
            else:
                fixed.append({"layer": layer, "semantic": str(val)})
        data["layer_semantics"] = fixed
    return data


def analyze_drawing(dxf_path: str | Path, out_json: str | Path | None = None,
                    *, model: str | None = None,
                    width_px: int = 2000) -> ToolResult[DrawingUnderstanding]:
    """多视图渲染 + VLM 识别 → DrawingUnderstanding（咨询层产物，不反写场景）。

    模型选择：显式 model > ARP_VISION_MODEL（Settings.vision_model，默认 glm-5.3-flash）
    > ARP_LLM_MODEL。
    """
    import asyncio

    s = get_settings()
    use_model = model or s.vision_model or s.llm_model
    dxf_path = str(Path(dxf_path).resolve())

    doc = ezdxf.readfile(dxf_path)
    gx0, gy0, gx1, gy1 = model_extent(doc)
    if gx1 <= gx0:
        return ToolResult(ok=False, error=ToolError(
            code="INPUT_INVALID", message="图纸无可渲染几何"))
    sw = gx1 - gx0
    views = [
        ("full", gx0, gy0, gx1, gy1),
        ("left", gx0, gy0, gx0 + sw * 0.55, gy1),
        ("right", gx1 - sw * 0.55, gy0, gx1, gy1),
    ]
    out_dir = Path(dxf_path).parent / "_vision"
    out_dir.mkdir(parents=True, exist_ok=True)
    images: list[Path] = []
    for label, vx0, vy0, vx1, vy1 in views:
        png = out_dir / f"{Path(dxf_path).stem}_{label}.png"
        render_modelspace(doc, (vx0, vy0, vx1, vy1), width_px, png)
        images.append(png)

    layer_table = _layer_table_text(dxf_path)
    ann_table = _annotations_table(doc, (gx0, gy0, gx1, gy1))
    labels = [p.stem.replace(Path(dxf_path).stem + "_", "") for p in images]
    task = (f"以上是同一张 CAD 图纸的 {len(images)} 个渲染视图"
            f"（{', '.join(labels)}）。"
            "结合下方图层实体统计表与文字标注表，输出《图纸解读书》JSON。"
            f"\n\n图层实体统计表：\n{layer_table}"
            f"\n\n文字标注表（图面占比坐标，左上原点）：\n{ann_table}")

    client = make_chat_model(s.model_copy(update={"llm_model": use_model}), temperature=0.0)
    content: list[dict] = _image_blocks(images)
    content.append({"type": "text", "text": task})
    messages = [SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=content)]  # type: ignore[arg-type]

    async def _invoke():
        return await client.ainvoke(messages)

    try:
        resp = asyncio.run(_invoke())
    except Exception as e:                          # noqa: BLE001 端点/模型/额度错误：咨询层不阻断主流程
        return ToolResult(ok=False, error=ToolError(
            code="VISION_LLM_ERROR", message=str(e)[:300], retryable=True))

    raw = resp.content if isinstance(resp.content, str) else \
        "".join(b.get("text", "") for b in resp.content if isinstance(b, dict))
    try:
        understanding = DrawingUnderstanding.model_validate(
            _normalize_understanding(_extract_json(raw)))
    except (ValueError, json.JSONDecodeError, ValidationError) as e:
        return ToolResult(ok=False, error=ToolError(
            code="VISION_PARSE_FAILED", message=f"{e}; raw={raw[:300]}"))
    understanding.model = use_model
    if out_json:
        Path(out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(out_json).write_text(understanding.model_dump_json(indent=1), encoding="utf-8")
    return ToolResult(ok=True, data=understanding, metrics=Metrics())


# ── 分块多图识别 + 交叉验证 + 精确回填 ────────────────────────────────────────


def render_tiles(doc, extent: tuple[float, float, float, float],
                 out_dir: Path, grid: tuple[int, int] = (3, 2),
                 width_px: int = 1400) -> list[tuple[int, Path, tuple]]:
    """模型空间按网格分块渲染 → [(tile 序号, 路径, 世界裁剪框)]。"""
    x0, y0, x1, y1 = extent
    w, h = (x1 - x0) / grid[0], (y1 - y0) / grid[1]
    out_dir.mkdir(parents=True, exist_ok=True)
    tiles = []
    for r in range(grid[1]):
        for c in range(grid[0]):
            vx0 = x0 + c * w - w * 0.03        # 3% 重叠避免边界元素被切
            vy0 = y0 + r * h - h * 0.03
            vx1 = x0 + (c + 1) * w + w * 0.03
            vy1 = y0 + (r + 1) * h + h * 0.03
            no = r * grid[0] + c + 1
            p = out_dir / f"tile_r{r}c{c}.png"
            render_modelspace(doc, (vx0, vy0, vx1, vy1), width_px, p)
            tiles.append((no, p, (vx0, vy0, vx1, vy1)))
    return tiles


def _analyze_one_tile(client, png: Path, tile_no: int,
                      layer_table: str, ann_table: str) -> TileReport:
    """单分块 VLM 识别 → TileReport（失败返回空报告）。"""
    content: list[dict] = _image_blocks([png])
    task = (f"这是 CAD 图纸的分块 {tile_no}。"
            "结合下方图层实体统计表，输出该分块的元素识别 JSON。"
            f"\n\n图层实体统计表（全图）：\n{layer_table}")
    content.append({"type": "text", "text": task})
    messages = [SystemMessage(content=SYSTEM_PROMPT_TILE),
                HumanMessage(content=content)]  # type: ignore[arg-type]

    async def _invoke():
        return await client.ainvoke(messages)

    resp = asyncio.run(_invoke())
    raw = resp.content if isinstance(resp.content, str) else \
        "".join(b.get("text", "") for b in resp.content if isinstance(b, dict))
    data = _extract_json(raw)
    els = []
    for el in data.get("elements", []):
        el = dict(el)
        el["tile"] = tile_no
        els.append(TileElement(**el))
    return TileReport(tile=tile_no, drawing_type=str(data.get("drawing_type", "")),
                      elements=els)


def _world_bbox_of_element(el: TileElement, box: tuple) -> list[float]:
    bx0, by0, bx1, by1 = box
    px = el.bbox_pct if len(el.bbox_pct) == 4 else [0, 0, 1, 1]
    ex0 = bx0 + (bx1 - bx0) * min(max(px[0], -0.05), 1.05)
    ey0 = by0 + (by1 - by0) * min(max(px[1], -0.05), 1.05)
    ex1 = bx0 + (bx1 - bx0) * min(max(px[2], -0.05), 1.05)
    ey1 = by0 + (by1 - by0) * min(max(px[3], -0.05), 1.05)
    return [round(ex0), round(ey0), round(ex1), round(ey1)]


def _iou(a: list[float], b: list[float]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    aa = (a[2] - a[0]) * (a[3] - a[1])
    ab = (b[2] - b[0]) * (b[3] - b[1])
    return inter / max(aa + ab - inter, 1.0)


def _dedupe_elements(elements: list[TileElement],
                     boxes: dict[int, tuple]) -> tuple[list[TileElement], int]:
    """世界坐标 IoU 去重（相邻分块 3% 重叠区的同一元素）。"""
    kept: list[TileElement] = []
    removed = 0
    for el in elements:
        box = boxes.get(el.tile)
        wb = _world_bbox_of_element(el, box) if box else None
        dup = False
        if wb:
            for k in kept:
                kb = _world_bbox_of_element(k, boxes.get(k.tile, (0, 0, 1, 1)))
                if k.category == el.category and _iou(wb, kb) > 0.7:
                    dup = True
                    break
        if dup:
            removed += 1
        else:
            if wb:
                el.world_bbox = wb
            kept.append(el)
    return kept, removed


def analyze_tiles(dxf_path: str | Path, out_json: str | Path | None = None,
                  *, model: str | None = None, grid: tuple[int, int] = (3, 2),
                  width_px: int = 1400) -> ToolResult[ElementRegistry]:
    """分块多图识别（话术 PDF 元素全集）→ 世界坐标合并去重 → 元素登记簿。"""

    s = get_settings()
    use_model = model or s.vision_model or s.llm_model
    dxf_path = str(Path(dxf_path).resolve())

    doc = ezdxf.readfile(dxf_path)
    extent = model_extent(doc)
    if extent[2] <= extent[0]:
        return ToolResult(ok=False, error=ToolError(
            code="INPUT_INVALID", message="图纸无可渲染几何"))

    tiles = render_tiles(doc, extent, Path(dxf_path).parent / "_vision", grid, width_px)
    layer_table = _layer_table_text(dxf_path)
    ann_table = _annotations_table(doc, extent)

    client = make_chat_model(s.model_copy(update={"llm_model": use_model}), temperature=0.0)
    tile_reports: list[TileReport] = []
    for no, png, _box in tiles:
        with contextlib.suppress(Exception):
            tr = _analyze_one_tile(client, png, no, layer_table, ann_table)
            tile_reports.append(tr)

    if not tile_reports:
        return ToolResult(ok=False, error=ToolError(
            code="VISION_PARSE_FAILED", message="所有分块识别均失败"))

    # 分块序号 → 世界框
    gx0, gy0, gx1, gy1 = extent
    gw, gh = (gx1 - gx0) / grid[0], (gy1 - gy0) / grid[1]
    boxes: dict[int, tuple] = {}
    for r in range(grid[1]):
        for c in range(grid[0]):
            boxes[r * grid[0] + c + 1] = (gx0 + c * gw, gy0 + r * gh,
                                          gx0 + (c + 1) * gw, gy0 + (r + 1) * gh)

    all_elements = [el for tr in tile_reports for el in tr.elements]
    for el in all_elements:
        box = boxes.get(el.tile)
        if box:
            el.world_bbox = _world_bbox_of_element(el, box)
    elements, removed = _dedupe_elements(all_elements, boxes)

    registry = ElementRegistry(
        source_dxf=dxf_path, tiles=tile_reports, elements=elements,
        best_plan_view="model_space",
        cross_notes=(f"分块 {grid[0]}x{grid[1]}，识别元素 {len(all_elements)} → "
                     f"去重后 {len(elements)}（跨块重复去除 {removed}）"),
        model=use_model)
    if out_json:
        Path(out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(out_json).write_text(registry.model_dump_json(indent=1), encoding="utf-8")
    return ToolResult(ok=True, data=registry, metrics=Metrics())
