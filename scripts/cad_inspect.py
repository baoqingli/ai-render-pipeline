"""CAD 内部诊断：DXF/DWG → CadReport + SceneJSON + markdown 报告（内部工具，不给终端用户）。"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logging import setup_logging
from app.models.cad_report import CadReport
from app.models.scene import SceneJSON
from app.tools.cad.convert import convert_dwg
from app.tools.cad.inspect import inspect_dxf
from app.tools.cad.parse import parse_scene


def ensure_dxf(path: Path) -> Path:
    if path.suffix.lower() == ".dwg":
        result = asyncio.run(convert_dwg(path, path.parent / "_converted"))
        if not result.ok or result.data is None:
            print(f"[error] DWG 转换失败: {result.error.code if result.error else '?'} "
                  f"{result.error.message if result.error else ''}", file=sys.stderr)
            raise SystemExit(2)
        return result.data
    return path


def render_report(report: CadReport, scene: SceneJSON | None, fallbacks: list[str]) -> str:
    lines = ["# CAD 解析诊断", "",
             f"- 置信度: **{report.confidence}**  天正 proxy 实体: {report.proxy_entity_count}  "
             f"单位推断: {report.unit_guess}",
             f"- 层高候选: {report.floor_height_candidates or '无（用默认 2800）'}",
             f"- 墙图层候选: {report.wall_layer_candidates or '无（走默认墙厚兜底）'}",
             f"- 完成面/房间图层候选: {report.room_layer_candidates or '无'}",
             f"- fallbacks: {fallbacks or '无'}", "",
             "| layer | line | pline | text | insert | other |",
             "| --- | --- | --- | --- | --- | --- |"]
    for s in report.layers:
        lines.append(f"| {s.name} | {s.line_count} | {s.polyline_count} | "
                     f"{s.text_count} | {s.insert_count} | {s.other_count} |")
    lines += ["", "| block | layer | count |", "| --- | --- | --- |"]
    for b in report.blocks:
        lines.append(f"| {b.name} | {b.layer} | {b.insert_count} |")
    if scene is not None:
        lines += ["", f"## 解析结果：墙 {len(scene.walls)} · 门 {len(scene.doors)}"
                      f" · 窗 {len(scene.windows)} · 家具 {len(scene.furniture)}"
                      f" · 房间 {len(scene.rooms)}",
                  f"- 层高 {scene.floor_height}，墙厚 {scene.wall_thickness}"]
        for r in scene.rooms:
            lines.append(f"  - {r.id}: {r.name or '未命名'}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="CAD 内部诊断")
    ap.add_argument("cad", type=Path)
    ap.add_argument("--out", type=Path, default=Path("experiments/cad"))
    args = ap.parse_args()
    setup_logging()
    dxf = ensure_dxf(args.cad)
    report = inspect_dxf(dxf)
    result = parse_scene(dxf, report)
    scene = result.data
    # 低置信度（ok=True 但 error 带 PARSE_LOW_CONFIDENCE）时把代码+原因标注进 fallbacks 行
    fallbacks = [f"{result.error.code}: {result.error.message}"] if result.error else []
    out_dir = args.out / args.cad.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(render_report(report, scene, fallbacks), encoding="utf-8")
    if scene is not None:
        (out_dir / "scene.json").write_text(
            json.dumps(scene.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告: {out_dir / 'report.md'}  置信度: {report.confidence}")


if __name__ == "__main__":
    main()
