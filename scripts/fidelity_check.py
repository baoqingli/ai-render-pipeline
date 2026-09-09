# scripts/fidelity_check.py
"""VLM Fidelity Check CLI：CAD 平面 vs 白模俯视渲染 的布局一致性咨询审查。

用法:
  uv run python scripts/fidelity_check.py --cad <plan.png> --render <white.png> \
      [--out report.json] [--model glm-4.5v]

输出: 结构化 JSON 报告（score/summary/issues），--out 缺省为 <render 目录>/fidelity_report.json。
咨询层：报告不反写场景；确定性主校验见 scripts/cad_overlay.py。
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools.fidelity import check_fidelity


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cad", required=True, help="CAD 平面渲染 PNG（真值）")
    ap.add_argument("--render", required=True, help="白模俯视渲染 PNG")
    ap.add_argument("--out", help="报告 JSON 输出路径（缺省 render 目录下 fidelity_report.json）")
    ap.add_argument("--model", help="VLM 模型名（缺省 ARP_FIDELITY_MODEL / ARP_LLM_MODEL）")
    args = ap.parse_args()

    out = args.out or str(Path(args.render).parent / "fidelity_report.json")
    result = asyncio.run(check_fidelity(args.cad, args.render, out, model=args.model))
    if not result.ok or result.data is None:
        print(f"FAILED: {result.error.code if result.error else '?'} "
              f"{result.error.message if result.error else ''}")
        raise SystemExit(1)
    tag = "cache" if result.cache_hit else "live"
    print(f"[{tag}] score={result.data.score} model={result.data.model} "
          f"issues={len(result.data.issues)}")
    print(result.data.summary)
    for i in result.data.issues:
        print(f"  [{i.severity}] {i.kind}: {i.description} ({i.location})")
    print(f"report: {out}")


if __name__ == "__main__":
    main()
