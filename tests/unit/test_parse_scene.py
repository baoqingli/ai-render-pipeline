# tests/unit/test_parse_scene.py
from pathlib import Path

import ezdxf

from app.tools.cad.geometry import pair_wall_segments
from app.tools.cad.parse import _segments_from, parse_scene
from scripts.gen_fixtures import make_apartment_dxf


def _seg(x1, y1, x2, y2):
    return ((x1, y1), (x2, y2))


def test_pair_wall_segments_finds_thickness():
    segs = [_seg(0, -100, 6000, -100), _seg(0, 100, 6000, 100), _seg(7000, 0, 7000, 4000)]
    centers = pair_wall_segments(segs)
    assert len(centers) == 2                       # 一对 + 一条落单
    assert any(abs(c[2] - 200.0) < 1e-6 for c in centers)  # 配对得到实际厚度
    assert any(c[1][0] == 7000 for c in centers)           # 落单段保留，厚度走默认


def test_pair_wall_segments_measures_thickness():
    # 机制锁定：±150 对必须量出 300 厚，配对退化成全默认（200）则此测试失败
    centers = pair_wall_segments([_seg(0, -150, 6000, -150), _seg(0, 150, 6000, 150)])
    assert len(centers) == 1
    assert abs(centers[0][2] - 300.0) < 1e-9


def test_segments_from_closed_lwpolyline_keeps_closing_edge():
    # ODA 转换的真实图纸墙是闭合 LWPOLYLINE；get_points 不重复首点，
    # 闭合边（末点→首点）必须补上，否则中心线网络断开、房间闭合失败
    doc = ezdxf.new("R2018")
    doc.layers.add("WALL")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (8000, 0), (8000, 6000), (0, 6000)],
                       dxfattribs={"layer": "WALL"}, close=True)
    segs = _segments_from(msp, {"WALL"})
    assert len(segs) == 4                          # 4 角点闭合矩形 = 4 条边
    assert ((0.0, 6000.0), (0.0, 0.0)) in segs     # 闭合边：末点回到首点


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


def test_parse_classifies_window_via_chinese_layer(tmp_path: Path):
    # 真实图纸形态（recon 发现 5）：窗块名 $WINLIB2D$00000005 无任何英文/数字关键词，
    # 仅图层 GL-玻璃窗线 可识别——中文必须扫图层+块名合并串，不能只看块名
    doc = ezdxf.new("R2018")
    doc.layers.add("GL-玻璃窗线")
    doc.blocks.new(name="$WINLIB$00001")
    doc.modelspace().add_blockref(
        "$WINLIB$00001", (3000.0, 4000.0), dxfattribs={"layer": "GL-玻璃窗线"}
    )
    p = tmp_path / "w.dxf"
    doc.saveas(p)
    result = parse_scene(p)
    assert result.ok
    scene = result.data
    assert scene is not None
    assert len(scene.windows) == 1                 # 经图层"窗"字识别
    assert scene.furniture == []                   # 不得误入家具


def test_parse_classifies_door_via_chinese_layer(tmp_path: Path):
    # 镜像：真实图纸门块 MS11 无关键词，仅图层 A-建筑门 可识别
    doc = ezdxf.new("R2018")
    doc.layers.add("A-建筑门")
    doc.blocks.new(name="MS11")
    doc.modelspace().add_blockref(
        "MS11", (4000.0, 1500.0), dxfattribs={"layer": "A-建筑门"}
    )
    p = tmp_path / "d.dxf"
    doc.saveas(p)
    result = parse_scene(p)
    assert result.ok
    scene = result.data
    assert scene is not None
    assert len(scene.doors) == 1                   # 经图层"门"字识别
    assert scene.furniture == []


def test_parse_rooms_from_finished_surface_layer(tmp_path: Path):
    # 精装图（平面系统图）：P-完成面 图层用封闭 LWPOLYLINE 勾勒各空间边界
    # 没有传统双线墙，polygonize 路径①无结果；路径②须从 room_layer_candidates 取轮廓
    doc = ezdxf.new("R2018")
    doc.layers.add("P-完成面")
    msp = doc.modelspace()
    # 两个相邻房间（各 4×3m，共用一面墙）
    msp.add_lwpolyline(
        [(0, 0), (4000, 0), (4000, 3000), (0, 3000)],
        dxfattribs={"layer": "P-完成面"}, close=True,
    )
    msp.add_lwpolyline(
        [(4000, 0), (8000, 0), (8000, 3000), (4000, 3000)],
        dxfattribs={"layer": "P-完成面"}, close=True,
    )
    p = tmp_path / "finished.dxf"
    doc.saveas(p)
    result = parse_scene(p)
    assert result.ok
    scene = result.data
    assert scene is not None
    assert len(scene.rooms) == 2, f"expected 2 rooms, got {len(scene.rooms)}"
