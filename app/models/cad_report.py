# app/models/cad_report.py
from typing import Literal

from pydantic import BaseModel


class LayerStat(BaseModel):
    name: str
    line_count: int = 0
    polyline_count: int = 0
    text_count: int = 0
    insert_count: int = 0
    other_count: int = 0


class BlockStat(BaseModel):
    name: str
    layer: str
    insert_count: int


class TextNote(BaseModel):
    layer: str
    content: str
    position: list[float]


class CadReport(BaseModel):
    layers: list[LayerStat] = []
    blocks: list[BlockStat] = []
    proxy_entity_count: int = 0
    text_annotations: list[TextNote] = []
    floor_height_candidates: list[float] = []
    unit_guess: Literal["mm", "m"] = "mm"
    confidence: float = 1.0
    wall_layer_candidates: list[str] = []
    room_layer_candidates: list[str] = []
    ceiling_layer_candidates: list[str] = []
