"""生成合成户型 DXF 测试语料（离线确定性，Task 3-6 的解析对象）。

用法: uv run python scripts/gen_fixtures.py   # 写入 fixtures/dxf/apartment.dxf
"""
import sys
from pathlib import Path

import ezdxf

T = 200.0  # 墙厚


def _wall_pair(msp, p1, p2, t=T):
    """一条墙边 → 两条相距 t 的平行线（法向偏移 ±t/2）"""
    (x1, y1), (x2, y2) = p1, p2
    dx, dy = x2 - x1, y2 - y1
    L = (dx * dx + dy * dy) ** 0.5
    nx, ny = -dy / L * t / 2, dx / L * t / 2
    for s in (1, -1):
        msp.add_line(
            (x1 + s * nx, y1 + s * ny), (x2 + s * nx, y2 + s * ny), dxfattribs={"layer": "WALL"}
        )


def make_apartment_dxf(path) -> Path:
    doc = ezdxf.new("R2018", setup=True)
    msp = doc.modelspace()
    for layer in ["WALL", "DOOR", "WINDOW", "FURN", "NOTE", "DIM", "GARBAGE"]:
        doc.layers.add(layer)
    # 外墙矩形 4 边 + 内墙 x=4000 于门洞 y=1500 处分 2 段 = 6 条墙边 × 2 线
    _wall_pair(msp, (0, 0), (6000, 0))
    _wall_pair(msp, (6000, 0), (6000, 4000))
    _wall_pair(msp, (6000, 4000), (0, 4000))
    _wall_pair(msp, (0, 4000), (0, 0))
    _wall_pair(msp, (4000, 0), (4000, 1500))  # 内墙下段（至门洞）
    _wall_pair(msp, (4000, 1500), (4000, 4000))  # 内墙上段
    # 门窗家具块（门 M_门_900 内墙、窗 C_1500 顶外墙、沙发 FURN）
    for name, layer, pos in [("M_门_900", "DOOR", (4000, 1500)),
                             ("C_1500", "WINDOW", (3000, 4000)),
                             ("sofa", "FURN", (2000, 800))]:
        doc.blocks.new(name=name)
        msp.add_blockref(name, pos, dxfattribs={"layer": layer})
    for text, pos in [("客厅", (2000, 2000)), ("卧室", (5000, 2000)), ("层高2800", (0, -500))]:
        msp.add_text(text, dxfattribs={"layer": "NOTE"}).set_placement(pos)
    # 垃圾：一个线性标注 + 一条随机斜线
    dim = msp.add_linear_dim(
        base=(3000, -300), p1=(0, 0), p2=(6000, 0), dxfattribs={"layer": "DIM"}
    )
    dim.render()
    msp.add_line((6500, -200), (7000, 300), dxfattribs={"layer": "GARBAGE"})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("fixtures/dxf/apartment.dxf")
    print(f"wrote {make_apartment_dxf(out)}")
