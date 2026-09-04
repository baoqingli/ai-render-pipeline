# tests/unit/test_pg_and_runner.py
"""Task 5：projects 状态表 roundtrip + handle_job 状态流转/事件/异常兜底。

全离线：sqlite in-memory（StaticPool 单连接复用，create_all 的 schema 跨
checkout 存活）+ FakeValkey dict stub，不连真 PG/Valkey。style agent 按图测试
惯例 monkeypatch 模块属性（真 run_style_agent 会打真 LLM——本机 .env 配了
GLM key，离线门禁不容许；同 tests/graph/test_pipeline.py）。
"""
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.infra.pg import PROJECTS, _meta, incomplete_projects, init_pg, upsert_project
from app.workers.graph_runner import handle_job
from tests.graph.fakes import fake_deps


@pytest.fixture(autouse=True)
def _fake_style_agent(monkeypatch):
    """style 固定 wood：style_node 走模块级 run_style_agent（真实现打真 LLM）。"""
    from app.agents.style.schemas import StyleAgentOutput
    from app.models.rendering import StyleParams

    monkeypatch.setattr(
        "app.graph.nodes.render_branch.run_style_agent",
        lambda d: StyleAgentOutput(params=StyleParams(style="wood"), fallbacks=[]))


async def test_projects_table_roundtrip():
    eng = await init_pg("sqlite+aiosqlite:///:memory:")
    await upsert_project(eng, "p1", 1, "running")
    await upsert_project(eng, "p2", 1, "done")
    pend = await incomplete_projects(eng)
    assert pend == [("p1", 1)]


class FakeValkey:
    """dict-backed pub stub：只记 (channel, message)，供事件断言。"""

    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel, message):
        self.published.append((channel, message))


async def _mk_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(_meta.create_all)
    return eng


async def _status_of(eng, pid: str) -> str:
    async with eng.connect() as conn:
        return (await conn.execute(
            select(PROJECTS.c.status).where(PROJECTS.c.id == pid))).scalar_one()


async def test_handle_job_runs_graph_and_updates_status(tmp_path):
    eng = await _mk_engine()
    deps = fake_deps(tmp_path)
    vk = FakeValkey()
    job = {"project_id": "pj", "iteration": 1, "cad_file_key": "a.dxf",
           "text_description": "x"}
    await handle_job(job, deps=deps, checkpointer=InMemorySaver(), engine=eng,
                     vclient=vk)
    assert await incomplete_projects(eng) == []          # running → done
    assert await _status_of(eng, "pj") == "done"
    chans = [c for c, _ in vk.published]
    assert chans and all(c == "arp:events:pj" for c in chans)
    events = [json.loads(m) for _, m in vk.published]
    assert any(e["stage"] == "finalized" for e in events)   # ≥ finalized
    assert all(set(e) == {"stage", "status", "iteration"} for e in events)
    assert all(e["iteration"] == 1 for e in events)


async def test_handle_job_input_invalid_marks_failed(tmp_path):
    """失败运行 finalize 也写 stage=finalized（keep_furthest_stage 对 finalized
    透传，T4 台账既知），状态判定必须以 errors 通道为准——INPUT_INVALID 短路
    落 failed 而非 done。"""
    eng = await _mk_engine()
    deps = fake_deps(tmp_path)
    job = {"project_id": "pj", "iteration": 1, "cad_file_key": "a.pdf",
           "text_description": "x"}
    await handle_job(job, deps=deps, checkpointer=InMemorySaver(), engine=eng,
                     vclient=FakeValkey())
    assert await _status_of(eng, "pj") == "failed"
    assert await incomplete_projects(eng) == []


async def test_handle_job_exception_marks_failed(tmp_path):
    """节点真抛异常（fail-soft 之外）→ ainvoke 上抛 → 作业级兜底 failed，
    handle_job 正常返回而非抛出（进程不死）。"""
    eng = await _mk_engine()
    deps = fake_deps(tmp_path)

    async def boom(scene_path, out_dir, blender_exe=None):
        raise RuntimeError("blender exploded")

    deps.build_white_model = boom
    job = {"project_id": "pj", "iteration": 2, "cad_file_key": "a.dxf",
           "text_description": "x"}
    await handle_job(job, deps=deps, checkpointer=InMemorySaver(), engine=eng,
                     vclient=FakeValkey())
    assert await _status_of(eng, "pj") == "failed"
    assert await incomplete_projects(eng) == []
