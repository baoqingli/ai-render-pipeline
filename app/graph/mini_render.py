# app/graph/mini_render.py
import json
import operator
import time
from pathlib import Path
from typing import Annotated, Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send
from typing_extensions import TypedDict

# 两个名字都必须模块级导入：测试 monkeypatch 的是 app.graph.mini_render.run_style_agent
from app.agents.style import StyleAgentOutput, run_style_agent
from app.engines.prompt import assemble_prompt
from app.engines.registry import ModelInfo
from app.models.rendering import RenderResult, RenderTask, StyleParams

DEFAULT_VARIANTS = 2
SEED_BASE = 20260902


class MiniState(TypedDict):
    description: str
    control_dir: str
    variants: int
    report_dir: str
    views: list[dict]
    style: StyleParams
    fallbacks: list[str]
    tasks: list[RenderTask]
    results: Annotated[list[RenderResult], operator.add]
    report_path: str


def discover_views(control_dir: Path | str) -> list[dict]:
    d = Path(control_dir)
    views = []
    for depth in sorted(d.glob("*_depth.png")):
        vid = depth.name.removesuffix("_depth.png")
        lineart, white = d / f"{vid}_lineart.png", d / f"{vid}_white.png"
        if lineart.exists() and white.exists():
            views.append({"view_id": vid, "depth": str(depth),
                          "lineart": str(lineart), "white": str(white)})
    return views


def plan_tasks(style: StyleParams, views: list[dict], models: list[ModelInfo],
               variants: int, seed_base: int) -> list[RenderTask]:
    tasks: list[RenderTask] = []
    for v in views:
        for m in models:
            prompt = assemble_prompt(style, m.prompt_variant)
            for i in range(variants):
                tasks.append(RenderTask(
                    view_id=v["view_id"], variant_id=f"var{i}", model_id=m.model_id,
                    prompt=prompt,
                    control_maps={"depth": v["depth"], "lineart": v["lineart"],
                                  "_white": v["white"], "_out_dir": "experiments/renders"},
                    seed=seed_base + i, params_hash=style.stable_hash()))
    return tasks


def build_mini_graph(engines: dict[str, object], registry_models: list[ModelInfo]) -> CompiledStateGraph:
    async def style_node(state: MiniState) -> dict:
        out: StyleAgentOutput = run_style_agent(state["description"])
        return {"style": out.params, "fallbacks": out.fallbacks}

    async def plan_node(state: MiniState) -> dict:
        views = discover_views(state["control_dir"])
        tasks = plan_tasks(state["style"], views, registry_models,
                           state.get("variants") or DEFAULT_VARIANTS, SEED_BASE)
        return {"views": views, "tasks": tasks}

    def fan_out(state: MiniState):
        info_by_id = {m.model_id: m.model_dump() for m in registry_models}
        return [Send("render", {"task": t.model_dump(),
                                "info": info_by_id[t.model_id],
                                "control_maps": t.control_maps})
                for t in state["tasks"]]

    async def render(item: dict) -> dict:
        task = RenderTask(**item["task"])
        info = ModelInfo(**item["info"])
        # brief 原文为 engine = engines[info.engine]；mypy 因 dict[str, object] 报
        # attr-defined。引擎形态由 ComfyEngine/DirectAPIEngine 各自的测试钉住，
        # 此处按 brief 的 EngineSet 接口（dict[str, object]）放宽为 Any。
        engine: Any = engines[info.engine]
        try:
            if info.engine == "comfy":
                result = await engine.submit(task, info)
            else:
                result = await engine.submit(
                    task, info, white_model_image=Path(task.control_maps["_white"]))
        except Exception as e:  # noqa: BLE001 — fail-soft：引擎违约抛错也记 ok=False，不阻塞扇出
            result = RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                                  model_id=task.model_id, ok=False,
                                  error_code=f"ENGINE_RAISED: {e}")
        return {"results": [result]}

    async def report_node(state: MiniState) -> dict:
        report_dir = Path(state.get("report_dir") or "experiments/phase1")
        report_dir.mkdir(parents=True, exist_ok=True)
        # 0 个视图/任务时 results 通道从未写入（state 里无该键），用 get 兜底以保持 fail-soft
        results: list[RenderResult] = state.get("results") or []
        lines = ["# Phase 1 A/B 渲染报告", "",
                 f"- 风格描述: {state['description']}",
                 f"- StyleParams: `{json.dumps(state['style'].model_dump(), ensure_ascii=False)}`",
                 f"- fallbacks: {state.get('fallbacks') or '无'}", "",
                 "| view | variant | model | ok | latency_ms | cost_usd | image |",
                 "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in results:
            lines.append(f"| {r.view_id} | {r.variant_id} | {r.model_id} | {r.ok} | "
                         f"{r.latency_ms} | {r.cost_usd} | {r.image_path or r.error_code} |")
        ok = sum(1 for r in results if r.ok)
        total_cost = sum(r.cost_usd for r in results)
        lines += ["", f"成功率 {ok}/{len(results)}，API 成本 ${total_cost:.2f}"]
        # 毫秒粒度文件名避免同秒两次运行相互覆盖；results.json 固定名 latest-wins 属有意设计
        path = report_dir / f"report_{int(time.time() * 1000)}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        (report_dir / "results.json").write_text(
            json.dumps([r.model_dump() for r in results], ensure_ascii=False, indent=2),
            encoding="utf-8")
        return {"report_path": str(path)}

    g = StateGraph(MiniState)
    g.add_node("style", style_node)
    g.add_node("plan", plan_node)
    # langgraph 1.2.11 的 add_node 存根只接受「入参类型 == 图 state schema」的节点，
    # 不建模 Send 扇出节点的 payload 入参（item: dict），实测 TypedDict/Any/input_schema
    # 均被拒——唯一干净写法是伪装成 MiniState 或此处的定向 ignore，选择后者（诚实）。
    g.add_node("render", render)  # type: ignore[arg-type]
    g.add_node("report", report_node)
    g.add_edge(START, "style")
    g.add_edge("style", "plan")
    g.add_conditional_edges("plan", fan_out, ["render"])
    g.add_edge("render", "report")
    g.add_edge("report", END)
    return g.compile()
