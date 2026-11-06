"""MATCH_CAMERA: fit a correction that brings camera B's colour to camera A's, from a frame pair of the same subject
(docs/decisions.md ADR-23). Pure computation on two PNG frames that ffmpeg-skill/look extracted: nothing here starts
a process or reads a video.

  frames      8- or 16-bit RGB(A) PNG, decoded here (zlib + the five PNG filters); the alpha channel, if any, ignored
  samples     whole frame: the mean colour of every 8x8-pixel block (both frames the same size, same framing);
              stated patches: the mean colour of each normalised rectangle, one in each frame
  models      curves: one monotone curve per channel (isotonic regression of A on B), linear outside the sampled
              range; matrix: a 3x3 matrix plus offset (least squares), which also corrects cross-channel mixing
  LUT         the fitted map evaluated on a cube grid (red fastest, clamped to [0, 1]); applied by ffmpeg-skill --lut
  residual    CIEDE2000 between the samples of two frames, RGB read as BT.709 primaries with the BT.1886 EOTF
              (gamma 2.4) and D65 white: an observed number, never a judgement"""
from __future__ import annotations

import math
import struct
import zlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import ColorError

MODELS = ("curves", "matrix")
ANALYSIS_WIDTH = 320          # look.py --width: every frame is analysed at this width (height by aspect)
BLOCK = 8                     # whole-frame sampling block, pixels of the analysis frame
CURVE_BINS = 64
RGB = List[float]


# ---- PNG
def read_png(data: bytes) -> Tuple[int, int, List[RGB]]:
    """(width, height, pixels row-major as [r, g, b] in 0..1). Non-interlaced 8/16-bit RGB or RGBA only, which is
    what ffmpeg writes for look.py's frames."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ColorError("VALIDATION_ERROR", "frame is not a PNG", {"reason": "frame_not_png"})
    pos, idat = 8, []
    width = height = depth = ctype = interlace = None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if width is None or ctype not in (2, 6) or depth not in (8, 16) or interlace != 0:
        raise ColorError("VALIDATION_ERROR", f"frame PNG layout not supported (colour type {ctype}, depth {depth}, interlace {interlace})",
                         {"reason": "frame_png_layout"})
    channels = 3 if ctype == 2 else 4
    bpp = channels * depth // 8
    stride = width * bpp
    raw = zlib.decompress(b"".join(idat))
    prev = bytearray(stride)
    rows: List[bytearray] = []
    for y in range(height):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else (b if pb <= pc else c))) & 255
        elif f != 0:
            raise ColorError("VALIDATION_ERROR", f"frame PNG has unknown filter {f}", {"reason": "frame_png_filter"})
        rows.append(line)
        prev = line
    pixels: List[RGB] = []
    scale = 255.0 if depth == 8 else 65535.0
    for line in rows:
        if depth == 8:
            for x in range(width):
                o = x * bpp
                pixels.append([line[o] / scale, line[o + 1] / scale, line[o + 2] / scale])
        else:
            for x in range(width):
                o = x * bpp
                pixels.append([((line[o] << 8) | line[o + 1]) / scale, ((line[o + 2] << 8) | line[o + 3]) / scale,
                               ((line[o + 4] << 8) | line[o + 5]) / scale])
    return width, height, pixels


# ---- sampling
def _mean(pixels: List[RGB], width: int, x0: int, y0: int, x1: int, y1: int) -> RGB:
    acc = [0.0, 0.0, 0.0]
    n = 0
    for y in range(y0, y1):
        row = y * width
        for x in range(x0, x1):
            p = pixels[row + x]
            acc[0] += p[0]
            acc[1] += p[1]
            acc[2] += p[2]
            n += 1
    return [c / n for c in acc]


def block_means(frame: Tuple[int, int, List[RGB]], block: int = BLOCK) -> List[RGB]:
    w, h, px = frame
    return [_mean(px, w, x, y, min(x + block, w), min(y + block, h)) for y in range(0, h - block + 1, block) for x in range(0, w - block + 1, block)]


def patch_means(frame: Tuple[int, int, List[RGB]], rects: Sequence[Sequence[float]]) -> List[RGB]:
    w, h, px = frame
    out = []
    for x, y, rw, rh in rects:
        x0, y0 = int(round(x * w)), int(round(y * h))
        x1, y1 = max(x0 + 1, int(round((x + rw) * w))), max(y0 + 1, int(round((y + rh) * h)))
        out.append(_mean(px, w, x0, y0, min(x1, w), min(y1, h)))
    return out


def samples(reference: Tuple[int, int, List[RGB]], target: Tuple[int, int, List[RGB]], patches: Optional[List[Dict[str, Any]]]) -> Tuple[List[RGB], List[RGB], str]:
    """(reference samples, target samples, how they were taken), index-aligned."""
    if patches:
        return (patch_means(reference, [p["reference"] for p in patches]), patch_means(target, [p["input"] for p in patches]),
                f"{len(patches)} stated patches")
    if reference[:2] != target[:2]:
        raise ColorError("INVALID_INPUT", f"the reference frame is {reference[0]}x{reference[1]} and the input frame {target[0]}x{target[1]} at the "
                         f"analysis width: whole-frame matching needs the same framing; state patches instead",
                         {"reason": "frame_size_mismatch", "reference": list(reference[:2]), "input": list(target[:2])})
    return block_means(reference), block_means(target), f"whole frame, {BLOCK}x{BLOCK}-pixel blocks at {ANALYSIS_WIDTH} px wide"


# ---- models
def _isotonic(xs: List[float], ys: List[float], ws: List[float]) -> List[float]:
    """Pool-adjacent-violators: the non-decreasing fit of ys (ordered by xs) with weights ws."""
    blocks: List[List[float]] = []    # [mean, weight, count]
    for y, w in zip(ys, ws):
        blocks.append([y, w, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            m2, w2, c2 = blocks.pop()
            m1, w1, c1 = blocks.pop()
            blocks.append([(m1 * w1 + m2 * w2) / (w1 + w2), w1 + w2, c1 + c2])
    out: List[float] = []
    for m, _, c in blocks:
        out += [m] * int(c)
    return out


def _linear_fit(xs: List[float], ys: List[float]) -> Tuple[float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-12:
        return 1.0, my - mx
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return slope, my - slope * mx


def fit_curve(b: List[float], a: List[float]) -> Dict[str, Any]:
    """One channel: knots (x, y) of the monotone curve through the sampled range, plus the least-squares line used
    beyond it."""
    pairs = sorted(zip(b, a))
    bins: List[List[float]] = []
    lo, hi = pairs[0][0], pairs[-1][0]
    width = (hi - lo) / CURVE_BINS or 1.0
    for x, y in pairs:
        k = min(CURVE_BINS - 1, int((x - lo) / width))
        if bins and bins[-1][3] == k:
            bins[-1][0] += x
            bins[-1][1] += y
            bins[-1][2] += 1
        else:
            bins.append([x, y, 1, k])
    xs = [s[0] / s[2] for s in bins]
    ys = _isotonic(xs, [s[1] / s[2] for s in bins], [s[2] for s in bins])
    slope, offset = _linear_fit([p[0] for p in pairs], [p[1] for p in pairs])
    return {"knots": [[round(x, 6), round(y, 6)] for x, y in zip(xs, ys)], "line": [round(slope, 6), round(offset, 6)]}


def eval_curve(curve: Dict[str, Any], x: float) -> float:
    knots, (slope, offset) = curve["knots"], curve["line"]
    if len(knots) == 1:
        return slope * x + offset
    if x <= knots[0][0]:
        return knots[0][1] + slope * (x - knots[0][0])
    if x >= knots[-1][0]:
        return knots[-1][1] + slope * (x - knots[-1][0])
    lo, hi = 0, len(knots) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if knots[mid][0] <= x:
            lo = mid
        else:
            hi = mid
    (x0, y0), (x1, y1) = knots[lo], knots[hi]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 > x0 else y0


def _solve(m: List[List[float]], v: List[float]) -> List[float]:
    n = len(v)
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-12:
            raise ColorError("INVALID_INPUT", "the samples do not determine a matrix (too few distinct colours); use model curves or more patches",
                             {"reason": "matrix_singular"})
        a[c], a[p] = a[p], a[c]
        for r in range(n):
            if r != c:
                f = a[r][c] / a[c][c]
                a[r] = [x - f * y for x, y in zip(a[r], a[c])]
    return [a[i][n] / a[i][i] for i in range(n)]


def fit_matrix(b: List[RGB], a: List[RGB]) -> List[List[float]]:
    """3 rows of [m_r, m_g, m_b, offset]: least squares a ~ M b + o."""
    xs = [p + [1.0] for p in b]
    ata = [[sum(x[i] * x[j] for x in xs) for j in range(4)] for i in range(4)]
    return [[round(c, 6) for c in _solve(ata, [sum(x[i] * t[ch] for x, t in zip(xs, a)) for i in range(4)])] for ch in range(3)]


def fit(model: str, reference: List[RGB], target: List[RGB]) -> Dict[str, Any]:
    if model == "matrix":
        if len(target) < 4:
            raise ColorError("INVALID_INPUT", "model matrix needs at least 4 samples", {"reason": "too_few_samples", "samples": len(target)})
        return {"model": "matrix", "matrix": fit_matrix(target, reference)}
    if len(target) < 2:
        raise ColorError("INVALID_INPUT", "model curves needs at least 2 samples", {"reason": "too_few_samples", "samples": len(target)})
    return {"model": "curves", "curves": [fit_curve([t[c] for t in target], [r[c] for r in reference]) for c in range(3)]}


def apply_fit(f: Dict[str, Any], rgb: Sequence[float]) -> RGB:
    if f["model"] == "matrix":
        return [min(1.0, max(0.0, row[0] * rgb[0] + row[1] * rgb[1] + row[2] * rgb[2] + row[3])) for row in f["matrix"]]
    return [min(1.0, max(0.0, eval_curve(f["curves"][c], rgb[c]))) for c in range(3)]


def lut_values(f: Dict[str, Any], n: int) -> List[float]:
    from .ocio import grid
    g = grid(n)
    out: List[float] = []
    for i in range(0, len(g), 3):
        out += [float(f"{v:.6f}") for v in apply_fit(f, g[i:i + 3])]
    return out


# ---- CIEDE2000
_M709 = ((0.4123908, 0.3575843, 0.1804808), (0.2126390, 0.7151687, 0.0721923), (0.0193308, 0.1191948, 0.9505322))
_WHITE = tuple(sum(row) for row in _M709)     # D65 as the matrix's own white (RGB 1,1,1), so neutral greys have a* = b* = 0


def lab(rgb: Sequence[float]) -> Tuple[float, float, float]:
    lin = [max(0.0, c) ** 2.4 for c in rgb]
    xyz = [sum(_M709[i][j] * lin[j] for j in range(3)) / _WHITE[i] for i in range(3)]
    f = [v ** (1 / 3) if v > (6 / 29) ** 3 else v / (3 * (6 / 29) ** 2) + 4 / 29 for v in xyz]
    return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])


def delta_e2000(lab1: Sequence[float], lab2: Sequence[float]) -> float:
    """CIEDE2000 (Sharma, Wu, Dalal 2005), kL = kC = kH = 1."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    cm = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt(cm ** 7 / (cm ** 7 + 25 ** 7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0
    dl, dc = L2 - L1, c2p - c1p
    if c1p * c2p == 0:
        dh = 0.0
    elif abs(h2p - h1p) <= 180:
        dh = h2p - h1p
    else:
        dh = h2p - h1p - 360 if h2p > h1p else h2p - h1p + 360
    dH = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dh / 2))
    lm, cmp_ = (L1 + L2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        hm = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hm = (h1p + h2p) / 2
    else:
        hm = (h1p + h2p + 360) / 2 if h1p + h2p < 360 else (h1p + h2p - 360) / 2
    t = 1 - 0.17 * math.cos(math.radians(hm - 30)) + 0.24 * math.cos(math.radians(2 * hm)) + 0.32 * math.cos(math.radians(3 * hm + 6)) \
        - 0.20 * math.cos(math.radians(4 * hm - 63))
    d_theta = 30 * math.exp(-(((hm - 275) / 25) ** 2))
    rc = 2 * math.sqrt(cmp_ ** 7 / (cmp_ ** 7 + 25 ** 7))
    sl = 1 + 0.015 * (lm - 50) ** 2 / math.sqrt(20 + (lm - 50) ** 2)
    sc, sh = 1 + 0.045 * cmp_, 1 + 0.015 * cmp_ * t
    rt = -math.sin(math.radians(2 * d_theta)) * rc
    return math.sqrt((dl / sl) ** 2 + (dc / sc) ** 2 + (dH / sh) ** 2 + rt * (dc / sc) * (dH / sh))


def residual(reference: List[RGB], target: List[RGB]) -> Dict[str, Any]:
    d = sorted(delta_e2000(lab(r), lab(t)) for r, t in zip(reference, target))
    return {"metric": "CIEDE2000", "samples": len(d), "mean": round(sum(d) / len(d), 4), "p95": round(d[min(len(d) - 1, int(0.95 * len(d)))], 4),
            "max": round(d[-1], 4)}
