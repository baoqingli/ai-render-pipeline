# app/agents/vision/input_adapter.py
"""统一输入适配器：DWG/DXF → 实体扫描；图片 → base64。
消除上游对文件类型的判断逻辑。"""
import base64
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ezdxf


@dataclass
class DrawingSource:
    """统一的图纸数据源。"""
    path: Path
    file_type: str                    # "dxf" | "image"
    doc: Any = None                   # ezdxf Drawing（仅 DXF）
    extent: tuple[float, float, float, float] | None = None  # 世界坐标范围
    base64_data: str = ""             # 图片的 base64（仅图片）
    layer_table: str = ""             # 图层实体统计表（仅 DXF）
    text_annotations: list[dict] = field(default_factory=list)  # 文字标注 [{text, x, y}]


def load_source(file_path: str | Path) -> DrawingSource:
    """根据文件类型加载图纸数据源。"""
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"file not found: {p}")

    suffix = p.suffix.lower()
    if suffix in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
        return _load_image(p)
    if suffix in (".dxf", ".dwg"):
        return _load_dxf(p)
    raise ValueError(f"unsupported: {suffix}")


def _load_image(p: Path) -> DrawingSource:
    data = p.read_bytes()
    return DrawingSource(
        path=p, file_type="image",
        base64_data=base64.b64encode(data).decode(),
    )


def _load_dxf(p: Path) -> DrawingSource:
    doc = ezdxf.readfile(str(p))
    msp = doc.modelspace()
    xs: list[float] = []
    ys: list[float] = []
    anns: list[dict] = []
    for e in msp:
        with contextlib.suppress(Exception):
            if e.dxftype() == "LINE":
                xs += [e.dxf.start.x, e.dxf.end.x]
                ys += [e.dxf.start.y, e.dxf.end.y]
            elif e.dxftype() == "LWPOLYLINE":
                for pt in e.get_points():  # type: ignore[attr-defined]
                    xs.append(pt[0])
                    ys.append(pt[1])
            elif e.dxftype() in ("TEXT", "MTEXT"):
                txt = (e.dxf.text if e.dxftype() == "TEXT" else e.text).strip()  # type: ignore[attr-defined]  # type: ignore[union-attr]  # type: ignore[union-attr]  # type: ignore[union-attr]
                ins = e.dxf.insert
                if txt:
                    anns.append({"text": txt, "x": round(ins.x), "y": round(ins.y)})
    if not xs:
        raise ValueError("图纸无可渲染几何")

    layer_table_lines = []
    from app.tools.cad.inspect import inspect_dxf
    rep = inspect_dxf(str(p))
    for lay in rep.layers[:18]:
        total = (lay.line_count + lay.polyline_count + lay.insert_count
                 + lay.text_count + lay.other_count)
        if total > 0:
            layer_table_lines.append(
                f"  {lay.name}  实体{total}（线{lay.line_count}/多段线{lay.polyline_count}/"
                f"块{lay.insert_count}/文字{lay.text_count}）")

    return DrawingSource(
        path=p, file_type="dxf", doc=doc,
        extent=(min(xs), min(ys), max(xs), max(ys)),
        layer_table="\n".join(layer_table_lines),
        text_annotations=anns,
    )


def contextlib_a():
    return __import__("contextlib").suppress(Exception)


# 供外部使用的 contextlib
