# app/api/deps.py
"""API 依赖容器：engine（PG）+ valkey client + data_dir（测试注入 stub）。"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass
class ApiDeps:
    engine: AsyncEngine
    vclient: Any                      # valkey async 客户端（测试注入 stub）
    data_dir: Path


async def make_api_deps(settings) -> ApiDeps:
    import valkey.asyncio

    from app.infra.pg import init_pg
    engine = await init_pg(settings.pg_dsn)
    return ApiDeps(engine=engine,
                   vclient=valkey.asyncio.from_url(settings.valkey_url),
                   data_dir=Path(settings.data_dir))
