# scripts/render_e2e.py
"""端到端一键渲染 CLI：DWG/DXF/PNG/JPG → dollhouse 3D 渲染图。

用法:
  uv run python scripts/render_e2e.py \
      --input fixtures/cad/01-平面系统图.dwg \
      --out output/render_out

可选:
  --model      识图+验证模型（默认回落 ARP_VISION_MODEL）
  --gpt-model  生图模型（默认 openai/gpt-image-2.5-sunburst）
  --n          生成张数（默认 1）
  --max-iters  验证最大轮数（默认 3）
  --force      验证未通过时仍继续生图
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.vision.render_e2e import run_e2e  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True,
                    help="DWG/DXF/PNG/JPG 路径")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--model", default=None,
                    help="识图+验证模型（如 qwen/qwen3.8-flash）")
    ap.add_argument("--gpt-model", default=None,
                    help="生图模型（默认 openai/gpt-image-2.5-sunburst）")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--max-iters", type=int, default=3)
    ap.add_argument("--force", action="store_true",
                    help="验证未通过时仍继续生图")
    ap.add_argument("--style", default=None,
                    help='自然语言渲染要求，如 "现代简约风，浅色木地板，暖色灯光"'
                         "（布局类要求会被自动剥离）")
    args = ap.parse_args()

    print(f"输入: {args.input}")
    if args.style:
        print(f"风格要求: {args.style}")
    r = await run_e2e(args.input, args.out,
                      model=args.model, gpt_model=args.gpt_model,
                      n=args.n, max_iters=args.max_iters, force=args.force,
                      style=args.style)

    if not r.ok or r.data is None:
        msg = r.error.message if r.error else "?"
        code = r.error.code if r.error else "?"
        print(f"FAILED [{code}]: {msg}")
        raise SystemExit(1)

    d = r.data
    for rd in d.get("validation", {}).get("rounds", []):
        print(f"  验证轮{rd['iter']}: {rd['checks']}  修正={rd['fixes']}")
    print(f"  验证: {'通过' if d['validation']['passed'] else '未通过(--force)'}")
    print(f"  渲染图: {len(d['renders'])} 张 → {d['renders']}")
    print(f"\n输出目录: {Path(d['out_dir']).resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
