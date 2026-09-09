# app/tools/cad/rules.py
"""CAD 解析规则集中表：图层语义正则、噪点块、家具类型映射、几何常量。

维护纪律（docs/white-model-fix-plan-2026-09.md §4.5）：
新图纸适配 = 只改本文件数据表 + 对应单测，不碰几何逻辑（parse/walls）。
"""
import re
from dataclasses import dataclass

# ── 图层语义正则（中英混合；图层名+块名合并串上匹配） ──────────────────────────

WALL_LAYER_RE = re.compile(r"(wall|墙|a-wall|arch|建筑)", re.IGNORECASE)
# 完成面图层：精装图中代表空间边界的专有图层名关键词，不得与 wall 共用
ROOM_LAYER_RE = re.compile(r"(完成面|finish|P-建筑|finished.?surface)", re.IGNORECASE)
# 天花轮廓线图层：精装图中各功能区天花板轮廓 = 房间水平边界的最可靠来源
CEILING_LAYER_RE = re.compile(r"(天花.*轮廓|ceiling.*contour|C-天花|C-ceiling)", re.IGNORECASE)
DOOR_HINT_RE = re.compile(r"(door|门)", re.IGNORECASE)
WINDOW_HINT_RE = re.compile(r"(window|窗|GL-)", re.IGNORECASE)
# 房间名关键词（"房"含客房/房间的口语命名，真实精装图房型名常用）
ROOM_NAME_RE = re.compile(r"(室|厅|卧|厨|卫|房|阳台)")
# 块名中的尺寸数字（如 M_门_900 / C_1500）
WIDTH_RE = re.compile(r"(\d{3,4})")
# 机电/设备图层：插座、开关、灯具、消防、空调、弱电、地面材质点等——块不入家具
MEP_LAYER_RE = re.compile(
    r"(插座|开关|light|灯|电|消防|空调|弱电|强电|RCU|legend|水管|水点|地漏|小夜灯"
    r"|天花|ceiling|GND|喷淋|烟感|扬声器|风口|检修)",
    re.IGNORECASE,
)
# 家具图层白名单（命中任一 → 家具层；文档内存在家具层时启用严格模式）
FURNITURE_LAYER_RE = re.compile(r"(家具|furn|洁具|木饰面|台盆)", re.IGNORECASE)
# 噪点块名（排水点位/匿名匿名块/图例符号等），永不入家具
NOISE_BLOCK_RE = re.compile(r"^(A\$C|\*U|\$WINLIB|HSXD)", re.IGNORECASE)

# ── 家具类型映射表：关键词 → 规范类型 + 默认尺寸 + 合理范围（mm，w×d×h） ────────


@dataclass(frozen=True)
class FurnType:
    type: str
    keywords: tuple[str, ...]
    default: tuple[float, float, float]
    # 每轴 (min, max)：量尺结果越界即回退默认（防止把符号/脏数据当家具）
    bounds: tuple[tuple[float, float], tuple[float, float], tuple[float, float]]


_TV = FurnType("tv", ("tv", "电视", "television"), (1200.0, 100.0, 800.0),
               ((600.0, 2400.0), (0.0, 400.0), (300.0, 1200.0)))
_TOILET = FurnType("toilet", ("toilet", "马桶", "wc"), (400.0, 700.0, 400.0),
                   ((250.0, 600.0), (400.0, 900.0), (250.0, 900.0)))
_BATHTUB = FurnType("bathtub", ("bath", "浴缸"), (1700.0, 800.0, 550.0),
                    ((1200.0, 2000.0), (600.0, 1200.0), (300.0, 800.0)))
_SINK = FurnType("sink", ("sink", "台盆", "洗手", "basin", "盥洗"), (600.0, 500.0, 850.0),
                 ((300.0, 1500.0), (300.0, 800.0), (500.0, 1000.0)))
_BED = FurnType("bed", ("bed", "床", "chuang", "drc"), (1500.0, 2000.0, 500.0),
                ((800.0, 2200.0), (1000.0, 2300.0), (300.0, 700.0)))
_SOFA = FurnType("sofa", ("sofa", "沙发"), (1800.0, 850.0, 850.0),
                 ((900.0, 3500.0), (500.0, 1200.0), (500.0, 1100.0)))
_CHAIR = FurnType("chair", ("chair", "椅", "凳", "stool", "座"), (500.0, 500.0, 850.0),
                  ((300.0, 800.0), (300.0, 800.0), (400.0, 1200.0)))
_PLANT = FurnType("plant", ("plant", "绿植", "盆栽", "tree"), (400.0, 400.0, 1200.0),
                  ((150.0, 1200.0), (150.0, 1200.0), (300.0, 2500.0)))
_WARDROBE = FurnType("wardrobe", ("wardrobe", "衣柜"), (1200.0, 600.0, 2000.0),
                     ((500.0, 3000.0), (400.0, 900.0), (1200.0, 2600.0)))
_TABLE = FurnType("table", ("table", "桌", "台", "茶几", "desk"),
                  (800.0, 800.0, 750.0),
                  ((400.0, 1600.0), (400.0, 1600.0), (400.0, 1100.0)))
_CABINET = FurnType("cabinet", ("cabinet", "柜", "desk"),
                    (1200.0, 600.0, 750.0),
                    ((300.0, 4000.0), (300.0, 2500.0), (400.0, 1100.0)))

# 顺序即匹配优先级：具体类型在前，cabinet 兜底在后
FURNITURE_TYPES: tuple[FurnType, ...] = (
    _TV, _TOILET, _BATHTUB, _SINK, _BED, _SOFA, _CHAIR, _PLANT, _WARDROBE,
    _TABLE, _CABINET,
)

# 电气/点位类块：即便落在白名单图层也不是家具
MEP_BLOCK_RE = re.compile(r"(喷淋|烟感|扬声器|插座|开关|风口|地漏|探头|alarm|sprinkler)",
                          re.IGNORECASE)
# 墙体填充图层（真实墙位来源：精装/建筑图墙体以 HATCH 填充绘制）
WALL_FILL_LAYER_RE = re.compile(
    r"(墙体填充|承重|墙柱|砌筑|隔墙填充|剪力|结构墙|轻钢龙骨隔墙|轻质砌块|新砌)",
    re.IGNORECASE,
)
# 天花网格识别：同层轮廓数 ≥ 此值视为造型网格，先 union 溶解再进房间流程
CEILING_GRID_MIN_CELLS = 12
# 内隔墙薄带图层（完成面墙体/淋浴玻璃隔断——细长闭合轮廓即墙）
FINISH_WALL_LAYER_RE = re.compile(r"(完成面|淋浴隔断|玻璃隔断)", re.IGNORECASE)

# ── 几何常量（mm） ────────────────────────────────────────────────────────────

DEFAULT_WALL_T = 200.0
MIN_ROOM_AREA_MM2 = 500_000            # 0.5 m²：保住马桶间等小分区；灯槽靠包含去重滤除
MAX_ROOM_AREA_MM2 = 200_000_000        # 200 m²：滤掉整层轮廓
SNAP_TOL = 15_000.0                    # 开放折线端点拼接容差（精装图缺口实测 ~9300）
EDGE_SNAP_TOL = 300.0                  # 房间边 ↔ 双线墙条带吸附容差
COLLINEAR_ANGLE_TOL_DEG = 3.0          # 房间边共线聚类角度容差
COLLINEAR_LATERAL_TOL = 150.0          # 房间边共线聚类横向容差（不同图层画的边界有
                                       # 完成面/天花画法差 50-100mm，墙厚级内视为同一道墙）
COLLINEAR_GAP_TOL = 300.0              # 共线边拼接缝隙容差（≤墙厚级的缝是同一段墙断开，
                                       # 真实洞口由门窗开洞负责，不由轮廓缝隙表达）
ROOM_OVERLAP_IOU = 0.5                 # 房间去重：IoU 超此值视为重复
ROOM_CONTAINMENT_RATIO = 0.85          # 房间去重：被包含面积比超此值视为重复
NAME_SNAP_DIST = 500.0                 # 房间命名文字邻近容差
DOOR_DEFAULT_W = 900.0
WINDOW_DEFAULT_W = 1500.0
WINDOW_MULLION_INTERVAL = 600.0        # 窗棂条间距
