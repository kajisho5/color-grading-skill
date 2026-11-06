"""LEGALIZE: soft-clip a picture into broadcast-legal levels, as a 3D LUT applied by ffmpeg-skill/color --lut
(docs/decisions.md ADR-24). Pure computation: the LUT depends only on the profile and the knee.

ffmpeg-skill's --lut (3.1.6 on) converts the frame to float RGB with the BT.709 matrix, where super-whites and
sub-blacks are values above 1 and below 0, applies the LUT, and converts back to limited-range 8-bit Y'CbCr. The LUT
declares a domain wider than [0, 1] (DOMAIN_MIN / DOMAIN_MAX) so those values reach it, and maps every channel
through a soft clip that never leaves the profile's RGB limits:

  bt709      ITU-R BT.709 / BT.1886 legal range: RGB 0..1, i.e. 8-bit luma 16-235 and chroma 16-240
             (10-bit 64-940 / 64-960; ffmpeg-skill's --lut writes 8 bits)
  ebu-r103   EBU R 103 (2020) tolerance: RGB components -5 %..105 % and luma -1 %..103 %; RGB is limited to
             -1 %..103 %, which keeps both (luma is a weighted mean of R, G, B)

Each channel is unchanged up to the knee and approaches the upper limit with a tanh shoulder above it (slope 1 at
the knee, never reaching the limit). Below 0 the clip is hard: lut3d clamps its input there (see DOMAIN)."""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from .ocio import cube_text, grid

PROFILES: Dict[str, Dict[str, Any]] = {
    "bt709": {"rgb": (0.0, 1.0), "reference": "ITU-R BT.709 legal range (8-bit luma 16-235, chroma 16-240; 10-bit 64-940 / 64-960)"},
    "ebu-r103": {"rgb": (-0.01, 1.03), "reference": "EBU R 103 (2020): RGB -5 %..105 %, luma -1 %..103 %; RGB kept within -1 %..103 %"},
}
# float RGB reaching the LUT: 8-bit Y' 255 at neutral is 1.091. The minimum stays 0: ffmpeg's .cube reader takes
# DOMAIN_MAX - DOMAIN_MIN as a scale but applies no offset (measured: a [-0.25, 1.25] domain shifted every value), and
# lut3d clamps below 0, so sub-blacks reach the LUT as 0 and leave it at the profile's floor or above (a hard clip)
DOMAIN = (0.0, 1.25)
DEFAULT_KNEE = 0.95
DEFAULT_LUT_SIZE = 65
SHARES_NOT_MEASURED = ("ffmpeg-skill exposes no per-pixel measurement of out-of-range Y'CbCr (its signalstats use reports "
                       "luma levels for --correct only, no BRNG share), and this skill runs no ffmpeg of its own")


def soft_clip(x: float, lo: float, hi: float, knee: float) -> float:
    """Unchanged from `lo` to the knee (lo + knee * span), a tanh shoulder above it that approaches `hi` without
    reaching it; below `lo`, `lo` (lut3d has already clamped negative input to 0, see DOMAIN)."""
    k_hi = lo + knee * (hi - lo)
    if x > k_hi:
        h = hi - k_hi
        return k_hi + h * math.tanh((x - k_hi) / h)
    return max(lo, x)


@dataclass
class LegalLut:
    kind = "legalize"
    text: str
    sha256: str
    profile: str
    knee: float
    lut_size: int

    def provenance(self) -> Dict[str, Any]:
        lo, hi = PROFILES[self.profile]["rgb"]
        return {"profile": self.profile, "reference": PROFILES[self.profile]["reference"], "rgb_limits": [lo, hi], "knee": self.knee,
                "domain": list(DOMAIN), "lut_size": self.lut_size, "lut_sha256": self.sha256,
                "out_of_range_share": {"measured": False, "reason": SHARES_NOT_MEASURED}}


def bake(params: Dict[str, Any]) -> LegalLut:
    lo, hi = PROFILES[params["profile"]]["rgb"]
    n, knee = int(params["lut_size"]), float(params["knee"])
    d0, d1 = DOMAIN
    nodes = grid(n)
    values: List[float] = [float(f"{soft_clip(d0 + v * (d1 - d0), lo, hi, knee):.6f}") for v in nodes]
    text = cube_text(values, n, title="color-grading LEGALIZE", domain=DOMAIN)
    return LegalLut(text, hashlib.sha256(text.encode("utf-8")).hexdigest(), params["profile"], knee, n)


def limits_8bit(profile: str) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """(luma, chroma) 8-bit code limits the profile's RGB limits imply for limited-range BT.709 Y'CbCr."""
    lo, hi = PROFILES[profile]["rgb"]
    luma = (16 + 219 * lo, 16 + 219 * hi)
    c = 0.5 * (hi - lo)       # largest |Cb|, |Cr| for RGB in [lo, hi] is half the span
    return luma, (128 - 224 * c, 128 + 224 * c)
