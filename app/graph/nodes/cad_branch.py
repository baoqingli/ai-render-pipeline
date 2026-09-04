"""CAD 分支节点：现有工具的薄封装。agent 位（diagnose/layout）为规则桩，Phase 3 升级。"""
from pathlib import Path
from typing import TYPE_CHECKING

from app.graph.state import PipelineState
from app.models.pipeline import FallbackEvent, NodeError, PipelineStage
from app.tools.cad.convert import convert_dwg

if TYPE_CHECKING:  # 仅类型：运行时反向导入会与 pipeline.py 成环
    from app.graph.pipeline import GraphDeps

# 注入口径（Task 4）：inspect/parse 工具经 keyword-only deps 注入（GraphDeps 持有，
# 图测试换 fakes；测试侧可用 SimpleNamespace 鸭子类型——tests 不进 mypy 门禁）；
# convert_dwg 仍为模块级 from-import——测试 monkeypatch 的是
# app.graph.nodes.cad_branch.convert_dwg（同 mini_render 惯例）
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


async def inspect_node(state: PipelineState, *, deps: "GraphDeps") -> dict:
    # mypy 收窄（dxf_key: str | None）：条件边保证 convert 先行，None 即误接线，
    # 按勘察降级口径处理（记 errors、置信 0，不 fail）
    dxf = state["dxf_key"]
    if dxf is None:
        return {"errors": [NodeError(node="inspect", code="DXF_MISSING",
                                     message="dxf_key 未设置（convert 未先行）")],
                "confidence": {"parse": 0.0}, "stage": PipelineStage.inspected}
    try:
        report = deps.inspect_dxf(dxf)
    except Exception as e:  # noqa: BLE001 —— fail-soft：勘察失败走降级
        return {"errors": [NodeError(node="inspect", code="INSPECT_FAILED", message=str(e))],
                "confidence": {"parse": 0.0}, "stage": PipelineStage.inspected}
    return {"cad_report": report, "confidence": {"parse": report.confidence},
            "stage": PipelineStage.inspected}


async def diagnose_node(state: PipelineState) -> dict:
    """规则桩（Phase 3 → cad_diagnosis_agent 受限 ReAct）：
    低置信/天正 proxy 只记录降级，不做策略推断——parse 已按 report 自适应。"""
    report = state.get("cad_report")
    events: list[FallbackEvent] = []
    if report is not None:
        if report.proxy_entity_count > 0:
            events.append(FallbackEvent(
                stage="diagnose",
                detail=f"proxy 实体 {report.proxy_entity_count} 个，非天正化内容按默认规则解析"))
        if report.confidence < 0.6:
            events.append(FallbackEvent(
                stage="diagnose",
                detail=f"置信度 {report.confidence} < 0.6，走默认值兜底"))
    return {"parse_strategy": None, "fallback_log": events}


async def parse_node(state: PipelineState, *, deps: "GraphDeps") -> dict:
    dxf = state["dxf_key"]  # 同 inspect 的 mypy 收窄；parse 失败一律按 fail 处理
    if dxf is None:
        return _fail("parse", "DXF_MISSING", "dxf_key 未设置（convert 未先行）")
    result = deps.parse_scene(dxf, state.get("cad_report"))
    if not result.ok or result.data is None:
        return _fail("parse", result.error.code if result.error else "PARSE_FAILED",
                     result.error.message if result.error else "unknown")
    events: list[FallbackEvent] = []
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
