# tests/unit/test_cad_inspect_tool.py
from pathlib import Path

from app.tools.cad.inspect import count_proxies, inspect_dxf
from scripts.gen_fixtures import make_apartment_dxf


class _FakeEnt:
    def __init__(self, t): self._t = t
    def dxftype(self): return self._t


def test_count_proxies_detects_acad_proxy():
    msp = [_FakeEnt("LINE"), _FakeEnt("ACAD_PROXY_ENTITY"), _FakeEnt("ACAD_PROXY_ENTITY")]
    assert count_proxies(msp) == 2


def test_inspect_apartment_fixture(tmp_path: Path):
    report = inspect_dxf(make_apartment_dxf(tmp_path / "a.dxf"))
    wall = next(lyr for lyr in report.layers if lyr.name == "WALL")
    assert wall.line_count == 12 and wall.insert_count == 0
    assert "WALL" in report.wall_layer_candidates
    assert 2800.0 in report.floor_height_candidates
    assert report.unit_guess == "mm"          # 图幅 6000+ → mm
    assert report.proxy_entity_count == 0
    assert report.confidence > 0.6
    names = {b.name for b in report.blocks}
    assert {"M_门_900", "C_1500", "sofa"} <= names
    assert any(t.content == "客厅" for t in report.text_annotations)
