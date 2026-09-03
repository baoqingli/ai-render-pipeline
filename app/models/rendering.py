# app/models/rendering.py
import hashlib
import json
from pydantic import BaseModel
from typing_extensions import Literal


class StyleParams(BaseModel):
    style: Literal["modern_minimal", "cream", "wood", "french", "wabi_sabi", "light_luxury"] = "modern_minimal"
    floor: Literal["wood_floor", "microcement", "marble", "tile"] = "wood_floor"
    wall: Literal["white", "art_paint", "wood_veneer", "stone"] = "white"
    light: Literal["warm", "natural", "no_main_light", "cool"] = "warm"
    budget: Literal["economy", "mid", "high"] = "mid"

    def stable_hash(self) -> str:
        payload = json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class PromptPair(BaseModel):
    positive: str
    negative: str = ""


class RenderTask(BaseModel):
    view_id: str
    variant_id: str
    model_id: str
    prompt: PromptPair
    control_maps: dict[str, str]   # {"depth": 路径/名称, "lineart": ...}
    seed: int
    params_hash: str
