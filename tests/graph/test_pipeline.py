# tests/graph/test_pipeline.py
"""主图五连：happy path / 低置信 diagnose / INPUT_INVALID 短路 / 事件发布 / 回放。

确定性 DAG 红利：全路径可断言（fakes 见 tests/graph/fakes.py）。
"""
import pytest

from app.graph.pipeline import build_pipeline
from app.graph.state import initial_state
from app.models.rendering import StyleParams
from tests.graph.fakes import fake_deps


@pytest.fixture(autouse=True)
def _fake_style_agent(monkeypatch):
    """style 固定 wood：style_node 走模块级 run_style_agent（真实现会打真 LLM），
    按既定惯例 monkeypatch 模块属性（同 test_render_nodes / mini_render）。"""
    from app.agents.style.schemas import StyleAgentOutput

    monkeypatch.setattr(
        "app.graph.nodes.render_branch.run_style_agent",
        lambda d: StyleAgentOutput(params=StyleParams(style="wood"), fallbacks=[]))


async def test_full_happy_path(tmp_path):
    deps = fake_deps(tmp_path)
    graph = build_pipeline(deps)                    # 无 checkpointer（InMemory 默认）
    final = await graph.ainvoke(initial_state("p1", 1, "a.dxf", "暖"),
                                config={"configurable": {"thread_id": "p1:1"}})
    assert final["stage"].value == "finalized"
    assert len(final["render_results"]) == len(deps.registry_models) * deps.variants
    assert final["style_params"] is not None and final["scene_json"] is not None
    assert final["views"] and final["qa_actions"]
    assert deps.engine_ref.submits == len(deps.registry_models) * deps.variants
    assert deps.calls["inspect"] and deps.calls["parse"] and deps.calls["white_model"]


async def test_low_confidence_routes_through_diagnose(tmp_path):
    deps = fake_deps(tmp_path, confidence=0.4)
    graph = build_pipeline(deps)
    final = await graph.ainvoke(initial_state("p2", 1, "a.dxf", "x"),
                                config={"configurable": {"thread_id": "p2:1"}})
    assert any(f.stage == "diagnose" for f in final["fallback_log"])
    # 高置信对照：happy path 无 diagnose 事件
    deps_hi = fake_deps(tmp_path)
    graph_hi = build_pipeline(deps_hi)
    final_hi = await graph_hi.ainvoke(initial_state("p2b", 1, "a.dxf", "x"),
                                      config={"configurable": {"thread_id": "p2b:1"}})
    assert not any(f.stage == "diagnose" for f in final_hi["fallback_log"])


async def test_input_invalid_short_circuits_to_finalize(tmp_path):
    deps = fake_deps(tmp_path)
    graph = build_pipeline(deps)
    final = await graph.ainvoke(initial_state("p3", 1, "a.pdf", "x"),
                                config={"configurable": {"thread_id": "p3:1"}})
    assert final["stage"].value == "finalized"
    assert final["errors"] and final["errors"][0].code == "INPUT_INVALID"
    assert final["render_results"] == []
    # 短路语义：ingest 失败后 CAD 链工具一个都不跑
    assert deps.calls == {"inspect": [], "parse": [], "white_model": []}


async def test_events_published_per_stage(tmp_path):
    events = []

    async def pub(stage, status):
        events.append((stage, status))

    deps = fake_deps(tmp_path)
    deps.publish = pub
    graph = build_pipeline(deps)
    await graph.ainvoke(initial_state("p4", 1, "a.dxf", "x"),
                        config={"configurable": {"thread_id": "p4:1"}})
    assert ("finalized", "ok") in events and ("ingested", "ok") in events
    assert ("ingested", "error") not in events


async def test_checkpointer_roundtrip(tmp_path):
    """InMemorySaver：同 thread 重放取回最终态（持久化语义冒烟）。"""
    from langgraph.checkpoint.memory import InMemorySaver

    deps = fake_deps(tmp_path)
    graph = build_pipeline(deps, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "p5:1"}}
    await graph.ainvoke(initial_state("p5", 1, "a.dxf", "x"), config=cfg)
    snap = await graph.aget_state(cfg)
    assert snap.values["stage"].value == "finalized"
    assert len(snap.values["render_results"]) == len(deps.registry_models) * deps.variants
