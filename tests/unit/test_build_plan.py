# tests/unit/test_build_plan.py
from app.models.build_plan import BuildPlan
from app.models.scene import SceneJSON, Wall
from app.tools.blender.geom import build_plan


def _scene():
    return SceneJSON(walls=[Wall(id="w1", polygon=[[1000.0, 100.0], [7000.0, 100.0],
                                                    [7000.0, -100.0], [1000.0, -100.0]])])


def test_build_plan_assembles_all_kinds():
    plan = build_plan(_scene(), output_dir="experiments/model/x")
    assert isinstance(plan, BuildPlan)
    kinds = {b.kind for b in plan.boxes}
    assert kinds == {"wall", "floor"}                    # 无家具/房间 → wall+floor
    assert plan.floor_height == 2800.0
    assert plan.cameras and plan.cameras[0].view_id == "view_01"   # 无房间→全局降级
    floor = next(b for b in plan.boxes if b.kind == "floor")
    assert floor.center[2] == -50.0 and floor.size[2] == 100.0
    # 归一化后墙 x 从 0 起；地板外扩 500 → 中心 (3000, 0)
    wall = next(b for b in plan.boxes if b.kind == "wall")
    assert wall.center[0] == pytest_approx(3000.0)


def pytest_approx(v):
    import pytest
    return pytest.approx(v)


def test_build_plan_json_roundtrip():
    import json
    plan = build_plan(_scene(), output_dir="x")
    data = json.loads(plan.model_dump_json())
    assert data["passes"] == ["depth", "lineart", "white"]
    assert BuildPlan.model_validate(data) == plan
