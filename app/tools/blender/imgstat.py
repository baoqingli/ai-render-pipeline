# app/tools/blender/imgstat.py
"""PNG 灰度统计（纯标准库，无 PIL 依赖）——渲染结果自动校验用。

支持 8-bit、非隔行、灰度(0)/RGB(2)/RGBA(6)；行滤波 0-4 全覆盖。
"""
import struct
import zlib
from pathlib import Path


def decode_png(path: str | Path) -> tuple[int, int, int, bytes]:
    data = Path(path).read_bytes()
    pos = 8
    idat = b""
    w = h = 0
    ct = 2
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, _bd, ct = struct.unpack(">IIBB", chunk[:10])
        elif typ == b"IDAT":
            idat += chunk
        pos += 12 + ln
    if w <= 0 or h <= 0:
        raise ValueError(f"invalid png: {path}")
    raw = zlib.decompress(idat)
    bpp = {0: 1, 2: 3, 4: 2, 6: 4}.get(ct, 4)
    stride = w * bpp
    out = bytearray()
    prev = bytearray(stride)
    p = 0
    for _y in range(h):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                line[i] = (line[i] + (a if (pa <= pb and pa <= pc)
                                      else (b if pb <= pc else c))) & 255
        out += line
        prev = line
    return w, h, bpp, bytes(out)


def gray_stats(path: str | Path) -> dict[str, float]:
    """灰度统计：min/max/mean/std（RGB 取亮度均值）。"""
    w, h, bpp, pix = decode_png(path)
    n = 0
    total = 0
    lo, hi = 255, 0
    sq = 0
    for y in range(0, h, 2):
        base = y * w * bpp
        for x in range(0, w, 2):
            o = base + x * bpp
            g = (pix[o] + pix[o + 1] + pix[o + min(bpp - 1, 2)]) // 3
            n += 1
            total += g
            sq += g * g
            lo = min(lo, g)
            hi = max(hi, g)
    if n == 0:
        return {"min": 0.0, "max": 0.0, "mean": 0.0, "std": 0.0}
    mean = total / n
    var = sq / n - mean * mean
    return {"min": float(lo), "max": float(hi),
            "mean": round(mean, 1), "std": round(max(var, 0.0) ** 0.5, 1)}
