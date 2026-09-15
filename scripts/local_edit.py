# scripts/local_edit.py
"""局部重绘 CLI：渲染图 + 自然语言指令 → 只改目标区域，其余严格不变。

用法:
  uv run python scripts/local_edit.py \
      --image output/test1.png \
      --instruction "把中央的布艺沙发换成深绿色布艺沙发" \
      --out output/edit_out

可选:
  --mask 遮罩文件     白=可编辑区（提供则跳过 VLM 定位，最精准）
  --edit-model        生图模型（默认 openai/gpt-image-2.5-sunburst）
  --vlm-model         定位/质检模型（默认 qwen/qwen3.8-flash）
  --max-retries       质检未过重试次数（默认 2）
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.engines.local_edit_agent import (  # noqa: E402
    DEFAULT_EDIT_MODEL, DEFAULT_VLM_MODEL, LocalEditAgent)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, help="原图路径")
    ap.add_argument("--instruction", required=True, help="局部调整指令")
    ap.add_argument("--out", required=True, help="产物目录")
    ap.add_argument("--mask", default=None,
                    help="遮罩文件（L 模式，白=可编辑）；缺省走 VLM 定位")
    ap.add_argument("--edit-model", default=DEFAULT_EDIT_MODEL)
    ap.add_argument("--vlm-model", default=DEFAULT_VLM_MODEL)
    ap.add_argument("--max-retries", type=int, default=2)
    ap.add_argument("--no-compile", action="store_true",
                    help="跳过指令编译，直接用原始指令")
    args = ap.parse_args()

    s = get_settings()
    api_key = s.llm_api_key or ""
    if not api_key:
        print("未配置 ARP_LLM_API_KEY，请在 .env 中设置 OpenRouter key")
        raise SystemExit(1)

    print(f"原图: {args.image}")
    print(f"指令: {args.instruction}")

    agent = LocalEditAgent(
        api_key=api_key, out_dir=Path(args.out),
        edit_model=args.edit_model, vlm_model=args.vlm_model,
        max_retries=args.max_retries)
    report = await agent.run(Path(args.image), args.instruction,
                             mask_path=Path(args.mask) if args.mask else None,
                             compile_instruction=not args.no_compile)
    if report.get("instruction_compiled") and \
            report["instruction_compiled"] != args.instruction:
        print(f"  编译后指令: {report['instruction_compiled']}")

    g = report.get("grounding") or []
    if g:
        for it in g:
            print(f"  定位: {it['label']} norm={it['bbox_norm']} "
                  f"px={it['bbox_px']}")
    else:
        print("  定位: 使用用户提供的遮罩")
    v = report.get("verify", {})
    ok = v.get("instruction_fulfilled") is True and \
        v.get("target_intact", True) is True and \
        v.get("outside_changed") is not True
    print(f"  质检: {'通过' if ok else '未确认'} ({v.get('reason', '-')})")
    print(f"  结果: {report['edited']}")
    print(f"  产物目录: {Path(args.out).resolve()}")
    # 链式迭代提示：看过图不满意时复制即用
    print("\n看过图不满意？继续调整（复制改指令即用）:")
    print(f'  uv run python scripts/local_edit.py --image '
          f'"{report["edited"]}" '
          f'--instruction "<下一处调整>" --out {Path(args.out) / "v2"}')


if __name__ == "__main__":
    asyncio.run(main())
