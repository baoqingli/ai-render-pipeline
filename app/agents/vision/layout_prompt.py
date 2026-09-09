# app/agents/vision/layout_prompt.py
"""ElementRegistry → AI 渲染提示词（自然语言空间描述）。

不依赖 LLM——纯确定性模板拼装，输出可直接用于 SDXL/Flux 等模型的正向提示词。
"""

# 房间类型 → 英文渲染关键词
ROOM_PROMPT = {
    "客房": "hotel guest room, comfortable beds, warm lighting",
    "卧室": "bedroom, cozy bed, bedside tables",
    "走廊": "hallway, clean passage",
    "淋浴间": "shower room, glass partition, rain shower",
    "盥洗区": "washroom area, vanity counter, mirror",
    "马桶间": "toilet room, toilet, ventilation",
    "阳台": "balcony, lounge chairs, outdoor view",
    "衣帽间": "walk-in closet, hanging rods, shelves",
    "客厅": "living room, comfortable seating",
}

# 家具类型 → 英文渲染关键词
FURN_PROMPT = {
    "bed": "bed with pillows and headboard",
    "sofa": "comfortable sofa with cushions",
    "table": "table",
    "chair": "chair",
    "wardrobe": "wardrobe with hanging space",
    "cabinet": "cabinet",
    "tv": "TV on console",
    "toilet": "toilet",
    "sink": "sink with counter",
    "shower": "shower with glass partition",
    "plant": "potted plant",
}


def generate_render_prompt(registry, room_name: str = "") -> str:
    """从元素登记簿生成 AI 渲染提示词（正向，英文）。"""
    # 按房间分组
    zones_text = []
    for tile in registry.tiles:
        for el in tile.elements:
            if el.category == "房间名称":
                zones_text.append(el.item)

    furniture_parts = []
    for el in registry.elements:
        if el.category == "家具":
            ftype = el.item or "furniture"
            furniture_parts.append(ftype)

    # 基础描述
    parts = [
        "hotel guest room interior design",
        "top-down perspective",
        "photorealistic 3D render",
    ]
    if room_name:
        parts.append(f"{room_name} style")

    # 家具描述
    if furniture_parts:
        parts.append(", ".join(furniture_parts[:8]))

    # 空间描述
    parts.append("wood flooring, white walls, warm ambient lighting")

    return ", ".join(parts)


def generate_prompt_text(registry) -> str:
    """生成完整 AI 渲染提示词（含中文描述 + 英文正向提示词）。"""
    from collections import Counter

    Counter(e.category for e in registry.elements)
    furn_types = Counter(e.item or e.type for e in registry.elements
                         if e.category == "家具")

    zh_lines = [
        "酒店客房室内设计，",
    ]
    if furn_types:
        furn_str = "、".join(f"{k}" for k in furn_types)
        zh_lines.append(f"家具包括：{furn_str}。")
    zh_lines.append("整体风格：现代简约，暖色调，木质地板，白色墙面。")
    zh_lines.append("光照：自然采光，暖色辅助照明。")

    en_prompt = generate_render_prompt(registry)

    return "\n".join(zh_lines) + "\n\nPositive prompt:\n" + en_prompt
