# scripts/ab_render.py
"""Phase 1 A/B 渲染 CLI。
用法:
  uv run python scripts/ab_render.py \
    --control-dir fixtures/control_maps \
    --description "温馨日式原木风，暖光" \
    --variants 2
"""
import argparse
import asyncio
import sys
from pathlib import Path

import httpx

# 让 brief 承诺的 `uv run python scripts/ab_render.py` 直接可用：直接执行脚本时
# sys.path[0] 是 scripts/ 而非 repo 根，须先补根路径再导入 app（app 导入因此
# 位于模块级语句之后；本项目 ruff 规则集未启用 E402，无需 noqa）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.engines.comfy import ComfyEngine
from app.engines.comfy_client import ComfyClient
from app.engines.direct_api import (
    ApiAdapter,
    DirectAPIEngine,
    GeminiAdapter,
    OpenAIImageAdapter,
)
from app.engines.registry import ModelInfo, ModelRegistry
from app.graph.mini_render import build_mini_graph, discover_views
from app.infra.pools import TokenBucket


def parse_models_filter(csv: str | None) -> set[str] | None:
    if not csv:
        return None
    return {m.strip() for m in csv.split(",") if m.strip()}


def ensure_views(control_dir: Path | str) -> list[dict]:
    """Task 11 评审加验：进图前确认控制图目录至少产出 1 个视图。

    复用 discover_views 的三件套口径；为空时打印清晰错误并以非零码退出
    （不跑图、不让图内 fan_out 拿 0 任务后在下游 KeyError）。
    """
    views = discover_views(control_dir)
    if not views:
        print(f"no valid views found in {control_dir}: expected "
              "viewXX_depth.png + viewXX_lineart.png + viewXX_white.png",
              file=sys.stderr)
        raise SystemExit(2)
    return views


def ensure_models(models: list[ModelInfo],
                  requested: set[str] | None) -> list[ModelInfo]:
    """--models 过滤 + 零匹配守卫（终审 Important #3）：请求 ID 一个都没命中
    （典型为拼写错误）时打印请求 ID 与可用 enabled ID 并以非零码退出，
    对称于 ensure_views；在任何 engine 构造之前调用。未传过滤时仅要求
    registry 至少有一个 enabled 模型。
    """
    available = sorted(m.model_id for m in models)
    if requested is not None:
        models = [m for m in models if m.model_id in requested]
    if not models:
        what = f"requested {sorted(requested)}, " if requested else ""
        print(f"--models matched no enabled model: {what}available: {available}",
              file=sys.stderr)
        raise SystemExit(2)
    return models


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--control-dir", default="fixtures/control_maps")
    ap.add_argument("--description", required=True)
    ap.add_argument("--models", default=None, help="逗号分隔 model_id 过滤")
    ap.add_argument(
        "--checkpoint", default="sd_xl_base_1.0.safetensors",
        help="ComfyUI checkpoint 文件名（默认 SDXL 家族组合；实际以操作者"
             "下载到 deploy/comfy-models/checkpoints 的文件名为准，按需覆盖）")
    ap.add_argument(
        "--controlnet-depth", default="xinsir/controlnet-depth-sdxl-1.0.safetensors",
        help="depth ControlNet 文件名（须与 checkpoint 同家族，SDXL 默认；"
             "以操作者下载到 controlnet/ 的文件名为准）")
    ap.add_argument(
        "--controlnet-lineart", default="xinsir/controlnet-lineart-sdxl-1.0.safetensors",
        help="lineart ControlNet 文件名（须与 checkpoint 同家族，SDXL 默认；"
             "以操作者下载到 controlnet/ 的文件名为准）")
    ap.add_argument("--variants", type=int, default=2)
    ap.add_argument("--report-dir", default="experiments/phase1")
    args = ap.parse_args()

    setup_logging()
    ensure_views(args.control_dir)  # 快速失败：控制图无效时不碰 registry/图
    s = get_settings()
    reg = ModelRegistry(s.registry_db_url)
    await reg.setup()
    models = await reg.list_enabled()
    flt = parse_models_filter(args.models)
    models = ensure_models(models, flt)  # 零匹配快速失败：在 engine 构造之前

    engines: dict[str, object] = {}
    if any(m.engine == "comfy" for m in models):
        engines["comfy"] = ComfyEngine(
            client=ComfyClient(s.comfy_url),
            template_dir=Path("workflows"), out_dir=Path("experiments/renders"),
            checkpoint=args.checkpoint,
            controlnet_depth=args.controlnet_depth,
            controlnet_lineart=args.controlnet_lineart)
    # truthiness 判断：空串视为未配置（与 Settings 的 str | None 兼容），跳过该适配器
    api_adapters: dict[str, ApiAdapter] = {}
    if s.gemini_api_key:
        api_adapters["gemini"] = GeminiAdapter(s.gemini_api_key)
    if s.openai_api_key:
        api_adapters["openai"] = OpenAIImageAdapter(s.openai_api_key)
    if any(m.engine == "direct_api" for m in models):
        engines["direct_api"] = DirectAPIEngine(
            http=httpx.AsyncClient(timeout=300), adapters=api_adapters,
            limiter=TokenBucket(30))

    graph = build_mini_graph(engines, models)
    final = await graph.ainvoke({"description": args.description,
                                 "control_dir": args.control_dir,
                                 "variants": args.variants,
                                 "report_dir": args.report_dir})
    print(f"报告: {final['report_path']}")


if __name__ == "__main__":
    asyncio.run(main())
