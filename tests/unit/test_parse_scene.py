# tests/unit/test_parse_scene.py
from pathlib import Path

from app.tools.cad.geometry import pair_wall_segments
from app.tools.cad.parse import parse_scene
from scripts.gen_fixtures import make_apartment_dxf


def _seg(x1, y1, x2, y2):
    return ((x1, y1), (x2, y2))


def test_pair_wall_segments_finds_thickness():
    segs = [_seg(0, -100, 6000, -100), _seg(0, 100, 6000, 100), _seg(7000, 0, 7000, 4000)]
    centers = pair_wall_segments(segs)
    assert len(centers) == 2                       # 一对 + 一条落单
    assert any(abs(c[2] - 200.0) < 1e-6 for c in centers)  # 配对得到实际厚度
    assert any(c[1][0] == 7000 for c in centers)           # 落单段保留，厚度走默认


def test_parse_apartment_fixture(tmp_path: Path):
    result = parse_scene(make_apartment_dxf(tmp_path / "a.dxf"))
    assert result.ok
    scene = result.data
    assert scene is not None
    assert scene.floor_height == 2800.0            # 从"层高2800"标注提取
    assert scene.unit == "mm"
    assert len(scene.rooms) == 2                    # 客厅 + 卧室
    names = {r.name for r in scene.rooms}
    assert names == {"客厅", "卧室"}
    assert len(scene.doors) == 1 and scene.doors[0].width == 900.0
    assert len(scene.windows) == 1 and scene.windows[0].width == 1500.0
    assert scene.doors[0].wall_id is not None       # 挂到最近墙
    assert len(scene.walls) >= 5                    # 6 条墙边（配对后 5-6 段中心线）
    assert scene.furniture and scene.furniture[0].source == "cad"
    assert scene.furniture[0].type == "sofa"
