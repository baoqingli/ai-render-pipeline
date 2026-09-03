# app/models/tooling.py
from typing import Generic, TypeVar
from pydantic import BaseModel

T = TypeVar("T")


class Metrics(BaseModel):
    latency_ms: int = 0
    cost_usd: float = 0.0


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ToolResult(BaseModel, Generic[T]):
    ok: bool
    data: T | None = None
    error: ToolError | None = None
    cache_key: str | None = None
    cache_hit: bool = False
    metrics: Metrics = Metrics()
