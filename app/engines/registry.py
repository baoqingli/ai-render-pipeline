# app/engines/registry.py
from sqlalchemy import Column, Float, Integer, MetaData, String, Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from typing_extensions import Literal

from app.models.tooling import Metrics  # noqa: F401  (保持依赖方向)
from pydantic import BaseModel

SEED = [
    dict(model_id="sdxl-control-v1", engine="comfy", workflow_template="sdxl-control-v1.json",
         prompt_variant="default", max_concurrency=1),
    dict(model_id="nano-banana-2", engine="direct_api", provider="gemini",
         model_name="gemini-3.1-flash-image", prompt_variant="api", max_concurrency=5,
         price_per_image=0.03),
    dict(model_id="gpt-image-2", engine="direct_api", provider="openai",
         model_name="gpt-image-2", prompt_variant="api", max_concurrency=5,
         price_per_image=0.08),
]

_meta = MetaData()
_TABLE = Table(
    "model_registry", _meta,
    Column("model_id", String, primary_key=True),
    Column("engine", String, nullable=False),
    Column("workflow_template", String),
    Column("provider", String),
    Column("model_name", String),
    Column("prompt_variant", String, default="default"),
    Column("max_concurrency", Integer, default=1),
    Column("price_per_image", Float, default=0.0),
    Column("enabled", Integer, default=1),
)


class ModelInfo(BaseModel):
    model_id: str
    engine: Literal["comfy", "direct_api"]
    workflow_template: str | None = None
    provider: str | None = None
    model_name: str | None = None
    prompt_variant: str = "default"
    max_concurrency: int = 1
    price_per_image: float = 0.0
    enabled: bool = True


class ModelRegistry:
    def __init__(self, db_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(db_url)

    async def setup(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(_meta.create_all)
            existing = (await conn.execute(select(_TABLE.c.model_id))).scalars().all()
            for row in SEED:
                if row["model_id"] not in existing:
                    await conn.execute(_TABLE.insert().values(**row))

    async def list_enabled(self) -> list[ModelInfo]:
        async with self._engine.connect() as conn:
            rows = (await conn.execute(select(_TABLE).where(_TABLE.c.enabled == 1))).mappings().all()
        return [ModelInfo(**{k: (bool(v) if k == "enabled" else v) for k, v in r.items()})
                for r in rows]

    async def get(self, model_id: str) -> ModelInfo | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(_TABLE).where(_TABLE.c.model_id == model_id))).mappings().first()
        return None if row is None else ModelInfo(
            **{k: (bool(v) if k == "enabled" else v) for k, v in row.items()})
