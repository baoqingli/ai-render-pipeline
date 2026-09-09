# app/agents/vision/dimension_chain.py
"""DIMENSION 实体 → 尺寸标注数据（精确墙体坐标推算的基础数据）。"""
import contextlib
from dataclasses import dataclass

import ezdxf


@dataclass
class DimEntry:
    measure: int                # 标注值 (mm)
    text_x: float               # 文字标注 X 坐标
    text_y: float               # 文字标注 Y 坐标
    angle: float                # 标注方向角度
    layer: str


def extract_dimensions(dxf_path: str) -> list[DimEntry]:
    """从 DXF 提取全部 DIMENSION 实体的测量值和位置。"""
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    dims: list[DimEntry] = []
    for e in msp.query("DIMENSION"):
        try:
            m = abs(e.get_measurement() or 0)  # type: ignore[attr-defined,union-attr]
            if m < 1:
                continue
            tp = e.dxf.text_midpoint
            angle = 0.0
            with contextlib.suppress(Exception):
                angle = e.dxf.angle
            dims.append(DimEntry(
                measure=round(m), angle=round(angle, 1),
                text_x=round(tp.x), text_y=round(tp.y),
                layer=e.dxf.layer,
            ))
        except Exception:  # noqa: BLE001
            continue
    return dims


def large_dimensions(dims: list[DimEntry], min_mm: int = 500) -> list[DimEntry]:
    """筛选 ≥min_mm 的大尺寸标注（墙体/房间级）。"""
    return sorted([d for d in dims if d.measure >= min_mm],
                  key=lambda d: -d.measure)
