# app/graph/pipeline.py
"""主图组装：固定 DAG + 条件边 + CAD/风格并行 + Map/Send 渲染扇出。

拓扑：START→ingest→[convert 链 ∥ style]；convert→inspect→(低置信/proxy→diagnose→
parse | 直连 parse)；parse→(furniture 空→layout→white_model | →white_model)；
white_model→plan→fan_out(render)→render→qa→finalize→END；各失败点→finalize。
plan 的汇合语义由超步屏障保证：style 在第 1 超步完成，plan 最早在第 6 超步执行。
"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.engines.registry import ModelInfo
from app.graph.nodes.cad_branch import (
    convert_node,
    diagnose_node,
    ingest_node,
    inspect_node,
    is_failed,
    layout_stub_node,
    parse_node,
)
from app.graph.nodes.render_branch import (
    finalize_node,
    make_fan_out,
    plan_node,
    qa_node,
    render_item,
    style_node,
    white_model_node,
)
from app.graph.state import PipelineState
from app.models.cad_report import CadReport
from app.models.pipeline import PipelineStage
from app.models.scene import SceneJSON
from app.models.tooling import ToolResult
from app.tools.blender.runner import build_white_model
from app.tools.cad.inspect import inspect_dxf
from app.tools.cad.parse import parse_scene


@dataclass
class GraphDeps:
    """组装依赖包：引擎表/注册模型/变体数/数据目录/事件发布 + 工具注入点。

    工具字段默认真实现；图测试（tests/graph/fakes.py）换确定型 fakes。
    引擎形态契约由 ComfyEngine/DirectAPIEngine 各自测试钉住，此处按 Any 收。
    """

    engines: dict[str, Any] = field(default_factory=dict)
    registry_models: list[ModelInfo] = field(default_factory=list)
    variants: int = 2
    data_dir: Path = field(default_factory=lambda: Path("experiments/data"))
    publish: Callable[[str, str], Awaitable[None]] | None = None
    inspect_dxf: Callable[[str | Path], CadReport] = inspect_dxf
    parse_scene: Callable[[str | Path, CadReport | None], ToolResult[SceneJSON]] = parse_scene
    build_white_model: Callable[..., Awaitable[ToolResult[Path]]] = build_white_model


def _emit(fn: Callable[..., Awaitable[dict]], name: str,
          deps: GraphDeps) -> RunnableLambda[PipelineState, dict]:
    """节点外层：执行后按 stage 变更发布事件（publish 为 None 时零开销直通）。

    返回 RunnableLambda：langgraph 对裸 callable 本就内部包一层，显式包是为了
    mypy 能从泛型解出节点输入类型（裸 Callable[[PipelineState], ...] 会被
    add_node 重载判成 _Node[Never]，Send 载荷节点另见下方定向 ignore）。
    """

    async def wrapped(state: PipelineState) -> dict:
        out = await fn(state)
        if deps.publish is not None:
            stage = out.get("stage", state.get("stage"))
            status = "error" if stage == PipelineStage.failed else "ok"
            await deps.publish(getattr(stage, "value", str(stage)), status)
        return out

    wrapped.__name__ = name
    return RunnableLambda(wrapped)


def build_pipeline(deps: GraphDeps,
                   checkpointer: BaseCheckpointSaver | None = None) -> CompiledStateGraph:
    g = StateGraph(PipelineState)
    g.add_node("ingest", _emit(ingest_node, "ingest", deps))
    g.add_node("convert", _emit(convert_node, "convert", deps))
    g.add_node("inspect", _emit(partial(inspect_node, deps=deps), "inspect", deps))
    g.add_node("diagnose", _emit(diagnose_node, "diagnose", deps))
    g.add_node("parse", _emit(partial(parse_node, deps=deps), "parse", deps))
    g.add_node("layout", _emit(layout_stub_node, "layout", deps))
    g.add_node("white_model", _emit(partial(white_model_node, data_dir=deps.data_dir,
                                            deps=deps), "white_model", deps))
    g.add_node("style", _emit(style_node, "style", deps))
    g.add_node("plan", _emit(partial(plan_node, registry_models=deps.registry_models,
                                     variants=deps.variants), "plan", deps))
    # Send 扇出载荷节点：入参是 item: dict 而非图 state（langgraph 1.2.11 存根
    # 不建模 Send 载荷，定向 ignore——同 mini_render 惯例）
    g.add_node("render", partial(render_item, engines=deps.engines))  # type: ignore[arg-type]
    g.add_node("qa", _emit(qa_node, "qa", deps))
    g.add_node("finalize", _emit(partial(finalize_node, data_dir=deps.data_dir),
                                 "finalize", deps))

    g.add_edge(START, "ingest")

    def after_ingest(state: PipelineState) -> list[str]:
        # INPUT_INVALID 短路：CAD 链与风格分支都不启动
        return ["finalize"] if is_failed(state) else ["convert", "style"]

    g.add_conditional_edges("ingest", after_ingest, ["convert", "style", "finalize"])

    def after_convert(state: PipelineState) -> list[str]:
        return ["finalize"] if is_failed(state) else ["inspect"]

    g.add_conditional_edges("convert", after_convert, ["inspect", "finalize"])

    def after_inspect(state: PipelineState) -> list[str]:
        if is_failed(state):
            return ["finalize"]
        rep = state.get("cad_report")
        low = rep is None or rep.confidence < 0.6 or rep.proxy_entity_count > 0
        return ["diagnose"] if low else ["parse"]

    g.add_conditional_edges("inspect", after_inspect, ["diagnose", "parse", "finalize"])
    g.add_edge("diagnose", "parse")

    def after_parse(state: PipelineState) -> list[str]:
        if is_failed(state):
            return ["finalize"]
        scene = state.get("scene_json")
        return ["layout"] if (scene is None or not scene.furniture) else ["white_model"]

    g.add_conditional_edges("parse", after_parse, ["layout", "white_model", "finalize"])
    g.add_edge("layout", "white_model")

    def after_white_model(state: PipelineState) -> list[str]:
        return ["finalize"] if is_failed(state) else ["plan"]

    g.add_conditional_edges("white_model", after_white_model, ["plan", "finalize"])
    # plan 与 make_fan_out 注入同一 registry_models（infos 查找 KeyError 防线）
    g.add_conditional_edges("plan", make_fan_out(deps.registry_models), ["render"])
    g.add_edge("render", "qa")
    g.add_edge("qa", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
