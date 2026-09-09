# tests/unit/test_parse_enhancements.py
"""P0.5 增强单测：HATCH 墙体提取 / 天花网格 union / 开放折线家具。"""
from pathlib import Path

import ezdxf

from app.tools.cad.parse import parse_scene


def _bbox(poly_pts):
    xs = [p[0] for p in poly_pts]
    ys = [p[1] for p in poly_pts]
    return max(xs) - min(xs), max(ys) - min(ys)


def test_hatch_wall_extracted(tmp_path: Path):
    """墙体填充 HATCH（细长条带）→ 真实墙多边形。"""
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    h = msp.add_hatch(dxfattribs={"layer": "P-新砌轻质砌块砖填充"})
    h.paths.add_polyline_path([(0, 0), (2000, 0), (2000, 100), (0, 100)], is_closed=True)
    p = tmp_path / "h.dxf"
    doc.saveas(p)
    scene = parse_scene(p).data
    assert scene is not None
    assert scene.walls, "HATCH 墙体未被提取"
    w, h_ = _bbox(scene.walls[0].polygon)
    assert abs(w - 2000) < 5 and abs(h_ - 100) < 5


def test_hatch_wall_replaces_overlapping_room_edge(tmp_path: Path):
    """房间轮廓边与 HATCH 墙重合时，轮廓近似墙被丢弃，只留真实墙。"""
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    doc.layers.add("P-完成面")
    # 房间 2×3m
    msp.add_lwpolyline([(0, 0), (2000, 0), (2000, 3000), (0, 3000)],
                       dxfattribs={"layer": "P-完成面"}, close=True)
    # 真实墙 HATCH 恰好沿房间底边（厚 100）
    h = msp.add_hatch(dxfattribs={"layer": "P-新砌轻质砌块砖填充"})
    h.paths.add_polyline_path([(-100, -100), (2100, -100), (2100, 0), (-100, 0)],
                              is_closed=True)
    p = tmp_path / "hr.dxf"
    doc.saveas(p)
    scene = parse_scene(p).data
    assert scene is not None
    # 底边位置不应同时存在轮廓近似墙与真实墙（重叠段去重）
    bottoms = [w for w in scene.walls
               if min(pt[1] for pt in w.polygon) < -50]
    assert len(bottoms) <= 1


def test_ceiling_grid_union(tmp_path: Path):
    """同层 ≥12 个共边小矩形（天花网格）→ union 成整区轮廓，不当 12 个房间。"""
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    doc.layers.add("C-天花轮廓线")
    for i in range(12):
        x0 = i * 1000.0
        msp.add_lwpolyline([(x0, 0), (x0 + 1000, 0), (x0 + 1000, 800), (x0, 800)],
                           dxfattribs={"layer": "C-天花轮廓线"}, close=True)
    p = tmp_path / "g.dxf"
    doc.saveas(p)
    scene = parse_scene(p).data
    assert scene is not None
    assert len(scene.rooms) == 1, f"网格应合并为 1 区，got {len(scene.rooms)}"
    w, _ = _bbox(scene.rooms[0].polygon)
    assert abs(w - 12000) < 10                       # 整区外轮廓


def test_open_polyline_furniture(tmp_path: Path):
    """家具图层上的开放折线（dashed 床轮廓）→ 家具提取。"""
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    doc.layers.add("P-固定家具（悬空）")
    msp.add_lwpolyline([(0, 0), (1500, 0), (1500, 2000), (0, 2000)],
                       dxfattribs={"layer": "P-固定家具（悬空）"})
    p = tmp_path / "f.dxf"
    doc.saveas(p)
    scene = parse_scene(p).data
    assert scene is not None
    assert scene.furniture, "开放折线家具未被提取"
    f = scene.furniture[0]
    assert f.measured
    assert abs(f.size[0] - 1500) < 5 and abs(f.size[1] - 2000) < 5


def test_block_wall_extraction_with_rotation(tmp_path: Path):
    """块内墙体轮廓（含旋转插入）→ 真实墙（virtual_entities 对含 HATCH 块静默失败，
    必须走手动矩阵变换遍历——YSJZQT 原始结构墙体的教训）。"""
    doc = ezdxf.new("R2018")
    doc.layers.add("01-原始结构承重墙柱")
    blk = doc.blocks.new(name="WALLBLK")
    blk.add_lwpolyline([(0, 0), (2000, 0), (2000, 100), (0, 100)],
                       dxfattribs={"layer": "01-原始结构承重墙柱"}, close=True)
    msp = doc.modelspace()
    msp.add_blockref("WALLBLK", (5000, 3000), dxfattribs={"rotation": 90.0})
    p = tmp_path / "bw.dxf"
    doc.saveas(p)
    scene = parse_scene(p).data
    assert scene is not None
    assert scene.walls, "块内墙体未被提取"
    w, h = _bbox(scene.walls[0].polygon)
    # 旋转 90° 后 2000 长边落到 y 向
    assert abs(h - 2000) < 10 and abs(w - 100) < 10


def test_furniture_scarcity_note(tmp_path: Path):
    """精装图家具稀少 → notes 提示输入可能为系统图。"""
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    doc.layers.add("P-固定家具（落地）")
    msp.add_lwpolyline([(0, 0), (1200, 0), (1200, 600), (0, 600)],
                       dxfattribs={"layer": "P-固定家具（落地）"}, close=True)
    p = tmp_path / "s.dxf"
    doc.saveas(p)
    result = parse_scene(p)
    scene = result.data
    assert scene is not None and scene.quality is not None
    assert any("系统图" in n for n in scene.quality.notes)
