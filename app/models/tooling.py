# app/models/tooling.py
from pydantic import BaseModel


class Metrics(BaseModel):
    latency_ms: int = 0
    cost_usd: float = 0.0


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False


# PEP 695 原生类型参数（Python 3.12）
class ToolResult[T](BaseModel):
    ok: bool
    data: T | None = None
    error: ToolError | None = None
    cache_key: str | None = None
    cache_hit: bool = False
    metrics: Metrics = Metrics()
