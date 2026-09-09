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
    "你是建筑图纸质量审查专家。给定两张图："
    "图1 = CAD 平面布置图（真值），图2 = 自动生成的白模俯视图。"
    "逐项对比找出所有不一致。"
    "只输出如下格式的 JSON（不要 markdown 代码块，不要多余文字）：\n"
    '{"score": <0-100 整数>, "summary": "<一句话>", '
    '"issues": [{"kind": "missing_wall|phantom_wall|wall_offset|'
    'missing_furniture|wrong_position|corridor_split|missing_door|'
    'extra_element|other", "severity": "high|medium|low", '
    '"description": "<差异>", "location": "<方位>", '
    '"action": "<修正建议>"}]}'
)


def _b64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


def _extract_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    start = t.find("{")
    if start < 0:
        raise ValueError("no json object")
    # raw_decode 解析首个完整 JSON 值，忽略尾随内容（VLM 常在 JSON 后追加说明）
    obj, _ = json.JSONDecoder().raw_decode(t[start:])
    return obj


def _scene_context_table(scene_json_path: str) -> str:
    """SceneJSON → 结构化清单表（供校验 Agent 按位置核对，不靠外形猜）。"""
    import json as _json
    from pathlib import Path as _Path
    p = _Path(scene_json_path)
    if not p.exists():
        return "（无场景数据）"
    sc = _json.loads(p.read_text(encoding="utf-8"))
    xs = [pt[0] for w in sc.get("walls", []) for pt in w["polygon"]]
    ys = [pt[1] for w in sc.get("walls", []) for pt in w["polygon"]]
    x0, y0 = (min(xs), min(ys)) if xs else (0, 0)
    x1, y1 = (max(xs), max(ys)) if xs else (1, 0)
    W, H = max(x1 - x0, 1), max(y1 - y0, 1)

    def pct(x, y):
        return f"({(x - x0) / W:.2f},{(y - y0) / H:.2f})"

    lines = ["场景实体清单（图面占比坐标，左上原点，与图2渲染同源）：",
             f"墙 {len(sc.get('walls', []))} 段 / 门 {len(sc.get('doors', []))} / "
             f"窗 {len(sc.get('windows', []))} / 家具 {len(sc.get('furniture', []))}："]
    for f in sc.get("furniture", []):
        px, py = f["position"]
        w, d, h = f["size"]
        lines.append(f"  {f['type']} @ {pct(px, py)} 尺寸{w:.0f}x{d:.0f}x{h:.0f}mm")
    for i, d in enumerate(sc.get("doors", [])):
        px, py = d["position"]
        lines.append(f"  door{i} @ {pct(px, py)} 宽{d['width']:.0f}mm")
    return "\n".join(lines[:60])


def validate_render(cad_png: str, render_png: str, *,
                    model: str | None = None,
                    scene_json: str | None = None) -> ToolResult[ValidationResult]:
    """VLM 对比 CAD 真值与白模渲染 → 结构化差异报告（含建议修正操作）。

    scene_json：白模的场景数据（家具/门坐标清单）——注入后校验按位置核对，
    消除"渲染了但因简化外形认不出"的假阴性。
    """
    s = get_settings()
    use_model = model or s.vision_model or s.llm_model

    scene_ctx = _scene_context_table(scene_json) if scene_json else ""

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
    task = ("图1 = CAD 平面布置图（真值），图2 = 自动生成的白模俯视图。"
            "请逐项对比并输出差异 JSON。关注：幻影墙/缺失墙/走廊连通性/家具位置/门洞。")
    if scene_ctx:
        task += ("\n\n图2 为语义配色校验渲染：墙=黑色、地板=白色、家具按类别着色"
                 "（床=红、洁具=绿、柜=蓝、椅=黄、桌/电视=品红）。"
                 "这是有意设计，不要把配色本身当差异。判定家具是否缺失时，"
                 "按下方场景清单的类型和坐标（图面占比）到图2对应位置核对"
                 "是否存在对应颜色块，不要凭外形是否像真实家具判断。\n\n" + scene_ctx)
    content.append({"type": "text", "text": task})

    messages = [SystemMessage(content=VALIDATION_SYSTEM),
                HumanMessage(content=content)]  # type: ignore[arg-type]

    async def _go():
        return await client.ainvoke(messages)

    raw = ""
    for _attempt in range(3):                     # VLM 输出漂移 → 退化检测重试
        try:
            resp = asyncio.run(_go())
        except Exception as e:  # noqa: BLE001
            return ToolResult(ok=False, error=ToolError(
                code="VALIDATION_LLM_ERROR", message=str(e)[:300], retryable=True))
        raw = resp.content if isinstance(resp.content, str) else \
            "".join(b.get("text", "") for b in resp.content
                    if isinstance(b, dict))
        try:
            data = _extract_json(raw)
            if bool(data.get("issues") or data.get("differences")
                   or data.get("diffs")) or isinstance(data.get("score"), int):
                break                              # 有效输出，停止重试
        except (ValueError, json.JSONDecodeError):
            continue
    try:
        data = _extract_json(raw)
        # VLM 键名容错：issues/differences/diffs 均接受；score 缺失时按严重度推导
        raw_issues = (data.get("issues") or data.get("differences")
                      or data.get("diffs") or [])
        issues = [ValidationIssue(**i) for i in raw_issues
                  if isinstance(i, dict)]
        score = data.get("score")
        if not isinstance(score, int):
            penalty = sum({"high": 15, "medium": 5, "low": 2}.get(i.severity, 5)
                          for i in issues)
            score = max(0, 100 - penalty)
        result = ValidationResult(
            score=score, summary=data.get("summary", ""), issues=issues)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        return ToolResult(ok=False, error=ToolError(
            code="VALIDATION_PARSE_FAILED", message=str(e)[:200]))
    from app.models.tooling import Metrics
    return ToolResult(ok=True, data=result, metrics=Metrics())
