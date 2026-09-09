# tests/unit/test_blender_views.py

from shapely.geometry import Polygon as ShapelyPolygon

from app.models.scene import Room, SceneJSON, Wall
from app.tools.blender.geom import SHRINK, plan_views


def _two_room_scene():
    walls = [Wall(id=f"w{i}", polygon=[[0.0, 0.0], [6000.0, 0.0], [6000.0, 200.0], [0.0, 200.0]])
             for i in range(1)]
    rooms = [Room(id="r1", name="客厅", polygon=[[0.0, 0.0], [4000.0, 0.0],
                                                 [4000.0, 4000.0], [0.0, 4000.0]]),
             Room(id="r2", name="卧室", polygon=[[4000.0, 0.0], [6000.0, 0.0],
                                                 [6000.0, 4000.0], [4000.0, 4000.0]])]
    return SceneJSON(walls=walls, rooms=rooms)


def test_plan_views_two_rooms_iso_plus_interior():
    """有房间 → view_iso（等轴测）+ view_interior（室内透视）共 2 个机位。"""
    poses = plan_views(_two_room_scene())
    ids = [p.view_id for p in poses]
    assert "view_iso" in ids
    assert "view_interior" in ids
    assert len(poses) == 2

    iso = next(p for p in poses if p.view_id == "view_iso")
    # 等轴测相机在场景包围盒外侧上方，z > floor_height
    assert iso.position[2] > 2800.0


def test_plan_views_iso_always_present_no_rooms():
    """无房间（仅墙）→ 仍产出 view_iso，无 view_interior。"""
    scene = SceneJSON(walls=[Wall(id="w1", polygon=[[0.0, 0.0], [6000.0, 0.0],
                                                    [6000.0, 200.0], [0.0, 200.0]])])
    poses = plan_views(scene)
    assert len(poses) == 1
    assert poses[0].view_id == "view_iso"
    # 相机在墙体包围盒外侧上方
    assert poses[0].position[2] > 2800.0


def test_plan_views_small_room_no_interior():
    """房间缩进后为空（<800x800）→ 只有 view_iso，无 view_interior。"""
    scene = SceneJSON(rooms=[Room(id="r1", name="tiny",
                                  polygon=[[0.0, 0.0], [700.0, 0.0],
                                           [700.0, 700.0], [0.0, 700.0]])])
    poses = plan_views(scene)
    ids = [p.view_id for p in poses]
    assert "view_iso" in ids
    assert "view_interior" not in ids


def test_plan_views_concave_room_multipolygon_no_crash():
    """哑铃形房间（走廊 400mm< 2×SHRINK）→ 不崩溃，至少 1 个机位。"""
    neck_room = [[0.0, 0.0], [2000.0, 0.0],
                 [2000.0, 1000.0], [2800.0, 1000.0],
                 [2800.0, 0.0], [4800.0, 0.0],
                 [4800.0, 2400.0], [2800.0, 2400.0],
                 [2800.0, 1400.0], [2000.0, 1400.0],
                 [2000.0, 2400.0], [0.0, 2400.0]]
    eroded = ShapelyPolygon(neck_room).buffer(-SHRINK)
    assert eroded.geom_type == "MultiPolygon"
    scene = SceneJSON(rooms=[Room(id="r1", name="哑铃", polygon=neck_room)])
    poses = plan_views(scene)
    assert len(poses) >= 1
    # 等轴测机位的 z 须高于 floor_height（2800默认）
    iso = next(p for p in poses if p.view_id == "view_iso")
    assert iso.position[2] > 2800.0


def test_plan_views_empty_scene_returns_empty():
    """完全空场景 → 无机位（不崩溃）。"""
    poses = plan_views(SceneJSON())
    assert poses == []
