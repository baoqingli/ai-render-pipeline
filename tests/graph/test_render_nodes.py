# tests/graph/test_render_nodes.py
import json
from pathlib import Path
from types import SimpleNamespace

from langgraph.types import Send

from app.agents.style.schemas import StyleAgentOutput
from app.engines.registry import ModelInfo
from app.graph.nodes.render_branch import (
    finalize_node,
    make_fan_out,
    plan_node,
    qa_node,
    render_item,
    style_node,
    white_model_node,
)
from app.graph.state import initial_state
from app.models.pipeline import NodeError, PipelineStage
from app.models.rendering import PromptPair, RenderResult, RenderTask, StyleParams
from app.models.scene import SceneJSON
from app.models.tooling import ToolError, ToolResult

MODELS = [ModelInfo(model_id="sdxl-control-v1", engine="comfy",
                    workflow_template="sdxl-control-v1.json")]


def _state(**kw):
    return initial_state("p1", 1, "a.dxf", "暖原木") | kw


def _deps(**tools):
    """Task 4 注入口径：build_white_model 经 keyword-only deps 注入节点，
    单测用 SimpleNamespace 鸭子类型（tests 不进 mypy 门禁）。"""
    return SimpleNamespace(**tools)


def _item(engine="comfy"):
    return {"task": {"view_id": "v", "variant_id": "var0", "model_id": "m",
                     "prompt": {"positive": "p", "negative": ""},
                     "control_maps": {}, "seed": 1, "params_hash": "h"},
            "info": {"model_id": "m", "engine": engine},
            "white": "w.png", "out_dir": "renders"}


async def test_white_model_node_caches_maps(tmp_path):
    out = tmp_path / "model"
    out.mkdir()
    for v in ("view_01", "view_02"):
        for p in ("depth", "lineart", "white"):
            (out / f"{v}_{p}.png").write_bytes(b"x" * 10)

    # 修（brief 原文 sync lambda）：build_white_model 是 async 工具，假体须可 await
    async def fake_build(scene_path, out_dir, blender_exe=None):
        return ToolResult(ok=True, data=Path(out), cache_key="bk")

    st = _state(scene_json=SceneJSON())
    res = await white_model_node(st, data_dir=tmp_path,
                                 deps=_deps(build_white_model=fake_build))
    assert set(res["views"]) == {"view_01", "view_02"}
    assert res["control_maps"]["view_01"]["depth"].endswith("view_01_depth.png")
    assert res["blend_cache_key"] == "bk"


async def test_white_model_node_writes_content_addressed_scene(tmp_path):
    # 新增（brief 外）：scene 落盘 {data_dir}/projects/{pid}/{sha8}.json、
    # 白模 out={data_dir}/model/{pid}——内容寻址缓存复用约定（增量重生成依赖）
    calls = {}

    async def fake_build(scene_path, out_dir, blender_exe=None):
        calls["scene"], calls["out"] = Path(scene_path), Path(out_dir)
        return ToolResult(ok=True, data=Path(out_dir), cache_key="bk")

    await white_model_node(_state(scene_json=SceneJSON()), data_dir=tmp_path,
                           deps=_deps(build_white_model=fake_build))
    assert calls["out"] == tmp_path / "model" / "p1"
    written = list((tmp_path / "projects" / "p1").glob("*.json"))
    assert len(written) == 1 and calls["scene"] == written[0]


async def test_white_model_node_missing_scene_fails(tmp_path):
    # 新增（brief 外）：scene_json 缺失 → NO_SCENE fail
    def _no_tool(*a, **k):
        raise AssertionError("scene 缺失时不应触达工具")

    res = await white_model_node(_state(), data_dir=tmp_path,
                                 deps=_deps(build_white_model=_no_tool))
    assert res["errors"][0].code == "NO_SCENE"
    assert res["stage"] is PipelineStage.failed


async def test_white_model_node_tool_failure_fails_soft(tmp_path):
    # 新增（brief 外）：工具 ok=False → 透传工具错误码 + stage=failed
    async def fake_build(scene_path, out_dir, blender_exe=None):
        return ToolResult(ok=False, error=ToolError(code="BLENDER_CRASH", message="rc=1"))

    res = await white_model_node(_state(scene_json=SceneJSON()), data_dir=tmp_path,
                                 deps=_deps(build_white_model=fake_build))
    assert res["errors"][0].code == "BLENDER_CRASH"
    assert res["stage"] is PipelineStage.failed


async def test_style_node_maps_fallbacks(monkeypatch):
    monkeypatch.setattr("app.graph.nodes.render_branch.run_style_agent",
                        lambda d: StyleAgentOutput(params=StyleParams(), fallbacks=["x"]))
    res = await style_node(_state())
    assert res["style_params"] is not None
    assert res["fallback_log"][0].detail == "x"


async def test_plan_node_fans_tasks():
    st = _state(views=["view_01"], style_params=StyleParams())
    res = await plan_node(st, registry_models=MODELS, variants=1)
    assert len(res["render_tasks"]) == 1
    assert res["render_tasks"][0].model_id == "sdxl-control-v1"
    assert res["render_tasks"][0].params_hash == st["style_params"].stable_hash()


async def test_plan_node_missing_style_fails():
    # 新增（brief 外）：style_params 缺失 → NO_STYLE fail（None 收窄守卫，防误接线）
    res = await plan_node(_state(views=["view_01"]), registry_models=MODELS, variants=1)
    assert res["errors"][0].code == "NO_STYLE"
    assert res["stage"] is PipelineStage.failed


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


async def test_render_item_missing_engine_never_raises():
    # 新增（brief 外）：引擎不在 engines 表 → ok=False，不抛
    res = await render_item(_item(), engines={})
    assert res["render_results"][0].ok is False
    assert res["render_results"][0].error_code is not None


async def test_render_item_dispatches_direct_api_with_white():
    # 新增（brief 外）：direct_api 引擎收 white_model_image kwarg；
    # out_dir 载荷经 control_maps["_out_dir"] 注入（DirectAPIEngine 落图位）
    captured = {}

    class FakeApi:
        async def submit(self, task, info, *, white_model_image):
            captured["white"] = white_model_image
            captured["out_dir"] = task.control_maps.get("_out_dir")
            return RenderResult(view_id=task.view_id, variant_id=task.variant_id,
                                model_id=task.model_id, ok=True)

    res = await render_item(_item(engine="direct_api"),
                            engines={"direct_api": FakeApi()})
    assert res["render_results"][0].ok is True
    assert captured["white"] == Path("w.png")
    assert captured["out_dir"] == "renders"


def test_make_fan_out_builds_send_payloads():
    # 新增（brief 外）：Send 载荷形状（Task 4 add_conditional_edges 消费）
    st = _state(
        views=["view_01"],
        control_maps={"view_01": {"depth": "/m/view_01_depth.png",
                                  "lineart": "/m/view_01_lineart.png",
                                  "white": "/m/view_01_white.png"}},
        render_tasks=[RenderTask(view_id="view_01", variant_id="var0",
                                 model_id="sdxl-control-v1",
                                 prompt=PromptPair(positive="p"),
                                 control_maps={}, seed=1, params_hash="h")])
    sends = make_fan_out(MODELS)(st)
    assert len(sends) == 1 and isinstance(sends[0], Send)
    assert sends[0].node == "render"
    assert sends[0].arg["task"]["model_id"] == "sdxl-control-v1"
    assert sends[0].arg["info"]["engine"] == "comfy"
    assert sends[0].arg["white"] == "/m/view_01_white.png"
    assert Path(sends[0].arg["out_dir"]).as_posix() == "/m"


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


async def test_qa_flags_tiny_image(tmp_path):
    # 新增（brief 外）：<5KB 已落盘文件与不存在文件 → flag（brief 测试只覆盖 None 图）
    tiny = tmp_path / "tiny.png"
    tiny.write_bytes(b"x" * 100)
    ghost = tmp_path / "ghost.png"
    results = [RenderResult(view_id="v", variant_id="t0", model_id="m", ok=True,
                            image_path=str(tiny)),
               RenderResult(view_id="v", variant_id="t1", model_id="m", ok=True,
                            image_path=str(ghost))]
    res = await qa_node(_state(render_results=results))
    acts = {a.variant_id: a.action for a in res["qa_actions"]}
    assert acts == {"t0": "flag", "t1": "flag"}


async def test_finalize_writes_summary(tmp_path):
    st = _state(render_results=[RenderResult(view_id="v", variant_id="var0",
                                             model_id="m", ok=True, cost_usd=0.0)],
                stage=PipelineStage.rendering)
    res = await finalize_node(st, data_dir=tmp_path)
    summary = tmp_path / "projects" / "p1" / "iteration-1" / "summary.json"
    assert summary.exists() and res["stage"].value == "finalized"


async def test_finalize_failed_stage_records_failed(tmp_path):
    # 新增（brief 外）：failed 进站 → summary 记 "failed" 且透传 errors
    st = _state(errors=[NodeError(node="white_model", code="NO_SCENE", message="x")],
                stage=PipelineStage.failed)
    await finalize_node(st, data_dir=tmp_path)
    data = json.loads((tmp_path / "projects" / "p1" / "iteration-1" /
                       "summary.json").read_text(encoding="utf-8"))
    assert data["stage"] == "failed" and data["errors"][0]["code"] == "NO_SCENE"
