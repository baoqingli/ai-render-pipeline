"""双线墙配对：平行、间距≤max_thickness、投影重叠——输出中心线+实际厚度。"""
import math

Vec = tuple[float, float]
Seg = tuple[Vec, Vec]
Centerline = tuple[Vec, Vec, float]  # (p1, p2, thickness)


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _unit(v: Vec) -> Vec:
    n = math.hypot(*v) or 1.0
    return (v[0] / n, v[1] / n)


def _line_dist(p: Vec, a: Vec, d: Vec) -> float:
    return abs(_sub(p, a)[0] * d[1] - _sub(p, a)[1] * d[0])


def pair_wall_segments(segments: list[Seg], max_thickness: float = 500.0,
                       default_thickness: float = 200.0) -> list[Centerline]:
    used: set[int] = set()
    out: list[Centerline] = []
    segs = list(segments)
    for i, (a1, a2) in enumerate(segs):
        if i in used:
            continue
        d1 = _unit(_sub(a2, a1))
        L1 = math.hypot(*_sub(a2, a1))
        best = None
        for j in range(i + 1, len(segs)):
            if j in used:
                continue
            b1, b2 = segs[j]
            d2 = _unit(_sub(b2, b1))
            if abs(abs(_dot(d1, d2)) - 1.0) > 1e-6:      # 不平行
                continue
            dists = [_line_dist(p, a1, d1) for p in (b1, b2)]
            if max(dists) > max_thickness or min(dists) < 1.0:  # 太远或共线
                continue
            ts = [_dot(_sub(p, a1), d1) for p in (b1, b2)]
            lo_t, hi_t = max(min(ts), 0.0), min(max(ts), L1)
            if hi_t - lo_t < 0.5 * min(L1, abs(ts[1] - ts[0])):  # 投影重叠不足
                continue
            thickness = (dists[0] + dists[1]) / 2.0
            best = (j, d1, lo_t, hi_t, thickness)
            break
        if best is None:
            out.append((a1, a2, default_thickness))       # 落单：默认厚度
            continue
        j, d1, lo_t, hi_t, thickness = best
        used.add(j)
        n = (-d1[1], d1[0])
        s_dist = _dot(_sub(segs[j][0], a1), n)            # 有符号偏移
        shift = (n[0] * s_dist / 2, n[1] * s_dist / 2)
        c1 = (a1[0] + d1[0] * lo_t + shift[0], a1[1] + d1[1] * lo_t + shift[1])
        c2 = (a1[0] + d1[0] * hi_t + shift[0], a1[1] + d1[1] * hi_t + shift[1])
        out.append((c1, c2, thickness))
    return out
