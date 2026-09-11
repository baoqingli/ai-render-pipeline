"""用视觉模型核对 CAD 布局与 AI 生成的平面渲染图。"""
import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

import httpx

from app.models.vision import ElementRegistry

DEFAULT_MODEL = "qwen/qwen3.8-flash"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

_SYSTEM = (
    "你是严格的室内 CAD 平面图验收专家。图1是 CAD 布局真值，图2是 AI 生成结果。"
    "两图均为俯视图，必须按相同像素方位逐区域比较，不得用整体风格相似替代布局核对。"
    "重点检查房间功能、墙体分区、床和卫浴洁具的位置与数量，以及生成图中的幻觉物品。"
    "若关键要求中的卫生间被生成为卧室、出现床、缺少淋浴功能，必须判为 wrong 或 missing。"
    "评分权重：结构35、房间功能35、家具与洁具20、无多余物体10。只输出 JSON。"
)

_SCHEMA = {
    "score": 0,
    "summary": "一句话结论",
    "orientation_alignment": "aligned|rotated|mirrored|unclear",
    "zone_checks": [{
        "zone": "区域名", "expected_location": "方位", "verdict": "correct|partial|wrong|missing",
        "observed": "实际内容", "expected_items": ["物品"], "found_items": ["物品"]
    }],
    "structure": {"verdict": "correct|partial|wrong", "note": "墙体和分区差异"},
    "issues": [{
        "kind": (
            "wrong_room_type|missing_fixture|extra_bed|wrong_position|"
            "structure|extra_element|other"
        ),
        "severity": "high|medium|low", "location": "方位", "description": "具体差异"
    }]
}


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
    start = stripped.find("{")
    if start < 0:
        raise ValueError("模型输出中没有 JSON 对象")
    value, _ = json.JSONDecoder().raw_decode(stripped[start:])
    if not isinstance(value, dict):
        raise ValueError("模型输出不是 JSON 对象")
    return value


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _registry_context(registry: ElementRegistry | None) -> str:
    if registry is None:
        return "（无结构化元素清单，以 CAD 图为准）"
    relevant = []
    for element in registry.elements:
        if element.category not in {"家具", "固定柜", "卫浴", "门", "窗", "房间名称"}:
            continue
        box = element.bbox_pct
        center = [round((box[0] + box[2]) / 2, 3), round((box[1] + box[3]) / 2, 3)]
        relevant.append({
            "category": element.category,
            "item": element.item,
            "count": element.count,
            "center_pct": center,
            "bbox_pct": [round(v, 3) for v in box],
        })
    return json.dumps(relevant, ensure_ascii=False, separators=(",", ":"))


def _message(layout: Path, render: Path, registry: ElementRegistry | None,
             requirements: list[str]) -> list[dict[str, Any]]:
    task = (
        "图1为 CAD 布局真值，图2为待验收生成图。归一化坐标左上为(0,0)、右下为(1,1)。\n"
        f"元素坐标摘要：{_registry_context(registry)}\n"
        f"关键要求：{json.dumps(requirements, ensure_ascii=False)}\n"
        "逐区域核对后严格按以下结构输出 JSON，字段不得缺失：\n"
        + json.dumps(_SCHEMA, ensure_ascii=False)
    )
    return [
        {"type": "text", "text": "图1：CAD 布局真值"},
        {"type": "image_url", "image_url": {"url": _data_uri(layout)}},
        {"type": "text", "text": "图2：AI 生成结果"},
        {"type": "image_url", "image_url": {"url": _data_uri(render)}},
        {"type": "text", "text": task},
    ]


def _call_openrouter(content: list[dict[str, Any]], *, api_key: str, model: str,
                     base_url: str, timeout: float) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": content}],
        "temperature": 0,
        "max_tokens": 4000,
        "thinking": {"type": "disabled"},
    }
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
        response.raise_for_status()
    body = response.json()
    result = body["choices"][0]["message"]["content"]
    if isinstance(result, str):
        return result
    if result is None:
        # 部分路由节点偶发 null；抛异常让 validate_render_directory 记录错误并继续
        raise ValueError("模型返回 content: null (路由节点偶发，请重跑)")
    return "".join(part.get("text", "") for part in result if isinstance(part, dict))

def _normalize_report(data: dict[str, Any], render: Path,
                      pass_score: int) -> dict[str, Any]:
    score = data.get("score", 0)
    if not isinstance(score, (int, float)):
        score = 0
    score = max(0, min(100, round(score)))
    zones = data.get("zone_checks", [])
    issues = data.get("issues", [])
    if not isinstance(zones, list):
        zones = []
    if not isinstance(issues, list):
        issues = []

    bathroom_failed = any(
        isinstance(zone, dict)
        and any(word in str(zone.get("zone", "")).lower()
                for word in ("bathroom", "shower", "卫生间", "淋浴", "洗澡"))
        and zone.get("verdict") in {"wrong", "missing"}
        for zone in zones
    )
    critical_issue = any(
        isinstance(issue, dict)
        and issue.get("severity") == "high"
        and issue.get("kind") in {
            "wrong_room_type", "missing_fixture", "extra_bed", "structure"
        }
        for issue in issues
    )
    orientation = data.get("orientation_alignment", "unclear")
    passed = score >= pass_score and not bathroom_failed and not critical_issue
    return {
        "file": render.name,
        "score": score,
        "passed": passed,
        "summary": str(data.get("summary", "")),
        "orientation_alignment": orientation,
        "zone_checks": zones,
        "structure": data.get("structure", {}),
        "issues": issues,
        "gate": {
            "threshold": pass_score,
            "bathroom_failed": bathroom_failed,
            "critical_issue": critical_issue,
        },
    }


def validate_generated_render(
    layout_png: str | Path,
    render_png: str | Path,
    *,
    elements_json: str | Path | None = None,
    requirements: list[str] | None = None,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    pass_score: int = 75,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """对比一张布局真值和一张生成图，返回带硬门禁的结构化报告。"""
    layout = Path(layout_png)
    render = Path(render_png)
    for label, path in (("布局图", layout), ("生成图", render)):
        if not path.is_file():
            raise FileNotFoundError(f"{label}不存在: {path}")

    registry = None
    if elements_json:
        elements_path = Path(elements_json)
        if not elements_path.is_file():
            raise FileNotFoundError(f"元素清单不存在: {elements_path}")
        registry = ElementRegistry.model_validate_json(
            elements_path.read_text(encoding="utf-8")
        )

    checks = requirements or [
        "卫生间区域必须包含马桶、台盆和淋浴功能，不得被生成为卧室或出现床",
        "右侧卧室区应保留两张床及其相对位置",
        "墙体、入口、过道与主要空间分区应和 CAD 布局一致",
        "家具和洁具不得跨功能区错位，不得凭空增加主要物品",
    ]
    key = api_key or os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("缺少 OpenRouter 密钥，请设置 OPENROUTER_API_KEY")
    raw = _call_openrouter(
        _message(layout, render, registry, checks),
        api_key=key,
        model=model,
        base_url=base_url,
        timeout=timeout,
    )
    report = _normalize_report(_extract_json(raw), render, pass_score)
    report["model"] = model
    return report


def validate_render_directory(
    layout_png: str | Path,
    renders_dir: str | Path,
    *,
    elements_json: str | Path | None = None,
    requirements: list[str] | None = None,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    pass_score: int = 75,
    pattern: str = "*.png",
    timeout: float = 180.0,
) -> dict[str, Any]:
    """逐张验证目录内生成图，并按得分从高到低汇总。"""
    render_dir = Path(renders_dir)
    files = sorted(path for path in render_dir.glob(pattern) if path.is_file())
    if not files:
        raise FileNotFoundError(f"目录中没有匹配 {pattern} 的图片: {render_dir}")

    reports = []
    for render in files:
        try:
            reports.append(validate_generated_render(
                layout_png,
                render,
                elements_json=elements_json,
                requirements=requirements,
                api_key=api_key,
                model=model,
                base_url=base_url,
                pass_score=pass_score,
                timeout=timeout,
            ))
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            reports.append({
                "file": render.name,
                "score": 0,
                "passed": False,
                "summary": f"验证失败: {exc}",
                "issues": [],
                "gate": {"validation_error": True},
                "model": model,
            })

    ranked = sorted(reports, key=lambda item: item["score"], reverse=True)
    passed = sum(1 for report in reports if report["passed"])
    return {
        "model": model,
        "layout": str(Path(layout_png)),
        "total": len(reports),
        "passed": passed,
        "pass_rate": round(passed / len(reports), 3),
        "best": ranked[0]["file"] if ranked else None,
        "ranking": [report["file"] for report in ranked],
        "reports": ranked,
    }
