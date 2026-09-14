# scripts/two_agent.py
"""两 Agent 管线 CLI：识图 → 验证 → 提示词包。

用法:
  uv run python scripts/two_agent.py \
      --input fixtures/cad/01-平面系统图.dwg --out output/two_agent

输出目录:
  prompt.txt      英文正向提示词（喂 gpt-image 类模型）
  prompt_zh.txt  中文布局描述
  layout.png     布局参考图（与提示词一起喂模型）
  layout_control.png  线稿控制图（可选，ControlNet 用）
  elements.json   元素清单
  validation.json 验证 Agent 逐轮记录
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.vision.two_agent_pipeline import run  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="DWG/DXF/图片路径")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--model", default=None,
                    help="识图+验证模型（如 qwen/qwen3.8-flash）；默认用 ARP_VISION_MODEL 或配置值")
    ap.add_argument("--max-iters", type=int, default=3)
    args = ap.parse_args()

    r = run(args.input, args.out, vlm_model=args.model, max_iters=args.max_iters)
    if r.ok and r.data:
        d = r.data
        v = d["validation"]
        print("═══ 两 Agent 管线 ═══")
        for rd in v["rounds"]:
            print(f"  验证轮{rd['iter']}: {rd['checks']} 修正={rd['fixes']}")
        print(f"  验证: {'通过' if v['passed'] else '未通过'}")
        print(f"  Prompt: {d['prompt'][:120]}...")
        print(f"  输出目录: {d['out_dir']}")
    else:
        print(f"FAILED: {r.error.message if r.error else '?'}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
