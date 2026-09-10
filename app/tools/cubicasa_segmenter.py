# app/tools/cubicasa_segmenter.py
"""CubiCasa5K 预训练分割模型封装：户型图片 → 墙/房间/门/窗多边形（像素坐标）。

架构定位（docs/agent-optimization-plan-2026-09.md 路径 B）：
  几何来自本模型（预训练多任务分割，44 类 = 21 房间 + 12 图标 + 11 墙边界），
  语义命名/定标来自 VLM 与尺寸文字——VLM 不再输出几何坐标。
模型：hg_furukawa_original + model_best_val_loss_var.pkl（209MB，官方权重）。
依赖：third_party/cubicasa5k（克隆）+ torch(CUDA) + pillow。
"""
import sys
from pathlib import Path
from typing import Any

import numpy as np

_THIRD_PARTY = Path(__file__).resolve().parents[2] / "third_party" / "cubicasa5k"
WEIGHTS = _THIRD_PARTY / "model_best_val_loss_var.pkl"

ROOM_CLS = ["Background", "Outdoor", "Wall", "Kitchen", "Living Room", "Bedroom",
            "Bath", "Hallway", "Railing", "Storage", "Garage", "Other rooms"]
ICON_CLS = ["Empty", "Window", "Door", "Closet", "Electr. Appl.", "Toilet",
            "Sink", "Sauna bench", "Fire Place", "Bathtub", "Chimney"]

_model_cache: dict[str, Any] = {}


def _load_model(device: str = "cuda"):
    """加载 CubiCasa5K 多任务模型（进程内缓存；需 8GB 显存以内）。"""
    if "m" in _model_cache:
        return _model_cache["m"]
    if not WEIGHTS.exists():
        raise FileNotFoundError(
            f"CubiCasa5K 权重缺失: {WEIGHTS}（gdown 1gRB7ez1e4H7a9Y09lLqRuna0luZO5VRK）")
    for p in (str(_THIRD_PARTY),):
        if p not in sys.path:
            sys.path.insert(0, p)
    import torch
    from floortrans.models.hg_furukawa_original import hg_furukawa_original

    model = hg_furukawa_original(n_classes=44)   # 跳过 MPII init_weights（直接载官方权重）
    ckpt = torch.load(WEIGHTS, map_location="cpu", weights_only=False)
    model.load_state_dict(ckpt.get("model_state", ckpt), strict=True)
    model.eval()
    if device == "cuda" and torch.cuda.is_available():
        model = model.cuda()
    _model_cache["m"] = model
    return model


def segment(png_path: str, size: int = 256, device: str = "cuda") -> dict:
    """户型图 → {rooms, icons, walls}（256×256 像素坐标的类别分割图 + 类别统计）。

    返回：
      rooms: (size,size) int 数组，ROOM_CLS 索引
      icons: (size,size) int 数组，ICON_CLS 索引
      room_classes / icon_classes: 出现的类别索引列表
    """
    import torch
    from PIL import Image

    model = _load_model(device)
    img = Image.open(png_path).convert("RGB").resize((size, size))
    x = torch.from_numpy(np.array(img)).permute(2, 0, 1).unsqueeze(0).float() / 255.0
    if device == "cuda" and torch.cuda.is_available():
        x = x.cuda()
    with torch.no_grad():
        pred = model(x)
    arr = pred.data.cpu().numpy()[0].transpose(1, 2, 0)   # (H, W, 44)
    rooms = np.argmax(arr[:, :, 21:33], axis=2).astype(np.int32)
    icons = np.argmax(arr[:, :, 33:44], axis=2).astype(np.int32)
    return {
        "rooms": rooms,
        "icons": icons,
        "room_classes": sorted(set(rooms.flatten().tolist())),
        "icon_classes": sorted(set(icons.flatten().tolist())),
        "size": size,
    }


def room_polygons(seg: dict, min_area_px: int = 80) -> list[dict]:
    """房间分割 → 外接矩形多边形（像素坐标，按类别）。"""
    rooms = seg["rooms"]
    out = []
    for cls_idx in seg["room_classes"]:
        if cls_idx == 0:
            continue
        mask = rooms == cls_idx
        ys, xs = np.nonzero(mask)
        if len(xs) < min_area_px:
            continue
        out.append({
            "type": ROOM_CLS[cls_idx],
            "bbox_px": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
            "area_px": len(xs),
        })
    return out


def icon_boxes(seg: dict, min_area_px: int = 8) -> list[dict]:
    """图标分割（门/窗/洁具）→ 连通域外接框（像素坐标）。"""

    icons = seg["icons"]
    out = []
    for cls_idx in seg["icon_classes"]:
        if cls_idx == 0:
            continue
        mask = icons == cls_idx
        for bbox, area in connected_components_bool(mask, min_area_px):
            out.append({"type": ICON_CLS[cls_idx], "bbox_px": bbox, "area_px": area})
    return out


# ── 无 scipy 的连通域（布尔 numpy 数组版） ─────────────────────────────────────

def connected_components_bool(mask: np.ndarray, min_area: int):
    """8 邻域连通域 → [(bbox(x0,y0,x1,y1), area)]。"""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    comps = []
    ys, xs = np.nonzero(mask)
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if seen[sy, sx]:
            continue
        stack = [(sy, sx)]
        seen[sy, sx] = True
        x0 = x1 = sx
        y0 = y1 = sy
        area = 0
        while stack:
            y, x = stack.pop()
            area += 1
            if x < x0: x0 = x
            if x > x1: x1 = x
            if y < y0: y0 = y
            if y > y1: y1 = y
            yy0, yy1 = max(0, y - 1), min(h, y + 2)
            xx0, xx1 = max(0, x - 1), min(w, x + 2)
            sub = mask[yy0:yy1, xx0:xx1] & ~seen[yy0:yy1, xx0:xx1]
            for ny, nx in zip(*np.nonzero(sub)):
                seen[yy0 + ny, xx0 + nx] = True
                stack.append((yy0 + ny, xx0 + nx))
        if area >= min_area:
            comps.append(((int(x0), int(y0), int(x1), int(y1)), int(area)))
    return comps
