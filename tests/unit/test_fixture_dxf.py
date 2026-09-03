from pathlib import Path

import ezdxf

from scripts.gen_fixtures import make_apartment_dxf


def test_apartment_fixture_matches_contract(tmp_path: Path):
    p = make_apartment_dxf(tmp_path / "apartment.dxf")
    doc = ezdxf.readfile(p)
    msp = doc.modelspace()
    lines = [e for e in msp if e.dxftype() == "LINE"]
    assert {e.dxf.layer for e in lines} >= {"WALL", "GARBAGE"}
    assert sum(1 for e in lines if e.dxf.layer == "WALL") == 12  # 4 外边 + 内墙 2 = 6 条墙边 × 2 线
    inserts = [e for e in msp if e.dxftype() == "INSERT"]
    assert {e.dxf.name for e in inserts} == {"M_门_900", "C_1500", "sofa"}
    texts = [e.dxf.text for e in msp.query("TEXT")]
    assert {"客厅", "卧室", "层高2800"} <= set(texts)
    assert "DIM" in {e.dxf.layer for e in msp}  # 垃圾标注存在
