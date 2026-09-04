# tests/unit/test_blender_openings.py
import pytest

from app.models.scene import Door, SceneJSON, Wall, Window
from app.tools.blender.geom import Box, Opening, project_opening, segment_wall


def _wall_scene_with(door_at, door_w=900.0, window_at=None, window_w=1500.0):
    """6000x200 墙 @ y[-100,100]，x[0,6000]，中心 (3000,0)"""
    walls = [Wall(id="w1", polygon=[[0.0, 100.0], [6000.0, 100.0],
                                    [6000.0, -100.0], [0.0, -100.0]])]
    doors = [Door(id="d1", position=door_at, width=door_w)] if door_at else []
    windows = [Window(id="x1", position=window_at, width=window_w)] if window_at else []
    return SceneJSON(walls=walls, doors=doors, windows=windows, floor_height=2800.0)


BOX = Box(center=(3000.0, 0.0, 1400.0), size=(6000.0, 200.0, 2800.0), rot_z=0.0)


def test_project_opening_to_local_u():
    o = project_opening(BOX, (4500.0, 50.0), 900.0, 0.0, 2100.0)
    assert o.u == pytest.approx(1500.0)        # 4500 - 中心 3000
    assert (o.z_bot, o.z_top) == (0.0, 2100.0)


def test_segment_wall_no_openings_is_single_box():
    segs = segment_wall(BOX, [])
    assert len(segs) == 1 and segs[0] == BOX


def test_segment_wall_with_door():
    o = Opening(u=1500.0, width=900.0, z_bot=0.0, z_top=2100.0)   # 洞区间 u[1050,1950]
    segs = segment_wall(BOX, [o])
    # 两段全高 + 一段过梁（u 1050..1950, z 2100..2800）
    fulls = [s for s in segs if s.size[2] == 2800.0]
    lintel = [s for s in segs if s.size[2] == pytest.approx(700.0)]
    assert len(fulls) == 2 and len(lintel) == 1
    # 洞前全高段 [-3000,1050]→4050@2025；洞后 [1950,3000]→1050@5475
    assert fulls[0].size[0] == pytest.approx(4050.0) and fulls[0].center[0] == pytest.approx(2025.0)
    assert fulls[1].size[0] == pytest.approx(1050.0) and fulls[1].center[0] == pytest.approx(5475.0)
    assert lintel[0].center[2] == pytest.approx(2450.0)            # (2100+2800)/2
    assert lintel[0].size[0] == pytest.approx(900.0)


def test_segment_wall_with_window():
    o = Opening(u=0.0, width=1500.0, z_bot=900.0, z_top=2100.0)    # 窗 u[-750,750]
    segs = segment_wall(BOX, [o])
    fulls = [s for s in segs if s.size[2] == 2800.0]
    sill = [s for s in segs if s.size[2] == pytest.approx(900.0)]      # 0..900
    header = [s for s in segs if s.size[2] == pytest.approx(700.0)]    # 2100..2800
    assert len(fulls) == 2 and len(sill) == 1 and len(header) == 1
    assert sill[0].center[2] == pytest.approx(450.0)
    assert sill[0].size[0] == pytest.approx(1500.0) and header[0].size[0] == pytest.approx(1500.0)


def test_segment_wall_overlapping_openings_merged():
    """两个门洞重叠/相邻 → 合并成一个洞区间再分段，不产生零厚碎片。"""
    segs = segment_wall(BOX, [Opening(u=1000.0, width=900.0, z_bot=0.0, z_top=2100.0),
                              Opening(u=1600.0, width=900.0, z_bot=0.0, z_top=2100.0)])
    lintels = [s for s in segs if s.size[2] == pytest.approx(700.0)]
    assert len(lintels) == 1                      # 合并区间 u[550,2050] 一段过梁
    assert lintels[0].size[0] == pytest.approx(1500.0)
