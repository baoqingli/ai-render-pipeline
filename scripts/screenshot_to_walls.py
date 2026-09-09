# scripts/screenshot_to_walls.py
"""从 CAD 截图反推墙线/家具/门洞 → 世界坐标 JSON → 供白模管线使用。

原理：VLM 读截图 → 输出结构化布局（bbox_pct 0-1 坐标）→ 换算世界 mm → SceneJSON。
精度 ±100mm（VLM 限制），比当前 DXF 提取（缺结构墙）好一个量级。
"""
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import get_settings
from app.infra.llm import make_chat_model

SYSTEM_PROMPT = (
    "你是建筑图纸数字化专家。给定一张酒店客房的 CAD 平面布置图截图。"
    "请精确识别以下元素并输出 JSON（bbox 用图面占比坐标 [左,上,右,下]，0-1）：\n\n"
    "1. walls：所有墙段。每段为矩形 [x0,y0,x1,y1]（占比）。"
    "包括外墙（四面）和所有内隔墙。注意墙有厚度（约 100-200mm）。\n"
    "2. doors：所有门洞。每个为 [x0,y0,x1,y1]。\n"
    "3. windows：所有窗/落地窗/阳台开口。每个为 [x0,y0,x1,y1]。\n"
    "4. furniture：所有家具。每个为 {type, bbox, orientation}。\n"
    "   type: bed/nightstand/wardrobe/desk/tv_console/sofa/armchair/table/toilet/"
    "sink/shower/bathtub/plant/luggage_bench\n"
    "5. rooms：功能分区。\n\n"
    "注意：\n"
    "- 坐标系：左上角(0,0)，右下角(1,1)\n"
    "- 管井和排气井在左下角外侧\n"
    "- 阳台在右侧\n"
    "- 图中有文字标注可辅助定位\n"
    "只输出严格 JSON，不要 markdown 代码块。"
)

USER_TASK = (
    "这是酒店客房的 CAD 平面布置图。请识别所有墙体、门洞、窗户和家具，"
    "输出 JSON。每个元素都要给出精确的 bbox_pct。"
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


def extract_layout(png_path: str, *, model: str | None = None) -> dict:
    """VLM 读截图 → 结构化布局 JSON。"""
    s = get_settings()
    use_model = model or s.llm_model
    b64 = _b64(png_path)

    client = make_chat_model(s.model_copy(update={"llm_model": use_model}), temperature=0.0)
    content: list[dict] = [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/png", "data": b64}}
        if "/api/anthropic" in s.llm_base_url else
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64(png_path)}"}},
        {"type": "text", "text": USER_TASK},
    ]
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=content)]  # type: ignore[arg-type]

    async def _invoke():
        return await client.ainvoke(messages)

    import asyncio
    resp = asyncio.run(_invoke())
    raw = resp.content if isinstance(resp.content, str) else \
        "".join(b.get("text", "") for b in resp.content if isinstance(b, dict))
    return _extract_json(raw)


# ── 世界坐标换算 ──────────────────────────────────────────────────────────────
# 截图覆盖的模型空间范围（从 DXF 模型空间实测）
WORLD = (219883.0, -375507.0, 229972.0, -370379.0)


def pct_to_world(bbox_pct: list[float]) -> list[float]:
    x0, y0, x1, y1 = WORLD
    w, h = x1 - x0, y1 - y0
    wx0 = x0 + bbox_pct[0] * w
    wy0 = y0 + (1 - bbox_pct[1]) * h  # 图面 y 向下 → 世界 y 向上
    wx1 = x0 + bbox_pct[2] * w
    wy1 = y0 + (1 - bbox_pct[3]) * h
    return [round(wx0), round(wy0), round(wx1), round(wy1)]


def main() -> None:
    png = "docs/image/ScreenShot_2026-09-07_105237_577.png"
    print("VLM 识别布置图…")
    data = extract_layout(png)
    out = Path("experiments/data/model/p0_verify/vlm_layout.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"written: {out}")
    for k, v in data.items():
        print(f"  {k}: {len(v) if isinstance(v, list) else v}")


if __name__ == "__main__":
    main()
