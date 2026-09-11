"""批量验证 AI 平面渲染图与 CAD 布局的一致性。"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools.generated_render_validate import validate_render_directory


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layout", required=True, help="CAD 布局真值 PNG")
    parser.add_argument("--renders", required=True, help="待验证生成图目录")
    parser.add_argument("--elements", help="ElementRegistry JSON")
    parser.add_argument("--out", required=True, help="验证报告 JSON")
    parser.add_argument("--model", default="qwen/qwen3.8-flash")
    parser.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--pass-score", type=int, default=75)
    parser.add_argument("--pattern", default="*.png")
    parser.add_argument("--requirement", action="append", default=[])
    args = parser.parse_args()

    report = validate_render_directory(
        args.layout,
        args.renders,
        elements_json=args.elements,
        requirements=args.requirement or None,
        model=args.model,
        base_url=args.base_url,
        pass_score=args.pass_score,
        pattern=args.pattern,
    )
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"模型: {report['model']}")
    print(f"通过: {report['passed']}/{report['total']}，最佳: {report['best']}")
    for index, item in enumerate(report["reports"], 1):
        status = "通过" if item["passed"] else "未通过"
        print(f"{index}. {item['file']}  {item['score']}分  {status}")
        print(f"   {item['summary']}")
    print(f"报告: {output}")


if __name__ == "__main__":
    main()
