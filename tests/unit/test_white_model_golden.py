# tests/unit/test_white_model_golden.py
"""P0 golden：解析质量指标区间锁定（docs/white-model-fix-plan-2026-09.md §4.5-3）。

只断言指标区间，不断言像素/精确数量——合法优化不破坏测试。
任何启发式修改必须保持本文件全绿。
"""
from pathlib import Path

import pytest

from app.tools.cad.parse import parse_scene
from scripts.gen_fixtures import make_apartment_dxf

CONVERTED = Path("fixtures/cad/_converted/01-平面系统图.31cd742821d4c1dc.dxf")


def test_golden_apartment_quality(tmp_path: Path):
    scene = parse_scene(make_apartment_dxf(tmp_path / "a.dxf")).data
    q = scene.quality
    assert 2 <= q.rooms <= 3                          # 客厅 + 卧室
    assert q.tiling_ratio >= 0.6                      # 房间铺满墙网凸包
    assert q.openings_attached_ratio == 1.0           # 门窗全挂墙
    assert q.wall_segments >= 4
    assert q.furniture_count >= 1


@pytest.mark.skipif(not CONVERTED.exists(), reason="ODA 转换缓存不存在（离线环境跳过）")
def test_golden_real_fitout_quality():
    """真实精装图（01-平面系统图）：房间源=天花轮廓线。"""
    scene = parse_scene(CONVERTED).data
    q = scene.quality
    assert 3 <= q.rooms <= 14                         # 尺寸墙细分房间（12 实测）
    assert q.tiling_ratio >= 0.6
    assert q.wall_segments >= 8                       # 房间边 + 未吸附条带
    assert q.openings_attached_ratio >= 0.8           # 门窗基本挂墙
    assert q.furniture_count >= 5
    sizes = {tuple(round(v) for v in f.size) for f in scene.furniture}
    assert len(sizes) >= 4    # 尺寸非单一默认值（9-7 全默认 113 件不得回归）
    assert any(f.measured for f in scene.furniture)   # 存在实测尺寸家具
    assert any(abs(f.rotation) > 1e-6 for f in scene.furniture)  # 朝向不再恒 0
