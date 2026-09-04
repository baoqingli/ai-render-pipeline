# Phase 2 切片 3：主图组装与服务化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已验证的三段能力（CAD 解析 / Blender 白模 / AI 渲染）缝进 LangGraph 主图并服务化——PipelineState 全量状态、条件边与并行分支、AsyncPostgresSaver 持久化与崩溃恢复、graph-runner 队列消费、FastAPI REST+SSE、增量重生成，最终 e2e 跑通"上传→出图→改描述→再出图"产品闭环。

**Architecture:** 节点层是现有工具的薄封装，`GraphDeps` dataclass 是唯一注入点（真实工具 vs 图测试 fakes）；主图为固定 DAG（ingest 并行展开 CAD 链与 style 节点，低置信走规则桩 diagnose，Map/Send 扇出渲染，规则版 QA 收口）；服务层 graph-runner 消费 Valkey 队列执行图并发布进度事件，FastAPI 无状态投递。agent 节点（diagnose/layout/qa）本切片为**规则桩**，Phase 3 换 LLM 子图，节点签名不变。

**Tech Stack:** LangGraph(已有) + langgraph-checkpoint-postgres(AsyncPostgresSaver) + FastAPI/uvicorn + valkey(客户端) + psycopg；无 MinIO（本地数据目录，见约束）。

**Spec:** [agent-platform-design §4 State/§5 主图/§7 工具/§9 API 与拓扑](../../superpowers/specs/2026-09-02-agent-platform-design.md) · [V2 模块 8 增量重生成](../../cad-to-render-pipeline-v2.md)

## Global Constraints

- Python `3.12`，uv 管理；Conventional Commits；trailer `Co-Authored-By: Claude <noreply@anthropic.com>`
- **spec §4 字段名逐字保留**（project_id/iteration/cad_file_key/text_description/reference_image_keys/dxf_key/cad_report/parse_strategy/scene_json/scene_cache_key/views/control_maps/blend_cache_key/style_params/render_tasks/render_results/qa_actions/confidence/fallback_log/errors/stage）
- **agent 节点=规则桩**：diagnose（低置信/proxy → fallback_log 记降级）、layout（家具空 → fallback_log 记"无布置图元"直通）、qa（规则：产物缺失/空文件 → QaAction）——节点函数签名留 Phase 3 替换位
- 图测试用 **InMemorySaver + fake deps**（离线）；PG/Valkey 真服务只在 e2e（Task 8）
- **MinIO 降级为本地目录**（spec §3 偏差声明）：`Settings.data_dir`（默认 `experiments/data`）存上传与产物；MinIO 抽象 Phase 4 替换
- 事件契约：`publish(stage: str, status: str)`；队列消息 `{"project_id","iteration"}`；thread_id = `f"{project_id}:{iteration}"`
- 增量重生成语义（spec D6）：改描述 → 新 iteration 新 thread → 上游产物缓存命中（build_white_model 内容寻址 out_dir）→ 仅渲染重跑
- 错误 fail-soft：节点异常捕获 → errors append + 降级继续；不可恢复（INPUT_INVALID）→ finalize 产出失败报告（spec §10）
- 新依赖仅：`fastapi`、`uvicorn[standard]`、`python-multipart`、`langgraph-checkpoint-postgres`、`psycopg[binary]`、`valkey`
- 测试离线 pristine；`uv run mypy app` 0 错；`uv run ruff check app tests scripts` 全过（钉选集）

---

### Task 1: 依赖与 PipelineState 全量 schema

**Files:**
- Modify: `pyproject.toml`（uv add 六依赖）
- Create: `app/models/pipeline.py`, `app/graph/__init__.py`（已存在，空）、`app/graph/state.py`
- Test: `tests/unit/test_pipeline_state.py`

**Interfaces:**
- Produces:
  - `PipelineStage(str, Enum)`: `created/ingested/converted/inspected/parsed/white_modelled/styled/planned/rendering/qa/finalized/failed`
  - `NodeError(node: str, code: str, message: str)`、`FallbackEvent(stage: str, detail: str)`、`QaAction(view_id: str, variant_id: str, action: str, reason: str)`（action ∈ `pass/flag/missing`）
  - `PipelineState(TypedDict)` spec §4 逐字段（pydantic 对象直存：`cad_report: CadReport | None`、`scene_json: SceneJSON | None`、`style_params: StyleParams | None`、`render_tasks: list[RenderTask]`；`reference_image_keys/render_results/qa_actions/fallback_log/errors` 带 `Annotated[list, operator.add]` reducer；`views: list[str]`；`control_maps: dict[str, dict[str, str]]`；`confidence: dict[str, float]`；`parse_strategy: dict | None`（Phase 3 升位）；标量 `project_id/iteration/cad_file_key/text_description/dxf_key/scene_cache_key/blend_cache_key/stage`）
  - `def initial_state(project_id: str, iteration: int, cad_file_key: str, text_description: str | None) -> PipelineState`（全部默认值填齐，含空容器——图输入唯一构造入口）

- [ ] **Step 1: 加依赖**：`uv add fastapi "uvicorn[standard]" python-multipart langgraph-checkpoint-postgres "psycopg[binary]" valkey`
- [ ] **Step 2: 写失败测试**

```python
# tests/unit/test_pipeline_state.py
from app.graph.state import PipelineState, initial_state
from app.models.pipeline import FallbackEvent, NodeError, PipelineStage, QaAction


def test_initial_state_has_all_spec_fields():
    s = initial_state("p1", 1, "uploads/a.dxf", "温馨原木风")
    for field in ["project_id", "iteration", "cad_file_key", "text_description",
                  "reference_image_keys", "dxf_key", "cad_report", "parse_strategy",
                  "scene_json", "scene_cache_key", "views", "control_maps",
                  "blend_cache_key", "style_params", "render_tasks", "render_results",
                  "qa_actions", "confidence", "fallback_log", "errors", "stage"]:
        assert field in s, field
    assert s["stage"] == PipelineStage.created and s["iteration"] == 1
    assert s["reference_image_keys"] == [] and s["render_results"] == []


def test_models_are_small_and_typed():
    assert NodeError(node="parse", code="X", message="m").node == "parse"
    assert FallbackEvent(stage="parse", detail="d").detail == "d"
    qa = QaAction(view_id="v", variant_id="var0", action="flag", reason="r")
    assert qa.action in ("pass", "flag", "missing")
    assert PipelineStage("finalized") is PipelineStage.finalized
```

- [ ] **Step 3: 跑测确认失败** → FAIL
- [ ] **Step 4: 实现 `app/models/pipeline.py` 与 `app/graph/state.py`**

```python
# app/models/pipeline.py
from enum import Enum

from pydantic import BaseModel


class PipelineStage(str, Enum):
    created = "created"
    ingested = "ingested"
    converted = "converted"
    inspected = "inspected"
    parsed = "parsed"
    white_modelled = "white_modelled"
    styled = "styled"
    planned = "planned"
    rendering = "rendering"
    qa = "qa"
    finalized = "finalized"
    failed = "failed"


class NodeError(BaseModel):
    node: str
    code: str
    message: str


class FallbackEvent(BaseModel):
    stage: str
    detail: str


class QaAction(BaseModel):
    view_id: str
    variant_id: str
    action: str   # pass / flag / missing
    reason: str
```

```python
# app/graph/state.py
import operator
from typing import Annotated, TypedDict

from app.models.cad_report import CadReport
from app.models.pipeline import PipelineStage
from app.models.rendering import RenderResult, RenderTask, StyleParams
from app.models.scene import SceneJSON


class PipelineState(TypedDict):
    # ── 输入 ──
    project_id: str
    iteration: int
    cad_file_key: str
    text_description: str | None
    reference_image_keys: Annotated[list[str], operator.add]

    # ── CAD 分支 ──
    dxf_key: str | None
    cad_report: CadReport | None
    parse_strategy: dict | None          # Phase 3 cad_diagnosis_agent 升位
    scene_json: SceneJSON | None
    scene_cache_key: str

    # ── 白模分支 ──
    views: list[str]
    control_maps: dict[str, dict[str, str]]   # {view_id: {depth,lineart,white: 路径}}
    blend_cache_key: str

    # ── 风格与渲染 ──
    style_params: StyleParams | None
    render_tasks: list[RenderTask]
    render_results: Annotated[list[RenderResult], operator.add]

    # ── 控制与诊断 ──
    qa_actions: Annotated[list, operator.add]
    confidence: dict[str, float]
    fallback_log: Annotated[list, operator.add]
    errors: Annotated[list, operator.add]
    stage: PipelineStage


def initial_state(project_id: str, iteration: int, cad_file_key: str,
                  text_description: str | None) -> PipelineState:
    return PipelineState(
        project_id=project_id, iteration=iteration, cad_file_key=cad_file_key,
        text_description=text_description, reference_image_keys=[],
        dxf_key=None, cad_report=None, parse_strategy=None, scene_json=None,
        scene_cache_key="", views=[], control_maps={}, blend_cache_key="",
        style_params=None, render_tasks=[], render_results=[],
        qa_actions=[], confidence={}, fallback_log=[], errors=[],
        stage=PipelineStage.created,
    )
```

- [ ] **Step 5: 跑测通过 → Commit**

```bash
uv run pytest tests/unit/test_pipeline_state.py -v
git add -A && git commit -m "feat: pipeline state schema per spec section 4"
```

---

### Task 2: CAD 分支节点（ingest/convert/inspect/diagnose 桩/parse/layout 桩）

**Files:**
- Create: `app/graph/nodes/__init__.py`, `app/graph/nodes/cad_branch.py`
- Test: `tests/graph/test_cad_nodes.py`

**Interfaces:**
- Consumes: Task 1 状态；现有工具 `convert_dxf 封装 convert_dwg(path, out_dir) -> ToolResult[Path]`、`inspect_dxf(path) -> CadReport`、`parse_scene(path, report=None) -> ToolResult[SceneJSON]`
- Produces（节点函数签名——Task 4 组装依赖这些精确名字）:
  - `async def ingest_node(state: PipelineState) -> dict`（校验扩展名 .dwg/.dxf；非法 → errors+INPUT_INVALID+stage=failed；通过 → stage=ingested）
  - `async def convert_node(state) -> dict`（dxf_key = 原路径或 convert_dwg 结果；失败 fail-soft errors 记录并 stage=failed——不可恢复走 finalize 由条件边处理）
  - `async def inspect_node(state) -> dict`（cad_report + confidence["parse"]；异常捕获 errors）
  - `async def diagnose_node(state) -> dict`（**规则桩**：proxy>0 或 confidence<0.6 → fallback_log 记降级明细；parse_strategy=None；注释留 Phase 3 替换位）
  - `async def parse_node(state) -> dict`（parse_scene → scene_json/scene_cache_key；低置信 PARSE_LOW_CONFIDENCE → fallback_log）
  - `async def layout_stub_node(state) -> dict`（**规则桩**：scene.furniture 为空 → fallback_log 记 "layout: 无布置图元（Phase 3 layout_agent 补全）"；有则直通）
  - 辅助 `def is_failed(state) -> bool`

- [ ] **Step 1: 写失败测试**

```python
# tests/graph/test_cad_nodes.py
from pathlib import Path

from app.graph.nodes.cad_branch import (convert_node, diagnose_node, ingest_node,
                                        inspect_node, is_failed, layout_stub_node,
                                        parse_node)
from app.graph.state import initial_state


def _state(cad="uploads/a.dxf", **kw):
    return initial_state("p1", 1, cad, "描述") | kw


async def test_ingest_accepts_dxf_and_rejects_other(tmp_path):
    ok = await ingest_node(_state())
    assert ok["stage"].value == "ingested" and ok.get("errors") is None
    bad = await ingest_node(_state(cad="uploads/a.pdf"))
    assert is_failed(_state() | bad) and bad["errors"][0].code == "INPUT_INVALID"


async def test_convert_passthrough_dxf(tmp_path):
    out = await convert_node(_state() | {"stage": None})
    assert out["dxf_key"] == "uploads/a.dxf"


async def test_inspect_and_diagnose_stub(tmp_path, monkeypatch):
    from app.models.cad_report import CadReport

    fake = CadReport(confidence=0.5, proxy_entity_count=3)
    monkeypatch.setattr("app.graph.nodes.cad_branch.inspect_dxf", lambda p: fake)
    out = await inspect_node(_state() | {"dxf_key": "x.dxf"})
    assert out["confidence"]["parse"] == 0.5
    diag = await diagnose_node(_state() | {"cad_report": fake, "confidence": {"parse": 0.5}})
    assert diag["fallback_log"][0].stage == "diagnose"
    assert diag["parse_strategy"] is None                 # 规则桩：策略 None


async def test_parse_node_uses_tool_result(tmp_path, monkeypatch):
    from app.models.scene import SceneJSON
    from app.models.tooling import ToolResult

    scene = SceneJSON()
    tr = ToolResult(ok=True, data=scene, cache_key="k123", error=None)
    monkeypatch.setattr("app.graph.nodes.cad_branch.parse_scene", lambda p, r=None: tr)
    out = await parse_node(_state() | {"dxf_key": "x.dxf"})
    assert out["scene_cache_key"] == "k123" and out["scene_json"] == scene


async def test_layout_stub_records_fallback_when_empty():
    out = await layout_stub_node(_state())
    assert out["fallback_log"][0].stage == "layout"
    from app.models.scene import SceneJSON
    out2 = await layout_stub_node(_state() | {"scene_json": SceneJSON(
        furniture=[__import__("app.models.scene", fromlist=["Furniture"]).Furniture(
            id="f", type="sofa", position=[0, 0], size=[1, 1, 1], source="cad")])})
    assert "fallback_log" not in out2 or not out2.get("fallback_log")
```

- [ ] **Step 2: 跑测确认失败** → FAIL
- [ ] **Step 3: 实现 `app/graph/nodes/cad_branch.py`**

```python
"""CAD 分支节点：现有工具的薄封装。agent 位（diagnose/layout）为规则桩，Phase 3 升级。"""
from pathlib import Path

from app.graph.state import PipelineState
from app.models.pipeline import FallbackEvent, NodeError, PipelineStage
from app.models.tooling import ToolResult
from app.tools.cad.convert import convert_dwg
from app.tools.cad.inspect import inspect_dxf
from app.tools.cad.parse import parse_scene

ALLOWED_EXT = {".dwg", ".dxf"}


def is_failed(state: PipelineState) -> bool:
    return state.get("stage") == PipelineStage.failed


def _fail(node: str, code: str, message: str) -> dict:
    return {"errors": [NodeError(node=node, code=code, message=message)],
            "stage": PipelineStage.failed}


async def ingest_node(state: PipelineState) -> dict:
    ext = Path(state["cad_file_key"]).suffix.lower()
    if ext not in ALLOWED_EXT:
        return _fail("ingest", "INPUT_INVALID",
                     f"不支持的图纸格式 {ext}（仅 .dwg/.dxf）")
    return {"stage": PipelineStage.ingested}


async def convert_node(state: PipelineState) -> dict:
    src = Path(state["cad_file_key"])
    if src.suffix.lower() == ".dxf":
        return {"dxf_key": str(src), "stage": PipelineStage.converted}
    result = await convert_dwg(src, src.parent / "_converted")
    if not result.ok or result.data is None:
        return _fail("convert", result.error.code if result.error else "CONVERT_FAILED",
                     result.error.message if result.error else "unknown")
    return {"dxf_key": str(result.data), "stage": PipelineStage.converted}


async def inspect_node(state: PipelineState) -> dict:
    try:
        report = inspect_dxf(state["dxf_key"])
    except Exception as e:  # noqa: BLE001 —— fail-soft：勘察失败走降级
        return {"errors": [NodeError(node="inspect", code="INSPECT_FAILED", message=str(e))],
                "confidence": {"parse": 0.0}, "stage": PipelineStage.inspected}
    return {"cad_report": report, "confidence": {"parse": report.confidence},
            "stage": PipelineStage.inspected}


async def diagnose_node(state: PipelineState) -> dict:
    """规则桩（Phase 3 → cad_diagnosis_agent 受限 ReAct）：
    低置信/天正 proxy 只记录降级，不做策略推断——parse 已按 report 自适应。"""
    report = state.get("cad_report")
    events = []
    if report is not None:
        if report.proxy_entity_count > 0:
            events.append(FallbackEvent(stage="diagnose",
                                        detail=f"proxy 实体 {report.proxy_entity_count} 个，非天正化内容按默认规则解析"))
        if report.confidence < 0.6:
            events.append(FallbackEvent(stage="diagnose",
                                        detail=f"置信度 {report.confidence} < 0.6，走默认值兜底"))
    return {"parse_strategy": None, "fallback_log": events}


async def parse_node(state: PipelineState) -> dict:
    result = parse_scene(state["dxf_key"], state.get("cad_report"))
    if not result.ok or result.data is None:
        return _fail("parse", result.error.code if result.error else "PARSE_FAILED",
                     result.error.message if result.error else "unknown")
    events = []
    if result.error is not None:   # ok=True 但带 PARSE_LOW_CONFIDENCE 提示
        events.append(FallbackEvent(stage="parse", detail=result.error.message))
    return {"scene_json": result.data, "scene_cache_key": result.cache_key or "",
            "fallback_log": events, "stage": PipelineStage.parsed}


async def layout_stub_node(state: PipelineState) -> dict:
    """规则桩（Phase 3 → layout_agent 生成+校验循环）：无布置仅记录。"""
    scene = state.get("scene_json")
    if scene is not None and not scene.furniture:
        return {"fallback_log": [FallbackEvent(
            stage="layout", detail="无布置图元（Phase 3 layout_agent 补全）")]}
    return {}
```

- [ ] **Step 4: 跑测通过**（必要时按实际断言微调测试中的字段访问——如 `ok.get("errors") is None` 与实现返回 dict 缺键的差异，以实现语义为准修正测试）→ PASS
- [ ] **Step 5: Commit**

```bash
uv run pytest tests/graph/test_cad_nodes.py -v
git add -A && git commit -m "feat: CAD branch graph nodes with rule-stub agents"
```

---

### Task 3: 渲染分支节点（white_model/style/plan/render/qa 规则/finalize）

**Files:**
- Create: `app/graph/nodes/render_branch.py`
- Test: `tests/graph/test_render_nodes.py`

**Interfaces:**
- Consumes: `build_white_model(scene_json_path, out_dir, blender_exe=None) -> ToolResult[Path]`、`run_style_agent(description) -> StyleAgentOutput`、`assemble_prompt(params, variant)`、`StyleParams.stable_hash()`、`ModelInfo/registry`、`ComfyEngine.submit(task, info)`/`DirectAPIEngine.submit(task, info, white_model_image)`、`RenderResult`
- Produces:
  - `async def white_model_node(state) -> dict`（scene 落盘 `data_dir/projects/{pid}/{sha8}.json` → build_white_model(out=experiments/model/{pid}) → glob 控制图 → views/control_maps/blend_cache_key；失败 fail-soft errors+stage=failed）
  - `async def style_node(state) -> dict`（run_style_agent；fallbacks → fallback_log）
  - `async def plan_node(state) -> dict`（views × variants（settings.render_variants 默认 2）× registry_models → render_tasks）
  - `async def render_item(item: dict) -> dict`（Map/Send 载荷执行体：按 engine 分发，**不抛异常**，结果进 render_results）
  - `def fan_out_render(state) -> list[Send]`（构造 Send 载荷：task/info 序列化 dict + white 路径 + out_dir）
  - `async def qa_node(state) -> dict`（**规则版**：每 render_result 分档——`not ok`→missing；`image_path` 为空或文件 <5KB→flag；否则 pass；写 qa_actions；stage 推进）
  - `async def finalize_node(state) -> dict`（汇总 summary.json 至 `data_dir/projects/{pid}/iteration-{n}/summary.json`：stage/errors/fallbacks/结果表/成本合计；stage=finalized；发布事件由 deps.publish 在组装层包（见 Task 4））

- [ ] **Step 1: 写失败测试**

```python
# tests/graph/test_render_nodes.py
from pathlib import Path

from app.engines.registry import ModelInfo
from app.models.rendering import RenderResult
from app.graph.state import initial_state

from app.graph.nodes.render_branch import (fan_out_render, finalize_node,
                                           plan_node, qa_node, render_item,
                                           style_node, white_model_node)

MODELS = [ModelInfo(model_id="sdxl-control-v1", engine="comfy",
                    workflow_template="sdxl-control-v1.json")]


def _state(**kw):
    return initial_state("p1", 1, "a.dxf", "暖原木") | kw


async def test_white_model_node_caches_maps(tmp_path, monkeypatch):
    from app.models.tooling import ToolResult

    out = tmp_path / "model"
    out.mkdir()
    for v in ("view_01", "view_02"):
        for p in ("depth", "lineart", "white"):
            (out / f"{v}_{p}.png").write_bytes(b"x" * 10)
    monkeypatch.setattr("app.graph.nodes.render_branch.build_white_model",
                        lambda s, o: ToolResult(ok=True, data=Path(out), cache_key="bk"))
    st = _state(scene_json=__import__("app.models.scene", fromlist=["SceneJSON"]).SceneJSON())
    res = await white_model_node(st, data_dir=tmp_path)
    assert set(res["views"]) == {"view_01", "view_02"}
    assert res["control_maps"]["view_01"]["depth"].endswith("view_01_depth.png")
    assert res["blend_cache_key"] == "bk"


async def test_style_node_maps_fallbacks(monkeypatch):
    from app.agents.style.schemas import StyleAgentOutput
    from app.models.rendering import StyleParams

    monkeypatch.setattr("app.graph.nodes.render_branch.run_style_agent",
                        lambda d: StyleAgentOutput(params=StyleParams(), fallbacks=["x"]))
    res = await style_node(_state())
    assert res["style_params"] is not None
    assert res["fallback_log"][0].detail == "x"


async def test_plan_node_fans_tasks():
    st = _state(views=["view_01"], style_params=__import__(
        "app.models.rendering", fromlist=["StyleParams"]).StyleParams())
    res = await plan_node(st, registry_models=MODELS, variants=1)
    assert len(res["render_tasks"]) == 1
    assert res["render_tasks"][0].model_id == "sdxl-control-v1"
    assert res["render_tasks"][0].params_hash == st would_be_hash  # 见修正注


async def test_render_item_never_raises():
    class Boom:
        async def submit(self, *a, **k):
            raise RuntimeError("engine down")

    item = {"task": {"view_id": "v", "variant_id": "var0", "model_id": "m",
                     "prompt": {"positive": "p", "negative": ""},
                     "control_maps": {}, "seed": 1, "params_hash": "h"},
            "info": {"model_id": "m", "engine": "comfy"},
            "white": "w.png", "out_dir": "."}
    res = await render_item(item, engines={"comfy": Boom()})
    assert res["render_results"][0].ok is False


async def test_qa_rule_version(tmp_path):
    ok_png = tmp_path / "ok.png"
    ok_png.write_bytes(b"x" * 10000)
    results = [RenderResult(view_id="v", variant_id="var0", model_id="m", ok=True,
                            image_path=str(ok_png)),
               RenderResult(view_id="v", variant_id="var1", model_id="m", ok=True,
                            image_path=None),
               RenderResult(view_id="v", variant_id="var2", model_id="m", ok=False)]
    res = await qa_node(_state(render_results=results))
    acts = {a.variant_id: a.action for a in res["qa_actions"]}
    assert acts == {"var0": "pass", "var1": "flag", "var2": "missing"}


async def test_finalize_writes_summary(tmp_path):
    st = _state(render_results=[RenderResult(view_id="v", variant_id="var0",
                                              model_id="m", ok=True, cost_usd=0.0)],
                stage=None)
    st["stage"] = __import__("app.models.pipeline", fromlist=["PipelineStage"]).PipelineStage.rendering
    res = await finalize_node(st, data_dir=tmp_path)
    summary = tmp_path / "projects" / "p1" / "iteration-1" / "summary.json"
    assert summary.exists() and res["stage"].value == "finalized"
```

（**修正注**：`test_plan_node_fans_tasks` 最后一行 `st would_be_hash` 是成文笔误——实现时写全：

```python
    assert res["render_tasks"][0].params_hash == st["style_params"].stable_hash()
```

且 `plan_node`/`white_model_node`/`finalize_node` 带 keyword-only 依赖参数（`registry_models/variants/data_dir`），测试直接传——Task 4 组装层用 `functools.partial` 注入。）

- [ ] **Step 2: 跑测确认失败** → FAIL
- [ ] **Step 3: 实现 `app/graph/nodes/render_branch.py`**

```python
"""渲染分支节点：白模→风格→规划→Map/Send 渲染→规则 QA→汇总。"""
import json
from pathlib import Path

from langgraph.types import Send

from app.agents.style import run_style_agent
from app.engines.prompt import assemble_prompt
from app.graph.state import PipelineState
from app.models.pipeline import FallbackEvent, NodeError, PipelineStage
from app.models.rendering import RenderResult, RenderTask
from app.models.tooling import ToolResult
from app.tools.blender.runner import build_white_model

PASSES = ("depth", "lineart", "white")


def _fail(node: str, code: str, message: str) -> dict:
    return {"errors": [NodeError(node=node, code=code, message=message)],
            "stage": PipelineStage.failed}


async def white_model_node(state: PipelineState, *, data_dir: Path) -> dict:
    scene = state.get("scene_json")
    if scene is None:
        return _fail("white_model", "NO_SCENE", "scene_json 缺失")
    proj_dir = Path(data_dir) / "projects" / state["project_id"]
    proj_dir.mkdir(parents=True, exist_ok=True)
    import hashlib
    scene_path = proj_dir / f"{hashlib.sha256(scene.model_dump_json().encode()).hexdigest()[:8]}.json"
    scene_path.write_text(scene.model_dump_json(), encoding="utf-8")
    out_dir = proj_dir.parent / "model" / state["project_id"]     # 内容寻址缓存复用
    result = await build_white_model(scene_path, out_dir)
    if not result.ok or result.data is None:
        return _fail("white_model",
                     result.error.code if result.error else "WHITE_MODEL_FAILED",
                     result.error.message if result.error else "unknown")
    views, maps = [], {}
    for png in sorted(result.data.glob("*_depth.png")):
        vid = png.name.removesuffix("_depth.png")
        entries = {p: str(result.data / f"{vid}_{p}.png") for p in PASSES
                   if (result.data / f"{vid}_{p}.png").exists()}
        if len(entries) == len(PASSES):
            views.append(vid)
            maps[vid] = entries
    return {"views": views, "control_maps": maps,
            "blend_cache_key": result.cache_key or "",
            "stage": PipelineStage.white_modelled}


async def style_node(state: PipelineState) -> dict:
    out = run_style_agent(state.get("text_description") or "")
    events = [FallbackEvent(stage="style", detail=f) for f in out.fallbacks]
    return {"style_params": out.params, "fallback_log": events,
            "stage": PipelineStage.styled}


async def plan_node(state: PipelineState, *, registry_models, variants: int) -> dict:
    tasks: list[RenderTask] = []
    style = state["style_params"]
    for vid in state["views"]:
        for m in registry_models:
            prompt = assemble_prompt(style, m.prompt_variant)
            for i in range(variants):
                tasks.append(RenderTask(
                    view_id=vid, variant_id=f"var{i}", model_id=m.model_id,
                    prompt=prompt,
                    control_maps=dict(state["control_maps"][vid]),
                    seed=20260904 + i, params_hash=style.stable_hash()))
    return {"render_tasks": tasks, "stage": PipelineStage.planned}


def fan_out_render(state: PipelineState) -> list[Send]:
    infos = {m.model_id: m.model_dump() for m in fan_out_render.models}  # noqa
    return [Send("render", {"task": t.model_dump(), "info": infos[t.model_id],
                            "white": state["control_maps"][t.view_id].get("white", ""),
                            "out_dir": str(Path(state["control_maps"][t.view_id]["depth"]).parent)})
            for t in state["render_tasks"]]


async def render_item(item: dict, *, engines: dict) -> dict:
    task = RenderTask(**item["task"])
    info = fan_out_render.info_cls(**item["info"])          # ModelInfo 重建
    engine = engines.get(info.engine)
    try:
        if engine is None:
            raise RuntimeError(f"engine missing: {info.engine}")
        if info.engine == "comfy":
            result = await engine.submit(task, info)
        else:
            result = await engine.submit(task, info, white_model_image=Path(item["white"]))
    except Exception as e:  # noqa: BLE001 —— 扇出体绝不抛
        result = RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                              model_id=task.model_id, ok=False, error_code="ENGINE_RAISED",
                              latency_ms=0)
        del e
    return {"render_results": [result]}


async def qa_node(state: PipelineState) -> dict:
    actions = []
    for r in state["render_results"]:
        if not r.ok or r.image_path is None:
            action, reason = "missing", "render failed or no image"
        elif not Path(r.image_path).exists() or Path(r.image_path).stat().st_size < 5120:
            action, reason = "flag", "empty or tiny image"
        else:
            action, reason = "pass", ""
        from app.models.pipeline import QaAction
        actions.append(QaAction(view_id=r.view_id, variant_id=r.variant_id,
                                action=action, reason=reason))
    return {"qa_actions": actions, "stage": PipelineStage.qa}


async def finalize_node(state: PipelineState, *, data_dir: Path) -> dict:
    out = Path(data_dir) / "projects" / state["project_id"] / f"iteration-{state['iteration']}"
    out.mkdir(parents=True, exist_ok=True)
    ok = sum(1 for r in state["render_results"] if r.ok)
    summary = {
        "project_id": state["project_id"], "iteration": state["iteration"],
        "stage": "finalized" if state["stage"] != PipelineStage.failed else "failed",
        "errors": [e.model_dump() for e in state["errors"]],
        "fallbacks": [f.model_dump() for f in state["fallback_log"]],
        "views": state["views"],
        "results": [r.model_dump() for r in state["render_results"]],
        "ok_count": ok, "total": len(state["render_results"]),
        "cost_usd": sum(r.cost_usd for r in state["render_results"]),
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    return {"stage": PipelineStage.finalized}
```

（**实现注意——两处在组装时修正**：① `fan_out_render.models`/`info_cls` 挂类属性是成文时的便捷想象，Task 4 组装时把 `fan_out_render` 改为闭包工厂 `make_fan_out(models: list[ModelInfo])`，`render_item` 的 info 重建直接 `ModelInfo(**item["info"])`（删 info_cls 行）；② `qa_node` 里 `from app.models.pipeline import QaAction` 上移到模块顶部。测试相应改调 `make_fan_out(MODELS)`——本任务测试未直接测 fan_out，无需改。）

- [ ] **Step 4: 跑测通过 → 全量门禁** → PASS
- [ ] **Step 5: Commit**

```bash
uv run pytest tests/graph/test_render_nodes.py -v && uv run mypy app
git add -A && git commit -m "feat: render branch nodes with rule QA and finalize summary"
```

---

### Task 4: 主图组装（条件边/并行/Map-Send）+ 图测试

**Files:**
- Create: `app/graph/pipeline.py`
- Test: `tests/graph/test_pipeline.py`

**Interfaces:**
- Consumes: Task 2/3 全部节点；`GraphDeps`（本任务定义）
- Produces:
  - `@dataclass class GraphDeps`: `convert_dwg=convert_dwg`、`inspect_dxf=inspect_dxf`、`parse_scene=parse_scene`、`build_white_model=build_white_model`、`run_style_agent=run_style_agent`、`engines: dict = field(default_factory=dict)`、`registry_models: list[ModelInfo] = field(default_factory=list)`、`variants: int = 2`、`data_dir: Path = Path("experiments/data")`、`publish: Callable[[str, str], Awaitable[None]] | None = None`
  - `def build_pipeline(deps: GraphDeps, checkpointer=None) -> CompiledStateGraph`（节点经 partial 注入依赖；每节点外层包 `_emit`（stage 变更后 `await deps.publish(stage, "ok"/"error")`，publish 为 None 时跳过）；条件边：ingest 失败→finalize；convert 后 failed→finalize；inspect→低置信(<0.6 或 proxy>0)→diagnose→parse 否则直连 parse；parse 后 furniture 空→layout_stub→white_model 否则直连；white_model 失败→finalize；**并行**：START→ingest；ingest→[convert 链, style_node]（add_edge(ingest, ["convert", "style"]) 并行展开）；join 于 plan_node（plan 需要 views+style，LangGraph 节点在全部前驱完成后执行）；plan→fan_out(render)→render→qa→finalize→END；failed 各点→finalize）
  - 图测试基建：`tests/graph/fakes.py` 提供 `fake_deps(tmp_path)`（全部工具/引擎为确定型 fakes：convert 原样、inspect 固定 CadReport(1.0)、parse 固定 SceneJSON(walls 1)、white_model 写 3×N png、style 固定 wood、engine 记录 submit 次数返回 ok RenderResult）

- [ ] **Step 1: 写失败测试（全路径断言——确定性 DAG 红利）**

```python
# tests/graph/test_pipeline.py
from pathlib import Path

import pytest

from app.graph.pipeline import GraphDeps, build_pipeline
from tests.graph.fakes import fake_deps


async def test_full_happy_path(tmp_path):
    deps = fake_deps(tmp_path)
    graph = build_pipeline(deps)                    # InMemory 默认无 checkpointer
    from app.graph.state import initial_state
    final = await graph.ainvoke(initial_state("p1", 1, "a.dxf", "暖"),
                                config={"configurable": {"thread_id": "p1:1"}})
    assert final["stage"].value == "finalized"
    assert len(final["render_results"]) == deps.registry_models.__len__() * 1 * deps.variants
    assert final["style_params"] is not None and final["scene_json"] is not None
    assert final["views"] and final["qa_actions"]


async def test_low_confidence_routes_through_diagnose(tmp_path):
    deps = fake_deps(tmp_path, confidence=0.4)
    graph = build_pipeline(deps)
    from app.graph.state import initial_state
    final = await graph.ainvoke(initial_state("p2", 1, "a.dxf", "x"),
                                config={"configurable": {"thread_id": "p2:1"}})
    assert any(f.stage == "diagnose" for f in final["fallback_log"])


async def test_input_invalid_short_circuits_to_finalize(tmp_path):
    deps = fake_deps(tmp_path)
    graph = build_pipeline(deps)
    from app.graph.state import initial_state
    final = await graph.ainvoke(initial_state("p3", 1, "a.pdf", "x"),
                                config={"configurable": {"thread_id": "p3:1"}})
    assert final["stage"].value == "finalized"
    assert final["errors"] and final["errors"][0].code == "INPUT_INVALID"
    assert final["render_results"] == []


async def test_events_published_per_stage(tmp_path):
    events = []
    async def pub(stage, status):
        events.append((stage, status))
    deps = fake_deps(tmp_path)
    deps.publish = pub
    graph = build_pipeline(deps)
    from app.graph.state import initial_state
    await graph.ainvoke(initial_state("p4", 1, "a.dxf", "x"),
                        config={"configurable": {"thread_id": "p4:1"}})
    assert ("finalized", "ok") in events and ("ingested", "ok") in events


async def test_checkpointer_roundtrip(tmp_path):
    """InMemorySaver：同 thread 重放取回最终态（持久化语义冒烟）。"""
    from langgraph.checkpoint.memory import InMemorySaver
    deps = fake_deps(tmp_path)
    graph = build_pipeline(deps, checkpointer=InMemorySaver())
    from app.graph.state import initial_state
    cfg = {"configurable": {"thread_id": "p5:1"}}
    await graph.ainvoke(initial_state("p5", 1, "a.dxf", "x"), config=cfg)
    snap = await graph.aget_state(cfg)
    assert snap.values["stage"].value == "finalized"
```

- [ ] **Step 2: 跑测确认失败** → FAIL
- [ ] **Step 3: 实现 `app/graph/pipeline.py`（含 fakes：`tests/graph/fakes.py`）**

```python
# app/graph/pipeline.py
"""主图组装：固定 DAG + 条件边 + CAD/风格并行 + Map/Send 渲染扇出。"""
import asyncio
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Awaitable, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.agents.style import run_style_agent
from app.engines.registry import ModelInfo
from app.graph.nodes.cad_branch import (convert_node, diagnose_node, ingest_node,
                                        inspect_node, is_failed, layout_stub_node,
                                        parse_node)
from app.graph.nodes.render_branch import (finalize_node, plan_node, qa_node,
                                           render_item, style_node, white_model_node)
from app.graph.state import PipelineState
from app.models.pipeline import PipelineStage
from app.models.rendering import RenderTask
from app.tools.blender.runner import build_white_model
from app.tools.cad.convert import convert_dwg
from app.tools.cad.inspect import inspect_dxf
from app.tools.cad.parse import parse_scene


@dataclass
class GraphDeps:
    engines: dict = field(default_factory=dict)
    registry_models: list[ModelInfo] = field(default_factory=list)
    variants: int = 2
    data_dir: Path = field(default_factory=lambda: Path("experiments/data"))
    publish: Callable[[str, str], Awaitable[None]] | None = None
    # 工具注入点（图测试换 fakes；生产默认真实实现）
    inspect_dxf: Callable = inspect_dxf
    parse_scene: Callable = parse_scene


def _wrap(fn, name: str, deps: "GraphDeps"):
    async def inner(state: PipelineState) -> dict:
        out = await fn(state)
        if deps.publish is not None:
            stage = out.get("stage", state.get("stage"))
            status = "error" if stage == PipelineStage.failed else "ok"
            await deps.publish(getattr(stage, "value", str(stage)), status)
        return out
    inner.__name__ = name
    return inner


def make_fan_out(models: list[ModelInfo]):
    infos = {m.model_id: m.model_dump() for m in models}

    def fan_out(state: PipelineState) -> list[Send]:
        items = []
        for t in state["render_tasks"]:
            items.append(Send("render", {
                "task": t.model_dump(), "info": infos[t.model_id],
                "white": state["control_maps"][t.view_id].get("white", ""),
                "out_dir": str(Path(state["control_maps"][t.view_id]["depth"]).parent)}))
        return items
    return fan_out


def build_pipeline(deps: GraphDeps, checkpointer=None):
    g = StateGraph(PipelineState)
    g.add_node("ingest", _wrap(ingest_node, "ingest", deps))
    g.add_node("convert", _wrap(convert_node, "convert", deps))
    g.add_node("inspect", _wrap(inspect_node, "inspect", deps))
    g.add_node("diagnose", _wrap(diagnose_node, "diagnose", deps))
    g.add_node("parse", _wrap(parse_node, "parse", deps))
    g.add_node("layout", _wrap(layout_stub_node, "layout", deps))
    g.add_node("white_model", _wrap(partial(white_model_node, data_dir=deps.data_dir),
                                    "white_model", deps))
    g.add_node("style", _wrap(style_node, "style", deps))
    g.add_node("plan", _wrap(partial(plan_node, registry_models=deps.registry_models,
                                     variants=deps.variants), "plan", deps))
    g.add_node("render", partial(render_item, engines=deps.engines))
    g.add_node("qa", _wrap(qa_node, "qa", deps))
    g.add_node("finalize", _wrap(partial(finalize_node, data_dir=deps.data_dir),
                                 "finalize", deps))

    g.add_edge(START, "ingest")
    g.add_edge("ingest", ["convert", "style"])          # CAD 链 ∥ 风格分支
    g.add_edge("convert", "inspect")

    def after_inspect(state: PipelineState):
        if is_failed(state):
            return ["finalize"]
        rep = state.get("cad_report")
        low = (rep is None or rep.confidence < 0.6 or rep.proxy_entity_count > 0)
        return ["diagnose"] if low else ["parse"]

    g.add_conditional_edges("inspect", after_inspect, ["diagnose", "parse", "finalize"])
    g.add_edge("diagnose", "parse")

    def after_parse(state: PipelineState):
        if is_failed(state):
            return ["finalize"]
        scene = state.get("scene_json")
        return ["layout"] if (scene is None or not scene.furniture) else ["white_model"]

    g.add_conditional_edges("parse", after_parse, ["layout", "white_model", "finalize"])
    g.add_edge("layout", "white_model")

    def after_white(state: PipelineState):
        return ["finalize"] if is_failed(state) else ["plan"]

    g.add_conditional_edges("white_model", after_white, ["plan", "finalize"])
    g.add_edge("plan", make_fan_out(deps.registry_models), ["render"])
    g.add_edge("render", "qa")
    g.add_edge("qa", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
```

（`after_inspect`/`after_parse` 返回 list 是 langgraph 多目标条件边的合法形态；`asyncio` import 备而未用的删掉。）

```python
# tests/graph/fakes.py
"""图测试确定型 fakes：全部工具/引擎可预测，含调用计数。"""
from pathlib import Path

from app.engines.registry import ModelInfo
from app.models.cad_report import CadReport
from app.models.rendering import RenderResult, StyleParams
from app.models.scene import SceneJSON
from app.models.tooling import ToolResult

from app.graph.pipeline import GraphDeps

PASSES = ("depth", "lineart", "white")


class CountingEngine:
    def __init__(self):
        self.submits = 0

    async def submit(self, task, info, **kw):
        self.submits += 1
        out = Path(kw.get("out_dir", ".")) / f"{task.view_id}_{task.variant_id}_{task.model_id}_{task.seed}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"f" * 6000)
        return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                            model_id=task.model_id, ok=True, image_path=str(out),
                            latency_ms=1)


def fake_deps(tmp_path: Path, confidence: float = 1.0, variants: int = 1) -> GraphDeps:
    engine = CountingEngine()
    deps = GraphDeps(
        engines={"comfy": engine, "direct_api": engine},
        registry_models=[ModelInfo(model_id="fake-comfy", engine="comfy",
                                   workflow_template="x.json")],
        variants=variants, data_dir=tmp_path / "data",
    )
    deps.inspect_dxf = lambda p: CadReport(confidence=confidence)
    deps.parse_scene = lambda p, r=None: ToolResult(ok=True, data=SceneJSON(
        walls=[__import__("app.models.scene", fromlist=["Wall"]).Wall(
            id="w1", polygon=[[0, 0], [6000, 0], [6000, 200], [0, 200]])]),
        cache_key="fake-scene-key")

    async def fake_build(scene_path, out_dir, blender_exe=None):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for v in ("view_01",):
            for p in PASSES:
                (out / f"{v}_{p}.png").write_bytes(b"f" * 6000)
        return ToolResult(ok=True, data=out, cache_key="fake-blend-key")

    deps.build_white_model = fake_build
    deps.engine_ref = engine                      # 测试侧取计数
    return deps
```

（**注意**：`GraphDeps` 需补 `build_white_model: Callable = build_white_model` 字段（默认真实现，fakes 覆盖）；`inspect_dxf/parse_scene` 已是字段。`white_model_node`/`inspect_node`/`parse_node` 内部对工具的引用须改为**经 state 不可达——直接模块级引用无法替换**：因此 cad_branch.py 顶部 `from app.tools... import` 的 monkeypatch 目标是模块属性，节点内调用写 `cad_branch.inspect_dxf(...)` 模块内引用即可被 fakes 替换？——不。**最终口径**：`GraphDeps` 持有工具，节点签名改为接收 deps（Task 2/3 的节点已在测试中用 monkeypatch 模块属性通过，保持不动）；`build_pipeline` 组装时用 `partial` 把 deps 的工具注入**新包装**：对 inspect/parse/white_model 三节点，组装层生成闭包节点先 setattr 替换 `cad_branch.inspect_dxf = deps.inspect_dxf` 等——**这是全局可变状态，多图共存会互扰**。改为最干净方案：cad_branch/render_branch 节点函数增加 keyword-only `deps` 参数（`async def inspect_node(state, *, deps)`），内部用 `deps.inspect_dxf`；Task 2/3 测试相应传 `deps=`（monkeypatch 行改为构造轻量 SimpleNamespace）。**执行本任务时同步做此重构并跑全量测试**——测试语义不变，只换注入方式。）

- [ ] **Step 4: 跑测通过 → 全量门禁**（含 Task 2/3 测试回归）→ PASS
- [ ] **Step 5: Commit**

```bash
uv run pytest tests/graph -v && uv run pytest -q && uv run mypy app && uv run ruff check app tests scripts
git add -A && git commit -m "feat: main pipeline graph with parallel branches and map-send fanout"
```

---

### Task 5: PG 持久化 + graph-runner（Valkey 消费/事件/恢复扫描）

**Files:**
- Create: `app/infra/pg.py`, `app/workers/__init__.py`, `app/workers/graph_runner.py`
- Modify: `app/core/config.py`（Settings 加 `pg_dsn: str = "postgresql://arp:arp@localhost:5432/arp"`、`valkey_url: str = "redis://localhost:6379/0"`、`render_variants: int = 2`、`data_dir: str = "experiments/data"`）
- Test: `tests/unit/test_pg_and_runner.py`

**Interfaces:**
- Produces:
  - `app/infra/pg.py`: `PROJECTS` sqlalchemy 表（id/iteration/status/created_at）；`async def init_pg(dsn) -> AsyncEngine`（create_all）；`async def upsert_project(engine, project_id, iteration, status)`；`async def incomplete_projects(engine) -> list[tuple[str, int]]`（status='running'）
  - `app/workers/graph_runner.py`: `async def handle_job(job: dict, *, deps, checkpointer, engine, vclient) -> None`（upsert running → `build_pipeline(deps, checkpointer)` → ainvoke thread `f"{pid}:{iter}"` → upsert done/failed）；`async def make_deps_from_settings(settings) -> GraphDeps`（真工具 + ComfyEngine 装配 + registry_models + valkey publish 闭包）；`async def main()`（init_pg → saver setup → 恢复扫描 incomplete → BLPOP `arp:jobs` 循环）
  - `make_saver(dsn)`：`AsyncPostgresSaver.from_conn_string` 上下文封装（e2e 用；单测不连 PG）
- 测试：handle_job 用 fake deps + InMemorySaver + **fake valkey**（dict-backed pub/sub stub）断言：状态表流转 running→done、事件 published ≥ finalized、job 异常→failed 状态

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_pg_and_runner.py
import json

import pytest

from app.infra.pg import PROJECTS, incomplete_projects, init_pg, upsert_project
from app.workers.graph_runner import handle_job


async def test_projects_table_roundtrip():
    eng = await init_pg("sqlite+aiosqlite:///:memory:")
    await upsert_project(eng, "p1", 1, "running")
    await upsert_project(eng, "p2", 1, "done")
    pend = await incomplete_projects(eng)
    assert pend == [("p1", 1)]


class FakeValkey:
    def __init__(self):
        self.published = []

    async def publish(self, channel, message):
        self.published.append((channel, message))


async def test_handle_job_runs_graph_and_updates_status(tmp_path):
    from langgraph.checkpoint.memory import InMemorySaver
    from tests.graph.fakes import fake_deps
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    from app.infra.pg import _meta
    async with eng.begin() as conn:
        await conn.run_sync(_meta.create_all)
    deps = fake_deps(tmp_path)
    vk = FakeValkey()
    job = {"project_id": "pj", "iteration": 1, "cad_file_key": "a.dxf",
           "text_description": "x"}
    await handle_job(job, deps=deps, checkpointer=InMemorySaver(), engine=eng,
                     vclient=vk)
    pend = await incomplete_projects(eng)
    assert pend == []
    chans = [c for c, _ in vk.published]
    assert any(c.endswith("pj") for c in chans)
```

- [ ] **Step 2: 跑测确认失败** → FAIL
- [ ] **Step 3: 实现 `app/infra/pg.py` 与 `app/workers/graph_runner.py`**

```python
# app/infra/pg.py
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_meta = MetaData()
PROJECTS = Table(
    "projects", _meta,
    Column("id", String, primary_key=True),
    Column("iteration", Integer, primary_key=True),
    Column("status", String, nullable=False),
    Column("created_at", DateTime, default=datetime.utcnow),
)


async def init_pg(dsn: str) -> AsyncEngine:
    engine = create_async_engine(dsn)
    async with engine.begin() as conn:
        await conn.run_sync(_meta.create_all)
    return engine


async def upsert_project(engine: AsyncEngine, project_id: str, iteration: int,
                         status: str) -> None:
    from sqlalchemy import delete
    async with engine.begin() as conn:
        await conn.execute(delete(PROJECTS)
                           .where(PROJECTS.c.id == project_id,
                                  PROJECTS.c.iteration == iteration))
        await conn.execute(PROJECTS.insert().values(
            id=project_id, iteration=iteration, status=status))


async def incomplete_projects(engine: AsyncEngine) -> list[tuple[str, int]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(
            select(PROJECTS.c.id, PROJECTS.c.iteration)
            .where(PROJECTS.c.status == "running"))).all()
    return [(r[0], r[1]) for r in rows]
```

```python
# app/workers/graph_runner.py
"""graph-runner：Valkey 队列消费 + PG 状态 + 检查点恢复（spec §9.2 拓扑）。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import get_settings
from app.graph.pipeline import GraphDeps, build_pipeline
from app.graph.state import initial_state
from app.infra.pg import incomplete_projects, init_pg, upsert_project

JOB_QUEUE = "arp:jobs"
EVENT_PREFIX = "arp:events"


async def handle_job(job: dict, *, deps, checkpointer, engine, vclient) -> None:
    pid, it = job["project_id"], job["iteration"]
    await upsert_project(engine, pid, it, "running")

    async def publish(stage: str, status: str) -> None:
        await vclient.publish(f"{EVENT_PREFIX}:{pid}",
                              json.dumps({"stage": stage, "status": status,
                                          "iteration": it}))

    deps.publish = publish
    graph = build_pipeline(deps, checkpointer=checkpointer)
    try:
        final = await graph.ainvoke(
            initial_state(pid, it, job["cad_file_key"], job.get("text_description")),
            config={"configurable": {"thread_id": f"{pid}:{it}"}})
        status = "done" if final.get("stage") and str(
            final["stage"].value) != "failed" else "failed"
    except Exception:  # noqa: BLE001 —— 作业级兜底：状态留痕，进程不死
        status = "failed"
    await upsert_project(engine, pid, it, status)


async def main() -> None:
    import valkey

    from app.engines.comfy import ComfyEngine
    from app.engines.comfy_client import ComfyClient
    from app.engines.registry import ModelRegistry
    from langgraph.checkpoint.memory import InMemorySaver

    s = get_settings()
    engine = await init_pg(s.pg_dsn)
    vclient = valkey.async_from_url(s.valkey_url)
    reg = ModelRegistry(s.registry_db_url)
    await reg.setup()
    models = await reg.list_enabled()
    comfy = any(m.engine == "comfy" for m in models)
    deps = GraphDeps(
        engines={"comfy": ComfyEngine(client=ComfyClient(s.comfy_url),
                                      template_dir=Path("workflows"),
                                      out_dir=Path(s.workspace_dir) / "renders")}
        if comfy else {},
        registry_models=models, variants=s.render_variants,
        data_dir=Path(s.data_dir))

    saver = InMemorySaver()          # e2e 升级点：AsyncPostgresSaver（见 e2e 手册）
    # 恢复扫描：重启后重投未完成作业
    for pid, it in await incomplete_projects(engine):
        await vclient.rpush(JOB_QUEUE, json.dumps({"project_id": pid, "iteration": it,
                                                   "cad_file_key": "",  # 由 API 侧重投时补
                                                   "text_description": None}))
    while True:
        _, raw = await vclient.blpop(JOB_QUEUE)
        job = json.loads(raw)
        if not job.get("cad_file_key"):
            await upsert_project(engine, job["project_id"], job["iteration"], "failed")
            continue
        await handle_job(job, deps=deps, checkpointer=saver, engine=engine,
                         vclient=vclient)


if __name__ == "__main__":
    asyncio.run(main())
```

（注：恢复扫描重投缺 cad_file_key 的作业标记 failed——完整恢复需 PG 存输入路径，为控制切片规模记为已知简化，Phase 3 补 projects 表输入列。`make_deps_from_settings` 并入 `main()` 内联——测试只测 `handle_job`。）

- [ ] **Step 4: 跑测通过 → 全量门禁** → PASS
- [ ] **Step 5: Commit**

```bash
uv run pytest tests/unit/test_pg_and_runner.py -v && uv run mypy app
git add -A && git commit -m "feat: postgres project state and valkey-driven graph runner"
```

---

### Task 6: FastAPI 服务（REST + SSE + 投递）

**Files:**
- Create: `app/api/__init__.py`, `app/api/app.py`, `app/api/deps.py`
- Test: `tests/unit/test_api.py`

**Interfaces:**
- Consumes: Task 5 `init_pg/upsert_project/JOB_QUEUE`；Settings
- Produces:
  - `app/api/deps.py`: `@dataclass class ApiDeps`: `engine(AsyncEngine)、vclient、data_dir: Path、blpop_rpush: Callable`(默认真 valkey)；`async def make_api_deps(settings) -> ApiDeps`
  - `app/api/app.py`: `def create_app(deps: ApiDeps) -> FastAPI`
    - `POST /api/projects`（multipart: cad_file + description?）→ 存 `data_dir/uploads/{uuid}{ext}` → PG upsert created → rpush job → 202 `{"project_id","iteration":1}`（project_id = uuid4 hex[:12]）
    - `GET /api/projects/{pid}` → `{"status_by_iteration": {...}, "latest": {...}}`（查 PROJECTS + 最新 summary.json 存在性）
    - `GET /api/projects/{pid}/events`（SSE：subscribe `arp:events:{pid}`，`text/event-stream`，10s 心跳注释行，客户端断开即取消）
    - `POST /api/projects/{pid}/regenerate`（json: description?）→ iteration = max+1 → 投递（cad_file_key 取 uploads 里该 pid 首个文件——注册表：PG 存? 简化：`data_dir/uploads/{pid}.*` glob）→ 202
  - 测试（TestClient + fake deps：dict 队列 stub、sqlite engine、FakeValkey pub/sub 用 asyncio 简单桥）：上传→202+队列有消息；regenerate→iteration 2 消息；events→SSE 流式收到一条已发布事件后断开

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_api.py
import asyncio
import json

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.app import create_app
from app.api.deps import ApiDeps
from app.infra.pg import _meta


class StubValkey:
    def __init__(self):
        self.queue = []
        self.subs = []

    async def rpush(self, key, msg):
        self.queue.append((key, json.loads(msg)))

    async def blpop(self, key):
        await asyncio.sleep(0)
        return (key, json.dumps(self.queue.pop(0)[1])) if self.queue else None

    async def publish(self, channel, message):
        for q in self.subs:
            q.put_nowait((channel, message))

    def pubsub(self):
        return StubPubSub(self)


class StubPubSub:
    def __init__(self, owner):
        self.owner = owner

    async def subscribe(self, channel):
        import asyncio as aio
        q = aio.Queue()
        self.owner.subs.append(q)
        self.q = q

    async def get_message(self, ignore_subscribe_messages=True, timeout=None):
        try:
            ch, msg = await asyncio.wait_for(self.q.get(), timeout=timeout or 5)
            return type("M", (), {"channel": ch, "data": msg})()
        except asyncio.TimeoutError:
            return None


def _client(tmp_path):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def setup():
        async with eng.begin() as conn:
            await conn.run_sync(_meta.create_all)
    asyncio.run(setup())
    vk = StubValkey()
    deps = ApiDeps(engine=eng, vclient=vk, data_dir=tmp_path)
    return create_app(deps), vk


def test_upload_creates_project_and_queues_job(tmp_path):
    app, vk = _client(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/projects", files={"cad_file": ("a.dxf", b"DXFBYTES")},
                   data={"description": "暖"})
        assert r.status_code == 202
        pid = r.json()["project_id"]
        assert r.json()["iteration"] == 1
        assert (tmp_path / "uploads" / f"{pid}.dxf").read_bytes() == b"DXFBYTES"
        key, job = vk.queue[0]
        assert key == "arp:jobs" and job["project_id"] == pid
        assert job["text_description"] == "暖"


def test_regenerate_bumps_iteration(tmp_path):
    app, vk = _client(tmp_path)
    with TestClient(app) as c:
        r1 = c.post("/api/projects", files={"cad_file": ("a.dxf", b"x")})
        pid = r1.json()["project_id"]
        r2 = c.post(f"/api/projects/{pid}/regenerate", json={"description": "换个风格"})
        assert r2.status_code == 202 and r2.json()["iteration"] == 2
        assert vk.queue[-1][1]["text_description"] == "换个风格"


def test_events_sse_streams_published(tmp_path):
    app, vk = _client(tmp_path)
    with TestClient(app) as c:
        r1 = c.post("/api/projects", files={"cad_file": ("a.dxf", b"x")})
        pid = r1.json()["project_id"]
        import threading

        def pub():
            asyncio.run(vk.publish(f"arp:events:{pid}", '{"stage":"finalized"}'))

        with c.stream("GET", f"/api/projects/{pid}/events") as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            threading.Timer(0.3, pub).start()
            lines = []
            for line in resp.iter_lines():
                lines.append(line)
                if "finalized" in str(line):
                    break
            assert any("finalized" in str(x) for x in lines)
```

- [ ] **Step 2: 跑测确认失败** → FAIL
- [ ] **Step 3: 实现 `app/api/deps.py` 与 `app/api/app.py`**

```python
# app/api/deps.py
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass
class ApiDeps:
    engine: AsyncEngine
    vclient: object                     # valkey async 客户端（测试注入 stub）
    data_dir: Path


async def make_api_deps(settings) -> ApiDeps:
    import valkey

    from app.infra.pg import init_pg
    engine = await init_pg(settings.pg_dsn)
    return ApiDeps(engine=engine,
                   vclient=valkey.async_from_url(settings.valkey_url),
                   data_dir=Path(settings.data_dir))
```

```python
# app/api/app.py
"""FastAPI 无状态业务接口（spec §9.1 契约）。"""
import asyncio
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import ApiDeps
from app.infra.pg import PROJECTS, upsert_project
from sqlalchemy import select

JOB_QUEUE = "arp:jobs"
EVENT_PREFIX = "arp:events"
ALLOWED_EXT = {".dwg", ".dxf"}


class RegenerateBody(BaseModel):
    description: str | None = None


def create_app(deps: ApiDeps) -> FastAPI:
    app = FastAPI(title="ai-render-pipeline")

    @app.post("/api/projects", status_code=202)
    async def create_project(cad_file: UploadFile, description: str | None = None):
        ext = Path(cad_file.filename or "").suffix.lower()
        if ext not in ALLOWED_EXT:
            raise HTTPException(400, f"不支持的图纸格式 {ext}（仅 .dwg/.dxf）")
        pid = uuid.uuid4().hex[:12]
        updir = Path(deps.data_dir) / "uploads"
        updir.mkdir(parents=True, exist_ok=True)
        (updir / f"{pid}{ext}").write_bytes(await cad_file.read())
        await upsert_project(deps.engine, pid, 1, "queued")
        await deps.vclient.rpush(JOB_QUEUE, json.dumps(
            {"project_id": pid, "iteration": 1,
             "cad_file_key": str(updir / f"{pid}{ext}"),
             "text_description": description}))
        return {"project_id": pid, "iteration": 1}

    @app.get("/api/projects/{pid}")
    async def project_status(pid: str):
        async with deps.engine.connect() as conn:
            rows = (await conn.execute(
                select(PROJECTS.c.iteration, PROJECTS.c.status)
                .where(PROJECTS.c.id == pid))).all()
        if not rows:
            raise HTTPException(404, "project not found")
        latest_iter, latest_status = max(rows, key=lambda r: r[0])
        summary = (Path(deps.data_dir) / "projects" / pid
                   / f"iteration-{latest_iter}" / "summary.json")
        return {"status_by_iteration": {str(i): s for i, s in rows},
                "latest": {"iteration": latest_iter, "status": latest_status,
                           "summary_exists": summary.exists()}}

    @app.post("/api/projects/{pid}/regenerate", status_code=202)
    async def regenerate(pid: str, body: RegenerateBody):
        async with deps.engine.connect() as conn:
            rows = (await conn.execute(
                select(PROJECTS.c.iteration)
                .where(PROJECTS.c.id == pid))).all()
        if not rows:
            raise HTTPException(404, "project not found")
        it = max(r[0] for r in rows) + 1
        uploads = sorted((Path(deps.data_dir) / "uploads").glob(f"{pid}.*"))
        if not uploads:
            raise HTTPException(410, "cad file missing")
        await upsert_project(deps.engine, pid, it, "queued")
        await deps.vclient.rpush(JOB_QUEUE, json.dumps(
            {"project_id": pid, "iteration": it,
             "cad_file_key": str(uploads[0]),
             "text_description": body.description}))
        return {"project_id": pid, "iteration": it}

    @app.get("/api/projects/{pid}/events")
    async def events(pid: str):
        async def gen():
            ps = deps.vclient.pubsub()
            await ps.subscribe(f"{EVENT_PREFIX}:{pid}")
            try:
                while True:
                    msg = await ps.get_message(ignore_subscribe_messages=True,
                                               timeout=10)
                    if msg is not None and msg.data:
                        yield f"data: {msg.data}\n\n"
                    else:
                        yield ": ping\n\n"
            finally:
                await ps.aclose() if hasattr(ps, "aclose") else None

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app
```

- [ ] **Step 4: 跑测通过 → 全量门禁** → PASS（SSE 测试如受 TestClient 流式时序影响，允许把 `threading.Timer` 延时调到 0.5–1.0s 并注释原因——超时即失败不许静默跳过）
- [ ] **Step 5: Commit**

```bash
uv run pytest tests/unit/test_api.py -v && uv run mypy app && uv run ruff check app tests scripts
git add -A && git commit -m "feat: FastAPI service with SSE and queue dispatch"
```

---

### Task 7: 增量重生成验证（真缓存语义图测试）

**Files:**
- Test: `tests/graph/test_incremental.py`

**Interfaces:**
- Consumes: Task 4 `build_pipeline/GraphDeps`；真 `build_white_model`（monkeypatch 其 subprocess 计数）；真 cad 工具（apartment fixture 走真解析）；FakeEngine 渲染
- Produces: 断言 spec D6：iteration 2（改描述）→ **white_model 的 Blender 子进程不重跑**（产物缓存命中）→ render 重跑（engine 计数翻倍）→ 两 thread 终态 finalized

- [ ] **Step 1: 写测试（核心断言直接写在实现前——本任务无新产品代码，是行为锁定）**

```python
# tests/graph/test_incremental.py
"""spec D6 增量重生成：改描述 → 新 iteration → 白模缓存命中仅渲染重跑。"""
import json
from pathlib import Path

import pytest

from app.graph.pipeline import GraphDeps, build_pipeline
from app.graph.state import initial_state
from scripts.gen_fixtures import make_apartment_dxf

from tests.graph.fakes import CountingEngine


@pytest.fixture()
def incremental_deps(tmp_path, monkeypatch):
    from app.tools.blender import runner as blender_runner

    calls = {"blender": 0}

    def fake_run(cmd, **kw):
        calls["blender"] += 1
        plan = json.loads(Path(cmd[cmd.index("--plan") + 1]).read_text(encoding="utf-8"))
        out = Path(plan["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        for cam in plan["cameras"]:
            for ps in plan["passes"]:
                (out / f"{cam['view_id']}_{ps}.png").write_bytes(b"z" * 6000)
        return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    monkeypatch.setattr(blender_runner.subprocess, "run", fake_run)
    monkeypatch.setattr("app.tools.blender.runner.SCENE_BUILDER", Path("blender/scene_builder.py"))

    from app.models.rendering import StyleParams
    from app.agents.style.schemas import StyleAgentOutput

    engine = CountingEngine()
    deps = GraphDeps(engines={"comfy": engine},
                     registry_models=[__import__("app.engines.registry",
                                                 fromlist=["ModelInfo"]).ModelInfo(
                         model_id="m", engine="comfy")],
                     variants=1, data_dir=tmp_path / "data")
    deps.engine_ref = engine
    deps.calls = calls
    return deps


async def test_second_iteration_skips_blender_rethrows_render(tmp_path, incremental_deps):
    from langgraph.checkpoint.memory import InMemorySaver

    dxf = make_apartment_dxf(tmp_path / "apartment.dxf")
    graph = build_pipeline(incremental_deps, checkpointer=InMemorySaver())

    r1 = await graph.ainvoke(initial_state("px", 1, str(dxf), "风格A"),
                             config={"configurable": {"thread_id": "px:1"}})
    assert r1["stage"].value == "finalized"
    assert incremental_deps.calls["blender"] == 1
    assert incremental_deps.engine_ref.submits == len(r1["render_results"])

    r2 = await graph.ainvoke(initial_state("px", 2, str(dxf), "风格B（改描述）"),
                             config={"configurable": {"thread_id": "px:2"}})
    assert r2["stage"].value == "finalized"
    assert incremental_deps.calls["blender"] == 1          # 白模缓存命中，未重跑
    assert incremental_deps.engine_ref.submits == 2 * len(r1["render_results"])  # 渲染重跑
```

- [ ] **Step 2: 跑测**（预期 FAIL 或 PASS 都要解释：若 PASS 说明既有实现已满足 D6——记录证据；若 FAIL 定位（最可能：white_model_node 的 out_dir 每 iteration 项目目录一致但 scene 落盘名含内容 hash 稳定 → 缓存命中应成立）修复在节点层）
- [ ] **Step 3: 按 Step 2 结果修到 PASS（若需改产品代码，遵循节点薄封装原则）→ 全量门禁**
- [ ] **Step 4: Commit**

```bash
uv run pytest tests/graph/test_incremental.py -v && uv run pytest -q
git add -A && git commit -m "test: incremental regeneration skips blender via content cache"
```

---

### Task 8: e2e 服务栈（compose + 运行手册 + 半自动验收）

**Files:**
- Create: `deploy/docker-compose.services.yml`, `scripts/run_api.py`, `scripts/run_runner.py`, `docs/e2e-2026-09.md`

**Interfaces:**
- Produces:
  - compose：`postgres:16`（arp/arp/arp, 5432）+ `valkey/valkey:8`（6379）
  - `scripts/run_api.py`：`uvicorn(app=create_app(make_api_deps(get_settings())), host="0.0.0.0", port=8000)` 的 asyncio 入口
  - `scripts/run_runner.py`：调用 `app.workers.graph_runner.main()` 入口
  - `docs/e2e-2026-09.md` 手册：起 compose → 两进程 → `curl -F cad_file=@fixtures/dxf/apartment.dxf -F description=温馨原木风 http://localhost:8000/api/projects` → SSE `curl -N .../events` → `GET .../projects/{pid}` 到 done → 查 `summary.json`/渲染图 → `POST regenerate` 换描述 → iteration-2 仅渲染重跑（对比 model 目录 mtime 不变、renders 新增）→ 记录结果与偏差
  - `.env` 追加 `ARP_PG_DSN=postgresql+psycopg://arp:arp@localhost:5432/arp`、`ARP_VALKEY_URL=redis://localhost:6379/0`（注：sqlalchemy 用 `postgresql+asyncpg`? 项目无 asyncpg——**用 `postgresql+psycopg`（async 支持）**；langgraph AsyncPostgresSaver 直连 dsn 用 `postgresql://`——两处差异在手册注明；e2e 把 runner 的 InMemorySaver 升级为 AsyncPostgresSaver 的**一行替换说明**写在手册（生产路径），代码默认 InMemory 以保离线测试稳定）
- 测试：`scripts/run_api.py` 可导入性冒烟（`python -c "import scripts.run_api"` 无副作用）+ compose 文件 YAML 语法校验（`yaml.safe_load`）

- [ ] **Step 1-4: 创建文件（内容如上，全部完整给出）→ 门禁 → Commit**

```bash
uv run pytest -q && uv run mypy app && uv run ruff check app tests scripts
git add -A && git commit -m "feat: e2e service stack - compose, api/runner entrypoints, runbook"
```

- [ ] **Step 5: 真机半自动验收（controller 执行，产物记录进 docs/e2e-2026-09.md）**：
  1. `docker compose -f deploy/docker-compose.services.yml up -d`
  2. 起 api 与 runner 两进程
  3. 走手册 curl 全流程（上传/SSE/状态/regenerate）
  4. 验收判定：两 iteration 均 done；iteration-2 白模目录无新写入；renders 新增；GLM 未充值时 style fallback 记录在 summary.fallbacks

**验收标准（切片 3 完成）**：图测试全路径绿（含并行/条件边/Map-Send/fail-soft/增量）；API 测试绿；e2e 手册全流程真机走通。

---

## Self-Review 记录

- **Spec 覆盖**：§4 State 逐字段（T1，parse_strategy 占位 dict 记 Phase 3）；§5 主图全节点+条件边+并行+Map-Send（T2/T3/T4，agent 三桩注释留替换位）；§5.4 持久化（T5 saver 工厂+恢复扫描，PG saver 生产路径在 e2e 手册，离线默认 InMemory——**有意取舍**：saver 连接生命周期与测试隔离，生产启用一行切换）；§9.1 REST 四端点+SSE（T6）；§9.2 拓扑/恢复（T5/T8）；§10 fail-soft（节点层 + handle_job 兜底 + INPUT_INVALID 短路 finalize）；模块 8 增量重生成（T7 真缓存断言）；minIO→本地目录偏差已在 Global Constraints 声明。**缺口（有意）**：SSE 事件粒度 O1 维持 stage 级；DirectAPI 引擎生产装配（GLM/Gemini key 未配时 skip）——e2e 手册注明。
- **占位符扫描**：T3 修正注（`st would_be_hash` 笔误已给全码）与实现注意（fan_out 闭包化、QaAction import 上移）为可执行指令；T4 的注入方式重构说明完整（deps keyword-only 参数化）。无 TBD。
- **类型一致性**：节点签名跨 T2/T3/T4 一致（deps keyword-only 统一口径在 T4 定稿并回归 T2/T3 测试）；`GraphDeps` 字段在 T4 定义、T5/T7 消费一致；`handle_job(job, deps=, checkpointer=, engine=, vclient=)` 在 T5 定义、T8 runner 消费；JOB_QUEUE/EVENT_PREFIX 常量 T5/T6 两处值一致（"arp:jobs"/"arp:events"）；summary 路径 `{data_dir}/projects/{pid}/iteration-{n}/summary.json` 在 T3 finalize/T6 status/T8 手册三处一致。
