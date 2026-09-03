# app/agents/style/graph.py
import asyncio
from concurrent.futures import ThreadPoolExecutor

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from typing_extensions import TypedDict

from app.agents.style.schemas import StyleAgentOutput
from app.infra.llm import make_chat_model
from app.models.rendering import StyleParams

SYSTEM_PROMPT = """你是室内设计风格参数提取器。把用户的自然语言描述提取为结构化参数。
只输出 schema 定义的字段；用户没提到或无法判断的字段不要编造，省略即可。
可选值：style=modern_minimal/cream/wood/french/wabi_sabi/light_luxury；
floor=wood_floor/microcement/marble/tile；wall=white/art_paint/wood_veneer/stone；
light=warm/natural/no_main_light/cool；budget=economy/mid/high。"""


class _State(TypedDict):
    description: str
    raw: dict | None
    params: StyleParams | None
    fallbacks: list[str]


def sanitize_params(raw: dict) -> tuple[StyleParams, list[str]]:
    fallbacks: list[str] = []
    clean: dict = {}
    for field in StyleParams.model_fields:
        if field in raw:
            allowed = StyleParams.model_fields[field].annotation
            val = raw[field]
            if not isinstance(val, str) or val not in getattr(allowed, "__args__", ()):
                fallbacks.append(f"{field}={val} -> {StyleParams.model_fields[field].default}")
            else:
                clean[field] = val
    return StyleParams(**clean), fallbacks


def build_style_graph(llm: BaseChatModel):
    extractor = llm.with_structured_output(dict)

    async def extract(state: _State) -> dict:
        try:
            raw = await extractor.ainvoke(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": state["description"]}]
            )
            return {"raw": dict(raw or {})}
        except Exception as e:  # noqa: BLE001 — vLLM 不可用等：全默认兜底，不向上抛
            return {"raw": None, "fallbacks": [f"llm_error: {e}"]}

    async def validate(state: _State) -> dict:
        if state["raw"] is None:
            return {"params": StyleParams()}
        params, fallbacks = sanitize_params(state["raw"])
        return {"params": params, "fallbacks": fallbacks}

    g = StateGraph(_State)
    g.add_node("extract", extract)
    g.add_node("validate", validate)
    g.add_edge(START, "extract")
    g.add_edge("extract", "validate")
    g.add_edge("validate", END)
    return g.compile()


def _invoke_graph(graph: CompiledStateGraph, state: dict) -> dict:
    """同步驱动 async 节点的图（langgraph 1.x 的 sync invoke 不支持 async 节点）。

    已在事件循环内时（如 Task 11 的 async langgraph 节点同步调用本 agent），
    asyncio.run 会报 "cannot be called from a running event loop"，
    因此另起工作线程跑独立事件循环。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(graph.ainvoke(state))
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, graph.ainvoke(state)).result()


def run_style_agent(description: str, llm: BaseChatModel | None = None) -> StyleAgentOutput:
    graph = build_style_graph(llm or make_chat_model())
    final = _invoke_graph(graph, {"description": description, "fallbacks": []})
    return StyleAgentOutput(params=final["params"], fallbacks=final["fallbacks"])
