# app/agents/vision/layout_vlm.py
"""VLM 布置图识别：从布置图截图中提取墙体/门/窗/家具的精确位置。

流程：截图 → VLM 识别元素（bbox_pct + 类型/朝向）→ 世界坐标换算
→ 在世界坐标区域内搜索 DXF 精确实体 → 精确坐标替换。
"""
import asyncio
import base64
import json
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import get_settings
from app.infra.llm import make_chat_model
from app.models.tooling import ToolError, ToolResult

USER_TASK = (
    "这是酒店客房的 CAD 平面布置图。"
    "请识别所有墙体、门洞、窗户和家具，输出 JSON。"
)


SYSTEM_PROMPT = (
    "你是建筑图纸数字化专家。给定一张酒店客房 CAD 平面布置图。"
    "请精确识别以下元素并输出 JSON。所有 bbox 用图面占比坐标 [左,上,右,下]（0-1，"
    "左上角为原点）。尽可能精确。\n\n"
    "1. walls：所有墙段。每段为 [x0,y0,x1,y1]。"
    "包括外墙（四面）和所有内隔墙。墙有厚度。\n"
    "2. doors：所有门洞。每个为 [x0,y0,x1,y1]，type 为 entry/bathroom/balcony。\n"
    "3. windows：所有窗/落地窗/阳台开口。每个为 [x0,y0,x1,y1]。\n"
    "4. furniture：所有家具。每个为 {type, bbox, orientation}。\n"
    "   type: bed/nightstand/wardrobe/desk/tv_console/sofa/armchair/table/toilet/"
    "sink/shower/bathtub/plant/luggage_bench\n"
    "5. rooms：功能分区。每个为 {name, bbox}。\n\n"
    "注意：\n"
    "- 坐标系：左上角(0,0)，右下角(1,1)\n"
    "- 墙体是最重要的元素，请精确标注每段墙的位置和范围\n"
    "- 管井/排气井在左下角\n"
    "- 阳台在右侧\n"
    "- 只输出严格 JSON"
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
        raise ValueError("no json object in response")
    return json.loads(t[start:end + 1])


def extract_layout_from_screenshot(
    png_path: str,
    *, model: str | None = None,
) -> ToolResult[dict]:
    """VLM 读布置图截图 → 结构化布局 JSON（墙体/门/窗/家具/分区，bbox_pct 坐标）。"""
    s = get_settings()
    use_model = model or s.vision_model or s.llm_model
    b64 = _b64(png_path)

    client = make_chat_model(s.model_copy(update={"llm_model": use_model}), temperature=0.0)
    content: list[dict] = [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/png", "data": b64}}
        if "/api/anthropic" in s.llm_base_url else
        {"type": "image_url",
         "image_url": {"url": f"data:image/png;base64,{_b64(png_path)}"}},
        {"type": "text", "text": USER_TASK},
    ]
    messages = [SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=content)]  # type: ignore[arg-type]

    async def _invoke():
        return await client.ainvoke(messages)

    try:
        resp = asyncio.run(_invoke())
    except Exception as e:  # noqa: BLE001
        return ToolResult(ok=False, error=ToolError(
            code="VISION_LLM_ERROR", message=str(e)[:300], retryable=True))

    raw = resp.content if isinstance(resp.content, str) else \
        "".join(b.get("text", "") for b in resp.content if isinstance(b, dict))
    try:
        data = _extract_json(raw)
    except (ValueError, json.JSONDecodeError) as e:
        return ToolResult(ok=False, error=ToolError(
            code="VISION_PARSE_FAILED", message=f"{e}; raw={raw[:300]}"))

    return ToolResult(ok=True, data=data)


from app.models.tooling import ToolResult  # noqa: E402
