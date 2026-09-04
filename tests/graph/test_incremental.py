# tests/graph/test_incremental.py
"""spec D6 增量重生成：改描述 → 新 iteration → 白模缓存命中仅渲染重跑。

与 test_pipeline 的 fakes 全替不同：本文件走「真解析 + 真白模工具」路径——
parse_scene/inspect_dxf/build_white_model 均为真实现，仅 monkeypatch
Blender 子进程层（subprocess.run 计数）与 style agent（真实现打真 LLM）。
apartment.dxf 由 gen_fixtures 现场生成（离线确定性语料）。
"""
import json
from pathlib import Path

import pytest

from app.graph.pipeline import GraphDeps, build_pipeline
from app.graph.state import initial_state
from scripts.gen_fixtures import make_apartment_dxf
from tests.graph.fakes import CountingEngine


@pytest.fixture(autouse=True)
def _fake_style_agent(monkeypatch):
    """style 固定 wood：同 test_pipeline 惯例（真实现会打真 LLM）。"""
    from app.agents.style.schemas import StyleAgentOutput
    from app.models.rendering import StyleParams

    monkeypatch.setattr(
        "app.graph.nodes.render_branch.run_style_agent",
        lambda d: StyleAgentOutput(params=StyleParams(style="wood"), fallbacks=[]))


@pytest.fixture()
def incremental_deps(tmp_path, monkeypatch):
    from app.tools.blender import runner as blender_runner

    calls = {"blender": 0}

    def fake_run(cmd, **kw):
        calls["blender"] += 1
        plan = json.loads(Path(cmd[cmd.index("--plan") + 1]).read_text(encoding="utf-8"))
        out = Path(plan["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        for cam in plan["cameras"]:
            for ps in plan["passes"]:
                (out / f"{cam['view_id']}_{ps}.png").write_bytes(b"z" * 6000)
        return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    monkeypatch.setattr(blender_runner.subprocess, "run", fake_run)
    monkeypatch.setattr("app.tools.blender.runner.SCENE_BUILDER", Path("blender/scene_builder.py"))

    from app.engines.registry import ModelInfo

    engine = CountingEngine()
    deps = GraphDeps(engines={"comfy": engine},
                     registry_models=[ModelInfo(model_id="m", engine="comfy")],
                     variants=1, data_dir=tmp_path / "data")
    deps.engine_ref = engine  # type: ignore[attr-defined]  # 测试侧计数手柄（同 fakes 惯例）
    deps.calls = calls        # type: ignore[attr-defined]
    return deps


async def test_second_iteration_skips_blender_reruns_render(tmp_path, incremental_deps):
    from langgraph.checkpoint.memory import InMemorySaver

    dxf = make_apartment_dxf(tmp_path / "apartment.dxf")
    graph = build_pipeline(incremental_deps, checkpointer=InMemorySaver())

    r1 = await graph.ainvoke(initial_state("px", 1, str(dxf), "风格A"),
                             config={"configurable": {"thread_id": "px:1"}})
    assert r1["stage"].value == "finalized"
    assert incremental_deps.calls["blender"] == 1
    assert incremental_deps.engine_ref.submits == len(r1["render_results"])

    r2 = await graph.ainvoke(initial_state("px", 2, str(dxf), "风格B（改描述）"),
                             config={"configurable": {"thread_id": "px:2"}})
    assert r2["stage"].value == "finalized"
    assert incremental_deps.calls["blender"] == 1          # 白模缓存命中，未重跑
    assert incremental_deps.engine_ref.submits == 2 * len(r1["render_results"])  # 渲染重跑
    assert len(r2["render_results"]) == len(r1["render_results"])  # 第二轮任务数不变
