# scripts/gpt_image_render.py
"""从两 Agent 管线输出目录调用 gpt-image 模型生成效果图。

用法:
  uv run python scripts/gpt_image_render.py \
      --pipeline-out output/render_qwen3 \
      --out output/render_gpt

可选:
  --model   模型名（默认 openai/gpt-image-2.5-sunburst）
  --n       生成张数（默认 4）
  --size    图片尺寸（默认 1024x1024）
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.engines.gpt_image_agent import DEFAULT_MODEL, GptImageAgent


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pipeline-out", required=True,
                    help="两 Agent 管线输出目录（含 prompt.txt + layout.png）")
    ap.add_argument("--out", required=True, help="效果图保存目录")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--reference", choices=("semantic", "layout"),
                    default="layout",
                    help="参考图：layout=原始 CAD 渲染图（默认，几何最准）；"
                         "semantic=语义配色俯视图（实验对比用）")
    ap.add_argument("--prompt-mode", choices=("minimal", "file"),
                    default="minimal",
                    help="minimal=只给转换指令（默认，实测最优）；"
                         "file=用 prompt.txt 逐房间描述（实验对比用）")
    args = ap.parse_args()

    s = get_settings()
    api_key = s.llm_api_key or ""
    if not api_key:
        print("未配置 ARP_LLM_API_KEY，请在 .env 中设置 OpenRouter key")
        raise SystemExit(1)

    # registry 只在 semantic 参考图模式下需要；prompt.txt 仅 file 模式读取
    from app.models.vision import ElementRegistry
    registry = ElementRegistry.model_validate_json(
        (Path(args.pipeline_out) / "elements.json").read_text(encoding="utf-8"))

    reference_img = None
    if args.reference == "semantic":
        from app.agents.vision.render_package import draw_semantic_reference
        reference_img = draw_semantic_reference(
            registry, Path(args.pipeline_out) / "layout_semantic.png")
        print(f"语义参考图: {reference_img}")

    agent = GptImageAgent(
        api_key=api_key,
        out_dir=Path(args.out),
        model=args.model,
        n=args.n,
        prompt_mode=args.prompt_mode,
    )

    print(f"模型: {args.model}")
    print(f"输入: {args.pipeline_out}")
    paths = await agent.run(Path(args.pipeline_out), reference_img=reference_img)

    if paths:
        print(f"\n共生成 {len(paths)} 张，保存至: {Path(args.out).resolve()}")
    else:
        print("未获得图片（可能模型返回 url，见上方输出）")
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
