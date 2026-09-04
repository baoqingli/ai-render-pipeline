# tests/unit/test_pipeline_state.py
from app.graph.state import initial_state, keep_furthest_stage
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


def test_keep_furthest_stage_finalized_wins_both_orders():
    # Task 7/8 checkpoint 恢复语义前置加固：finalized 对 failed 的合并必须交换律
    # （原实现 cur=finalized + new=failed 会漏进失败粘性分支返回 failed）。
    assert keep_furthest_stage(PipelineStage.finalized, PipelineStage.failed) \
        is PipelineStage.finalized
    assert keep_furthest_stage(PipelineStage.failed, PipelineStage.finalized) \
        is PipelineStage.finalized


def test_keep_furthest_stage_failed_sticky_and_rank_order():
    # 失败粘性（无 finalized 参与时）与最远进度仍保持既有语义
    assert keep_furthest_stage(PipelineStage.parsed, PipelineStage.failed) \
        is PipelineStage.failed
    assert keep_furthest_stage(PipelineStage.ingested, PipelineStage.parsed) \
        is PipelineStage.parsed
