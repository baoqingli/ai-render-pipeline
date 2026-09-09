# app/models/vision.py
"""VLM 图纸识别 Agent 的解读书模型（供 parse 消费，避免 agents→tools 反向依赖）。"""
from pydantic import BaseModel, Field


class LayerSemantics(BaseModel):
    layer: str
    # semantic: wall_new|wall_existing|partition|furniture_fixed|furniture_loose|
    #           ceiling|floor_finish|door|window|annotation|mep|other
    semantic: str = "other"
    note: str = ""


class Zone(BaseModel):
    name: str
    bbox_pct: list[float] = Field(
        default_factory=lambda: [0.0, 0.0, 1.0, 1.0])
    confidence: float = 0.8


class FurnitureSighting(BaseModel):
    # 合法 type 取值: bed/sofa/table/chair/wardrobe/cabinet/tv/toilet/sink/
    # shower/bathtub/plant/appliance/other
    type: str = "other"
    count: int = 1
    location: str = ""
    layer_guess: str = ""


class DrawingUnderstanding(BaseModel):
    drawing_type: str = ""    # furniture_layout|ceiling_plan|systems|architecture|combined
    layer_semantics: list[LayerSemantics] = []
    zones: list[Zone] = []
    furniture: list[FurnitureSighting] = []
    walls_notes: str = ""
    openings_notes: str = ""
    model: str = ""


class TileElement(BaseModel):
    """分块识别出的单体元素（VLM 语义 + 确定性精确数据回填）。"""
    # category: 墙|柱|门|窗|楼梯|房间边界|家具|固定柜|卫浴|厨房|设备|
    #           天花|灯具|烟感喷淋|尺寸标注|房间名称|其他
    category: str
    item: str = ""   # 具体名称（双人床/筒灯/隔断等）
    count: int = 1
    bbox_pct: list[float] = Field(default_factory=lambda: [0.0, 0.0, 1.0, 1.0])
    orientation: str = ""
    size_mm: str = ""
    confidence: float = 0.8
    tile: int = 0
    # 确定性回填（世界坐标 mm）：元素 bbox 内命中的精确实体
    world_bbox: list[float] | None = None
    exact_items: list[dict] = Field(default_factory=list)


class TileReport(BaseModel):
    tile: int = 0
    drawing_type: str = ""
    elements: list[TileElement] = []


class ElementRegistry(BaseModel):
    """多图交叉验证后的元素登记簿（识别 Agent 最终输出，供后续模块消费）。"""
    source_dxf: str = ""
    tiles: list[TileReport] = []
    elements: list[TileElement] = []
    best_plan_view: str = ""     # 最详尽的布局平面图（视图标识）
    cross_notes: str = ""
    model: str = ""
