from typing import Literal

from pydantic import BaseModel


class Wall(BaseModel):
    id: str
    polygon: list[list[float]]


class Door(BaseModel):
    id: str
    position: list[float]
    width: float
    height: float = 2100.0
    wall_id: str | None = None


class Window(BaseModel):
    id: str
    position: list[float]
    width: float
    height: float = 1200.0
    sill_height: float = 900.0
    wall_id: str | None = None


class Furniture(BaseModel):
    id: str
    type: str
    position: list[float]
    size: list[float]
    rotation: float = 0.0
    source: Literal["cad", "ai_supplement"]
    measured: bool = False   # 尺寸来自图块/轮廓实测（False=类型默认值兜底）


class Room(BaseModel):
    id: str
    name: str | None = None
    polygon: list[list[float]]


class ParseQuality(BaseModel):
    """解析质量指标（质量门禁，docs/white-model-fix-plan-2026-09.md §4 P0-4）。"""
    rooms: int = 0
    tiling_ratio: float = 0.0            # 房间并集面积 / 房间加墙凸包面积
    wall_segments: int = 0
    openings_attached_ratio: float = 0.0  # 门窗挂墙成功比例
    furniture_count: int = 0
    furniture_measured_ratio: float = 0.0
    furniture_in_room_ratio: float = 0.0
    notes: list[str] = []


class SceneJSON(BaseModel):
    unit: Literal["mm", "m"] = "mm"
    floor_height: float = 2800.0
    wall_thickness: float = 200.0
    walls: list[Wall] = []
    doors: list[Door] = []
    windows: list[Window] = []
    furniture: list[Furniture] = []
    rooms: list[Room] = []
    quality: ParseQuality | None = None
