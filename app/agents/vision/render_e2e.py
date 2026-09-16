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
from collections.abc import Callable
from datetime import datetime
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
                  n: int = 1,
                  desc: str | None = None,
                  edit: str | list[str] | None = None,
                  progress_cb: Callable[[str], None] | None = None) \
        -> ToolResult[dict]:
    """一键渲染：识图 → 验证 → gpt-image 生图 →（可选）局部编辑。

    file_path: DWG/DXF（走确定性解析+VLM 增强）或 PNG/JPG（走 CubiCasa 分割）。
    model:     识图+验证模型（缺省回落 ARP_VISION_MODEL）。
    gpt_model: 生图模型（缺省 openai/gpt-image-2.5-sunburst）。
    force:     验证未通过时仍继续生图。
    desc:      用户自然语言生图描述（任意语言自由输入——风格/材质/光照/
               氛围/家具偏好/夜景等）。经描述编译器识别生图相关内容并转
               英文拼入 prompt；布局类要求与无关内容被剥离（布局由参考图
               决定）。不影响识图/验证两段。
    edit:      出图后的局部编辑指令（str 或多条 list[str] 串行迭代），
               仅修改指令目标区域，其余像素构造性保持不变（局部重绘
               Agent，docs/local-edit-agent-plan-2026-09.md）。
    返回 ToolResult[dict]：prompt/out_dir/validation/renders/desc_prompt/
               edited/edit_reports。
    progress_cb: 可选进度回调（HTTP API 用它更新作业 stage），阶段取值
               vision | validation | generate | edit | done；回调异常被
               吞掉，绝不打断业务。
    """

    def _stage(s: str) -> None:
        if progress_cb:
            try:
                progress_cb(s)
            except Exception:  # noqa: BLE001 进度回调不打断业务
                pass
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
    # 目录约定：<根>/<日期>/<唯一运行目录>，一次运行的所有产物都在其中
    now = datetime.now()
    out = out / now.strftime("%Y-%m-%d") / now.strftime("%H%M%S")
    seq = 1
    while out.exists():   # 同秒重跑保护，保证目录唯一
        out = out.parent / f"{out.name}_{seq}"
        seq += 1
    out.mkdir(parents=True, exist_ok=True)

    # ── Stage 1: 两 Agent 管线（同步实现含内部 asyncio.run，放线程防嵌套）──
    _stage("vision")
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

    # ── Stage 1.5: 描述编译（LLM 调用放线程，避免嵌套事件循环）───────────
    _stage("generate")
    desc_en: str | None = None
    if desc:
        from app.agents.vision.desc_compiler import compile_desc
        desc_en = await asyncio.get_event_loop().run_in_executor(
            None, functools.partial(compile_desc, desc, model=model))
        if desc_en:
            (out / "desc_prompt.txt").write_text(desc_en, encoding="utf-8")
            print(f"  描述编译: {desc_en}")
        else:
            print("  描述编译: 无生图相关内容，使用默认渲染")

    renders = await agent.run(out, reference_img=out / "layout.png",
                              desc_prompt=desc_en)

    data = dict(r.data or {})
    data["renders"] = renders
    data["desc_prompt"] = desc_en

    # ── Stage 3: 局部编辑（串行迭代，每次以上一次产物为基图）───────────────
    if edit:
        _stage("edit")
    final_image: Path | None = Path(renders[0]) if renders else None
    if edit and final_image:
        from app.engines.local_edit_agent import LocalEditAgent
        edits = [edit] if isinstance(edit, str) else list(edit)
        editor = LocalEditAgent(api_key=api_key, out_dir=out / "edits",
                                edit_model=gpt_model or DEFAULT_MODEL)
        edit_reports = []
        for i, ins in enumerate(edits, 1):
            print(f"  局部编辑 {i}/{len(edits)}: {ins}")
            rep = await editor.run(final_image, ins)
            final_image = Path(rep["edited"])
            edit_reports.append(rep)
            v = rep.get("verify", {})
            ok = v.get("instruction_fulfilled") is True and \
                v.get("target_intact", True) is True and \
                v.get("outside_changed") is not True
            print(f"    质检: {'通过' if ok else '未确认'}"
                  f"（{v.get('reason', '-')}）")
        data["edited"] = str(final_image)
        data["edit_reports"] = edit_reports

    # 最终成品固定命名 final.png 放运行目录根部（后续 local_edit 引用方便）
    _stage("done")
    if final_image:
        import shutil

        final_path = out / "final.png"
        shutil.copy2(final_image, final_path)
        data["final"] = str(final_path)
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
