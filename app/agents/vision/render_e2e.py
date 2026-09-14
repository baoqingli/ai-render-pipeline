# app/agents/vision/render_e2e.py
"""端到端一键渲染：DWG/DXF/PNG/JPG → dollhouse 3D 渲染图。

封装 2026-09-14 定版全链路（docs/pipeline-two-agent-gpt-image.md）：
  ①识图 Agent（qwen3.8-flash 等，--model 可切）
  ②验证 Agent（清单 × 布局图核对，≤max_iters 轮）
  ③生图 Agent（openai/gpt-image-2.5-sunburst，原始 CAD 图 + 最小指令）

用法（库调用）:
    from app.agents.vision.render_e2e import run_e2e
    r = asyncio.run(run_e2e("plan.dwg", "output/render_out"))

用法（CLI）:
    uv run python scripts/render_e2e.py --input plan.dwg --out output/render_out
"""
import asyncio
import functools
import json
from pathlib import Path

from app.models.tooling import ToolError, ToolResult

# 支持的输入格式
_CAD_EXTS = {".dwg", ".dxf"}
_IMG_EXTS = {".png", ".jpg", ".jpeg"}


async def run_e2e(file_path: str | Path, out_dir: str | Path, *,
                  model: str | None = None,
                  max_iters: int = 3,
                  force: bool = False,
                  gpt_model: str | None = None,
                  n: int = 1) -> ToolResult[dict]:
    """一键渲染：识图 → 验证 → gpt-image 生图。

    file_path: DWG/DXF（走确定性解析+VLM 增强）或 PNG/JPG（走 CubiCasa 分割）。
    model:     识图+验证模型（缺省回落 ARP_VISION_MODEL）。
    gpt_model: 生图模型（缺省 openai/gpt-image-2.5-sunburst）。
    force:     验证未通过时仍继续生图。
    返回 ToolResult[dict]：prompt/out_dir/validation/renders。
    """
    from app.agents.vision.two_agent_pipeline import run as pipeline_run
    from app.engines.gpt_image_agent import GptImageAgent

    src = Path(file_path)
    if not src.exists():
        return ToolResult(ok=False, error=ToolError(
            code="INPUT_INVALID", message=f"输入不存在: {src}"))
    if src.suffix.lower() not in _CAD_EXTS | _IMG_EXTS:
        return ToolResult(ok=False, error=ToolError(
            code="INPUT_INVALID",
            message=f"不支持的格式 {src.suffix}（支持 {_CAD_EXTS | _IMG_EXTS}）"))

    out = Path(out_dir)
    # --out 以 .png 结尾视为"期望的最终图片路径"：目录取其父级，渲染完成后拷贝过去
    out_file: Path | None = None
    if out.suffix.lower() in _IMG_EXTS:
        out_file = out
        out = out.parent
    out.mkdir(parents=True, exist_ok=True)

    # ── Stage 1: 两 Agent 管线（同步实现含内部 asyncio.run，放线程防嵌套）──
    r = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(pipeline_run, src, out,
                                vlm_model=model, max_iters=max_iters))

    # 验证失败但 force：从已存清单重建 prompt 后继续
    if (not r.ok or r.data is None) and force:
        if r.error and r.error.code == "VALIDATION_FAILED":
            r = await asyncio.get_event_loop().run_in_executor(
                None, functools.partial(_force_outputs, out))
        if r is None or not r.ok:
            return r  # 非验证类失败（识图失败等）原样返回
    elif not r.ok or r.data is None:
        return r

    # ── Stage 2: gpt-image 生图（原始 CAD 布局图 + 最小指令）──────────────
    from app.core.config import get_settings
    api_key = get_settings().llm_api_key or ""
    if not api_key:
        return ToolResult(ok=False, error=ToolError(
            code="API_KEY_MISSING",
            message="未配置 ARP_LLM_API_KEY（OpenRouter key）"))
    from app.engines.gpt_image_agent import DEFAULT_MODEL
    agent = GptImageAgent(api_key=api_key, out_dir=out / "renders",
                          model=gpt_model or DEFAULT_MODEL, n=n)
    renders = await agent.run(out, reference_img=out / "layout.png")

    data = dict(r.data or {})
    data["renders"] = renders
    if out_file and renders:
        import shutil

        shutil.copy2(renders[0], out_file)
        data["render_file"] = str(out_file)
    return ToolResult(ok=True, data=data)


def _force_outputs(out: Path) -> ToolResult[dict]:
    """验证失败后的降级出包：从 elements.json 重建 prompt（--force 用）。"""
    from app.agents.vision.render_package import build_prompt, layout_description_zh
    from app.models.vision import ElementRegistry

    try:
        registry = ElementRegistry.model_validate_json(
            (out / "elements.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ToolResult(ok=False, error=ToolError(
            code="VISION_FAILED", message="elements.json 缺失，无法降级出包"))
    _style = ("modern cozy hotel interior, warm wood flooring, "
              "white walls, soft natural lighting, "
              "photorealistic, architecturally accurate")
    prompt_en = build_prompt(registry, _style)
    (out / "prompt.txt").write_text(prompt_en, encoding="utf-8")
    (out / "prompt_zh.txt").write_text(
        layout_description_zh(registry), encoding="utf-8")
    return ToolResult(ok=True, data={
        "prompt": prompt_en, "out_dir": str(out),
        "validation": json.loads(
            (out / "validation.json").read_text(encoding="utf-8"))})
