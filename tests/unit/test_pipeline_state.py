# tests/unit/test_pipeline_state.py
from app.graph.state import initial_state
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
