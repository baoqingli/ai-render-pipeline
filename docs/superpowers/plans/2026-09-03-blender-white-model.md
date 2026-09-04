# Phase 2 切片 2：Blender 白模工具链实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 SceneJSON 变成白模控制图——归一化/开洞分段/机位规划在纯 Python 几何层（可测），bpy 薄壳建模渲染（depth/lineart/white 三 pass），子进程工具封装 + CLI，最终打通 `CAD 解析 → 白模 → 控制图 → Phase 1 渲染` 全链。

**Architecture:** 三层分离：`app/tools/blender/geom.py` 纯几何（归一化、最小旋转矩形墙体、开洞分段、机位规划、BuildPlan 组装——零 bpy 依赖，全部 pytest 覆盖）；顶层 `blender/scene_builder.py` bpy 薄壳（读 BuildPlan JSON → 立方体建模 → 相机 → CYCLES CPU 渲三 pass，逻辑全部上移故只做静态校验+真机冒烟）；`app/tools/blender/runner.py` 子进程封装（ToolResult 信封/缓存/类型化错误，ODA 同模式）。

**Tech Stack:** shapely（已有，最小旋转矩形）、Blender headless（外部依赖，winget `BlenderFoundation.Blender`）、无新增 Python 依赖。

**Spec:** [agent-platform-design §7 工具层/§14 Phase 2](../../superpowers/specs/2026-09-02-agent-platform-design.md) · [V2 模块 4/5 白模与视角](../../cad-to-render-pipeline-v2.md) · 勘察输入 [cad-recon-2026-09](../../cad-recon-2026-09.md)

## Global Constraints

- Python `3.12`，uv 管理；Conventional Commits；trailer `Co-Authored-By: Claude <noreply@anthropic.com>`
- **geom 层全程 mm**（与 SceneJSON 一致）；bpy 侧统一 ×0.001 转 Blender 米制
- **控制图命名契约（Phase 1 ab_render 直接消费）**：`{view_id}_{depth|lineart|white}.png`，view_id 形如 `view_01`
- 错误码：`INPUT_INVALID`（scene 缺失/损坏）/ `BLENDER_MISSING`（不可重试）/ `BLENDER_TIMEOUT`（可重试）/ `BLENDER_CRASH`（可重试）——超时信封化是 P2-Task3 的既定教训，直接内置
- 缓存键含工具版本（`TOOL_VERSIONS["build_white_model"] = "1"`）+ scene 内容 hash
- 开洞用**分段法**（V2：过梁/窗台/窗上段），不用 Boolean
- 房间 0 时降级：场景包围盒全局机位（勘察发现 1a 的输入侧兜底；完成面推房间属 Phase 3）
- bpy 代码不在 app/ 下 import（进程隔离）；零新增 Python 依赖
- 测试全部离线（mock subprocess + 纯几何），pristine；`uv run mypy app` 0 错；`uv run ruff check app tests scripts` 全过（钉选集）
- CAD 勘察四输入的落点：坐标归零=Task 1；房间 0 降级=Task 4；家具占位体块全放=Task 5（图层过滤属 Phase 3 parse 侧）

---

### Task 1: 坐标归一化与墙体矩形化（geom 基础）

**Files:**
- Create: `app/tools/blender/__init__.py`, `app/tools/blender/geom.py`
- Test: `tests/unit/test_blender_geom.py`

**Interfaces:**
- Consumes: `app/models/scene.py` 的 `SceneJSON/Wall/Door/Window/Furniture/Room`
- Produces:
  - `Box = NamedTuple("Box", center: tuple[float, float, float], size: tuple[float, float, float], rot_z: float)`（center 是几何中心，size 是全尺寸，rot_z 弧度绕 z）
  - `def scene_offset(scene: SceneJSON) -> tuple[float, float]`（全部几何的 x/y 最小值）
  - `def normalize_scene(scene: SceneJSON) -> SceneJSON`（平移使 offset→(0,0)；墙/房间 polygon、门窗家具 position 全部平移；返回新对象不改入参）
  - `def wall_box(wall: Wall, floor_height: float) -> Box`（polygon 的 shapely `minimum_rotated_rectangle` → Box：z center=fh/2, dz=fh）

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_blender_geom.py
import pytest

from app.models.scene import Wall
from app.tools.blender.geom import Box, normalize_scene, scene_offset, wall_box


def _scene_with_offset():
    from app.models.scene import SceneJSON
    return SceneJSON(walls=[Wall(id="w1", polygon=[[1000.0, 2000.0], [7000.0, 2000.0],
                                                    [7000.0, 2200.0], [1000.0, 2200.0]])])


def test_scene_offset_and_normalize():
    s = _scene_with_offset()
    assert scene_offset(s) == (1000.0, 2000.0)
    n = normalize_scene(s)
    assert n.walls[0].polygon[0] == [0.0, 0.0]
    assert s.walls[0].polygon[0] == [1000.0, 2000.0]   # 入参不变


def test_normalize_shifts_openings_and_furniture():
    from app.models.scene import Door, Furniture, SceneJSON, Wall
    s = SceneJSON(walls=[Wall(id="w1", polygon=[[1000.0, 0.0], [7000.0, 0.0],
                                                [7000.0, 200.0], [1000.0, 200.0]])],
                  doors=[Door(id="d1", position=[4000.0, 100.0], width=900.0)],
                  furniture=[Furniture(id="f1", type="sofa", position=[2000.0, 500.0],
                                       size=[1000.0, 500.0, 500.0], source="cad")])
    n = normalize_scene(s)
    assert n.doors[0].position == [3000.0, 100.0]
    assert n.furniture[0].position == [1000.0, 500.0]


def test_wall_box_axis_aligned():
    box = wall_box(Wall(id="w1", polygon=[[0.0, 100.0], [6000.0, 100.0],
                                          [6000.0, -100.0], [0.0, -100.0]]), floor_height=2800.0)
    assert isinstance(box, Box)
    assert box.center == (3000.0, 0.0, 1400.0)
    assert box.size == (6000.0, 200.0, 2800.0)
    assert box.rot_z == pytest.approx(0.0)


def test_wall_box_rotated_90():
    box = wall_box(Wall(id="w1", polygon=[[-100.0, 0.0], [100.0, 0.0],
                                          [100.0, 4000.0], [-100.0, 4000.0]]), floor_height=2800.0)
    assert box.size == (4000.0, 200.0, 2800.0)
    assert box.rot_z == pytest.approx(1.5707963267948966)   # 90°
    assert box.center == (0.0, 2000.0, 1400.0)
```

- [ ] **Step 2: 跑测确认失败** → `uv run pytest tests/unit/test_blender_geom.py -v` FAIL（模块不存在）
- [ ] **Step 3: 实现 `app/tools/blender/geom.py`**

```python
"""白模纯几何层：归一化、墙体矩形化、开洞分段、机位规划、BuildPlan 组装。全程 mm。"""
from typing import NamedTuple

from shapely.geometry import Polygon

from app.models.scene import SceneJSON, Wall


class Box(NamedTuple):
    center: tuple[float, float, float]
    size: tuple[float, float, float]
    rot_z: float


def _bbox_min(scene: SceneJSON) -> tuple[float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for w in scene.walls:
        xs += [p[0] for p in w.polygon]
        ys += [p[1] for p in w.polygon]
    for r in scene.rooms:
        xs += [p[0] for p in r.polygon]
        ys += [p[1] for p in r.polygon]
    if not xs:
        return (0.0, 0.0)
    return (min(xs), min(ys))


def scene_offset(scene: SceneJSON) -> tuple[float, float]:
    return _bbox_min(scene)


def normalize_scene(scene: SceneJSON) -> SceneJSON:
    dx, dy = _bbox_min(scene)
    data = scene.model_dump()
    for w in data["walls"]:
        w["polygon"] = [[p[0] - dx, p[1] - dy] for p in w["polygon"]]
    for r in data["rooms"]:
        r["polygon"] = [[p[0] - dx, p[1] - dy] for p in r["polygon"]]
    for d in data["doors"]:
        d["position"] = [d["position"][0] - dx, d["position"][1] - dy]
    for w in data["windows"]:
        w["position"] = [w["position"][0] - dx, w["position"][1] - dy]
    for f in data["furniture"]:
        f["position"] = [f["position"][0] - dx, f["position"][1] - dy]
    return SceneJSON.model_validate(data)


def wall_box(wall: Wall, floor_height: float) -> Box:
    rect = Polygon(wall.polygon).minimum_rotated_rectangle
    c = list(rect.exterior.coords)          # 5 点闭合
    ax, ay = c[1][0] - c[0][0], c[1][1] - c[0][1]
    bx, by = c[3][0] - c[0][0], c[3][1] - c[0][1]
    la, lb = (ax * ax + ay * ay) ** 0.5, (bx * bx + by * by) ** 0.5
    # 取长边为局部 x 轴
    if la >= lb:
        length, width, angle = la, lb, _angle(ax, ay)
    else:
        length, width, angle = lb, la, _angle(bx, by)
    cx = sum(p[0] for p in c[:4]) / 4.0
    cy = sum(p[1] for p in c[:4]) / 4.0
    return Box(center=(cx, cy, floor_height / 2.0),
               size=(length, width, floor_height), rot_z=angle)


def _angle(dx: float, dy: float) -> float:
    import math
    a = math.atan2(dy, dx)
    if a < -math.pi / 2:      # 归一到 (-π/2, π/2]，使长边方向稳定
        a += math.pi
    elif a > math.pi / 2:
        a -= math.pi
    return a
```

- [ ] **Step 4: 跑测通过** → PASS
- [ ] **Step 5: Commit**

```bash
uv run pytest tests/unit/test_blender_geom.py -v
git add app/tools/blender/ tests/unit/test_blender_geom.py
git commit -m "feat: scene normalization and wall rectangularization for white model"
```

---

### Task 2: 开洞分段（过梁/窗台/窗上段）

**Files:**
- Modify: `app/tools/blender/geom.py`
- Test: `tests/unit/test_blender_openings.py`

**Interfaces:**
- Consumes: Task 1 的 `Box`/`wall_box`
- Produces:
  - `Opening = NamedTuple("Opening", u: float, width: float, z_bot: float, z_top: float)`（u=洞中心沿墙局部 x 轴的坐标，原点在墙中心）
  - `def project_opening(box: Box, position: tuple[float, float], width: float, z_bot: float, z_top: float) -> Opening`（全局 position → 墙局部 u）
  - `def segment_wall(box: Box, openings: list[Opening]) -> list[Box]`（分段法：无洞区间全高段 + 门洞上方过梁段 + 窗台下段/窗上段；全部与墙同 rot_z/thickness/长度方向）

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_blender_openings.py
import pytest

from app.models.scene import Door, SceneJSON, Wall, Window
from app.tools.blender.geom import Box, Opening, project_opening, segment_wall


def _wall_scene_with(door_at, door_w=900.0, window_at=None, window_w=1500.0):
    """6000x200 墙 @ y[-100,100]，x[0,6000]，中心 (3000,0)"""
    walls = [Wall(id="w1", polygon=[[0.0, 100.0], [6000.0, 100.0],
                                    [6000.0, -100.0], [0.0, -100.0]])]
    doors = [Door(id="d1", position=door_at, width=door_w)] if door_at else []
    windows = [Window(id="x1", position=window_at, width=window_w)] if window_at else []
    return SceneJSON(walls=walls, doors=doors, windows=windows, floor_height=2800.0)


BOX = Box(center=(3000.0, 0.0, 1400.0), size=(6000.0, 200.0, 2800.0), rot_z=0.0)


def test_project_opening_to_local_u():
    o = project_opening(BOX, (4500.0, 50.0), 900.0, 0.0, 2100.0)
    assert o.u == pytest.approx(1500.0)        # 4500 - 中心 3000
    assert (o.z_bot, o.z_top) == (0.0, 2100.0)


def test_segment_wall_no_openings_is_single_box():
    segs = segment_wall(BOX, [])
    assert len(segs) == 1 and segs[0] == BOX


def test_segment_wall_with_door():
    o = Opening(u=1500.0, width=900.0, z_bot=0.0, z_top=2100.0)   # 洞区间 u[1050,1950]
    segs = segment_wall(BOX, [o])
    # 两段全高 + 一段过梁（u 1050..1950, z 2100..2800）
    fulls = [s for s in segs if s.size[2] == 2800.0]
    lintel = [s for s in segs if s.size[2] == pytest.approx(700.0)]
    assert len(fulls) == 2 and len(lintel) == 1
    assert fulls[0].size[0] == pytest.approx(1050.0) and fulls[0].center[0] == pytest.approx(525.0)
    assert fulls[1].size[0] == pytest.approx(4050.0)
    assert lintel[0].center[2] == pytest.approx(2450.0)            # (2100+2800)/2
    assert lintel[0].size[0] == pytest.approx(900.0)


def test_segment_wall_with_window():
    o = Opening(u=0.0, width=1500.0, z_bot=900.0, z_top=2100.0)    # 窗 u[-750,750]
    segs = segment_wall(BOX, [o])
    fulls = [s for s in segs if s.size[2] == 2800.0]
    sill = [s for s in segs if s.size[2] == pytest.approx(900.0)]      # 0..900
    header = [s for s in segs if s.size[2] == pytest.approx(700.0)]    # 2100..2800
    assert len(fulls) == 2 and len(sill) == 1 and len(header) == 1
    assert sill[0].center[2] == pytest.approx(450.0)
    assert sill[0].size[0] == pytest.approx(1500.0) and header[0].size[0] == pytest.approx(1500.0)


def test_segment_wall_overlapping_openings_merged():
    """两个门洞重叠/相邻 → 合并成一个洞区间再分段，不产生零厚碎片。"""
    segs = segment_wall(BOX, [Opening(u=1000.0, width=900.0, z_bot=0.0, z_top=2100.0),
                              Opening(u=1600.0, width=900.0, z_bot=0.0, z_top=2100.0)])
    lintels = [s for s in segs if s.size[2] == pytest.approx(700.0)]
    assert len(lintels) == 1                      # 合并区间 u[550,2050] 一段过梁
    assert lintels[0].size[0] == pytest.approx(1500.0)
```

- [ ] **Step 2: 跑测确认失败** → FAIL
- [ ] **Step 3: 实现（追加进 geom.py）**

```python
class Opening(NamedTuple):
    u: float
    width: float
    z_bot: float
    z_top: float


def project_opening(box: Box, position: tuple[float, float], width: float,
                    z_bot: float, z_top: float) -> Opening:
    import math
    dx, dy = position[0] - box.center[0], position[1] - box.center[1]
    cos, sin = math.cos(-box.rot_z), math.sin(-box.rot_z)
    u = dx * cos - dy * sin
    return Opening(u=u, width=width, z_bot=z_bot, z_top=z_top)


def _merged_intervals(openings: list[Opening]) -> list[tuple[float, float, float, float]]:
    """按 u 排序合并重叠区间；z 范围取并集区间的包围（分段法 MVP：同墙多洞按各自 z 独立处理见下）。"""
    ivs = sorted((o.u - o.width / 2.0, o.u + o.width / 2.0, o.z_bot, o.z_top)
                 for o in openings)
    merged: list[tuple[float, float, float, float]] = []
    for lo, hi, zb, zt in ivs:
        if merged and lo <= merged[-1][1]:
            m = merged[-1]
            merged[-1] = (m[0], max(m[1], hi), min(m[2], zb), max(m[3], zt))
        else:
            merged.append((lo, hi, zb, zt))
    return merged


def _local_box(box: Box, u_center: float, du: float, z_bot: float, z_top: float) -> Box:
    """墙局部坐标 → 全局 Box（rot/thickness 继承墙）。"""
    import math
    cos, sin = math.cos(box.rot_z), math.sin(box.rot_z)
    gx = box.center[0] + u_center * cos
    gy = box.center[1] + u_center * sin
    zc = (z_bot + z_top) / 2.0
    return Box(center=(gx, gy, zc),
               size=(du, box.size[1], z_top - z_bot), rot_z=box.rot_z)


def segment_wall(box: Box, openings: list[Opening]) -> list[Box]:
    if not openings:
        return [box]
    half_l = box.size[0] / 2.0
    fh = box.size[2]
    merged = _merged_intervals(openings)
    segs: list[Box] = []
    cursor = -half_l
    for lo, hi, zb, zt in merged:
        lo_c, hi_c = max(lo, -half_l), min(hi, half_l)
        if hi_c - lo_c <= 1.0:
            continue
        if lo_c - cursor > 1.0:                                    # 洞前全高段
            segs.append(_local_box(box, (cursor + lo_c) / 2.0, lo_c - cursor, 0.0, fh))
        if zb > 1.0:                                               # 下段（窗台/门无）
            segs.append(_local_box(box, (lo_c + hi_c) / 2.0, hi_c - lo_c, 0.0, zb))
        if fh - zt > 1.0:                                          # 上段（过梁/窗上）
            segs.append(_local_box(box, (lo_c + hi_c) / 2.0, hi_c - lo_c, zt, fh))
        cursor = hi_c
    if half_l - cursor > 1.0:                                      # 尾部全高段
        segs.append(_local_box(box, (cursor + half_l) / 2.0, half_l - cursor, 0.0, fh))
    return segs
```

- [ ] **Step 4: 跑测通过** → PASS
- [ ] **Step 5: Commit**

```bash
uv run pytest tests/unit/test_blender_openings.py -v
git add app/tools/blender/geom.py tests/unit/test_blender_openings.py
git commit -m "feat: opening segmentation (lintel/sill/header) for white model walls"
```

---

### Task 3: 机位规划（房间内候选 + 降级全局）

**Files:**
- Modify: `app/tools/blender/geom.py`
- Test: `tests/unit/test_blender_views.py`

**Interfaces:**
- Consumes: Task 1 的 `normalize_scene`
- Produces:
  - `CameraPose = NamedTuple("CameraPose", view_id: str, position: tuple[float, float, float], target: tuple[float, float, float])`
  - `def plan_views(scene: SceneJSON, per_room: int = 2) -> list[CameraPose]`（输入须已归一化或原始均可——内部自归一；相机 z=1500，target=房间质心 z=1200；候选=质心+长轴 ±25% 三点取 `buffer(-400)` 内者，按到边界距离评分取前 per_room；**无房间或全部候选落外 → 全局降级单机位**：场景包围盒中心 z=1500 → 中心 z=1200，view_id 递增 `view_{n:02d}`）

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_blender_views.py
from app.models.scene import Room, SceneJSON, Wall
from app.tools.blender.geom import plan_views


def _two_room_scene():
    walls = [Wall(id=f"w{i}", polygon=[[0.0, 0.0], [6000.0, 0.0], [6000.0, 200.0], [0.0, 200.0]])
             for i in range(1)]
    rooms = [Room(id="r1", name="客厅", polygon=[[0.0, 0.0], [4000.0, 0.0],
                                                 [4000.0, 4000.0], [0.0, 4000.0]]),
             Room(id="r2", name="卧室", polygon=[[4000.0, 0.0], [6000.0, 0.0],
                                                 [6000.0, 4000.0], [4000.0, 4000.0]])]
    return SceneJSON(walls=walls, rooms=rooms)


def test_plan_views_per_room():
    poses = plan_views(_two_room_scene(), per_room=2)
    assert len(poses) == 4
    ids = [p.view_id for p in poses]
    assert ids == ["view_01", "view_02", "view_03", "view_04"]
    for p in poses:
        assert p.position[2] == 1500.0 and p.target[2] == 1200.0
    # 客厅机位在客厅内（x < 4000）
    assert all(p.position[0] < 4000.0 for p in poses[:2])
    assert all(p.position[0] > 4000.0 for p in poses[2:])


def test_plan_views_global_fallback_when_no_rooms():
    scene = SceneJSON(walls=[Wall(id="w1", polygon=[[0.0, 0.0], [6000.0, 0.0],
                                                    [6000.0, 200.0], [0.0, 200.0]])])
    poses = plan_views(scene)
    assert len(poses) == 1
    assert poses[0].view_id == "view_01"
    assert poses[0].position == (3000.0, 100.0, 1500.0)     # 墙 bbox 中心
    assert poses[0].target == (3000.0, 100.0, 1200.0)


def test_plan_views_global_fallback_when_room_too_small():
    """房间收缩 400 后为空（小于 800x800）→ 降级全局。"""
    scene = SceneJSON(rooms=[Room(id="r1", name="tiny",
                                  polygon=[[0.0, 0.0], [700.0, 0.0],
                                           [700.0, 700.0], [0.0, 700.0]])])
    poses = plan_views(scene)
    assert len(poses) == 1 and poses[0].view_id == "view_01"
```

- [ ] **Step 2: 跑测确认失败** → FAIL
- [ ] **Step 3: 实现（追加进 geom.py）**

```python
class CameraPose(NamedTuple):
    view_id: str
    position: tuple[float, float, float]
    target: tuple[float, float, float]


CAM_Z = 1500.0
TARGET_Z = 1200.0
SHRINK = 400.0


def plan_views(scene: SceneJSON, per_room: int = 2) -> list[CameraPose]:
    scene = normalize_scene(scene)
    poses: list[CameraPose] = []
    n = 0
    for room in scene.rooms:
        poly = Polygon(room.polygon).buffer(-SHRINK)
        if poly.is_empty:
            continue
        minx, miny, maxx, maxy = poly.bounds
        cx, cy = poly.centroid.x, poly.centroid.y
        long_x = (maxx - minx) >= (maxy - miny)
        span = (maxx - minx) if long_x else (maxy - miny)
        cands = [(cx, cy)]
        for s in (-0.25, 0.25):
            cands.append((cx + s * span, cy) if long_x else (cx, cy + s * span))
        inside = [(x, y) for x, y in cands if poly.contains(Point(x, y))]
        if not inside:
            continue
        inside.sort(key=lambda p: -poly.exterior.distance(Point(p[0], p[1])))
        for x, y in inside[:per_room]:
            n += 1
            poses.append(CameraPose(view_id=f"view_{n:02d}",
                                    position=(x, y, CAM_Z), target=(cx, cy, TARGET_Z)))
    if not poses:                                    # 全局降级（无房间/全落外）
        walls_poly = unary_union([Polygon(w.polygon) for w in scene.walls]) \
            if scene.walls else None
        if walls_poly is None or walls_poly.is_empty:
            return []
        c = walls_poly.centroid
        return [CameraPose(view_id="view_01",
                           position=(c.x, c.y, CAM_Z), target=(c.x, c.y, TARGET_Z))]
    return poses
```

（`Point`/`unary_union` 从 shapely 顶部 import 补齐：`from shapely.geometry import Point, Polygon` 与 `from shapely.ops import unary_union`。）

- [ ] **Step 4: 跑测通过** → PASS
- [ ] **Step 5: Commit**

```bash
uv run pytest tests/unit/test_blender_views.py -v
git add app/tools/blender/geom.py tests/unit/test_blender_views.py
git commit -m "feat: view planning with room candidates and global fallback"
```

---

### Task 4: BuildPlan 模型与组装

**Files:**
- Create: `app/models/build_plan.py`
- Modify: `app/tools/blender/geom.py`
- Test: `tests/unit/test_build_plan.py`

**Interfaces:**
- Consumes: Task 1-3 全部
- Produces:
  - `PlanBox(center: list[float], size: list[float], rot_z: float = 0.0, kind: Literal["wall","furniture","floor"])`
  - `PlanCamera(view_id: str, position: list[float], target: list[float])`
  - `BuildPlan(floor_height: float, boxes: list[PlanBox], cameras: list[PlanCamera], output_dir: str, passes: list[str] = ["depth","lineart","white"])`
  - `def build_plan(scene: SceneJSON, output_dir: str) -> BuildPlan`（组装：归一化 → 每墙 wall_box+投影其门窗→segment_wall→wall 段 boxes；家具 center=(x,y,size_z/2) kind=furniture；地面板=bbox 外扩 500、z 中心 -50、厚 100 kind=floor；cameras=plan_views）

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_build_plan.py
from app.models.build_plan import BuildPlan
from app.models.scene import SceneJSON, Wall
from app.tools.blender.geom import build_plan


def _scene():
    return SceneJSON(walls=[Wall(id="w1", polygon=[[1000.0, 100.0], [7000.0, 100.0],
                                                    [7000.0, -100.0], [1000.0, -100.0]])])


def test_build_plan_assembles_all_kinds():
    plan = build_plan(_scene(), output_dir="experiments/model/x")
    assert isinstance(plan, BuildPlan)
    kinds = {b.kind for b in plan.boxes}
    assert kinds == {"wall", "floor"}                    # 无家具/房间 → wall+floor
    assert plan.floor_height == 2800.0
    assert plan.cameras and plan.cameras[0].view_id == "view_01"   # 无房间→全局降级
    floor = next(b for b in plan.boxes if b.kind == "floor")
    assert floor.center[2] == -50.0 and floor.size[2] == 100.0
    # 归一化后墙 x 从 0 起；地板外扩 500 → 中心 (3000, 0)
    wall = next(b for b in plan.boxes if b.kind == "wall")
    assert wall.center[0] == pytest_approx(3000.0)


def pytest_approx(v):
    import pytest
    return pytest.approx(v)


def test_build_plan_json_roundtrip():
    import json
    plan = build_plan(_scene(), output_dir="x")
    data = json.loads(plan.model_dump_json())
    assert data["passes"] == ["depth", "lineart", "white"]
    assert BuildPlan.model_validate(data) == plan
```

- [ ] **Step 2: 跑测确认失败** → FAIL
- [ ] **Step 3: 实现 `app/models/build_plan.py`**

```python
from pydantic import BaseModel
from typing import Literal


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
```

**geom.py 追加组装函数：**

```python
from app.models.build_plan import BuildPlan, PlanBox, PlanCamera   # 顶部 import 补入


def build_plan(scene: SceneJSON, output_dir: str) -> BuildPlan:
    scene = normalize_scene(scene)
    fh = scene.floor_height
    boxes: list[PlanBox] = []
    door_by_wall: dict[str, list] = {}
    win_by_wall: dict[str, list] = {}
    for d in scene.doors:
        door_by_wall.setdefault(d.wall_id or "", []).append(d)
    for w in scene.windows:
        win_by_wall.setdefault(w.wall_id or "", []).append(w)
    for wall in scene.walls:
        base = wall_box(wall, fh)
        openings = []
        for d in door_by_wall.get(wall.id, []):
            openings.append(project_opening(base, d.position, d.width, 0.0, d.height))
        for w in win_by_wall.get(wall.id, []):
            openings.append(project_opening(base, w.position, w.width,
                                            w.sill_height, w.sill_height + w.height))
        for seg in segment_wall(base, openings):
            boxes.append(PlanBox(center=list(seg.center), size=list(seg.size),
                                 rot_z=seg.rot_z, kind="wall"))
    for f in scene.furniture:
        sx, sy, sz = f.size
        boxes.append(PlanBox(center=[f.position[0], f.position[1], sz / 2.0],
                             size=[sx, sy, sz], rot_z=f.rotation, kind="furniture"))
    xs = [p[0] for b in boxes for p in [b.center]]
    minx = min((b.center[0] - b.size[0] / 2.0) for b in boxes) if boxes else 0.0
    maxx = max((b.center[0] + b.size[0] / 2.0) for b in boxes) if boxes else 0.0
    miny = min((b.center[1] - b.size[1] / 2.0) for b in boxes) if boxes else 0.0
    maxy = max((b.center[1] + b.size[1] / 2.0) for b in boxes) if boxes else 0.0
    floor = PlanBox(center=[(minx + maxx) / 2.0, (miny + maxy) / 2.0, -50.0],
                    size=[(maxx - minx) + 1000.0, (maxy - miny) + 1000.0, 100.0],
                    kind="floor")
    boxes.append(floor)
    cameras = [PlanCamera(view_id=p.view_id, position=list(p.position),
                          target=list(p.target)) for p in plan_views(scene)]
    return BuildPlan(floor_height=fh, boxes=boxes, cameras=cameras, output_dir=output_dir)
```

- [ ] **Step 4: 跑测通过 → 全量门禁** → PASS
- [ ] **Step 5: Commit**

```bash
uv run pytest tests/unit/test_build_plan.py -v && uv run mypy app
git add app/models/build_plan.py app/tools/blender/geom.py tests/unit/test_build_plan.py
git commit -m "feat: BuildPlan model and assembly from SceneJSON"
```

---

### Task 5: bpy 建模渲染脚本（薄壳）

**Files:**
- Create: `blender/scene_builder.py`（顶层 blender/ 目录，进程隔离——app 内禁止 import）

**Interfaces:**
- Consumes: Task 4 的 `BuildPlan` JSON（文件路径经 argv 传入）
- Produces: `<output_dir>/{view_id}_{pass}.png` 三 pass（white/depth/lineart）；`--plan <path>` 参数；CYCLES CPU、单位 ×0.001

**测试策略（诚实声明）**：bpy 只在 Blender 进程内存在，本任务离线测试为**静态校验**（编译 + 关键 API 标记断言）；几何正确性由 Task 1-4 覆盖，真实渲染由 Task 8 真机验收。这是薄壳的代价与边界。

- [ ] **Step 1: 写静态校验测试**

```python
# tests/unit/test_scene_builder_static.py
import ast
from pathlib import Path

SRC = Path("blender/scene_builder.py")


def test_scene_builder_compiles():
    compile(SRC.read_text(encoding="utf-8"), str(SRC), "exec")


def test_scene_builder_marks():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert {"main", "add_boxes", "add_camera", "render_pass"} <= funcs
    text = SRC.read_text(encoding="utf-8")
    for marker in ["use_pass_mist", "use_freestyle", "to_track_quat",
                   "0.001", "--plan", "depth", "lineart", "white"]:
        assert marker in text, f"missing marker: {marker}"
```

- [ ] **Step 2: 跑测确认失败** → FAIL（文件不存在）
- [ ] **Step 3: 实现 `blender/scene_builder.py`**

```python
"""Blender 进程内白模构建脚本（薄壳：几何全部在上游 geom 层完成）。

用法（由 app/tools/blender/runner.py 子进程调用，也可手动）:
  blender --background --factory-startup --python blender/scene_builder.py -- \
      --plan <build_plan.json>

产出: <output_dir>/{view_id}_{depth|lineart|white}.png   （单位: 上游 mm → Blender m ×0.001）
"""
import argparse
import json
import math
import os
import sys

import bpy
from mathutils import Vector

S = 0.001  # mm → m


def add_boxes(boxes):
    mat_wall = bpy.data.materials.new("wall")
    mat_wall.diffuse_color = (0.8, 0.8, 0.8, 1.0)
    mat_furn = bpy.data.materials.new("furniture")
    mat_furn.diffuse_color = (0.6, 0.6, 0.65, 1.0)
    for i, b in enumerate(boxes):
        bpy.ops.mesh.primitive_cube_add(size=1.0)
        o = bpy.context.active_object
        o.name = f"{b['kind']}_{i:03d}"
        sx, sy, sz = (d * S for d in b["size"])
        o.dimensions = (abs(sx), abs(sy), abs(sz))
        cx, cy, cz = (c * S for c in b["center"])
        o.location = (cx, cy, cz)
        o.rotation_euler[2] = b.get("rot_z", 0.0)
        mat = mat_wall if b["kind"] != "furniture" else mat_furn
        o.data.materials.append(mat)


def add_camera(cam):
    if "ARP_CAM" not in bpy.data.objects:
        cam_data = bpy.data.cameras.new("ARP_CAM")
        obj = bpy.data.objects.new("ARP_CAM", cam_data)
        bpy.context.collection.objects.link(obj)
    obj = bpy.data.objects["ARP_CAM"]
    obj.location = Vector([c * S for c in cam["position"]])
    direction = Vector([t * S for t in cam["target"]]) - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = obj
    return obj


def _setup_world(scene):
    scene.world = scene.world or bpy.data.worlds.new("ARP_WORLD")
    scene.world.use_nodes = True
    bg = scene.world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.9, 0.9, 0.9, 1.0)
        bg.inputs[1].default_value = 1.0


def _setup_depth_output(scene, path):
    scene.view_layers[0].use_pass_mist = True
    diag = _scene_diag(scene)
    ms = scene.world.mist_settings
    ms.start, ms.depth, ms.falloff = 0.0, diag * 1.5, "LINEAR"
    scene.use_nodes = True
    nt = scene.node_tree
    nt.nodes.clear()
    rl = nt.nodes.new("CompositorNodeRLayers")
    fo = nt.nodes.new("CompositorNodeOutputFile")
    fo.format.file_format, fo.format.color_mode = "PNG", "BW"
    fo.base_path = path
    nt.links.new(rl.outputs["Mist"], fo.inputs["Image"])
    return fo


def _scene_diag(scene) -> float:
    xs = [o.location.x for o in scene.objects]
    ys = [o.location.y for o in scene.objects]
    if not xs:
        return 10.0
    return max(max(xs) - min(xs), max(ys) - min(ys), 1.0) * 1.5


def _setup_freestyle(scene):
    scene.render.use_freestyle = True
    vl = scene.view_layers[0]
    if "ARP" not in [ls.name for ls in vl.freestyle_settings.linesets]:
        ls = vl.freestyle_settings.linesets.new("ARP")
        ls.linestyle = bpy.data.linestyles.new("ARP_LINE")
        ls.linestyle.color = (0.0, 0.0, 0.0)


def render_pass(scene, cam, pass_name, out_dir):
    out = os.path.join(out_dir, f"{cam['view_id']}_{pass_name}.png")
    scene.render.filepath = out
    scene.render.image_settings.file_format = "PNG"
    scene.render.resolution_x, scene.render.resolution_y = 1024, 768
    fo = None
    scene.render.use_freestyle = False
    scene.use_nodes = False
    scene.view_layers[0].use_pass_mist = False
    if pass_name == "depth":
        fo = _setup_depth_output(scene, out)
    elif pass_name == "lineart":
        _setup_freestyle(scene)
    try:
        bpy.ops.render.render(write_still=(pass_name != "depth"))
    finally:
        if fo is not None:
            scene.use_nodes = False
        scene.view_layers[0].use_pass_mist = False
        scene.render.use_freestyle = False
    return out


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    args = ap.parse_args(argv)
    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    out_dir = plan["output_dir"]
    os.makedirs(out_dir, exist_ok=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 16
    scene.cycles.device = "CPU"
    _setup_world(scene)
    add_boxes(plan["boxes"])
    for cam in plan["cameras"]:
        add_camera(cam)
        for pass_name in plan.get("passes", ["depth", "lineart", "white"]):
            render_pass(scene, cam, pass_name, out_dir)
    print(f"ARP_DONE views={len(plan['cameras'])}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑静态测试通过** → PASS
- [ ] **Step 5: Commit**

```bash
uv run pytest tests/unit/test_scene_builder_static.py -v
git add blender/scene_builder.py tests/unit/test_scene_builder_static.py
git commit -m "feat: bpy scene builder shell rendering depth/lineart/white passes"
```

（实现注记：Mist pass 的 FileOutput 路径按次设置；write_still=False 时 depth 由 compositor 写出。真机首跑若 Freestyle 与 CYCLES 组合报错，Task 8 记录并将 lineart 降级为 EEVEE 引擎二次渲染——薄壳层允许现场校准，几何层不受影响。）

---

### Task 6: build_white_model 工具（子进程封装）

**Files:**
- Modify: `app/core/config.py`（Settings 加 `blender_exe: str = "blender"`）
- Modify: `app/tools/cache.py`（TOOL_VERSIONS 加 `"build_white_model": "1"`）
- Create: `app/tools/blender/runner.py`
- Test: `tests/unit/test_blender_runner.py`

**Interfaces:**
- Consumes: Task 4 `build_plan`、Task 5 `blender/scene_builder.py`
- Produces: `async def build_white_model(scene_json_path: str | Path, out_dir: str | Path, blender_exe: str | None = None) -> ToolResult[Path]`
  - 流程：读 scene.json（缺失/JSON 损坏/校验失败 → `INPUT_INVALID`）→ `build_plan` → 写 `<out_dir>/build_plan.json` → 子进程 `[exe, "--background", "--factory-startup", "--python", <repo>/blender/scene_builder.py, "--", "--plan", <plan_path>]` timeout 1800 → 校验每相机×pass 的 PNG 存在
  - `FileNotFoundError` → `BLENDER_MISSING`（不可重试）；`TimeoutExpired` → `BLENDER_TIMEOUT`（可重试）；rc≠0 或产物缺 → `BLENDER_CRASH`（可重试）
  - 缓存：键 = `build_cache_key("build_white_model", sha256(scene 文件字节), out_dir.name)`；命中条件 = 全部期望 PNG 已存在

- [ ] **Step 1: 写失败测试（mock subprocess，产物由桩写出）**

```python
# tests/unit/test_blender_runner.py
import json
from pathlib import Path

import pytest

from app.models.scene import SceneJSON, Wall
from app.tools.blender import runner


def _write_scene(p: Path) -> Path:
    scene = SceneJSON(walls=[Wall(id="w1", polygon=[[0.0, 100.0], [6000.0, 100.0],
                                                    [6000.0, -100.0], [0.0, -100.0]])])
    p.write_text(scene.model_dump_json(), encoding="utf-8")
    return p


def _fake_blender_ok(plan_path_arg="--plan"):
    def fake_run(cmd, **kw):
        idx = cmd.index("--plan") + 1 if "--plan" in cmd else None
        plan = json.loads(Path(cmd[idx]).read_text(encoding="utf-8"))
        out = Path(plan["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        for cam in plan["cameras"]:
            for ps in plan["passes"]:
                (out / f"{cam['view_id']}_{ps}.png").write_bytes(b"PNG")
        return type("R", (), {"returncode": 0})()
    return fake_run


async def test_build_success(tmp_path, monkeypatch):
    scene = _write_scene(tmp_path / "s.json")
    monkeypatch.setattr(runner.subprocess, "run", _fake_blender_ok())
    result = await runner.build_white_model(scene, tmp_path / "model",
                                            blender_exe="blender")
    assert result.ok and result.data is not None
    assert (result.data / "view_01_white.png").exists()


async def test_cache_hit_second_call(tmp_path, monkeypatch):
    scene = _write_scene(tmp_path / "s.json")
    runs = []

    def fake_run(cmd, **kw):
        runs.append(1)
        return _fake_blender_ok()(cmd, **kw)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    a = await runner.build_white_model(scene, tmp_path / "model", blender_exe="blender")
    b = await runner.build_white_model(scene, tmp_path / "model", blender_exe="blender")
    assert a.ok and b.ok and b.cache_hit and len(runs) == 1


async def test_blender_missing_not_retryable(tmp_path, monkeypatch):
    scene = _write_scene(tmp_path / "s.json")

    def boom(cmd, **kw):
        raise FileNotFoundError("no blender")

    monkeypatch.setattr(runner.subprocess, "run", boom)
    r = await runner.build_white_model(scene, tmp_path / "model", blender_exe="blender")
    assert not r.ok and r.error.code == "BLENDER_MISSING" and not r.error.retryable


async def test_blender_timeout_retryable(tmp_path, monkeypatch):
    import subprocess

    scene = _write_scene(tmp_path / "s.json")

    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd="blender", timeout=1800)

    monkeypatch.setattr(runner.subprocess, "run", hang)
    r = await runner.build_white_model(scene, tmp_path / "model", blender_exe="blender")
    assert not r.ok and r.error.code == "BLENDER_TIMEOUT" and r.error.retryable


async def test_blender_crash_on_missing_artifacts(tmp_path, monkeypatch):
    scene = _write_scene(tmp_path / "s.json")

    def rc1_no_output(cmd, **kw):
        return type("R", (), {"returncode": 0})()          # rc 0 但什么都没写

    monkeypatch.setattr(runner.subprocess, "run", rc1_no_output)
    r = await runner.build_white_model(scene, tmp_path / "model", blender_exe="blender")
    assert not r.ok and r.error.code == "BLENDER_CRASH" and r.error.retryable


async def test_invalid_scene_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    r = await runner.build_white_model(p, tmp_path / "model", blender_exe="blender")
    assert not r.ok and r.error.code == "INPUT_INVALID"
    missing = await runner.build_white_model(tmp_path / "nope.json", tmp_path / "m",
                                             blender_exe="blender")
    assert not missing.ok and missing.error.code == "INPUT_INVALID"
```

- [ ] **Step 2: 跑测确认失败** → FAIL
- [ ] **Step 3: 实现 `app/tools/blender/runner.py`**

```python
import hashlib
import subprocess
from pathlib import Path

from pydantic import ValidationError

from app.models.scene import SceneJSON
from app.models.tooling import Metrics, ToolError, ToolResult
from app.tools.cache import build_cache_key

SCENE_BUILDER = Path(__file__).resolve().parents[3] / "blender" / "scene_builder.py"
TIMEOUT_S = 1800


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


async def build_white_model(scene_json_path: str | Path, out_dir: str | Path,
                            blender_exe: str | None = None) -> ToolResult[Path]:
    from app.core.config import get_settings
    from app.tools.blender.geom import build_plan
    src, out = Path(scene_json_path), Path(out_dir)
    if not src.exists():
        return ToolResult(ok=False, error=ToolError(code="INPUT_INVALID",
                                                    message=f"scene not found: {src}"))
    try:
        scene = SceneJSON.model_validate_json(src.read_text(encoding="utf-8"))
    except (ValueError, ValidationError) as e:
        return ToolResult(ok=False, error=ToolError(code="INPUT_INVALID",
                                                    message=f"bad scene json: {e}"))
    plan = build_plan(scene, str(out))
    expected = [f"{c.view_id}_{p}.png" for c in plan.cameras for p in plan.passes]
    key = build_cache_key("build_white_model", _sha(src), out.name)
    if all((out / n).exists() for n in expected):
        return ToolResult(ok=True, data=out, cache_key=key, cache_hit=True)
    out.mkdir(parents=True, exist_ok=True)
    plan_path = out / "build_plan.json"
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")
    exe = blender_exe or get_settings().blender_exe
    try:
        proc = subprocess.run(
            [exe, "--background", "--factory-startup", "--python", str(SCENE_BUILDER),
             "--", "--plan", str(plan_path)],
            capture_output=True, timeout=TIMEOUT_S)  # noqa: ASYNC221,ASYNC240
    except FileNotFoundError:
        return ToolResult(ok=False, error=ToolError(
            code="BLENDER_MISSING", message=f"blender not found: {exe}", retryable=False))
    except subprocess.TimeoutExpired:
        return ToolResult(ok=False, error=ToolError(
            code="BLENDER_TIMEOUT", message=f"blender timed out (> {TIMEOUT_S}s)",
            retryable=True))
    if proc.returncode != 0 or not all((out / n).exists() for n in expected):
        detail = proc.stdout[-200:] if isinstance(proc.stdout, (bytes, str)) else ""
        return ToolResult(ok=False, error=ToolError(
            code="BLENDER_CRASH",
            message=f"blender rc={proc.returncode}; missing artifacts; {detail!r}",
            retryable=True))
    return ToolResult(ok=True, data=out, cache_key=key, metrics=Metrics())
```

（注：`build_plan` 从 geom 延迟导入在函数体内——模块顶部 import 会与 geom 的 `from app.models.build_plan import ...` 形成环。）

- [ ] **Step 4: 跑测通过 → 全量门禁** → PASS
- [ ] **Step 5: Commit**

```bash
uv run pytest tests/unit/test_blender_runner.py -v && uv run mypy app && uv run ruff check app tests scripts
git add app/core/config.py app/tools/cache.py app/tools/blender/runner.py tests/unit/test_blender_runner.py
git commit -m "feat: build_white_model subprocess tool with typed errors and cache"
```

---

### Task 7: build_model CLI

**Files:**
- Create: `scripts/build_model.py`
- Test: `tests/unit/test_build_model_cli.py`

**Interfaces:**
- Consumes: Task 6 `build_white_model`
- Produces: `uv run python scripts/build_model.py <scene.json> [--out experiments/model]`；`ensure_scene(path: Path) -> None`（存在 + 可解析守卫，失败 stderr + SystemExit(2)）；成功打印控制图目录与喂给 ab_render 的提示命令

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_build_model_cli.py
from pathlib import Path

import pytest

from app.models.scene import SceneJSON, Wall
from scripts.build_model import ensure_scene


def test_ensure_scene_passes_valid(tmp_path, capsys):
    p = tmp_path / "s.json"
    p.write_text(SceneJSON(walls=[Wall(id="w", polygon=[[0, 0], [1, 0], [1, 1], [0, 1]])])
                 .model_dump_json(), encoding="utf-8")
    ensure_scene(p)                                    # 不抛即通过


def test_ensure_scene_rejects_missing(tmp_path, capsys):
    with pytest.raises(SystemExit) as ei:
        ensure_scene(tmp_path / "nope.json")
    assert ei.value.code == 2


def test_ensure_scene_rejects_corrupt(tmp_path, capsys):
    p = tmp_path / "bad.json"
    p.write_text("{oops", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        ensure_scene(p)
    assert ei.value.code == 2
```

- [ ] **Step 2: 跑测确认失败** → FAIL
- [ ] **Step 3: 实现 `scripts/build_model.py`**

```python
"""SceneJSON → 白模控制图 CLI（内部工具）。

用法: uv run python scripts/build_model.py <scene.json> [--out experiments/model]
产出: --out 下 {view_id}_{depth|lineart|white}.png + build_plan.json
后续: 控制图目录直接可作 Phase 1 渲染输入:
      uv run python scripts/ab_render.py --control-dir <out> --description "..."
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logging import setup_logging
from app.models.scene import SceneJSON
from app.tools.blender.runner import build_white_model


def ensure_scene(path: Path) -> None:
    if not path.exists():
        print(f"[error] scene 不存在: {path}", file=sys.stderr)
        raise SystemExit(2)
    try:
        SceneJSON.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[error] scene.json 无法解析: {e}", file=sys.stderr)
        raise SystemExit(2)


def main() -> None:
    ap = argparse.ArgumentParser(description="SceneJSON → 白模控制图（内部工具）")
    ap.add_argument("scene", type=Path)
    ap.add_argument("--out", type=Path, default=Path("experiments/model"))
    args = ap.parse_args()
    setup_logging()
    ensure_scene(args.scene)
    result = asyncio.run(build_white_model(args.scene, args.out))
    if not result.ok or result.data is None:
        code = result.error.code if result.error else "?"
        msg = result.error.message if result.error else ""
        print(f"[error] 白模构建失败: {code} {msg}", file=sys.stderr)
        raise SystemExit(2)
    print(f"控制图目录: {result.data}")
    print(f'下一步: uv run python scripts/ab_render.py --control-dir "{result.data}" '
          f'--description "风格描述"')


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑测通过 → 全量门禁** → PASS
- [ ] **Step 5: Commit**

```bash
uv run pytest tests/unit/test_build_model_cli.py -v && uv run mypy app && uv run ruff check app tests scripts
git add scripts/build_model.py tests/unit/test_build_model_cli.py
git commit -m "feat: build_model CLI bridging scene JSON to render control maps"
```

---

### Task 8: 真机全链验收（人工，需 Blender）

**Files:**
- Create: `docs/blender-acceptance-2026-09.md`（验收记录）

前置：`winget install BlenderFoundation.Blender`（5.2.x；安装后 `blender` 进 PATH，或 `.env` 设 `ARP_BLENDER_EXE`）。

- [ ] 合成户型全链：
  ```bash
  uv run python scripts/cad_inspect.py fixtures/dxf/apartment.dxf
  uv run python scripts/build_model.py experiments/cad/apartment/scene.json --out experiments/model/apartment
  ```
  验收：`view_01..04` 的 depth/lineart/white PNG 各就位；lineart 图上可辨认 2 房间墙线与门洞；depth 灰度层次合理
- [ ] 渲染衔接（GPU 就绪时）：把控制图目录喂 `scripts/ab_render.py --control-dir experiments/model/apartment --description "温馨日式原木风"` —— CAD→白模→AI 渲染一条龙首跑
- [ ] 真图降级冒烟：`scripts/build_model.py experiments/cad/01-平面系统图/scene.json` → 无房间走全局单机位（勘察发现 1a 兜底路径），16 墙白模可辨
- [ ] 结果与偏差（Freestyle×CYCLES 兼容性、引擎名、Mist 范围）记入 `docs/blender-acceptance-2026-09.md`；薄壳层允许现场校准（几何层不许改）

**验收标准（切片 2 完成判定）**：离线 80+ 测试全绿（68 + 本切片新增）；真机合成户型三 pass 产出并可被 ab_render 消费；真图降级路径有产出。

---

## Self-Review 记录

- **Spec 覆盖**：V2 模块 4 白模要素——墙体挤出/门洞/窗洞（分段法 Task 2）/地面（Task 4 floor）/家具体块（Task 4 furniture）/相机（Task 3/5）/三 pass 控制图（Task 5；天花与 normal pass 不做——V2 允许"天花或开放顶部"，MVP 开放顶部；normal 无消费者，YAGNI）。V2 模块 5 视角规则——房间中心/1.5m/离墙（buffer -400 收缩近似）/评分取前 N（Task 3；raycast 属 Phase 3 增强，V2 口径允许）。spec §7 工具规范——ToolResult/缓存/错误码/池标签（cpu 类，进程内已足够，不建池）。勘察四输入落点见 Global Constraints 末行。
- **占位符扫描**：Task 5 测试策略的静态校验是薄壳边界的诚实声明而非 TBD；Task 6 的 geom 延迟导入有环依赖说明。无 TBD/TODO。
- **类型一致性**：`Box`/`Opening`/`CameraPose` NamedTuple 字段在 Task 1-4 一致；`BuildPlan.passes` 默认值与 Task 5 脚本消费、Task 6 expected 推导一致；`build_white_model(scene, out_dir, blender_exe)` 在 Task 7 CLI 调用一致；控制图命名 `{view_id}_{pass}.png` 在 Task 5/6/7 三处一致且 view_id 生成规则（`view_{n:02d}`）与 Phase 1 ab_render 的 `*_depth.png` glob 契约兼容。
