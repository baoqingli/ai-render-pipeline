# app/tools/fidelity.py
"""VLM Fidelity Check：CAD 平面（真值）vs 白模俯视渲染 的布局一致性咨询审查。

架构定位（docs/white-model-fix-plan-2026-09.md §3.3/§7）：确定性优先——
cad_overlay.py 叠加图与 parse_quality 断言是主校验；本工具是 VLM 咨询层，
输出结构化差异报告与一致度分数。只出报告，不拥有几何修改权，结果不反写场景。
"""
import base64
import json
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.infra.llm import make_chat_model
from app.models.tooling import Metrics, ToolError, ToolResult
from app.tools.cache import build_cache_key

PROMPT_VERSION = "1"

SYSTEM_PROMPT = (
    "你是建筑室内设计图纸的质量审查员。将给出两张俯视图："
    "图1 是 CAD 平面图（布局真值），图2 是由该平面自动生成的 3D 白模俯视渲染图。"
    "请逐项对比布局一致性：①墙体（外墙轮廓/内隔墙位置/门窗洞口）"
    "②房间划分（功能区数量与边界）③家具（种类/数量/位置/朝向——只比较位置与大小，"
    "不比较造型细节与材质）。只报告布局层面的差异；忽略渲染风格、线条粗细、颜色、"
    "标注文字、材料纹理。只输出严格 JSON，不要 markdown 代码块，schema："
    '{"score": <0-100 整数，布局一致度>, "summary": "<一句话总结>", "issues": '
    '[{"kind": "missing_furniture|extra_furniture|wrong_position|wall_offset|'
    'missing_wall|room_structure|orientation|other", "description": "<差异描述>", '
    '"location": "<大致方位>", "severity": "low|medium|high"}]}'
)


class FidelityIssue(BaseModel):
    kind: str
    description: str
    location: str = ""
    severity: str = "medium"


class FidelityReport(BaseModel):
    score: int = 0
    summary: str = ""
    issues: list[FidelityIssue] = []
    model: str = ""
    error: str | None = None


def _b64(path: Path) -> str:
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


def _image_blocks(cad_png: Path, render_png: Path) -> list[dict]:
    """按端点协议构造图像块（anthropic 协议 vs OpenAI data-URI）。"""
    s = get_settings()
    if "/api/anthropic" in s.llm_base_url:
        return [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/png", "data": _b64(cad_png)}},
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/png", "data": _b64(render_png)}},
        ]
    return [
        {"type": "image_url",
         "image_url": {"url": f"data:image/png;base64,{_b64(cad_png)}"}},
        {"type": "image_url",
         "image_url": {"url": f"data:image/png;base64,{_b64(render_png)}"}},
    ]


async def check_fidelity(cad_png: str | Path, render_png: str | Path,
                         out_json: str | Path | None = None,
                         *, model: str | None = None) -> ToolResult[FidelityReport]:
    """对比 CAD 平面与白模渲染，返回 VLM 布局一致性报告（咨询层，不反写场景）。"""
    s = get_settings()
    model = model or s.fidelity_model or s.llm_model
    cad, render = Path(cad_png), Path(render_png)
    for p in (cad, render):
        if not p.exists():
            return ToolResult(ok=False, error=ToolError(
                code="INPUT_INVALID", message=f"missing {p}"))
    out = Path(out_json) if out_json else None
    key = build_cache_key("fidelity_check", model,
                          f"{cad.name}:{cad.stat().st_size}",  # noqa: ASYNC240
                          str(cad.stat().st_mtime),  # noqa: ASYNC240
                          f"{render.name}:{render.stat().st_size}",  # noqa: ASYNC240
                          str(render.stat().st_mtime))  # noqa: ASYNC240
    if out is not None and out.exists():
        try:
            cached = FidelityReport.model_validate_json(out.read_text(encoding="utf-8"))
            return ToolResult(ok=True, data=cached, cache_key=key, cache_hit=True)
        except (ValueError, ValidationError):
            pass                                    # 缓存损坏则重算

    client = make_chat_model(s.model_copy(update={"llm_model": model}), temperature=0.0)
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=[*_image_blocks(cad, render),
                              {"type": "text",
                               "text": "图1 为 CAD 平面真值，图2 为白模俯视渲染。"
                                       "请逐项对比布局一致性并只输出 JSON。"}]),
    ]
    try:
        resp = await client.ainvoke(messages)
    except Exception as e:                          # noqa: BLE001 端点错误不阻断主流程
        return ToolResult(ok=False, error=ToolError(
            code="FIDELITY_LLM_ERROR", message=str(e)[:300], retryable=True))
    raw = resp.content if isinstance(resp.content, str) else \
        "".join(b.get("text", "") for b in resp.content if isinstance(b, dict))
    try:
        report = FidelityReport.model_validate(_extract_json(raw))
    except (ValueError, ValidationError) as e:
        return ToolResult(ok=False, error=ToolError(
            code="FIDELITY_PARSE_FAILED", message=f"{e}; raw={raw[:300]}"))
    report.model = model
    report.score = max(0, min(100, report.score))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report.model_dump_json(indent=1), encoding="utf-8")
    return ToolResult(ok=True, data=report, cache_key=key, metrics=Metrics())
