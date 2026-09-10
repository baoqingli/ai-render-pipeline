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


def build_prompt(reg: ElementRegistry, style: str = "modern cozy hotel room, "
                   "warm wood flooring, white walls, soft natural lighting") -> str:
    """正向渲染 prompt：布局事实（英文）+ 风格层（可替换）。"""
    by_cat: dict[str, list] = {}
    for e in reg.elements:
        by_cat.setdefault(e.category, []).append(e)
    parts = ["interior design rendering, top-down floor plan perspective",
             "photorealistic, architecturally accurate layout"]
    for cat in ("客厅", "卧室", "厨房", "卫浴", "走廊通道", "阳台"):
        for _room in by_cat.get(cat, []):
            parts.append(_CAT_EN.get(cat, "room"))
    furn_items = []
    for f in by_cat.get("家具", []) + by_cat.get("固定柜", []):
        furn_items.append(_ITEM_EN.get(f.item, f.item))
    if furn_items:
        parts.append("with " + ", ".join(sorted(set(furn_items))))
    if by_cat.get("窗"):
        parts.append(f"{len(by_cat['窗'])} windows with natural light")
    parts.append(style)
    return ", ".join(dict.fromkeys(parts))   # 去重保序


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
