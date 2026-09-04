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
