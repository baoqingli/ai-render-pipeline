# tests/unit/test_blender_views.py
from app.models.scene import Room, SceneJSON, Wall
from app.tools.blender.geom import plan_views


def _two_room_scene():
    walls = [Wall(id=f"w{i}", polygon=[[0.0, 0.0], [6000.0, 0.0], [6000.0, 200.0], [0.0, 200.0]])
             for i in range(1)]
    rooms = [Room(id="r1", name="客厅", polygon=[[0.0, 0.0], [4000.0, 0.0],
                                                 [4000.0, 4000.0], [0.0, 4000.0]]),
             Room(id="r2", name="卧室", polygon=[[4000.0, 0.0], [6000.0, 0.0],
                                                 [6000.0, 4000.0], [4000.0, 4000.0]])]
    return SceneJSON(walls=walls, rooms=rooms)


def test_plan_views_per_room():
    poses = plan_views(_two_room_scene(), per_room=2)
    assert len(poses) == 4
    ids = [p.view_id for p in poses]
    assert ids == ["view_01", "view_02", "view_03", "view_04"]
    for p in poses:
        assert p.position[2] == 1500.0 and p.target[2] == 1200.0
    # 客厅机位在客厅内（x < 4000）
    assert all(p.position[0] < 4000.0 for p in poses[:2])
    assert all(p.position[0] > 4000.0 for p in poses[2:])


def test_plan_views_global_fallback_when_no_rooms():
    scene = SceneJSON(walls=[Wall(id="w1", polygon=[[0.0, 0.0], [6000.0, 0.0],
                                                    [6000.0, 200.0], [0.0, 200.0]])])
    poses = plan_views(scene)
    assert len(poses) == 1
    assert poses[0].view_id == "view_01"
    assert poses[0].position == (3000.0, 100.0, 1500.0)     # 墙 bbox 中心
    assert poses[0].target == (3000.0, 100.0, 1200.0)


def test_plan_views_global_fallback_when_room_too_small():
    """房间收缩 400 后为空（小于 800x800）→ 降级全局。"""
    scene = SceneJSON(rooms=[Room(id="r1", name="tiny",
                                  polygon=[[0.0, 0.0], [700.0, 0.0],
                                           [700.0, 700.0], [0.0, 700.0]])])
    poses = plan_views(scene)
    assert len(poses) == 1 and poses[0].view_id == "view_01"
