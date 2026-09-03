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


class Room(BaseModel):
    id: str
    name: str | None = None
    polygon: list[list[float]]


class SceneJSON(BaseModel):
    unit: Literal["mm", "m"] = "mm"
    floor_height: float = 2800.0
    wall_thickness: float = 200.0
    walls: list[Wall] = []
    doors: list[Door] = []
    windows: list[Window] = []
    furniture: list[Furniture] = []
    rooms: list[Room] = []
