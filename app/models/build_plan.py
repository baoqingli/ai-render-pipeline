from typing import Literal

from pydantic import BaseModel


class PlanBox(BaseModel):
    center: list[float]
    size: list[float]
    rot_z: float = 0.0
    kind: Literal["wall", "furniture", "floor", "frame"]
    label: str = ""   # 家具规范类型（bed/sofa/...），scene_builder 据此选低模套件


class PlanCamera(BaseModel):
    view_id: str
    position: list[float]
    target: list[float]
    ortho: bool = False          # 正交俯视（白模对照平面图的主机位）
    ortho_scale: float = 0.0     # 正交视野宽度（0=场景包围盒自动）


class BuildPlan(BaseModel):
    floor_height: float
    boxes: list[PlanBox]
    cameras: list[PlanCamera]
    output_dir: str
    passes: list[str] = ["depth", "lineart", "white"]
