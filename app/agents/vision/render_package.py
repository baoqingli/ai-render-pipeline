# app/agents/vision/render_package.py
"""AI 直出包：ElementRegistry → 图像生成大模型（gpt-image / SDXL）输入。

用户方案（docs/agent-optimization-plan-2026-09.md T4）：
  识图 Agent 输出 + 布局图 → 强大图像模型直接生成渲染图，
  不依赖 3D 白模管线也可出图。

输出三件套：
  1. layout_description：结构化布局描述（中/英，含房间/墙/门窗/家具清单）
  2. control_image：俯视线稿控制图（从 registry 重新绘制，语义配色）
  3. prompt：正向渲染 prompt（布局事实 + 风格占位符）
"""
from pathlib import Path

from app.models.vision import ElementRegistry

# 类别 → 英文渲染词
_CAT_EN = {
    "客厅": "living room", "卧室": "bedroom", "厨房": "kitchen", "卫浴": "bathroom",
    "走廊通道": "hallway", "阳台": "balcony", "储物": "storage room",
    "墙": "wall", "门": "door", "窗": "window", "家具": "furniture",
    "固定柜": "built-in cabinet", "设备": "appliance", "其他": "room",
    "房间边界": "room",
}
# CubiCasa 英文类名 → 中文（布局描述/审阅用）
_ITEM_ZH = {
    "Living Room": "客厅", "Bedroom": "卧室", "Kitchen": "厨房", "Bath": "卫生间",
    "Hallway": "走廊", "Storage": "储物间", "Garage": "车库", "Other rooms": "其他房间",
    "Outdoor": "室外/阳台", "Wall": "墙体", "Door": "门", "Window": "窗",
    "Closet": "衣柜", "Electr. Appl.": "电器", "Toilet": "马桶", "Sink": "台盆",
    "Bathtub": "浴缸", "Sauna bench": "桑拿椅", "Fire Place": "壁炉",
    "Chimney": "烟道", "Railing": "栏杆",
}

_ITEM_EN = {
    "bed": "double bed with pillows and headboard", "sofa": "sofa",
    "table": "table", "chair": "chair", "wardrobe": "wardrobe",
    "cabinet": "cabinet", "tv": "TV on console", "toilet": "toilet",
    "sink": "vanity sink", "bathtub": "bathtub", "shower": "shower area",
    "plant": "potted plant", "Door": "door", "Window": "window",
    "Closet": "walk-in closet", "Electr. Appl.": "appliance",
}

# 洁具关键词 → 该区域推断为卫生间
_BATHROOM_ITEMS = {"toilet", "bathtub", "shower", "sink"}
# 卧室关键词
_BEDROOM_ITEMS = {"bed"}

# 方位描述（归一化 bbox_pct 中心 → 方向词）
def _quadrant(cx: float, cy: float) -> str:
    """将归一化图像坐标 (cx, cy) 映射为方位词，y 轴向下（图像坐标系）。"""
    v = "upper" if cy < 0.45 else ("lower" if cy > 0.55 else "center")
    h = "left"  if cx < 0.40 else ("right"  if cx > 0.60 else "")
    return (v + "-" + h).rstrip("-") if h else v


def _infer_zones(reg: ElementRegistry) -> list[dict]:
    """从 elements 里的洁具/床推断功能区，返回 [{zone, direction, items}]。"""
    zones: list[dict] = []
    # 按 bbox 中心聚合洁具
    bathroom_pts: list[tuple[float, float]] = []
    bathroom_items: list[str] = []
    bedroom_pts:  list[tuple[float, float]] = []

    for e in reg.elements:
        item = (e.item or "").lower()
        bp = e.bbox_pct
        if not bp or len(bp) < 4:
            continue
        cx = (bp[0] + bp[2]) / 2
        cy = (bp[1] + bp[3]) / 2
        if item in _BATHROOM_ITEMS:
            bathroom_pts.append((cx, cy))
            bathroom_items.append(_ITEM_EN.get(e.item, e.item))
        elif item in _BEDROOM_ITEMS:
            bedroom_pts.append((cx, cy))

    if bathroom_pts:
        avg_x = sum(p[0] for p in bathroom_pts) / len(bathroom_pts)
        avg_y = sum(p[1] for p in bathroom_pts) / len(bathroom_pts)
        zones.append({
            "zone": "bathroom",
            "direction": _quadrant(avg_x, avg_y),
            "items": list(dict.fromkeys(bathroom_items)),
        })
    if bedroom_pts:
        avg_x = sum(p[0] for p in bedroom_pts) / len(bedroom_pts)
        avg_y = sum(p[1] for p in bedroom_pts) / len(bedroom_pts)
        zones.append({
            "zone": "bedroom",
            "direction": _quadrant(avg_x, avg_y),
            "items": ["double bed with pillows"],
        })
    return zones


def layout_description_zh(reg: ElementRegistry) -> str:
    """中文结构化布局描述（供用户审阅 / 二次编辑）。"""
    by_cat: dict[str, list] = {}
    for e in reg.elements:
        by_cat.setdefault(e.category, []).append(e)
    lines = ["布局描述（由识图 Agent 生成）："]
    rooms = by_cat.get("客厅", []) + by_cat.get("卧室", []) + \
        by_cat.get("厨房", []) + by_cat.get("卫浴", []) + \
        by_cat.get("走廊通道", []) + by_cat.get("阳台", [])
    if rooms:
        names = [_ITEM_ZH.get(r.item, r.item) for r in rooms]
        lines.append(f"功能区 {len(rooms)} 个：" + "、".join(names))
    if "门" in by_cat:
        lines.append(f"门 {len(by_cat['门'])} 樘")
    if "窗" in by_cat:
        lines.append(f"窗 {len(by_cat['窗'])} 樘")
    furn = by_cat.get("家具", []) + by_cat.get("固定柜", [])
    if furn:
        seen: dict[str, int] = {}
        for f in furn:
            seen[f.item] = seen.get(f.item, 0) + 1
        zh_seen = {_ITEM_ZH.get(k, k): v for k, v in seen.items()}
        lines.append("家具：" + "、".join(f"{k}×{v}" for k, v in zh_seen.items()))
    lines.append(f"坐标单位：{reg.cross_notes}")
    return "\n".join(lines)


def _room_zones(reg: ElementRegistry) -> list[dict]:
    """房间边界 → 逐间分区描述，家具按 bbox 中心归属到所在房间。

    返回 [{direction, kind, items}]，按图面位置排序（上→下、左→右），
    供 build_prompt 生成空间明确的逐间描述。
    """
    _ROOM_CATS = ("房间边界", "客厅", "卧室", "厨房", "卫浴", "走廊通道", "阳台")
    rooms = [e for e in reg.elements
             if e.category in _ROOM_CATS and e.bbox_pct and len(e.bbox_pct) >= 4]
    furniture = [e for e in reg.elements
                 if e.category in ("家具", "固定柜", "设备")
                 and e.bbox_pct and len(e.bbox_pct) >= 4]

    zones = []
    for r in rooms:
        bx0, by0, bx1, by1 = r.bbox_pct
        inside = []
        for f in furniture:
            fx = (f.bbox_pct[0] + f.bbox_pct[2]) / 2
            fy = (f.bbox_pct[1] + f.bbox_pct[3]) / 2
            if bx0 <= fx <= bx1 and by0 <= fy <= by1:
                inside.append(f)
        kinds = {(f.item or "").lower() for f in inside}
        if kinds & _BATHROOM_ITEMS:
            kind = "bathroom"
        elif kinds & _BEDROOM_ITEMS:
            kind = "bedroom"
        elif {"sofa", "tv"} & kinds:
            kind = "living room"
        elif {"wardrobe", "cabinet"} & kinds:
            kind = "walk-in closet"
        elif r.category in _ITEM_ZH or r.item in _ITEM_ZH:
            kind = _CAT_EN.get(r.category, "room")
        else:
            kind = "room"
        zones.append({
            "direction": _quadrant((bx0 + bx1) / 2, (by0 + by1) / 2),
            "kind": kind,
            "items": [_ITEM_EN.get(f.item, f.item) for f in inside],
        })
    # 稳定排序：上→下、左→右，保证同一 registry 多次生成 prompt 一致
    _DIR_ORDER = {"upper-left": 0, "upper": 1, "upper-right": 2,
                  "left": 3, "center": 4, "right": 5,
                  "lower-left": 6, "lower": 7, "lower-right": 8}
    zones.sort(key=lambda z: (_DIR_ORDER.get(z["direction"], 9), z["kind"]))
    return zones


def build_prompt(reg: ElementRegistry, style: str = "modern cozy hotel room, "
                   "warm wood flooring, white walls, soft natural lighting") -> str:
    """正向渲染 prompt：逐房间空间描述（英文）+ 风格层。

    房间级描述（方位+类型+家具）比全局家具清单更能约束图像模型的
    空间布局；配合参考图指令可显著提高结构一致性。
    """
    by_cat: dict[str, list] = {}
    for e in reg.elements:
        by_cat.setdefault(e.category, []).append(e)

    parts = ["interior design rendering, top-down dollhouse cutaway view "
             "of the exact attached floor plan",
             "STRICTLY preserve the floor plan layout: every wall, room "
             "division, door and window stays in its planned position, "
             "do not merge or remove rooms",
             "photorealistic, architecturally accurate layout"]

    # 逐房间空间描述（家具已归属到房间）
    zones = _room_zones(reg)
    for z in zones:
        if z["items"]:
            items_str = " with " + ", ".join(dict.fromkeys(z["items"]))
        else:
            items_str = ""
        parts.append(f"{z['direction']}: {z['kind']}{items_str}")

    if by_cat.get("窗"):
        parts.append(f"{len(by_cat['窗'])} windows with natural light")
    if by_cat.get("门"):
        parts.append(f"{len(by_cat['门'])} door openings as planned")
    parts.append(style)
    return ", ".join(dict.fromkeys(parts))   # 去重保序


def draw_semantic_reference(reg: ElementRegistry, out_png: str | Path,
                            width: int = 1536) -> Path:
    """语义配色俯视参考图（喂 gpt-image 类模型的 image 参数）。

    CAD 全图层渲染（虚线、无填充）对图像模型可读性差；本图按模型易读的
    方式重画：房间按功能填色、墙体实心黑、家具深灰描边、门窗高亮。
    坐标变换与 bbox_pct 同向（y-down），保证与 build_prompt 的方位词一致。
    """
    from PIL import Image, ImageDraw

    bboxes = [e.world_bbox for e in reg.elements if e.world_bbox]
    if not bboxes:
        raise ValueError("registry 无坐标元素")
    x0 = min(b[0] for b in bboxes)
    y0 = min(b[1] for b in bboxes)
    x1 = max(b[2] for b in bboxes)
    y1 = max(b[3] for b in bboxes)
    W, H = max(x1 - x0, 1), max(y1 - y0, 1)
    height = max(int(width * H / W), 1)

    def to_px(wx: float, wy: float) -> tuple[float, float]:
        return ((wx - x0) / W * width, (1 - (wy - y0) / H) * height)

    img = Image.new("RGB", (width, height), (255, 255, 255))
    dr = ImageDraw.Draw(img)

    # 房间填色（含家具归属分类，逻辑与 _room_zones 一致）
    _ROOM_CATS = ("房间边界", "客厅", "卧室", "厨房", "卫浴", "走廊通道", "阳台")
    rooms = [e for e in reg.elements
             if e.category in _ROOM_CATS and e.world_bbox]
    furniture = [e for e in reg.elements
                 if e.category in ("家具", "固定柜", "设备") and e.world_bbox]
    _FILL = {
        "bathroom": (198, 224, 244), "bedroom": (208, 234, 202),
        "living room": (245, 238, 196), "walk-in closet": (226, 212, 240),
        "kitchen": (247, 220, 190), "hallway": (234, 234, 234),
        "room": (243, 243, 243),
    }
    for r in rooms:
        bx0, by0, bx1, by1 = r.world_bbox
        cx_w, cy_w = (bx0 + bx1) / 2, (by0 + by1) / 2
        inside = [f for f in furniture
                  if bx0 <= (f.world_bbox[0] + f.world_bbox[2]) / 2 <= bx1
                  and by0 <= (f.world_bbox[1] + f.world_bbox[3]) / 2 <= by1]
        kinds = {(f.item or "").lower() for f in inside}
        if kinds & _BATHROOM_ITEMS:
            kind = "bathroom"
        elif kinds & _BEDROOM_ITEMS:
            kind = "bedroom"
        elif {"sofa", "tv"} & kinds:
            kind = "living room"
        elif {"wardrobe", "cabinet"} & kinds:
            kind = "walk-in closet"
        else:
            kind = _CAT_EN.get(r.category, "room")
        dr.rectangle([to_px(bx0, by1), to_px(bx1, by0)],
                     fill=_FILL.get(kind, _FILL["room"]))

    # 家具描边
    for f in furniture:
        bx0, by0, bx1, by1 = f.world_bbox
        dr.rectangle([to_px(bx0, by1), to_px(bx1, by0)],
                     outline=(95, 95, 95), width=3)

    # 墙体最后画（实心黑，压在填色之上）
    for w_el in reg.elements:
        if w_el.category != "墙" or not w_el.world_bbox:
            continue
        bx0, by0, bx1, by1 = w_el.world_bbox
        dr.rectangle([to_px(bx0, by1), to_px(bx1, by0)], fill=(28, 28, 28))

    # 门（橙）窗（蓝）高亮
    for e in reg.elements:
        if e.category not in ("门", "窗") or not e.world_bbox:
            continue
        bx0, by0, bx1, by1 = e.world_bbox
        dr.rectangle([to_px(bx0, by1), to_px(bx1, by0)],
                     fill=(235, 150, 60) if e.category == "门" else (70, 130, 210))

    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out))
    return out


def draw_control_image(reg: ElementRegistry, out_png: str | Path,
                       width: int = 1200) -> Path:
    """从 registry 重绘俯视线稿（黑白控制图，可喂 ControlNet lineart）。

    语义配色变体：墙深灰、房间边界浅灰、家具中灰、门窗标记。
    """

    from app.tools.cad_render import Canvas

    bboxes = [e.world_bbox for e in reg.elements if e.world_bbox]
    if not bboxes:
        raise ValueError("registry 无坐标元素")
    x0 = min(b[0] for b in bboxes)
    y0 = min(b[1] for b in bboxes)
    x1 = max(b[2] for b in bboxes)
    y1 = max(b[3] for b in bboxes)
    cv = Canvas(x0 - 500, y0 - 500, x1 + 500, y1 + 500, width)
    for e in reg.elements:
        if not e.world_bbox:
            continue
        bx0, by0, bx1, by1 = e.world_bbox
        if e.category == "墙":
            rgb, w = (40, 40, 40), 4
        elif e.category == "房间边界":
            rgb, w = (150, 150, 150), 2
        elif e.category in ("门", "窗"):
            rgb, w = (80, 80, 200), 3
        else:
            rgb, w = (100, 100, 100), 2
        # 画矩形边（Canvas 只有 line——画四边）
        cv.line(bx0, by0, bx1, by0, rgb, bold=w)
        cv.line(bx1, by0, bx1, by1, rgb, bold=w)
        cv.line(bx1, by1, bx0, by1, rgb, bold=w)
        cv.line(bx0, by1, bx0, by0, rgb, bold=w)
    out = Path(out_png)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv.save(str(out))
    return out


def build_package(reg: ElementRegistry, out_dir: str | Path,
                  style: str | None = None) -> dict:
    """生成完整 AI 直出包 → {description, prompt, control_image}。"""
    _style = style or ("modern cozy hotel room, warm wood flooring, "
                       "white walls, soft natural lighting")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ctrl = draw_control_image(reg, out / "layout_control.png")
    pkg = {
        "layout_description": layout_description_zh(reg),
        "prompt": build_prompt(reg, _style),
        "control_image": str(ctrl),
        "usage": ("control_image 喂 ControlNet(lineart)/img2img 底图；"
                  "prompt 喂正向提示词；风格可由 style 参数或 style agent 覆盖"),
    }
    (out / "render_package.json").write_text(
        __import__("json").dumps(pkg, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return pkg
