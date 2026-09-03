# app/engines/prompt.py
from app.models.rendering import PromptPair, StyleParams

STYLE_PHRASES = {
    "modern_minimal": "modern minimalist interior design, clean lines",
    "cream": "cream style interior, soft rounded furniture, warm neutrals",
    "wood": "japanese-style warm wood aesthetic, natural materials",
    "french": "french elegant interior, moldings, refined details",
    "wabi_sabi": "wabi-sabi interior, rustic textures, understated",
    "light_luxury": "light luxury interior, metal accents, marble details",
}
FLOOR_PHRASES = {
    "wood_floor": "warm wood flooring", "microcement": "microcement flooring",
    "marble": "marble flooring", "tile": "large-format tile flooring",
}
WALL_PHRASES = {
    "white": "white walls", "art_paint": "art paint textured walls",
    "wood_veneer": "wood veneer wall panels", "stone": "natural stone walls",
}
LIGHT_PHRASES = {
    "warm": "warm lighting", "natural": "natural daylight",
    "no_main_light": "indirect ambient lighting without ceiling fixture",
    "cool": "cool neutral lighting",
}
BUDGET_PHRASES = {"economy": "simple affordable furniture",
                  "mid": "well-made mid-range furniture",
                  "high": "high-end designer furniture"}

NEGATIVE = ("distorted wall, warped geometry, extra window, extra door, "
            "broken perspective, floating furniture, cluttered layout, "
            "blurry, low quality, watermark, text")


def assemble_prompt(params: StyleParams, variant: str = "default") -> PromptPair:
    positive = ", ".join([
        STYLE_PHRASES[params.style], FLOOR_PHRASES[params.floor],
        WALL_PHRASES[params.wall], LIGHT_PHRASES[params.light],
        BUDGET_PHRASES[params.budget],
        "realistic architectural visualization, photorealistic, high quality render",
    ])
    if variant == "api":
        return PromptPair(positive=positive, negative="")  # API 模型不吃长负面词
    return PromptPair(positive=positive, negative=NEGATIVE)
