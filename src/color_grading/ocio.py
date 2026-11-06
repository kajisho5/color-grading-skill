"""OCIO_TRANSFORM: bake an OpenColorIO transform to a 3D .cube LUT (docs/decisions.md ADR-22).

PyOpenColorIO (BSD-3-Clause) is an optional extra (`pip install 'color-grading-skill[ocio]'`). It is used for one
thing only: evaluating the colour transform a config names (source space -> destination space, or source space ->
display / view, with an optional look) on the nodes of a cube grid. The LUT it produces is then applied by
ffmpeg-skill/color --lut exactly as LUT_APPLY applies a user's LUT, so ffmpeg-skill stays the only thing that
touches media. Nothing here starts a process or reads a frame.

The baked LUT is deterministic for a given config, transform, LUT size and OpenColorIO build: grid order is the
.cube convention (red fastest), every value is clamped to [0, 1] (what lut3d's RGB legs can carry back to YUV) and
written with six decimals. Its sha256 is part of the operation identity, so a different config, look or library
build that changes the numbers changes the identity, and one that does not, does not.

The accuracy of every bake is measured, not assumed: the baked LUT, read back and interpolated tetrahedrally (the
interpolation ffmpeg-skill's lut3d uses), is compared with OpenColorIO's own CPU processor on a test chart and the
error is recorded in provenance (`accuracy`)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import ColorError

MODULE = "PyOpenColorIO"
CAPABILITY = "python:PyOpenColorIO"
INSTALL_HINT = "pip install 'color-grading-skill[ocio]'  (PyOpenColorIO, BSD-3-Clause)"
BUILTIN_PREFIX = "ocio://"
DEFAULT_CONFIG = "ocio://studio-config-latest"
CONFIG_EXTENSIONS = (".ocio",)
LUT_SIZES = (17, 33, 65, 129)
DEFAULT_LUT_SIZE = 65
MAX_CONFIG_BYTES = 16 * 1024 * 1024

# Destination -> the colour tag set (model.RETAG_TARGETS) the output must carry. Only names whose encoding is
# unambiguous are listed: a destination outside this table needs an explicit `output_tags` (model validation).
# Display / view transforms are keyed by the display; colour-space transforms by the destination space.
KNOWN_DESTINATION_TAGS: Dict[str, str] = {
    "Rec.1886 Rec.709 - Display": "bt709",
    "Rec.2100-PQ - Display": "bt2020-pq",
    "Rec.2100-HLG - Display": "bt2020-hlg",
    "Gamma 2.4 Encoded Rec.709": "bt709",
    "Camera Rec.709": "bt709",
}

# Test chart for the accuracy measurement: the 24 ColorChecker patches (sRGB 8-bit, BabelColor average) at five
# exposures (-2..+2 stops) plus an 18 % grey ramp from -10 to +8 stops in 1/4 stops, as scene-linear Rec.709,
# encoded into the source space by the config itself. Chart points outside [0, 1] after encoding are left out
# (a video frame cannot carry them). A config without a linear Rec.709 space falls back to a code-value grey ramp.
COLORCHECKER_SRGB8: Tuple[Tuple[int, int, int], ...] = (
    (115, 82, 68), (194, 150, 130), (98, 122, 157), (87, 108, 67), (133, 128, 177), (103, 189, 170),
    (214, 126, 44), (80, 91, 166), (193, 90, 99), (94, 60, 108), (157, 188, 64), (224, 163, 46),
    (56, 61, 150), (70, 148, 73), (175, 54, 60), (231, 199, 31), (187, 86, 149), (8, 133, 161),
    (243, 243, 242), (200, 200, 200), (160, 160, 160), (122, 122, 121), (85, 85, 85), (52, 52, 52))
LINEAR_REC709_NAMES = ("Linear Rec.709 (sRGB)", "lin_rec709_srgb", "lin_rec709", "Utility - Linear - sRGB")
CHART_NAME = "colorchecker24 x 5 exposures + grey ramp (-10..+8 stops), scene-linear Rec.709 encoded into the source space"
CHART_FALLBACK_NAME = "code-value grey ramp 0..1 in 1/1023 steps"


def status() -> Dict[str, Any]:
    """{"available": bool, "version": str|None, "error": str|None}; never raises (doctor and planning read it)."""
    try:
        import PyOpenColorIO as ocio  # type: ignore[import-not-found]
    except Exception as e:   # ImportError, or a broken binary wheel (OSError)
        return {"available": False, "version": None, "error": f"{type(e).__name__}: {e}"}
    return {"available": True, "version": str(getattr(ocio, "__version__", "unknown")), "error": None}


def _module() -> Any:
    try:
        import PyOpenColorIO as ocio  # type: ignore[import-not-found]
    except Exception as e:
        raise ColorError("TOOL_ERROR", f"OCIO_TRANSFORM needs PyOpenColorIO, which is not installed: {INSTALL_HINT}",
                         {"reason": "ocio_missing", "capability": CAPABILITY, "error": f"{type(e).__name__}: {e}"}, retryable=False)
    return ocio


def destination_key(params: Dict[str, Any]) -> str:
    return params["display"] if params.get("display") is not None else params["destination_space"]


@dataclass
class BakedLut:
    kind = "ocio"                 # the key its provenance is reported under
    text: str
    sha256: str
    lut_size: int
    clamped_values: int
    config: Dict[str, Any]
    transform: Dict[str, Any]
    library_version: str
    accuracy: Dict[str, Any] = field(default_factory=dict)

    def provenance(self) -> Dict[str, Any]:
        return {"library": MODULE, "library_version": self.library_version, "config": dict(self.config), "transform": dict(self.transform),
                "lut_size": self.lut_size, "lut_sha256": self.sha256, "clamped_values": self.clamped_values, "accuracy": dict(self.accuracy)}


def _load_config(ocio: Any, config: str, config_path: Optional[Path]) -> Tuple[Any, Dict[str, Any]]:
    info: Dict[str, Any] = {}
    try:
        if config_path is not None:
            size = config_path.stat().st_size
            if size <= 0 or size > MAX_CONFIG_BYTES:
                raise ColorError("INVALID_INPUT", f"OCIO config {config_path.name} is empty or larger than {MAX_CONFIG_BYTES} bytes",
                                 {"reason": "ocio_config_size", "path": str(config_path), "size": size})
            data = config_path.read_bytes()
            cfg = ocio.Config.CreateFromFile(str(config_path))
            info.update({"kind": "file", "path": str(config_path), "file_sha256": hashlib.sha256(data).hexdigest()})
        else:
            resolved = str(ocio.ResolveConfigPath(config)) if hasattr(ocio, "ResolveConfigPath") else config
            cfg = ocio.Config.CreateFromFile(config)
            info.update({"kind": "builtin", "requested": config, "resolved": resolved})
    except ColorError:
        raise
    except Exception as e:
        raise ColorError("INVALID_INPUT", f"OCIO config {config_path.name if config_path else config!r} cannot be loaded: {e}",
                         {"reason": "ocio_config", "config": str(config_path or config)}, retryable=False)
    try:
        cfg.validate()
    except Exception as e:
        raise ColorError("INVALID_INPUT", f"OCIO config {config_path.name if config_path else config!r} does not validate: {e}",
                         {"reason": "ocio_config_invalid", "config": str(config_path or config)}, retryable=False)
    serialized = cfg.serialize()
    info.update({"name": cfg.getName() or None, "description": ((cfg.getDescription() or "").strip().splitlines() or [""])[0][:200] or None,
                 "profile_version": f"{cfg.getMajorVersion()}.{cfg.getMinorVersion()}",
                 "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest()})
    return cfg, info


def _check_names(cfg: Any, p: Dict[str, Any]) -> None:
    def bad(field_name: str, value: str, allowed: Sequence[str]) -> ColorError:
        return ColorError("INVALID_REQUEST", f"OCIO_TRANSFORM {field_name} {value!r} is not in the config", {"field": field_name, "value": value,
                          "allowed": sorted(allowed)[:200]})
    spaces = list(cfg.getColorSpaceNames())
    for key in ("source_space", "destination_space"):
        v = p.get(key)
        if v is not None and cfg.getColorSpace(v) is None:
            raise bad(key, v, spaces)
    if p.get("display") is not None:
        displays = list(cfg.getDisplays())
        if p["display"] not in displays:
            raise bad("display", p["display"], displays)
        views = list(cfg.getViews(p["display"]))
        if p["view"] not in views:
            raise bad("view", p["view"], views)
    if p.get("look") is not None and cfg.getLook(p["look"]) is None:
        raise bad("look", p["look"], list(cfg.getLookNames()))


def _transform(ocio: Any, p: Dict[str, Any]) -> Any:
    src, look = p["source_space"], p.get("look")
    if p.get("display") is not None:
        dv = ocio.DisplayViewTransform(src=src, display=p["display"], view=p["view"])
        if look is None:
            return dv
        return ocio.GroupTransform([ocio.LookTransform(src=src, dst=src, looks=look), dv])
    if look is None:
        return ocio.ColorSpaceTransform(src=src, dst=p["destination_space"])
    return ocio.LookTransform(src=src, dst=p["destination_space"], looks=look)


def grid(n: int) -> List[float]:
    """The cube's input nodes, red fastest, flattened RGB."""
    step = [i / (n - 1) for i in range(n)]
    out: List[float] = []
    for b in step:
        for g in step:
            for r in step:
                out += (r, g, b)
    return out


def _apply(cpu: Any, flat: List[float], chunk: int = 3 * 65536) -> List[float]:
    out: List[float] = []
    for i in range(0, len(flat), chunk):
        out += cpu.applyRGB(flat[i:i + chunk])
    return out


def cube_text(values: Sequence[float], n: int, title: str = "color-grading OCIO_TRANSFORM", domain: Optional[Tuple[float, float]] = None) -> str:
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {n}"]
    if domain is not None:
        lines += [f"DOMAIN_MIN {domain[0]:g} {domain[0]:g} {domain[0]:g}", f"DOMAIN_MAX {domain[1]:g} {domain[1]:g} {domain[1]:g}"]
    lines += [f"{values[i]:.6f} {values[i + 1]:.6f} {values[i + 2]:.6f}" for i in range(0, len(values), 3)]
    return "\n".join(lines) + "\n"


def parse_cube(text: str) -> Tuple[int, List[float]]:
    """A 3D .cube (LUT_3D_SIZE + rows, red fastest) as (size, flattened values). Only what cube_text writes and the
    plain Adobe/Resolve 3D layout; a 1D or DOMAIN-scaled cube is refused, it is not what OCIO_TRANSFORM writes."""
    n = 0
    vals: List[float] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("TITLE"):
            continue
        if line.startswith("LUT_3D_SIZE"):
            n = int(line.split()[1])
            continue
        if line[0].isalpha():
            raise ValueError(f"unsupported .cube keyword: {line.split()[0]}")
        vals += [float(x) for x in line.split()]
    if n < 2 or len(vals) != 3 * n ** 3:
        raise ValueError(f"cube has {len(vals) // 3} rows, LUT_3D_SIZE {n} needs {n ** 3}")
    return n, vals


def interpolate(values: Sequence[float], n: int, rgb: Sequence[float]) -> List[float]:
    """Tetrahedral interpolation of a 3D LUT (red fastest), the interpolation ffmpeg-skill/color asks lut3d for
    (`interp=tetrahedral`); the input is clamped to [0, 1] as lut3d does."""
    def at(r: int, g: int, b: int) -> Sequence[float]:
        i = 3 * (r + n * (g + n * b))
        return values[i:i + 3]
    s = [min(1.0, max(0.0, c)) * (n - 1) for c in rgb]
    r0, g0, b0 = (min(int(x), n - 2) for x in s)
    fr, fg, fb = s[0] - r0, s[1] - g0, s[2] - b0
    c000, c111 = at(r0, g0, b0), at(r0 + 1, g0 + 1, b0 + 1)
    if fr > fg:
        if fg > fb:
            c1, c2, w = at(r0 + 1, g0, b0), at(r0 + 1, g0 + 1, b0), (1 - fr, fr - fg, fg - fb, fb)
        elif fr > fb:
            c1, c2, w = at(r0 + 1, g0, b0), at(r0 + 1, g0, b0 + 1), (1 - fr, fr - fb, fb - fg, fg)
        else:
            c1, c2, w = at(r0, g0, b0 + 1), at(r0 + 1, g0, b0 + 1), (1 - fb, fb - fr, fr - fg, fg)
    else:
        if fb > fg:
            c1, c2, w = at(r0, g0, b0 + 1), at(r0, g0 + 1, b0 + 1), (1 - fb, fb - fg, fg - fr, fr)
        elif fb > fr:
            c1, c2, w = at(r0, g0 + 1, b0), at(r0, g0 + 1, b0 + 1), (1 - fg, fg - fb, fb - fr, fr)
        else:
            c1, c2, w = at(r0, g0 + 1, b0), at(r0 + 1, g0 + 1, b0), (1 - fg, fg - fr, fr - fb, fb)
    return [w[0] * c000[k] + w[1] * c1[k] + w[2] * c2[k] + w[3] * c111[k] for k in range(3)]


def _srgb8_to_linear(c: int) -> float:
    v = c / 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def chart_points(ocio: Any, cfg: Any, source_space: str) -> Tuple[str, List[List[float]]]:
    """The chart (see CHART_NAME) as source-space code values in [0, 1]."""
    linear = next((name for name in LINEAR_REC709_NAMES if cfg.getColorSpace(name) is not None), None)
    if linear is not None:
        try:
            enc = cfg.getProcessor(ocio.ColorSpaceTransform(src=linear, dst=source_space)).getDefaultCPUProcessor()
        except Exception:
            enc = None
        if enc is not None:
            scene = [[_srgb8_to_linear(c) * e for c in patch] for patch in COLORCHECKER_SRGB8 for e in (0.25, 0.5, 1.0, 2.0, 4.0)]
            scene += [[0.18 * 2.0 ** (q / 4.0)] * 3 for q in range(-40, 33)]
            flat = _apply(enc, [c for p in scene for c in p])
            chart = [flat[i:i + 3] for i in range(0, len(flat), 3)]
            chart = [list(p) for p in chart if all(0.0 <= v <= 1.0 for v in p)]
            if chart:
                return CHART_NAME, chart
    return CHART_FALLBACK_NAME, [[i / 1023.0] * 3 for i in range(1024)]


def measure(cpu: Any, values: Sequence[float], n: int, chart: List[List[float]]) -> Dict[str, Any]:
    """Largest and mean per-channel difference between the LUT and OCIO's CPU processor on `chart`, in 1/1023 units
    (10-bit code values); the processor's result is clamped to [0, 1] as the LUT's nodes are."""
    ref = [min(1.0, max(0.0, v)) for v in _apply(cpu, [c for p in chart for c in p])]
    errs = [max(abs(a - b) for a, b in zip(interpolate(values, n, p), ref[3 * i:3 * i + 3])) for i, p in enumerate(chart)]
    worst = max(range(len(errs)), key=lambda i: errs[i])
    return {"points": len(chart), "max_error_1023": round(errs[worst] * 1023.0, 4), "mean_error_1023": round(sum(errs) / len(errs) * 1023.0, 4),
            "worst_point": [round(v, 6) for v in chart[worst]], "within_1_1023": errs[worst] * 1023.0 <= 1.0}


def bake(params: Dict[str, Any], config_path: Optional[Path] = None) -> BakedLut:
    """The OCIO_TRANSFORM of validated `params` (model.OPERATION_TYPES) as a .cube LUT, with its measured accuracy."""
    ocio = _module()
    cfg, config_info = _load_config(ocio, params["config"], config_path)
    _check_names(cfg, params)
    try:
        cpu = cfg.getProcessor(_transform(ocio, params)).getDefaultCPUProcessor()
    except Exception as e:
        raise ColorError("INVALID_REQUEST", f"OCIO_TRANSFORM: OpenColorIO cannot build this transform: {e}", {"reason": "ocio_transform"}, retryable=False)
    n = int(params["lut_size"])
    raw = _apply(cpu, grid(n))
    clamped = 0
    values: List[float] = []
    for v in raw:
        c = min(1.0, max(0.0, v))
        if c != v:
            clamped += 1
        values.append(float(f"{c:.6f}"))
    text = cube_text(values, n)
    chart_name, chart = chart_points(ocio, cfg, params["source_space"])
    accuracy = {"chart": chart_name, **measure(cpu, values, n, chart)}
    transform = {"source_space": params["source_space"], "destination_space": params.get("destination_space"),
                 "display": params.get("display"), "view": params.get("view"), "look": params.get("look")}
    return BakedLut(text, hashlib.sha256(text.encode("utf-8")).hexdigest(), n, clamped, config_info, transform,
                    str(getattr(ocio, "__version__", "unknown")), accuracy)
