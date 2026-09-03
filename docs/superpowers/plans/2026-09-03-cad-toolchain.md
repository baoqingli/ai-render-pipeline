# Phase 2 切片 1：CAD 工具链实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 DWG/DXF 解析为结构化 SceneJSON（V2 模块 3 中间层）——ODA 转换封装、ezdxf 图层/块/天正 proxy 勘察、双线墙体/门窗/房间提取，附内部诊断 CLI，并用真实图纸（fixtures/cad/01-平面系统图.dwg）完成首次勘察。

**Architecture:** 全部落在 `app/tools/cad/`（spec §7 工具层规范：ToolResult 信封、缓存键幂等、错误码化）；解析用"平行线配对 → 墙中心线+实际厚度 → shapely polygonize 出房间 → 块引用提取门窗 → 文字标注取层高/房间名"的确定性管线，置信度评分 + 默认值兜底（V2/requirement：解析失败不中断）。测试语料用 ezdxf 程序化生成的合成户型图（离线、确定性），真实图纸勘察作为收尾人工步骤。

**Tech Stack:** ezdxf（读 DXF）、shapely（几何）、subprocess（ODA File Converter）；沿用 Phase 1 全部门禁（pytest/mypy/ruff 钉选集）。

**上游文档:** [spec §4/§7](../../superpowers/specs/2026-09-02-agent-platform-design.md) · [V2 模块 1-3](../../cad-to-render-pipeline-v2.md) · Phase 1 计划（工具规范先例）

## Global Constraints

- Python `3.12`，uv 管理；Conventional Commits；提交带 trailer `Co-Authored-By: Claude <noreply@anthropic.com>`
- 第三方（ezdxf/shapely/ODA）只出现在 `app/tools/cad/`，graph/agents 层禁止直接 import
- **SceneJSON 字段以 V2 模块 3 为准（逐字段）**：`unit/floor_height/wall_thickness/walls[].polygon/doors[].{position,width,height,wall_id}/windows[].{...,sill_height}/furniture[].{...,source: "cad"|"ai_supplement"}/rooms[].{name,polygon}`；坐标统一归一到 **mm**
- 默认值兜底不中断：层高 2800、墙厚 200；兜底动作记入结果（`fallbacks`）
- 错误码：`INPUT_INVALID`（文件不可读）/ `ODA_MISSING`（转换器未装，不可重试）/ `PROXY_ENTITY`（天正代理实体，触发降级而非失败）/ `PARSE_LOW_CONFIDENCE`（置信度 < 0.6）
- 天正 proxy 判定 = dxftype 为 `ACAD_PROXY_ENTITY`（spec §6.2）
- 缓存键含工具版本号（沿用 `app/tools/cache.py` 的 `TOOL_VERSIONS`/`build_cache_key`）
- 测试全部离线（合成 DXF + monkeypatch subprocess），输出 pristine；`uv run mypy app` 0 错；`uv run ruff check app tests scripts` 全过（规则集已钉）
- 新依赖只加 `ezdxf>=1.3`、`shapely>=2.0`（networkx 本切片不用，不加——YAGNI）

---

### Task 1: 依赖与 SceneJSON 领域模型

**Files:**
- Modify: `pyproject.toml`（dependencies 加 `"ezdxf>=1.3", "shapely>=2.0"`）
- Create: `app/models/scene.py`
- Test: `tests/unit/test_scene_models.py`

**Interfaces:**
- Produces（后续任务与切片 2/3 依赖的精确类型）:
  - `Wall(id: str, polygon: list[list[float]])`
  - `Door(id, position: list[float], width: float, height: float = 2100.0, wall_id: str | None = None)`
  - `Window(id, position: list[float], width: float, height: float = 1200.0, sill_height: float = 900.0, wall_id: str | None = None)`
  - `Furniture(id, type: str, position: list[float], size: list[float], rotation: float = 0.0, source: Literal["cad","ai_supplement"])`
  - `Room(id, name: str | None = None, polygon: list[list[float]])`
  - `SceneJSON(unit: Literal["mm","m"]="mm", floor_height: float=2800.0, wall_thickness: float=200.0, walls/doors/windows/furniture/rooms: list[...] = [])`

- [ ] **Step 1: 加依赖并锁**：`uv add "ezdxf>=1.3" "shapely>=2.0"`（会同时更新 uv.lock）
- [ ] **Step 2: 写失败测试**

```python
# tests/unit/test_scene_models.py
import pytest
from pydantic import ValidationError

from app.models.scene import Furniture, SceneJSON, Wall


def test_scene_defaults_follow_v2():
    s = SceneJSON()
    assert s.unit == "mm" and s.floor_height == 2800.0 and s.wall_thickness == 200.0
    assert s.walls == [] and s.rooms == []


def test_furniture_source_is_closed_enum():
    Furniture(id="f1", type="sofa", position=[0, 0], size=[100, 50], source="cad")
    with pytest.raises(ValidationError):
        Furniture(id="f2", type="sofa", position=[0, 0], size=[100, 50], source="dream")


def test_wall_polygon_roundtrip():
    w = Wall(id="wall_001", polygon=[[0, 0], [5000, 0], [5000, 200], [0, 200]])
    assert w.polygon[1] == [5000, 0]
```

- [ ] **Step 3: 跑测确认失败** → `uv run pytest tests/unit/test_scene_models.py -v` FAIL（模块不存在）
- [ ] **Step 4: 实现 `app/models/scene.py`**

```python
from pydantic import BaseModel
from typing import Literal


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
```

- [ ] **Step 5: 跑测通过 → Commit**

```bash
uv run pytest tests/unit/test_scene_models.py -v
git add -A && git commit -m "feat: scene JSON domain models per V2 module 3"
```

---

### Task 2: 合成户型 DXF 测试语料

**Files:**
- Create: `scripts/gen_fixtures.py`
- Create: `fixtures/dxf/apartment.dxf`（生成后提交）
- Test: `tests/unit/test_fixture_dxf.py`

**Interfaces:**
- Produces: `make_apartment_dxf(path: str | Path) -> Path`（scripts/gen_fixtures.py 内，供测试复用；文件提交供 inspect/parse/CLI 任务与人工查看）
- 语料内容契约（后续所有断言的依据）：外墙 6000×4000 双线（墙厚 200，即每条墙边画两条相距 200 的平行 LINE，图层 `WALL`）；内墙 x=4000 处双线；`DOOR` 图层块引用 `M_门_900` 插于 (4000, 1500)；`WINDOW` 图层块 `C_1500` 插于 (3000, 4000)；`FURN` 图层块 `sofa` 插于 (2000, 800)；TEXT 实体：`"客厅"`@(2000,2000)、`"卧室"`@(5000,2000)、`"层高2800"`@(0,-500)（图层 `NOTE`）；垃圾内容：`DIM` 图层一个线性标注 + `GARBAGE` 图层一条随机斜线；DXF 版本 R2018

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_fixture_dxf.py
from pathlib import Path

import ezdxf

from scripts.gen_fixtures import make_apartment_dxf


def test_apartment_fixture_matches_contract(tmp_path: Path):
    p = make_apartment_dxf(tmp_path / "apartment.dxf")
    doc = ezdxf.readfile(p)
    msp = doc.modelspace()
    lines = [e for e in msp if e.dxftype() == "LINE"]
    assert {e.dxf.layer for e in lines} >= {"WALL", "GARBAGE"}
    assert sum(1 for e in lines if e.dxf.layer == "WALL") == 12  # 4 外边 + 内墙 2 = 6 条墙边 × 2 线
    inserts = [e for e in msp if e.dxftype() == "INSERT"]
    assert {e.dxf.name for e in inserts} == {"M_门_900", "C_1500", "sofa"}
    texts = [e.dxf.text for e in msp.query("TEXT")]
    assert {"客厅", "卧室", "层高2800"} <= set(texts)
    assert "DIM" in {e.dxf.layer for e in msp}  # 垃圾标注存在
```

- [ ] **Step 2: 跑测确认失败** → FAIL
- [ ] **Step 3: 实现 `scripts/gen_fixtures.py`**

```python
"""生成合成户型 DXF 测试语料（离线确定性，Task 3-6 的解析对象）。

用法: uv run python scripts/gen_fixtures.py   # 写入 fixtures/dxf/apartment.dxf
"""
import sys
from pathlib import Path

import ezdxf

T = 200.0  # 墙厚


def _wall_pair(msp, p1, p2, t=T):
    """一条墙边 → 两条相距 t 的平行线（法向偏移 ±t/2）"""
    (x1, y1), (x2, y2) = p1, p2
    dx, dy = x2 - x1, y2 - y1
    L = (dx * dx + dy * dy) ** 0.5
    nx, ny = -dy / L * t / 2, dx / L * t / 2
    for s in (1, -1):
        msp.add_line((x1 + s * nx, y1 + s * ny), (x2 + s * nx, y2 + s * ny), dxfattribs={"layer": "WALL"})


def make_apartment_dxf(path) -> Path:
    doc = ezdxf.new("R2018", setup=True)
    msp = doc.modelspace()
    for layer in ["WALL", "DOOR", "WINDOW", "FURN", "NOTE", "DIM", "GARBAGE"]:
        doc.layers.add(layer)
    # 外墙矩形 + 内墙（x=4000, 0→4000）
    _wall_pair(msp, (0, 0), (6000, 0))
    _wall_pair(msp, (6000, 0), (6000, 4000))
    _wall_pair(msp, (6000, 4000), (0, 4000))
    _wall_pair(msp, (0, 4000), (0, 0))
    _wall_pair(msp, (4000, 0), (4000, 4000))
    # 门窗家具块（门 M_门_900 内墙、窗 C_1500 顶外墙、沙发 FURN）
    for name, layer, pos in [("M_门_900", "DOOR", (4000, 1500)),
                             ("C_1500", "WINDOW", (3000, 4000)),
                             ("sofa", "FURN", (2000, 800))]:
        doc.blocks.new(name=name)
        msp.add_blockref(name, pos, dxfattribs={"layer": layer})
    for text, pos in [("客厅", (2000, 2000)), ("卧室", (5000, 2000)), ("层高2800", (0, -500))]:
        msp.add_text(text, dxfattribs={"layer": "NOTE"}).set_placement(pos)
    # 垃圾：一个线性标注 + 一条随机斜线
    msp.add_linear_dim(base=(3000, -300), p1=(0, 0), p2=(6000, 0), dxfattribs={"layer": "DIM"}).render()
    msp.add_line((6500, -200), (7000, 300), dxfattribs={"layer": "GARBAGE"})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("fixtures/dxf/apartment.dxf")
    print(f"wrote {make_apartment_dxf(out)}")
```

- [ ] **Step 4: 跑测通过；生成并提交语料**

```bash
uv run pytest tests/unit/test_fixture_dxf.py -v
uv run python scripts/gen_fixtures.py
git add scripts/gen_fixtures.py fixtures/dxf/apartment.dxf tests/unit/test_fixture_dxf.py
git commit -m "test: synthetic apartment DXF fixture corpus"
```

---

### Task 3: convert_dwg 工具（ODA 封装）

**Files:**
- Modify: `app/core/config.py`（Settings 加 `oda_exe: str = "ODAFileConverter"`）
- Modify: `app/tools/cache.py`（TOOL_VERSIONS 加 `"convert_dwg": "1"`）
- Create: `app/tools/cad/__init__.py`, `app/tools/cad/convert.py`
- Test: `tests/unit/test_convert_dwg.py`

**Interfaces:**
- Produces: `async def convert_dwg(dwg_path: str | Path, out_dir: str | Path) -> ToolResult[Path]`——成功时 `data` 为转换后 DXF 路径；缓存键 = `build_cache_key("convert_dwg", sha256(dwg内容), out_dir名)`；ODA 是**目录对目录**转换（`ODAFileConverter <in> <out> ACAD2018 DXF 0 1`），单文件用临时目录中转；exe 不在 → `ToolError(code="ODA_MISSING", retryable=False)`

- [ ] **Step 1: 写失败测试（monkeypatch subprocess，离线）**

```python
# tests/unit/test_convert_dwg.py
from pathlib import Path

import pytest

from app.tools.cad import convert as conv


def _make_dwg(p: Path) -> Path:
    p.write_bytes(b"AC1032fake-dwg-bytes")
    return p


async def test_convert_success_via_fake_oda(tmp_path, monkeypatch):
    dwg = _make_dwg(tmp_path / "a.dwg")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = list(cmd)
        Path(cmd[2], "a.dxf").write_bytes(b"fake-dxf")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(conv.subprocess, "run", fake_run)
    result = await conv.convert_dwg(dwg, tmp_path / "out")
    assert result.ok and result.data is not None and result.data.name == "a.dxf"
    assert "DXF" in seen["cmd"]                     # 参数含 DXF 输出类型


async def test_convert_cache_hit_second_call(tmp_path, monkeypatch):
    dwg = _make_dwg(tmp_path / "a.dwg")
    runs = []

    def fake_run(cmd, **kw):
        runs.append(1)
        Path(cmd[2], "a.dxf").write_bytes(b"fake-dxf")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(conv.subprocess, "run", fake_run)
    first = await conv.convert_dwg(dwg, tmp_path / "out")
    second = await conv.convert_dwg(dwg, tmp_path / "out")
    assert first.ok and second.ok and second.cache_hit and len(runs) == 1


async def test_oda_missing_is_not_retryable(tmp_path, monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError("no exe")

    monkeypatch.setattr(conv.subprocess, "run", boom)
    result = await conv.convert_dwg(_make_dwg(tmp_path / "a.dwg"), tmp_path / "out")
    assert not result.ok and result.error is not None
    assert result.error.code == "ODA_MISSING" and not result.error.retryable


async def test_empty_input_file_invalid(tmp_path):
    p = tmp_path / "a.dwg"
    p.write_bytes(b"")
    result = await conv.convert_dwg(p, tmp_path / "out")
    assert not result.ok and result.error.code == "INPUT_INVALID"
```

- [ ] **Step 2: 跑测确认失败** → FAIL
- [ ] **Step 3: 实现 `app/tools/cad/convert.py`**

```python
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.core.config import get_settings
from app.models.tooling import Metrics, ToolError, ToolResult
from app.tools.cache import build_cache_key

ODA_ARGS = ["ACAD2018", "DXF", "0", "1"]  # 输出版本/类型/递归/审计——装好 ODA 后用真实版本核对参数顺序


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


async def convert_dwg(dwg_path: str | Path, out_dir: str | Path) -> ToolResult[Path]:
    src, out = Path(dwg_path), Path(out_dir)
    if not src.exists() or src.stat().st_size == 0:
        return ToolResult(ok=False, error=ToolError(code="INPUT_INVALID", message=f"bad dwg: {src}"))
    key = build_cache_key("convert_dwg", _sha(src), out.name)
    cached = out / (src.stem + ".dxf")
    if cached.exists():  # 简式缓存：产物存在即命中（键含内容 hash，上游换了文件则重建）
        return ToolResult(ok=True, data=cached, cache_key=key, cache_hit=True)
    exe = get_settings().oda_exe
    if shutil.which(exe) is None and not Path(exe).exists():
        return ToolResult(ok=False, error=ToolError(code="ODA_MISSING",
                                                    message=f"ODA File Converter not found: {exe}"))
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tin = Path(td) / "in"
        tin.mkdir()
        shutil.copy2(src, tin / src.name)
        proc = subprocess.run([exe, str(tin), str(out), *ODA_ARGS],
                              capture_output=True, timeout=300)
    if proc.returncode != 0 or not cached.exists():
        return ToolResult(ok=False, error=ToolError(code="INPUT_INVALID",
                                                    message=f"ODA failed rc={proc.returncode}: "
                                                            f"{proc.stdout[:120]!r}"))
    return ToolResult(ok=True, data=cached, cache_key=key, metrics=Metrics())
```

- [ ] **Step 4: 跑测通过 → Commit**

```bash
uv run pytest tests/unit/test_convert_dwg.py -v
git add -A && git commit -m "feat: ODA-backed DWG->DXF conversion tool"
```

---

### Task 4: inspect_dxf 工具（CadReport）

**Files:**
- Create: `app/models/cad_report.py`
- Create: `app/tools/cad/inspect.py`
- Modify: `app/tools/cache.py`（TOOL_VERSIONS 加 `"inspect_dxf": "1"`）
- Test: `tests/unit/test_cad_inspect_tool.py`

**Interfaces:**
- Produces:
  - `LayerStat(name: str, line_count: int, polyline_count: int, text_count: int, insert_count: int, other_count: int)`
  - `BlockStat(name: str, layer: str, insert_count: int)`
  - `TextNote(layer: str, content: str, position: list[float])`
  - `CadReport(layers: list[LayerStat], blocks: list[BlockStat], proxy_entity_count: int, text_annotations: list[TextNote], floor_height_candidates: list[float], unit_guess: Literal["mm","m"], confidence: float, wall_layer_candidates: list[str])`
  - `def inspect_dxf(dxf_path: str | Path) -> CadReport`（同步纯读；外层如需 ToolResult 由调用方包）
  - `def count_proxies(msp) -> int`（独立函数，dxftype == "ACAD_PROXY_ENTITY" 计数）

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_cad_inspect_tool.py
from pathlib import Path

from scripts.gen_fixtures import make_apartment_dxf

from app.tools.cad.inspect import count_proxies, inspect_dxf


class _FakeEnt:
    def __init__(self, t): self._t = t
    def dxftype(self): return self._t


def test_count_proxies_detects_acad_proxy():
    msp = [_FakeEnt("LINE"), _FakeEnt("ACAD_PROXY_ENTITY"), _FakeEnt("ACAD_PROXY_ENTITY")]
    assert count_proxies(msp) == 2


def test_inspect_apartment_fixture(tmp_path: Path):
    report = inspect_dxf(make_apartment_dxf(tmp_path / "a.dxf"))
    wall = next(l for l in report.layers if l.name == "WALL")
    assert wall.line_count == 12 and wall.insert_count == 0
    assert "WALL" in report.wall_layer_candidates
    assert 2800.0 in report.floor_height_candidates
    assert report.unit_guess == "mm"          # 图幅 6000+ → mm
    assert report.proxy_entity_count == 0
    assert report.confidence > 0.6
    names = {b.name for b in report.blocks}
    assert {"M_门_900", "C_1500", "sofa"} <= names
    assert any("客厅" == t.content for t in report.text_annotations)
```

- [ ] **Step 2: 跑测确认失败** → FAIL
- [ ] **Step 3: 实现 `app/models/cad_report.py` 与 `app/tools/cad/inspect.py`**

```python
# app/models/cad_report.py
from pydantic import BaseModel
from typing import Literal


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
```

```python
# app/tools/cad/inspect.py
import re
from pathlib import Path

import ezdxf

from app.models.cad_report import BlockStat, CadReport, LayerStat, TextNote

WALL_LAYER_RE = re.compile(r"(wall|墙|a-wall|arch)", re.IGNORECASE)
HEIGHT_RE = re.compile(r"层高\s*(\d{3,4})")

LINE_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE"}
TEXT_TYPES = {"TEXT", "MTEXT"}


def count_proxies(msp) -> int:
    return sum(1 for e in msp if e.dxftype() == "ACAD_PROXY_ENTITY")


def _extent(msp) -> float:
    xs, ys = [], []
    for e in msp:
        if e.dxftype() == "LINE":
            xs += [e.dxf.start.x, e.dxf.end.x]
            ys += [e.dxf.start.y, e.dxf.end.y]
    if not xs:
        return 0.0
    return max(max(xs) - min(xs), max(ys) - min(ys))


def inspect_dxf(dxf_path: str | Path) -> CadReport:
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    layers: dict[str, LayerStat] = {}
    blocks: dict[tuple[str, str], BlockStat] = {}
    notes: list[TextNote] = []
    proxies = 0

    def stat(name: str) -> LayerStat:
        return layers.setdefault(name, LayerStat(name=name))

    for e in msp:
        t, layer = e.dxftype(), e.dxf.layer
        s = stat(layer)
        if t == "ACAD_PROXY_ENTITY":
            proxies += 1
            s.other_count += 1
        elif t == "LINE":
            s.line_count += 1
        elif t in ("LWPOLYLINE", "POLYLINE"):
            s.polyline_count += 1
        elif t in TEXT_TYPES:
            s.text_count += 1
            content = e.dxf.text if t == "TEXT" else e.text
            pos = list(e.dxf.insert[:2]) if t == "MTEXT" else list(e.dxf.align_point[:2]) \
                if getattr(e.dxf, "align_point", None) else [0.0, 0.0]
            notes.append(TextNote(layer=layer, content=content.strip(), position=pos))
        elif t == "INSERT":
            s.insert_count += 1
            key = (e.dxf.name, layer)
            b = blocks.setdefault(key, BlockStat(name=e.dxf.name, layer=layer, insert_count=0))
            b.insert_count += 1
        else:
            s.other_count += 1

    heights = [float(m.group(1)) for n in notes if (m := HEIGHT_RE.search(n.content))]
    ext = _extent(msp)
    unit: str = "mm" if ext > 1000 or ext == 0 else "m"
    wall_candidates = [n for n, s in layers.items()
                       if WALL_LAYER_RE.search(n) and (s.line_count + s.polyline_count) >= 4]
    confidence = 1.0
    if not wall_candidates:
        confidence -= 0.4
    if not heights:
        confidence -= 0.1
    if proxies:
        confidence -= 0.3
    if ext == 0:
        confidence -= 0.2
    return CadReport(layers=sorted(layers.values(), key=lambda s: -s.line_count - s.polyline_count),
                     blocks=list(blocks.values()), proxy_entity_count=proxies,
                     text_annotations=notes, floor_height_candidates=heights,
                     unit_guess=unit,  # type: ignore[arg-type]
                     confidence=round(confidence, 2), wall_layer_candidates=wall_candidates)
```

- [ ] **Step 4: 跑测通过 → Commit**

```bash
uv run pytest tests/unit/test_cad_inspect_tool.py -v
git add -A && git commit -m "feat: DXF inspection producing CadReport with proxy/unit/confidence"
```

---

### Task 5: parse_scene 工具（墙/门窗/房间 → SceneJSON）

**Files:**
- Create: `app/tools/cad/geometry.py`, `app/tools/cad/parse.py`
- Modify: `app/tools/cache.py`（TOOL_VERSIONS 加 `"parse_scene": "1"`）
- Test: `tests/unit/test_parse_scene.py`

**Interfaces:**
- Consumes: `inspect_dxf`/`CadReport`（Task 4）、`SceneJSON`（Task 1）、语料（Task 2）
- Produces:
  - `Centerline = tuple[tuple[float, float], tuple[float, float], float]`（(p1, p2, thickness)）
  - `def pair_wall_segments(segments: list[tuple[vec, vec]], max_thickness: float = 500.0) -> list[Centerline]`（geometry.py）
  - `def parse_scene(dxf_path: str | Path, report: CadReport | None = None) -> ToolResult[SceneJSON]`——置信度 < 0.6 时 `ok=True` 但带 `PARSE_LOW_CONFIDENCE` 于 `error` 字段提示（不失败，走默认值兜底）

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_parse_scene.py
from pathlib import Path

from scripts.gen_fixtures import make_apartment_dxf

from app.tools.cad.geometry import pair_wall_segments
from app.tools.cad.parse import parse_scene


def _seg(x1, y1, x2, y2):
    return ((x1, y1), (x2, y2))


def test_pair_wall_segments_finds_thickness():
    segs = [_seg(0, -100, 6000, -100), _seg(0, 100, 6000, 100), _seg(7000, 0, 7000, 4000)]
    centers = pair_wall_segments(segs)
    assert len(centers) == 2                       # 一对 + 一条落单
    paired = [c for c in centers if c[2] != 200.0]
    assert any(abs(c[2] - 200.0) < 1e-6 for c in centers)  # 配对得到实际厚度
    assert any(c[1][0] == 7000 for c in centers)           # 落单段保留，厚度走默认


def test_parse_apartment_fixture(tmp_path: Path):
    result = parse_scene(make_apartment_dxf(tmp_path / "a.dxf"))
    assert result.ok
    scene = result.data
    assert scene is not None
    assert scene.floor_height == 2800.0            # 从"层高2800"标注提取
    assert scene.unit == "mm"
    assert len(scene.rooms) == 2                    # 客厅 + 卧室
    names = {r.name for r in scene.rooms}
    assert names == {"客厅", "卧室"}
    assert len(scene.doors) == 1 and scene.doors[0].width == 900.0
    assert len(scene.windows) == 1 and scene.windows[0].width == 1500.0
    assert scene.doors[0].wall_id is not None       # 挂到最近墙
    assert len(scene.walls) >= 5                    # 6 条墙边（配对后 5-6 段中心线）
    assert scene.furniture and scene.furniture[0].source == "cad"
    assert scene.furniture[0].type == "sofa"
```

- [ ] **Step 2: 跑测确认失败** → FAIL
- [ ] **Step 3: 实现 `app/tools/cad/geometry.py`**

```python
"""双线墙配对：平行、间距≤max_thickness、投影重叠——输出中心线+实际厚度。"""
import math

Vec = tuple[float, float]
Seg = tuple[Vec, Vec]
Centerline = tuple[Vec, Vec, float]  # (p1, p2, thickness)


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _unit(v: Vec) -> Vec:
    n = math.hypot(*v) or 1.0
    return (v[0] / n, v[1] / n)


def _line_dist(p: Vec, a: Vec, d: Vec) -> float:
    return abs(_sub(p, a)[0] * d[1] - _sub(p, a)[1] * d[0])


def pair_wall_segments(segments: list[Seg], max_thickness: float = 500.0,
                       default_thickness: float = 200.0) -> list[Centerline]:
    used: set[int] = set()
    out: list[Centerline] = []
    segs = list(segments)
    for i, (a1, a2) in enumerate(segs):
        if i in used:
            continue
        d1 = _unit(_sub(a2, a1))
        L1 = math.hypot(*_sub(a2, a1))
        best = None
        for j in range(i + 1, len(segs)):
            if j in used:
                continue
            b1, b2 = segs[j]
            d2 = _unit(_sub(b2, b1))
            if abs(abs(_dot(d1, d2)) - 1.0) > 1e-6:      # 不平行
                continue
            dists = [_line_dist(p, a1, d1) for p in (b1, b2)]
            if max(dists) > max_thickness or min(dists) < 1.0:  # 太远或共线
                continue
            ts = [_dot(_sub(p, a1), d1) for p in (b1, b2)]
            lo_t, hi_t = max(min(ts), 0.0), min(max(ts), L1)
            if hi_t - lo_t < 0.5 * min(L1, abs(ts[1] - ts[0])):  # 投影重叠不足
                continue
            thickness = (dists[0] + dists[1]) / 2.0
            best = (j, d1, lo_t, hi_t, thickness)
            break
        if best is None:
            out.append((a1, a2, default_thickness))       # 落单：默认厚度
            continue
        j, d1, lo_t, hi_t, thickness = best
        used.add(j)
        n = (-d1[1], d1[0])
        s_dist = _dot(_sub(segs[j][0], a1), n)            # 有符号偏移
        shift = (n[0] * s_dist / 2, n[1] * s_dist / 2)
        c1 = (a1[0] + d1[0] * lo_t + shift[0], a1[1] + d1[1] * lo_t + shift[1])
        c2 = (a1[0] + d1[0] * hi_t + shift[0], a1[1] + d1[1] * hi_t + shift[1])
        out.append((c1, c2, thickness))
    return out
```

- [ ] **Step 4: 实现 `app/tools/cad/parse.py`**

```python
"""DXF → SceneJSON：墙体中心线缓冲成条带、polygonize 出房间、块引用成门窗、标注取名。"""
import re
from pathlib import Path

import ezdxf
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

from app.models.cad_report import CadReport
from app.models.scene import Door, Furniture, Room, SceneJSON, Wall, Window
from app.models.tooling import Metrics, ToolError, ToolResult
from app.tools.cache import build_cache_key
from app.tools.cad.geometry import Centerline, pair_wall_segments
from app.tools.cad.inspect import inspect_dxf

WIDTH_RE = re.compile(r"(\d{3,4})")
ROOM_NAME_RE = re.compile(r"(室|厅|卧|厨|卫|阳台)")
DEFAULT_WALL_T = 200.0
MIN_ROOM_AREA_MM2 = 1_000_000    # 1 m²
MAX_ROOM_AREA_MM2 = 1_000_000_000  # 1000 m²


def _segments_from(msp, layers: set[str]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    segs = []
    for e in msp:
        if e.dxf.layer not in layers:
            continue
        if e.dxftype() == "LINE":
            segs.append(((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)))
        elif e.dxftype() == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in e.get_points()]
            segs += list(zip(pts, pts[1:]))
    return segs


def _strip_polygon(c: Centerline) -> list[list[float]]:
    (x1, y1), (x2, y2), t = c
    ls = LineString([(x1, y1), (x2, y2)]).buffer(t / 2, cap_style=2, join_style=2)
    return [[float(x), float(y)] for x, y in ls.exterior.coords]


def _nearest_wall_id(point: Point, walls: list[Wall], strips: dict[str, LineString]) -> str | None:
    best, best_d = None, None
    for w in walls:
        d = point.distance(strips[w.id])
        if best_d is None or d < best_d:
            best, best_d = w.id, d
    return best


def parse_scene(dxf_path: str | Path, report: CadReport | None = None) -> ToolResult[SceneJSON]:
    rep = report or inspect_dxf(dxf_path)
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    fallbacks: list[str] = []
    scale = 1000.0 if rep.unit_guess == "m" else 1.0   # 统一归一 mm

    wall_layers = set(rep.wall_layer_candidates) or {"WALL"}
    segs = [((a[0] * scale, a[1] * scale), (b[0] * scale, b[1] * scale))
            for a, b in _segments_from(msp, wall_layers)]
    centers = pair_wall_segments(segs, default_thickness=DEFAULT_WALL_T)
    walls = [Wall(id=f"wall_{i+1:03d}", polygon=_strip_polygon(c)) for i, c in enumerate(centers)]
    strips: dict[str, LineString] = {}
    for i, w in enumerate(walls):
        c = centers[i]
        strips[w.id] = LineString([(c[0][0], c[0][1]), (c[1][0], c[1][1])]).buffer(c[2] / 2)

    # 房间：中心线网络 polygonize
    from shapely.ops import polygonize
    room_polys = [p for p in polygonize([LineString([(c[0][0], c[0][1]), (c[1][0], c[1][1])]) for c in centers])
                  if MIN_ROOM_AREA_MM2 <= p.area <= MAX_ROOM_AREA_MM2]
    rooms = []
    for i, poly in enumerate(sorted(room_polys, key=lambda p: -p.area)):
        name = None
        for note in rep.text_annotations:
            if ROOM_NAME_RE.search(note.content) and poly.contains(Point(note.position[0] * scale,
                                                                         note.position[1] * scale)):
                name = note.content
                break
        rooms.append(Room(id=f"room_{i+1:03d}", name=name,
                          polygon=[[float(x), float(y)] for x, y in poly.exterior.coords]))

    # 门窗家具：块引用
    doors, windows, furniture = [], [], []
    for e in msp.query("INSERT"):
        layer, name = e.dxf.layer, e.dxf.name
        pos = Point(e.dxf.insert.x * scale, e.dxf.insert.y * scale)
        m = WIDTH_RE.search(name)
        width = float(m.group(1)) if m else None
        lw = name.lower()
        if "door" in lw or "门" in name:
            doors.append(Door(id=f"door_{len(doors)+1:03d}", position=[pos.x, pos.y],
                              width=width or 900.0, wall_id=_nearest_wall_id(pos, walls, strips)))
        elif "window" in lw or "窗" in name:
            windows.append(Window(id=f"window_{len(windows)+1:03d}", position=[pos.x, pos.y],
                                  width=width or 1500.0, wall_id=_nearest_wall_id(pos, walls, strips)))
        else:
            furniture.append(Furniture(id=f"furn_{len(furniture)+1:03d}", type=name.split("_")[0],
                                       position=[pos.x, pos.y], size=[1000.0, 500.0, 500.0],
                                       source="cad"))

    floor_height = rep.floor_height_candidates[0] if rep.floor_height_candidates else 2800.0
    if not rep.floor_height_candidates:
        fallbacks.append("floor_height -> 2800 默认")
    thicknesses = [c[2] for c in centers]
    wall_t = max(set(thicknesses), key=thicknesses.count) if thicknesses else DEFAULT_WALL_T
    scene = SceneJSON(floor_height=floor_height, wall_thickness=wall_t, walls=walls,
                      doors=doors, windows=windows, furniture=furniture, rooms=rooms)
    err = ToolError(code="PARSE_LOW_CONFIDENCE", message="; ".join(fallbacks)) \
        if rep.confidence < 0.6 else None
    key = build_cache_key("parse_scene", str(dxf_path), str(sorted(wall_layers)))
    return ToolResult(ok=True, data=scene, error=err, cache_key=key, metrics=Metrics())
```

- [ ] **Step 5: 跑测通过（必要时按语料实际几何微调 polygonize 面积阈值——阈值改动要同步进 MIN/MAX 常量并写注释）→ Commit**

```bash
uv run pytest tests/unit/test_parse_scene.py -v
git add -A && git commit -m "feat: parse_scene extracting walls/rooms/openings to SceneJSON"
```

---

### Task 6: 内部诊断 CLI

**Files:**
- Create: `scripts/cad_inspect.py`
- Test: `tests/unit/test_cad_cli.py`

**Interfaces:**
- Consumes: `convert_dwg`、`inspect_dxf`、`parse_scene`
- Produces: `uv run python scripts/cad_inspect.py <dxf或dwg> [--out experiments/cad]`——DWG 自动先转（ODA 缺失给明确错误退出 2）；输出 `<out>/<stem>/report.md`（图层表/块表/置信度/房间墙数/fallbacks）+ `scene.json`；纯函数 `def render_report(report, scene, fallbacks) -> str` 与 `def ensure_dxf(path: Path) -> Path`（DWG→DXF 转换封装，供测试）

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_cad_cli.py
from pathlib import Path

from scripts.gen_fixtures import make_apartment_dxf

from app.tools.cad.inspect import inspect_dxf
from app.tools.cad.parse import parse_scene
from scripts.cad_inspect import render_report


def test_render_report_contains_key_sections(tmp_path: Path):
    dxf = make_apartment_dxf(tmp_path / "a.dxf")
    report = inspect_dxf(dxf)
    result = parse_scene(dxf)
    md = render_report(report, result.data, [])
    assert "WALL" in md and "置信度" in md and "客厅" in md
    assert "房间" in md and "2800" in md
```

- [ ] **Step 2: 跑测确认失败** → FAIL
- [ ] **Step 3: 实现 `scripts/cad_inspect.py`**

```python
"""CAD 内部诊断：DXF/DWG → CadReport + SceneJSON + markdown 报告（内部工具，不给终端用户）。"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logging import setup_logging
from app.models.cad_report import CadReport
from app.models.scene import SceneJSON
from app.tools.cad.convert import convert_dwg
from app.tools.cad.inspect import inspect_dxf
from app.tools.cad.parse import parse_scene


def ensure_dxf(path: Path) -> Path:
    if path.suffix.lower() == ".dwg":
        result = asyncio.run(convert_dwg(path, path.parent / "_converted"))
        if not result.ok or result.data is None:
            print(f"[error] DWG 转换失败: {result.error.code if result.error else '?'} "
                  f"{result.error.message if result.error else ''}", file=sys.stderr)
            raise SystemExit(2)
        return result.data
    return path


def render_report(report: CadReport, scene: SceneJSON | None, fallbacks: list[str]) -> str:
    lines = ["# CAD 解析诊断", "",
             f"- 置信度: **{report.confidence}**  天正 proxy 实体: {report.proxy_entity_count}  "
             f"单位推断: {report.unit_guess}",
             f"- 层高候选: {report.floor_height_candidates or '无（用默认 2800）'}",
             f"- 墙图层候选: {report.wall_layer_candidates or '无（走默认墙厚兜底）'}",
             f"- fallbacks: {fallbacks or '无'}", "",
             "| layer | line | pline | text | insert | other |", "| --- | --- | --- | --- | --- | --- |"]
    for s in report.layers:
        lines.append(f"| {s.name} | {s.line_count} | {s.polyline_count} | "
                     f"{s.text_count} | {s.insert_count} | {s.other_count} |")
    lines += ["", "| block | layer | count |", "| --- | --- | --- |"]
    for b in report.blocks:
        lines.append(f"| {b.name} | {b.layer} | {b.insert_count} |")
    if scene is not None:
        lines += ["", f"## 解析结果：墙 {len(scene.walls)} · 门 {len(scene.doors)} · "
                      f"窗 {len(scene.windows)} · 家具 {len(scene.furniture)} · 房间 {len(scene.rooms)}",
                  f"- 层高 {scene.floor_height}，墙厚 {scene.wall_thickness}"]
        for r in scene.rooms:
            lines.append(f"  - {r.id}: {r.name or '未命名'}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="CAD 内部诊断")
    ap.add_argument("cad", type=Path)
    ap.add_argument("--out", type=Path, default=Path("experiments/cad"))
    args = ap.parse_args()
    setup_logging()
    dxf = ensure_dxf(args.cad)
    report = inspect_dxf(dxf)
    result = parse_scene(dxf, report)
    scene = result.data
    out_dir = args.out / args.cad.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(render_report(report, scene, []), encoding="utf-8")
    if scene is not None:
        (out_dir / "scene.json").write_text(
            json.dumps(scene.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告: {out_dir / 'report.md'}  置信度: {report.confidence}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑测通过 → 全量门禁 → Commit**

```bash
uv run pytest tests/unit/test_cad_cli.py -v
uv run pytest -q && uv run mypy app && uv run ruff check app tests scripts
git add -A && git commit -m "feat: CAD internal diagnosis CLI with report and scene output"
```

---

### Task 7: 真实图纸勘察（人工触发，需 ODA 已安装）

**Files:**
- Create: `docs/cad-recon-2026-09.md`（勘察记录，人工/半自动填写）

非代码任务——ODA File Converter 装好后执行：

- [ ] `uv run python scripts/cad_inspect.py fixtures/cad/01-平面系统图.dwg`
- [ ] 检查 `experiments/cad/01-平面系统图/report.md`：图层表里有没有墙图层（WALL/墙/A-WALL 模式）？proxy 实体数（天正判定）？是建筑平面还是系统/MEP 图？
- [ ] 把结论写进 `docs/cad-recon-2026-09.md`：图纸类型、图层清单摘要、解析置信度、是否适配主链路、需要的解析规则补强（喂给 cad_diagnosis_agent 的 Phase 3 输入）
- [ ] 若为系统图不适配主链路：向用户要一张真正带墙体门窗布置的建筑平面图，放入 `fixtures/cad/`

**验收标准（本切片完成判定）**：合成户型全链绿（转换桩/勘察/解析/CLI 全测试通过）；真实图纸产出诊断报告（无论结论是适配还是降级）。

---

## Self-Review 记录

- **Spec 覆盖**：spec §7 工具清单 CAD 三件套（convert_dwg/inspect_dxf/parse_scene）→ Task 3/4/5；SceneJSON（V2 模块 3 逐字段）→ Task 1；天正 proxy 检测 → Task 4（count_proxies + confidence 罚分）；错误码四枚 → Task 3（INPUT_INVALID/ODA_MISSING）+ Task 5（PARSE_LOW_CONFIDENCE）+ Task 4（PROXY 检测供降级）；缓存键幂等 → Task 3/5（TOOL_VERSIONS）；内部诊断视图 → Task 6；真实图纸 → Task 7。Phase 3 的 cad_diagnosis_agent 本切片不做（规则版 wall_layer_candidates 即其输入），layout_agent 不做（furniture source=cad 路径已通，ai_supplement 留白合法）。
- **占位符扫描**：Task 3 成文过程中保留了"中间态测试反例 + 最终版"结构（有意为之的执行提示，非 TBD）；Task 5 实现注意明示删除 `if False` 残留分支——两处均已给出可执行指令与最终代码。无 TBD/TODO。
- **类型一致性**：`ToolResult[Path]`（Task 3）与 CLI `ensure_dxf` 消费一致；`CadReport`/`TextNote.position`（Task 4）在 Task 5/6 消费一致；`pair_wall_segments` 返回 `Centerline` 三元组在 geometry/parse 两处一致；`SceneJSON` 字段名与 V2 模块 3、Phase 1 `models/rendering.py` 无冲突（不同模块不同名）。
