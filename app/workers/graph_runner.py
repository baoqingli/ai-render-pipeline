# app/workers/graph_runner.py
"""graph-runner：Valkey 队列消费 + PG 状态 + 检查点恢复（spec §9.2 拓扑）。

单进程顺序消费：BLPOP `arp:jobs` → handle_job（upsert running → 图执行 →
done/failed）。重启恢复 = incomplete_projects 扫描重投；重投作业缺输入路径
（cad_file_key=""）直接标 failed——完整恢复需 PG 存输入列，为控制切片规模
记为已知简化（Phase 3 补）。
"""
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# 让 `uv run python app/workers/graph_runner.py` 直接可用：脚本方式执行时
# sys.path[0] 是 app/workers/ 而非 repo 根，须先补根路径再导入 app
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.graph.pipeline import GraphDeps, build_pipeline
from app.graph.state import initial_state
from app.infra.pg import incomplete_projects, init_pg, upsert_project

JOB_QUEUE = "arp:jobs"
EVENT_PREFIX = "arp:events"


async def handle_job(job: dict, *, deps: GraphDeps,
                     checkpointer: BaseCheckpointSaver, engine: AsyncEngine,
                     vclient: Any) -> None:
    """单作业编排：upsert running → 图执行 → upsert done/failed。

    build_pipeline 每次新建（checkpointer 复用传入）——节点在组装期闭包捕获
    deps，复用旧图会锚定旧闭包，防 deps 全局污染。作业级 except 兜底：图内
    fail-soft 之外的异常也只落 failed 状态，消费进程不死。
    """
    pid, it = job["project_id"], job["iteration"]
    await upsert_project(engine, pid, it, "running")

    async def publish(stage: str, status: str) -> None:
        await vclient.publish(f"{EVENT_PREFIX}:{pid}",
                              json.dumps({"stage": stage, "status": status,
                                          "iteration": it}))

    deps.publish = publish
    graph = build_pipeline(deps, checkpointer=checkpointer)
    try:
        final = await graph.ainvoke(
            initial_state(pid, it, job["cad_file_key"], job.get("text_description")),
            config={"configurable": {"thread_id": f"{pid}:{it}"}})
        # 终态判定：失败运行 finalize 也写 stage=finalized（keep_furthest_stage
        # 对 finalized 透传，T4 台账既知事项），stage 不足以判失败——以 errors
        # 通道为准，stage==failed 仅作双保险
        failed = (not final or bool(final.get("errors"))
                  or str(getattr(final.get("stage"), "value", "")) == "failed")
        status = "failed" if failed else "done"
    except Exception:  # noqa: BLE001 —— 作业级兜底：状态留痕，进程不死
        status = "failed"
    await upsert_project(engine, pid, it, status)


async def main() -> None:
    import valkey.asyncio
    from langgraph.checkpoint.memory import InMemorySaver

    from app.engines.comfy import ComfyEngine
    from app.engines.comfy_client import ComfyClient
    from app.engines.registry import ModelRegistry

    s = get_settings()
    engine = await init_pg(s.pg_dsn)
    # valkey 6.1.1 无顶层 async_from_url（brief 笔误，redis-py 旧式 API）——
    # 正确入口是 valkey.asyncio.from_url
    vclient = valkey.asyncio.from_url(s.valkey_url)
    reg = ModelRegistry(s.registry_db_url)
    await reg.setup()
    models = await reg.list_enabled()
    comfy = any(m.engine == "comfy" for m in models)
    deps = GraphDeps(
        engines={"comfy": ComfyEngine(client=ComfyClient(s.comfy_url),
                                      template_dir=Path("workflows"),
                                      out_dir=Path(s.workspace_dir) / "renders")}
        if comfy else {},
        registry_models=models, variants=s.render_variants,
        data_dir=Path(s.data_dir))

    saver = InMemorySaver()  # e2e 升级点：AsyncPostgresSaver（见 e2e 手册）
    # 恢复扫描：重启后重投未完成作业
    for pid, it in await incomplete_projects(engine):
        await vclient.rpush(JOB_QUEUE, json.dumps(
            {"project_id": pid, "iteration": it,
             "cad_file_key": "",  # 由 API 侧重投时补（已知简化，见模块 docstring）
             "text_description": None}))
    while True:
        _, raw = await vclient.blpop(JOB_QUEUE)
        job = json.loads(raw)
        if not job.get("cad_file_key"):
            await upsert_project(engine, job["project_id"], job["iteration"], "failed")
            continue
        await handle_job(job, deps=deps, checkpointer=saver, engine=engine,
                         vclient=vclient)


if __name__ == "__main__":
    asyncio.run(main())
