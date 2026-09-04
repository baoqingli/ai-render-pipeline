# tests/unit/test_cad_cli.py
from pathlib import Path

from app.tools.cad.inspect import inspect_dxf
from app.tools.cad.parse import parse_scene
from scripts.cad_inspect import render_report
from scripts.gen_fixtures import make_apartment_dxf


def test_render_report_contains_key_sections(tmp_path: Path):
    dxf = make_apartment_dxf(tmp_path / "a.dxf")
    report = inspect_dxf(dxf)
    result = parse_scene(dxf)
    md = render_report(report, result.data, [])
    assert "WALL" in md and "置信度" in md and "客厅" in md
    assert "房间" in md and "2800" in md
