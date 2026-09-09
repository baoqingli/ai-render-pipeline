# app/tools/validation_loop.py
"""校验反馈闭环：渲染 → VLM 对比 → 差异修正 → 重渲，循环直到达标或超限。"""
import asyncio
import base64
import json
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.infra.llm import make_chat_model
from app.models.tooling import ToolError, ToolResult


class ValidationIssue(BaseModel):
    kind: str
    severity: str = "medium"
    description: str
    location: str = ""
    action: str = ""


class ValidationResult(BaseModel):
    score: int = 0
    summary: str = ""
    issues: list[ValidationIssue] = Field(default_factory=list)
    iteration: int = 0


VALIDATION_SYSTEM = (
    "你是建筑图纸质量审查专家。给定两张图：\n"
    "图1 = CAD 平面布置图（真值）\n"
    "图2 = 自动生成的白模俯视图\n\n"
    "逐项对比找出所有不一致。每个差异必须含 type/severity/description/location/action。"
    "type: phantom_wall|missing_wall|wall_offset|missing_furniture|wrong_position|"
    "corridor_split|missing_door|extra_element|other\n"
    "action: 建议修正操作（如'删除走廊多余隔墙'）\n"
    "只输出严格 JSON。"
)


def _b64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


def _extract_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object")
    return json.loads(t[start:end + 1])


def validate_render(cad_png: str, render_png: str, *,
                    model: str | None = None) -> ToolResult[ValidationResult]:
    """VLM 对比 CAD 真值与白模渲染 → 结构化差异报告（含建议修正操作）。"""
    s = get_settings()
    use_model = model or s.vision_model or s.llm_model

    client = make_chat_model(s.model_copy(update={"llm_model": use_model}), temperature=0.0)
    content: list[dict] = []
    for label, png in [("CAD 真值", cad_png), ("白模渲染", render_png)]:
        b64 = _b64(png)
        if "/api/anthropic" in s.llm_base_url:
            content.append({"type": "image",
                            "source": {"type": "base64", "media_type": "image/png",
                                       "data": b64}})
        else:
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{_b64(png)}"}})
        content.append({"type": "text", "text": f"↑ {label}"})
    content.append({"type": "text", "text":
        "图1 = CAD 平面布置图（真值），图2 = 自动生成的白模俯视图。"
        "请逐项对比并输出差异 JSON。关注：幻影墙/缺失墙/走廊连通性/家具位置/门洞。"})

    messages = [SystemMessage(content=VALIDATION_SYSTEM),
                HumanMessage(content=content)]  # type: ignore[arg-type]

    async def _go():
        return await client.ainvoke(messages)

    try:
        resp = asyncio.run(_go())
    except Exception as e:  # noqa: BLE001
        return ToolResult(ok=False, error=ToolError(
            code="VALIDATION_LLM_ERROR", message=str(e)[:300], retryable=True))

    raw = resp.content if isinstance(resp.content, str) else \
        "".join(b.get("text", "") for b in resp.content if isinstance(b, dict))
    try:
        data = _extract_json(raw)
        result = ValidationResult(
            score=data.get("score", 0),
            summary=data.get("summary", ""),
            issues=[ValidationIssue(**i) for i in data.get("issues", [])])
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        return ToolResult(ok=False, error=ToolError(
            code="VALIDATION_PARSE_FAILED", message=str(e)[:200]))
    from app.models.tooling import Metrics
    return ToolResult(ok=True, data=result, metrics=Metrics())
