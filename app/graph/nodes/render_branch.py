"""渲染分支节点：白模→风格→规划→Map/Send 渲染→规则 QA→汇总。"""
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langgraph.types import Send

from app.agents.style import run_style_agent
from app.engines.prompt import assemble_prompt
from app.engines.registry import ModelInfo
from app.graph.state import PipelineState
from app.models.pipeline import FallbackEvent, NodeError, PipelineStage, QaAction
from app.models.rendering import RenderResult, RenderTask

if TYPE_CHECKING:  # 仅类型：运行时反向导入会与 pipeline.py 成环
    from app.graph.pipeline import GraphDeps

# 注入口径（Task 4）：build_white_model 经 keyword-only deps 注入（GraphDeps 持有，
# 图测试换 fakes；测试侧可用 SimpleNamespace 鸭子类型——tests 不进 mypy 门禁）；
# run_style_agent 仍为模块级 from-import——测试 monkeypatch 的是
# app.graph.nodes.render_branch.run_style_agent（同 cad_branch 惯例）

PASSES = ("depth", "lineart", "white")


def _fail(node: str, code: str, message: str) -> dict:
    return {"errors": [NodeError(node=node, code=code, message=message)],
            "stage": PipelineStage.failed}


async def white_model_node(state: PipelineState, *, data_dir: Path,
                           deps: "GraphDeps") -> dict:
    """scene 落盘（内容寻址）→ build_white_model → glob 控制图。"""
    scene = state.get("scene_json")
    if scene is None:
        return _fail("white_model", "NO_SCENE", "scene_json 缺失")
    proj_dir = Path(data_dir) / "projects" / state["project_id"]
    proj_dir.mkdir(parents=True, exist_ok=True)
    scene_text = scene.model_dump_json()
    sha8 = hashlib.sha256(scene_text.encode()).hexdigest()[:8]
    scene_path = proj_dir / f"{sha8}.json"        # 同 scene 同名复用（增量重生成）
    scene_path.write_text(scene_text, encoding="utf-8")
    out_dir = Path(data_dir) / "model" / state["project_id"]  # 内容寻址缓存复用
    result = await deps.build_white_model(scene_path, out_dir)
    if not result.ok or result.data is None:
        return _fail("white_model",
                     result.error.code if result.error else "WHITE_MODEL_FAILED",
                     result.error.message if result.error else "unknown")
    views: list[str] = []
    maps: dict[str, dict[str, str]] = {}
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


async def plan_node(state: PipelineState, *, registry_models: list[ModelInfo],
                    variants: int) -> dict:
    style = state.get("style_params")
    if style is None:
        # mypy 收窄（style_params: StyleParams | None）：正常接线 style 先行，None 即误接线
        return _fail("plan", "NO_STYLE", "style_params 未设置（style 未先行）")
    tasks: list[RenderTask] = []
    maps = state.get("control_maps") or {}
    for vid in state.get("views") or []:
        for m in registry_models:
            prompt = assemble_prompt(style, m.prompt_variant)
            for i in range(variants):
                tasks.append(RenderTask(
                    view_id=vid, variant_id=f"var{i}", model_id=m.model_id,
                    prompt=prompt,
                    control_maps=dict(maps.get(vid) or {}),
                    seed=20260904 + i, params_hash=style.stable_hash()))
    return {"render_tasks": tasks, "stage": PipelineStage.planned}


def make_fan_out(models: list[ModelInfo]) -> Callable[[PipelineState], list[Send]]:
    """Send 扇出工厂（Task 4 add_conditional_edges 消费）：task/info 序列化 dict
    + white 路径 + out_dir（depth 同目录，即白模产物目录）。"""
    infos = {m.model_id: m.model_dump() for m in models}

    def fan_out_render(state: PipelineState) -> list[Send]:
        maps = state.get("control_maps") or {}
        sends: list[Send] = []
        for t in state.get("render_tasks") or []:
            view_maps = maps.get(t.view_id) or {}
            sends.append(Send("render", {
                "task": t.model_dump(), "info": infos[t.model_id],
                "white": view_maps.get("white", ""),
                "out_dir": str(Path(view_maps.get("depth", ".")).parent)}))
        return sends

    return fan_out_render


async def render_item(item: dict, *, engines: dict[str, Any]) -> dict:
    """Map/Send 载荷执行体：按 engine 分发；扇出体绝不抛——引擎缺失/抛错一律
    转 ok=False 的 RenderResult（qa_node 归 missing，不阻塞扇出）。"""
    task = RenderTask(**item["task"])
    info = ModelInfo(**item["info"])
    engine: Any = engines.get(info.engine)
    try:
        if engine is None:
            raise RuntimeError(f"engine missing: {info.engine}")
        # DirectAPIEngine 从 control_maps["_out_dir"] 取落图位（Comfy 忽略之）
        task.control_maps["_out_dir"] = str(item["out_dir"])
        if info.engine == "comfy":
            result = await engine.submit(task, info)
        else:
            result = await engine.submit(task, info,
                                         white_model_image=Path(item["white"]))
    except Exception as e:  # noqa: BLE001 —— 扇出体绝不抛
        result = RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                              model_id=task.model_id, ok=False,
                              error_code=f"ENGINE_RAISED: {e}", latency_ms=0)
    return {"render_results": [result]}


async def qa_node(state: PipelineState) -> dict:
    """规则版 QA（Phase 3 → qa_agent 替换位）：not ok → missing；
    无图或文件 <5KB → flag；否则 pass。"""
    actions: list[QaAction] = []
    for r in state.get("render_results") or []:
        if not r.ok:
            action, reason = "missing", "render failed or no image"
        elif (r.image_path is None or not Path(r.image_path).exists()  # noqa: ASYNC240
              or Path(r.image_path).stat().st_size < 5120):  # noqa: ASYNC240
            action, reason = "flag", "empty or tiny image"
        else:
            action, reason = "pass", ""
        actions.append(QaAction(view_id=r.view_id, variant_id=r.variant_id,
                                action=action, reason=reason))
    return {"qa_actions": actions, "stage": PipelineStage.qa}


async def finalize_node(state: PipelineState, *, data_dir: Path) -> dict:
    out = Path(data_dir) / "projects" / state["project_id"] / f"iteration-{state['iteration']}"
    out.mkdir(parents=True, exist_ok=True)
    results = state.get("render_results") or []
    ok = sum(1 for r in results if r.ok)
    summary = {
        "project_id": state["project_id"], "iteration": state["iteration"],
        "stage": "finalized" if state["stage"] != PipelineStage.failed else "failed",
        "errors": [e.model_dump() for e in state.get("errors") or []],
        "fallbacks": [f.model_dump() for f in state.get("fallback_log") or []],
        "views": state.get("views") or [],
        "results": [r.model_dump() for r in results],
        "ok_count": ok, "total": len(results),
        "cost_usd": sum(r.cost_usd for r in results),
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"stage": PipelineStage.finalized}
