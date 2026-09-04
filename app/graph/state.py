# app/graph/state.py
import operator
from typing import Annotated, TypedDict

from app.models.cad_report import CadReport
from app.models.pipeline import PipelineStage
from app.models.rendering import RenderResult, RenderTask, StyleParams
from app.models.scene import SceneJSON

_STAGE_RANK = {s: i for i, s in enumerate(PipelineStage)}


def keep_furthest_stage(cur: PipelineStage, new: PipelineStage) -> PipelineStage:
    """stage 通道合并律（Task 4 并行分支引入）：convert ∥ style 同超步各写 stage，
    LastValue 通道会抛 InvalidUpdateError，故改为聚合通道。合并规则（交换律）：
    - finalized 双向压制：finalize 是唯一终态写者（含失败收口），一旦 stage 落
      finalized，后续任何合并（含 failed，Task 7/8 checkpoint 恢复语义）都不回退；
    - 失败粘性：无 finalized 参与时，任一分支 failed 即 failed
      （失败短路不成功分支被覆盖）；
    - 其余取最远进度：枚举定义顺序即管线进度序（created→…→finalized）。
    """
    if PipelineStage.finalized in (cur, new):
        return PipelineStage.finalized
    if PipelineStage.failed in (cur, new):
        return PipelineStage.failed
    return max(cur, new, key=_STAGE_RANK.__getitem__)


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
    # 并行分支多写 stage：聚合通道（合并律见 keep_furthest_stage）
    stage: Annotated[PipelineStage, keep_furthest_stage]


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
