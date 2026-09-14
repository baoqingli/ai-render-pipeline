# app/agents/vision/two_agent_pipeline.py
"""两 Agent 最短路径（用户定案 2026-09-10）：

  识图 Agent → ElementRegistry
  验证 Agent → registry × 布局图核对（不过→修正→再验，≤3 轮）
  通过 → 生成提示词 → 输出目录（prompt + 布局图 + 元素清单 + 验证报告）

用户拿输出目录中的提示词 + 布局图喂 gpt-image 类大模型生成 3D 渲染图。
其他已建能力（白模/ComfyUI/tri_validate）保留为后续升级参考，不进本路径。

用法:
  uv run python scripts/two_agent.py --input <dwg|dxf|png> --out <dir>
"""
import json
import shutil
from pathlib import Path

from app.models.tooling import ToolError, ToolResult
from app.models.vision import ElementRegistry


# ── 识图 Agent ─────────────────────────────────────────────────────────────────

def vision_agent(file_path: str | Path, *,
                 model: str | None = None) -> tuple[ElementRegistry, Path]:
    """识图：按扩展名分路，返回 (registry, 布局参考图路径)。

    DWG → 先 ODA 转 DXF（内容 hash 缓存，同文件不重转）；
    DXF → 确定性提取 + 渲染全图层布局图；
    图片 → CubiCasa 分割 + VLM 定标，输入图即布局图。
    """
    from app.agents.vision.agent_v3 import analyze_dwg, analyze_image

    p = Path(file_path)
    suffix = p.suffix.lower()
    if suffix == ".dwg":
        # ezdxf 读不了 DWG，先转 DXF（此前直接 readfile 会报 not a DXF file）
        import asyncio

        from app.tools.cad.convert import convert_dwg
        r_conv = asyncio.run(convert_dwg(p, p.parent / "_converted"))
        if not r_conv.ok or r_conv.data is None:
            msg = r_conv.error.message if r_conv.error else "?"
            raise RuntimeError(f"DWG 转 DXF 失败（需 ODA File Converter）: {msg}")
        p = r_conv.data
        suffix = ".dxf"
    if suffix == ".dxf":
        r = analyze_dwg(p, model=model)
        if not r.ok or r.data is None:
            raise RuntimeError(f"识图失败: {r.error.message if r.error else '?'}")
        import ezdxf

        from app.tools.cad_render import model_extent, render_modelspace
        from app.tools.cad_sheets import pick_layout_view, render_sheet_view

        out_img = p.parent / "_two_agent_layout.png"
        doc = ezdxf.readfile(str(p))
        best_sv, _ = pick_layout_view(doc)
        # 强制叠加地面材质填充图层，即使在家具布置视口中被冻结
        # 使卫生间区域在 layout.png 里具备可识别的视觉特征
        _gnd_layers = {"GND-地面材质填充", "GND-地面铺装"}
        if best_sv is not None:
            render_sheet_view(doc, best_sv, out_img, width_px=1600,
                              extra_hatch_layers=_gnd_layers)
        else:
            render_modelspace(doc, model_extent(doc), 1600, out_img)
        return r.data, out_img
    r = analyze_image(p, model=model)
    if not r.ok or r.data is None:
        raise RuntimeError(f"识图失败: {r.error.message if r.error else '?'}")
    return r.data, p


# ── 验证 Agent ─────────────────────────────────────────────────────────────────

def _apply_fixes(registry: ElementRegistry) -> list[str]:
    """按确定性健康检查定向修正（复用 gated_analyze 修正器）。"""
    from app.agents.vision.gated_analyze import (_fix_room_overcount,
                                                  _fix_wall_fragmentation)
    from app.tools.tri_validate import registry_health

    fixes: list[str] = []
    health = registry_health(registry)
    kinds = {i["kind"] for i in health["issues"]}
    if "fragmentation" in kinds:
        n = _fix_wall_fragmentation(registry, boost=2.0)
        fixes.append(f"wall_merge(-{n})")
    if "over_count" in kinds:
        n = _fix_room_overcount(registry)
        fixes.append(f"room_merge(-{n})")
    return fixes


def _verdict_ok(vlm: dict) -> bool:
    """VLM 核对是否通过：墙/房间结构 under 才是硬性 fail；门窗/家具细节不阻断。"""
    issues = vlm.get("issues", [])
    structural_high = [i for i in issues
                       if i.get("severity") == "high"
                       and i.get("kind") in ("under_count", "fragmentation")]
    if structural_high:
        return False
    for c in vlm.get("category_checks", []):
        if c.get("verdict") == "under" and c.get("category") in ("墙",):
            return False
    return True


def validation_agent(registry: ElementRegistry, layout_png: str | Path, *,
                    model: str | None = None, max_iters: int = 3) -> dict:
    """验证：registry × 布局图核对循环，返回 {passed, rounds, final_vlm}。"""
    from app.tools.tri_validate import validate_registry

    rounds = []
    vlm: dict = {}
    for it in range(1, max_iters + 1):
        vlm = validate_registry(registry, str(layout_png), model=model)
        ok = _verdict_ok(vlm)
        fixes = [] if ok or it == max_iters else _apply_fixes(registry)
        checks = {c.get("category"):
                  f"{c.get('listed')}v{c.get('visible_estimate')}"
                  f"[{c.get('verdict')}]"
                  for c in vlm.get("category_checks", [])}
        rounds.append({"iter": it, "checks": checks,
                      "issues": len(vlm.get("issues", [])), "fixes": fixes})
        if ok or not fixes:
            break
    return {"passed": _verdict_ok(vlm), "rounds": rounds, "final_vlm": vlm}


# ── 提示词生成 + 输出 ────────────────────────────────────────────────────────

def build_outputs(registry: ElementRegistry, layout_png: Path,
                 out_dir: str | Path, style: str | None = None) -> dict:
    """验证通过后：提示词三件套写入输出目录。"""
    from app.agents.vision.render_package import (build_prompt,
                                                 draw_control_image,
                                                 layout_description_zh)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    _style = style or ("modern cozy hotel interior, warm wood flooring, "
                       "white walls, soft natural lighting, "
                       "photorealistic, architecturally accurate")
    prompt_en = build_prompt(registry, _style)
    (out / "prompt.txt").write_text(prompt_en, encoding="utf-8")
    (out / "prompt_zh.txt").write_text(layout_description_zh(registry),
                                       encoding="utf-8")
    # 布局参考图：拷贝原图（用户喂大模型用）；另存线稿控制图备 ControlNet
    shutil.copy2(layout_png, out / "layout.png")
    try:
        draw_control_image(registry, out / "layout_control.png")
    except Exception:  # noqa: BLEED001 控制图为增强件，失败不阻断
        pass
    (out / "elements.json").write_text(registry.model_dump_json(indent=1),
                                        encoding="utf-8")
    return {"prompt": prompt_en, "out_dir": str(out)}


def run(file_path: str | Path, out_dir: str | Path, *,
        vlm_model: str | None = None, max_iters: int = 3) -> ToolResult[dict]:
    """两 Agent 主流程：识图 → 验证 → 通过则出提示词包。"""
    try:
        registry, layout_png = vision_agent(file_path, model=vlm_model)
    except RuntimeError as e:
        return ToolResult(ok=False, error=ToolError(code="VISION_FAILED",
                                                     message=str(e)))
    val = validation_agent(registry, layout_png, model=vlm_model,
                           max_iters=max_iters)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "validation.json").write_text(
        json.dumps(val, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "elements.json").write_text(registry.model_dump_json(indent=1),
                                        encoding="utf-8")
    if not val["passed"]:
        return ToolResult(ok=False, error=ToolError(
            code="VALIDATION_FAILED",
            message="验证未通过——validation.json 有逐轮记录，"
                    "elements.json 已存当前清单供排查"))
    outs = build_outputs(registry, layout_png, out_dir)
    outs["validation"] = val
    return ToolResult(ok=True, data=outs)
