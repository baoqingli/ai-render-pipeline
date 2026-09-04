# app/infra/pg.py
"""projects 状态表：生产 PG / 测试 sqlite 同构（sqlalchemy Core，无 ORM）。

复合主键 (id, iteration)；status ∈ {created, running, done, failed}。
upsert 走 delete+insert（方言无关：pg ON CONFLICT 与 sqlite 的写法分歧不值得
为此分叉），created_at 随最近一次状态写入刷新。
"""
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    delete,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_meta = MetaData()
PROJECTS = Table(
    "projects", _meta,
    Column("id", String, primary_key=True),
    Column("iteration", Integer, primary_key=True),
    Column("status", String, nullable=False),
    Column("created_at", DateTime, default=datetime.utcnow),
)


async def init_pg(dsn: str) -> AsyncEngine:
    engine = create_async_engine(dsn)
    async with engine.begin() as conn:
        await conn.run_sync(_meta.create_all)
    return engine


async def upsert_project(engine: AsyncEngine, project_id: str, iteration: int,
                         status: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(delete(PROJECTS)
                           .where(PROJECTS.c.id == project_id,
                                  PROJECTS.c.iteration == iteration))
        await conn.execute(PROJECTS.insert().values(
            id=project_id, iteration=iteration, status=status))


async def incomplete_projects(engine: AsyncEngine) -> list[tuple[str, int]]:
    """恢复扫描输入：status='running' 的 (project_id, iteration) 列表。"""
    async with engine.connect() as conn:
        rows = (await conn.execute(
            select(PROJECTS.c.id, PROJECTS.c.iteration)
            .where(PROJECTS.c.status == "running"))).all()
    return [(r[0], r[1]) for r in rows]
