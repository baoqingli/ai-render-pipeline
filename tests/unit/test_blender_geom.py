# tests/unit/test_blender_geom.py
import pytest

from app.models.scene import Wall
from app.tools.blender.geom import Box, normalize_scene, scene_offset, wall_box


def _scene_with_offset():
    from app.models.scene import SceneJSON
    return SceneJSON(walls=[Wall(id="w1", polygon=[[1000.0, 2000.0], [7000.0, 2000.0],
                                                    [7000.0, 2200.0], [1000.0, 2200.0]])])


def test_scene_offset_and_normalize():
    s = _scene_with_offset()
    assert scene_offset(s) == (1000.0, 2000.0)
    n = normalize_scene(s)
    assert n.walls[0].polygon[0] == [0.0, 0.0]
    assert s.walls[0].polygon[0] == [1000.0, 2000.0]   # 入参不变


def test_normalize_shifts_openings_and_furniture():
    from app.models.scene import Door, Furniture, SceneJSON, Wall
    s = SceneJSON(walls=[Wall(id="w1", polygon=[[1000.0, 0.0], [7000.0, 0.0],
                                                [7000.0, 200.0], [1000.0, 200.0]])],
                  doors=[Door(id="d1", position=[4000.0, 100.0], width=900.0)],
                  furniture=[Furniture(id="f1", type="sofa", position=[2000.0, 500.0],
                                       size=[1000.0, 500.0, 500.0], source="cad")])
    n = normalize_scene(s)
    assert n.doors[0].position == [3000.0, 100.0]
    assert n.furniture[0].position == [1000.0, 500.0]


def test_wall_box_axis_aligned():
    box = wall_box(Wall(id="w1", polygon=[[0.0, 100.0], [6000.0, 100.0],
                                          [6000.0, -100.0], [0.0, -100.0]]), floor_height=2800.0)
    assert isinstance(box, Box)
    assert box.center == (3000.0, 0.0, 1400.0)
    assert box.size == (6000.0, 200.0, 2800.0)
    assert box.rot_z == pytest.approx(0.0)


def test_wall_box_rotated_90():
    box = wall_box(Wall(id="w1", polygon=[[-100.0, 0.0], [100.0, 0.0],
                                          [100.0, 4000.0], [-100.0, 4000.0]]), floor_height=2800.0)
    assert box.size == (4000.0, 200.0, 2800.0)
    assert box.rot_z == pytest.approx(1.5707963267948966)   # 90°
    assert box.center == (0.0, 2000.0, 1400.0)
