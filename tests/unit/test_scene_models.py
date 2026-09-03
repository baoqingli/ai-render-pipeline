import pytest
from pydantic import ValidationError

from app.models.scene import Furniture, SceneJSON, Wall


def test_scene_defaults_follow_v2():
    s = SceneJSON()
    assert s.unit == "mm" and s.floor_height == 2800.0 and s.wall_thickness == 200.0
    assert s.walls == [] and s.rooms == []


def test_furniture_source_is_closed_enum():
    Furniture(id="f1", type="sofa", position=[0, 0], size=[100, 50], source="cad")
    with pytest.raises(ValidationError):
        Furniture(id="f2", type="sofa", position=[0, 0], size=[100, 50], source="dream")


def test_wall_polygon_roundtrip():
    w = Wall(id="wall_001", polygon=[[0, 0], [5000, 0], [5000, 200], [0, 200]])
    assert w.polygon[1] == [5000, 0]
