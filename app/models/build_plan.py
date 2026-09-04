from typing import Literal

from pydantic import BaseModel


class PlanBox(BaseModel):
    center: list[float]
    size: list[float]
    rot_z: float = 0.0
    kind: Literal["wall", "furniture", "floor"]


class PlanCamera(BaseModel):
    view_id: str
    position: list[float]
    target: list[float]


class BuildPlan(BaseModel):
    floor_height: float
    boxes: list[PlanBox]
    cameras: list[PlanCamera]
    output_dir: str
    passes: list[str] = ["depth", "lineart", "white"]
