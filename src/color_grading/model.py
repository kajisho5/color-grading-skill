"""Typed Colour Project Model and request validation.

Concepts (docs/architecture.md):
  ColorProject   one source + a graph of colour operations + one or more outputs
  ColorSource    one media file (path); fingerprinted (sha256) at execution
  ColorOperation typed node of the operation graph: op_id, type, input (ref: "source" or "op:<id>"), parameters
  ColorOutput    a terminal artifact: output_id, operation ref, path, format, expectations
  OperationDependency / OperationResult  graph.py / executor.py

Validation is structural and semantic but never touches the file system: the PathPolicy (security.py) and the
executor do that. Unknown fields are rejected everywhere; fields that could carry a command, a filter string or an
executable are rejected by name, everywhere in the document.

Every operation here maps 1:1 onto a mode of ffmpeg-skill/color (`--to-sdr`, `--lut`, `--retag`, `--strip-dovi`,
`--correct`): this skill is a typed front end for that tool's public contract, not a colour-correction engine of
its own. PRIMARY_CORRECTION (ffmpeg-skill >= 0.9.2) covers exposure / contrast / saturation / white balance
(temperature + tint) as five typed, range-checked parameters, exactly mirroring `color.py --correct`'s own five
original flags and ranges (docs/ffmpeg-skill.md) -- never a raw filter string. ffmpeg-skill 0.12.3 added five more
typed `--correct` flags (gamma, three-way lift/gain, colorlevels, and the curves filter's built-in presets);
PRIMARY_CORRECTION gained the matching eight parameters (gamma / lift / gain / levels_in_black / levels_in_white /
levels_out_black / levels_out_white / curves) the same way, still never a raw filter string (docs/decisions.md
ADR-18). WHITE_BALANCE is an alias (OPERATION_ALIASES), not an operation type of its own: it is accepted as a
request `type`, restricted to PRIMARY_CORRECTION's temperature / tint (plus the encode and audio-stream parameters),
and resolved to PRIMARY_CORRECTION at validation, so everything downstream (graph, identity, provenance) only ever
sees PRIMARY_CORRECTION (docs/decisions.md ADR-20). OCIO_TRANSFORM (ADR-22) is the one operation that is two
ffmpeg-skill/color calls: `--lut` with a LUT this skill baked from an OpenColorIO config (a computation on the cube's
nodes, never on media), then `--retag` when the destination's colour tags are not the BT.709 `--lut` writes. This package will never work around a genuine ffmpeg-skill gap
by improvising a raw ffmpeg filter string of its own (that would cross the ffmpeg-skill boundary, see
docs/architecture.md)."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import REQUEST_SCHEMA_VERSION, SKILL_ID
from .errors import ColorError
from .legalize import DEFAULT_KNEE as LEGAL_KNEE, DEFAULT_LUT_SIZE as LEGAL_LUT_SIZE, PROFILES as LEGAL_PROFILES
from .match import MODELS as MATCH_MODELS
from .ocio import BUILTIN_PREFIX, CONFIG_EXTENSIONS, DEFAULT_CONFIG, DEFAULT_LUT_SIZE, KNOWN_DESTINATION_TAGS, LUT_SIZES

REQUEST_SCHEMA_ID = f"{SKILL_ID}/request@{REQUEST_SCHEMA_VERSION}"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REF_RE = re.compile(r"^(source|op:([A-Za-z0-9][A-Za-z0-9._-]{0,63}))$")

FORBIDDEN_KEYS = frozenset({"command", "commands", "argv", "args", "cmd", "shell", "exec", "executable", "script",
                            "filter", "filters", "filter_complex", "vf", "af", "ffmpeg", "env", "cwd", "api_key",
                            "workspace"})

# output container: format name -> extension. color-grading-skill does not convert containers (that is
# ffmpeg-skill/export's job); an output's container must match the source's, see executor._check_output_format.
OUTPUT_FORMATS: Dict[str, Dict[str, Any]] = {
    "mp4": {"extension": ".mp4"},
    "mov": {"extension": ".mov"},
    "m4v": {"extension": ".m4v"},
    "mkv": {"extension": ".mkv"},
}

X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo")
TONEMAPS = ("hable", "mobius", "reinhard", "bt2390", "clip", "linear", "gamma")
# HDR_TO_SDR's tone-mapping curve when the request gives none (ADR-28): a fixed rule on the transfer of the
# operation's input, never a look at the picture. HLG (arib-std-b67) gets mobius: hable makes HLG camera footage
# (measured: a phone's HLG BT.2020 HEVC clip) clearly too dark, mobius keeps it natural. PQ (smpte2084) and anything
# else keep hable, ffmpeg-skill/color's own default. An explicit `tonemap` always wins.
HLG_TRANSFER = "arib-std-b67"
TONEMAP_DEFAULT = "hable"
TONEMAP_HLG_DEFAULT = "mobius"
# tonemap_selection.rule values: which branch chose the curve
TONEMAP_RULES = {"request": "the request named the curve (always wins)",
                 "hlg_default": f"no curve in the request and the input transfer is HLG ({HLG_TRANSFER}): {TONEMAP_HLG_DEFAULT}",
                 "default": f"no curve in the request and the input transfer is not HLG (PQ smpte2084 or anything else): {TONEMAP_DEFAULT}"}


def select_tonemap(requested: Optional[str], input_transfer: Optional[str]) -> Dict[str, Any]:
    """The curve an HDR_TO_SDR runs with and why (ADR-28), recorded as tonemap_selection in results and provenance."""
    if requested is not None:
        curve, rule = requested, "request"
    elif input_transfer == HLG_TRANSFER:
        curve, rule = TONEMAP_HLG_DEFAULT, "hlg_default"
    else:
        curve, rule = TONEMAP_DEFAULT, "default"
    return {"tonemap": curve, "rule": rule, "input_transfer": input_transfer}
RETAG_TARGETS = ("bt709", "bt2020-pq", "bt2020-hlg", "bt601")
# color.py's own curves filter built-in presets (its CURVES_PRESETS, `ffmpeg -h filter=curves`), excluding the
# filter's own "none" (0): omitting --curves already gets that identity result without a curves filter term.
CURVES_PRESETS = ("color_negative", "cross_process", "darker", "increase_contrast", "lighter",
                  "linear_contrast", "medium_contrast", "negative", "strong_contrast", "vintage")
# color.py's own retag mapping (colorspace, color_primaries, color_trc), used again in executor._expected_retag_tags
RETAG_TAGS: Dict[str, Tuple[str, str, str]] = {
    "bt709": ("bt709", "bt709", "bt709"),
    "bt2020-pq": ("bt2020nc", "bt2020", "smpte2084"),
    "bt2020-hlg": ("bt2020nc", "bt2020", "arib-std-b67"),
    "bt601": ("smpte170m", "smpte170m", "smpte170m"),
}

MAX_LUT_BYTES = 200 * 1024 * 1024   # 200 MiB; a .cube LUT is normally a few KB to a few MB
CUBE_EXTENSIONS = (".cube",)        # the only LUT format ffmpeg-skill/color accepts (lut3d)
MAX_OPERATIONS = 200

# ---- parameter schemas: name -> {type, required, min, max, enum, default, description}
_NUM, _INT, _BOOL, _STR = "number", "integer", "boolean", "string"
_PATCHES = "patches"     # validated by _patches(); declared to consumers as a string (see _param_schema in contract.py)
MAX_PATCHES = 64

# common encode parameters accepted by every re-encoding mode of ffmpeg-skill/color (--crf / --preset); RETAG and
# STRIP_DOVI do not take them: RETAG is a stream copy that only re-encodes on an automatic ffmpeg-skill fallback
# (still using its own defaults, crf/preset are not forwarded for that path) and STRIP_DOVI is always a stream copy.
_ENCODE_PARAMS: Dict[str, Dict[str, Any]] = {
    "crf": {"type": _INT, "required": False, "min": 0, "max": 51, "default": 18, "description": "x264 constant rate factor (0=lossless, 51=worst); lower is higher quality / larger file"},
    "preset": {"type": _STR, "required": False, "enum": list(X264_PRESETS), "default": "medium", "description": "x264 encoding speed/efficiency preset"},
}

# which audio stream of a multi-audio-track input to keep (ffmpeg-skill/color --audio-stream, >= 0.12.0), 0-based in
# file order; default 0 matches every prior release's unconditional behaviour (always the first track). Unlike
# crf/preset, this DOES apply to RETAG: color.py forwards --audio-stream to RETAG's automatic re-encode fallback
# (`-map 0:a:{audio_stream}?`) even though crf/preset there still use ffmpeg-skill's own defaults; a successful
# RETAG stream-copy and STRIP_DOVI's stream copy both keep every audio track untouched (`-map 0`), so the flag has
# nothing to select for STRIP_DOVI and is not offered there.
_AUDIO_STREAM_PARAM: Dict[str, Dict[str, Any]] = {
    "audio_stream": {"type": _INT, "required": False, "min": 0, "default": 0,
                      "description": "which audio stream of the input to keep, 0-based in file order; matters on a multi-track "
                                     "input (dubbed languages, M&E stems) -- only affects paths that re-encode audio"},
}

OPERATION_TYPES: Dict[str, Dict[str, Any]] = {
    "HDR_TO_SDR": {"description": "Tone-map HDR (PQ/HLG, BT.2020) to SDR BT.709 (ffmpeg-skill/color --to-sdr)", "parameters": {
        "tonemap": {"type": _STR, "required": False, "enum": list(TONEMAPS), "default": None, "description": "tone-mapping curve; when omitted "
                    "(None), chosen by the input's transfer: mobius for HLG (arib-std-b67), hable for PQ (smpte2084) and anything else; "
                    "the choice and its rule are recorded as tonemap_selection in results and provenance"},
        "peak_nits": {"type": _NUM, "required": False, "min": 1.0, "max": 10000.0, "default": 1000.0, "description": "source peak brightness in nits, used for PQ"},
        "desat": {"type": _NUM, "required": False, "min": 0.0, "max": 5.0, "default": 0.0, "description": "tonemap desaturation strength"},
        "force": {"type": _BOOL, "required": False, "default": False, "description": "tone-map even if the source is not tagged HDR (treat as PQ)"},
        **_ENCODE_PARAMS, **_AUDIO_STREAM_PARAM}},
    "LUT_APPLY": {"description": "Apply a 3D .cube LUT (ffmpeg-skill/color --lut); LUT is data, resolved and hashed by this skill, never a filter string", "parameters": {
        "lut_path": {"type": _STR, "required": True, "description": "path to a .cube LUT file, resolved through the LUT PathPolicy"},
        "lut_strength": {"type": _NUM, "required": False, "min": 0.0, "max": 1.0, "default": 1.0, "description": "blend of the LUT result with the original, 0..1 "
                          "(ffmpeg-skill applies the LUT at full strength for both 0.0 and 1.0; only a value strictly between them blends, see docs/ffmpeg-skill.md)"},
        **_ENCODE_PARAMS, **_AUDIO_STREAM_PARAM}},
    "RETAG": {"description": "Rewrite colour tags only, no re-encode when the container allows it (ffmpeg-skill/color --retag)", "parameters": {
        "target": {"type": _STR, "required": True, "enum": list(RETAG_TARGETS), "description": "colour tag set to write"},
        **_AUDIO_STREAM_PARAM}},
    "STRIP_DOVI": {"description": "Remove the Dolby Vision RPU (profile 8.4 clips), keeping the HLG/HDR10 base layer; stream copy (ffmpeg-skill/color --strip-dovi)", "parameters": {}},
    "PRIMARY_CORRECTION": {"description": "Typed primary colour correction: exposure, contrast, saturation, white balance (temperature + tint), "
                           "gamma, three-way shadows/highlights (lift/gain), levels and a curves preset "
                           "(ffmpeg-skill/color --correct, requires ffmpeg-skill >= 0.9.2 for the first five, >= 0.12.3 for the other eight); "
                           "each parameter is one option of one real ffmpeg filter (exposure / eq / colortemperature / colorbalance / colorlevels / "
                           "curves), range-checked by ffmpeg-skill itself -- never a filter string", "parameters": {
        "exposure": {"type": _NUM, "required": False, "min": -3.0, "max": 3.0, "default": 0.0, "description": "exposure correction in stops; 0 is unchanged"},
        "contrast": {"type": _NUM, "required": False, "min": 0.0, "max": 2.0, "default": 1.0, "description": "contrast; 1 is unchanged, 0 is flat grey, 2 is double contrast"},
        "saturation": {"type": _NUM, "required": False, "min": 0.0, "max": 2.0, "default": 1.0, "description": "saturation; 1 is unchanged, 0 is grayscale, 2 is double saturation"},
        "temperature": {"type": _NUM, "required": False, "min": 2000.0, "max": 12000.0, "default": 6500.0, "description": "white-balance temperature in Kelvin; 6500 is unchanged"},
        "tint": {"type": _NUM, "required": False, "min": -1.0, "max": 1.0, "default": 0.0, "description": "green(-1)/magenta(+1) tint; 0 is unchanged"},
        "gamma": {"type": _NUM, "required": False, "min": 0.1, "max": 10.0, "default": 1.0, "description": "master gamma (eq filter's own gamma); 1 is unchanged"},
        "lift": {"type": _NUM, "required": False, "min": -1.0, "max": 1.0, "default": 0.0, "description": "shadows lift (colorbalance rs/gs/bs, three-way colour correction); 0 is unchanged"},
        "gain": {"type": _NUM, "required": False, "min": -1.0, "max": 1.0, "default": 0.0, "description": "highlights gain (colorbalance rh/gh/bh, three-way colour correction); 0 is unchanged"},
        "levels_in_black": {"type": _INT, "required": False, "min": 0, "max": 255, "default": 0, "description": "colorlevels input black point, 0..255 (8-bit units); 0 is unchanged"},
        "levels_in_white": {"type": _INT, "required": False, "min": 0, "max": 255, "default": 255, "description": "colorlevels input white point, 0..255 (8-bit units); 255 is unchanged"},
        "levels_out_black": {"type": _INT, "required": False, "min": 0, "max": 255, "default": 0, "description": "colorlevels output black point, 0..255 (8-bit units); 0 is unchanged"},
        "levels_out_white": {"type": _INT, "required": False, "min": 0, "max": 255, "default": 255, "description": "colorlevels output white point, 0..255 (8-bit units); 255 is unchanged"},
        "curves": {"type": _STR, "required": False, "enum": list(CURVES_PRESETS), "default": None, "description": "curves filter built-in preset; None (default) adds no curves term"},
        **_ENCODE_PARAMS, **_AUDIO_STREAM_PARAM}},
    "OCIO_TRANSFORM": {"description": "Colour-space conversion named by an OpenColorIO config (the built-in ACES studio / CG configs or a .ocio file): "
                       "source space -> destination space, or source space -> display / view, with an optional look. PyOpenColorIO (optional "
                       "extra `ocio`) bakes it to a 3D .cube LUT of the stated size; ffmpeg-skill/color --lut applies it and --retag writes the "
                       "destination's colour tags (docs/decisions.md ADR-22)", "parameters": {
        "config": {"type": _STR, "required": False, "default": DEFAULT_CONFIG, "description": "an OpenColorIO built-in config URI (ocio://studio-config-latest, "
                   "ocio://cg-config-latest, or a versioned ocio://...-config-v... name) or the path of a .ocio file, resolved through the LUT PathPolicy"},
        "source_space": {"type": _STR, "required": True, "description": "colour space of the input pixels, as the config names it (e.g. 'S-Log3 S-Gamut3.Cine', 'ACEScct')"},
        "destination_space": {"type": _STR, "required": False, "default": None, "description": "destination colour space; give this or display + view"},
        "display": {"type": _STR, "required": False, "default": None, "description": "destination display (e.g. 'Rec.1886 Rec.709 - Display'); needs view"},
        "view": {"type": _STR, "required": False, "default": None, "description": "view of the display (e.g. 'ACES 2.0 - SDR 100 nits (Rec.709)'); needs display"},
        "look": {"type": _STR, "required": False, "default": None, "description": "an optional look of the config, applied before the destination"},
        "lut_size": {"type": _INT, "required": False, "enum": list(LUT_SIZES), "default": DEFAULT_LUT_SIZE, "description": "edge length of the baked 3D LUT; "
                     "129 is the size measured within 1/1023 of OpenColorIO's processor for the ACES display transforms on the test chart (ADR-22)"},
        "output_tags": {"type": _STR, "required": False, "enum": list(RETAG_TARGETS), "default": None, "description": "colour tags the output must carry; "
                        "derived from a known destination (Rec.1886 Rec.709 -> bt709, Rec.2100-PQ -> bt2020-pq, Rec.2100-HLG -> bt2020-hlg), "
                        "required for any other destination"},
        **_ENCODE_PARAMS, **_AUDIO_STREAM_PARAM}},
    "LEGALIZE": {"description": "Soft-clip into broadcast-legal levels: BT.709 legal range (8-bit luma 16-235, chroma 16-240) or the EBU R 103 "
                 "tolerance, as a 3D .cube LUT over float RGB (super-whites and sub-blacks included) applied by ffmpeg-skill/color --lut "
                 "(docs/decisions.md ADR-24)", "parameters": {
        "profile": {"type": _STR, "required": False, "enum": list(LEGAL_PROFILES), "default": "bt709", "description": "bt709: RGB 0..1 (luma 16-235, chroma 16-240); "
                    "ebu-r103: EBU R 103 tolerance (RGB -5..105 %, luma -1..103 %)"},
        "knee": {"type": _NUM, "required": False, "min": 0.8, "max": 0.99, "default": LEGAL_KNEE, "description": "where the highlight soft clip starts, as a "
                 "fraction of the legal RGB span; values below it are unchanged"},
        "lut_size": {"type": _INT, "required": False, "enum": [33, 65], "default": LEGAL_LUT_SIZE, "description": "edge length of the written 3D LUT"},
        **_ENCODE_PARAMS, **_AUDIO_STREAM_PARAM}},
    "MATCH_CAMERA": {"description": "Match this camera's colour to a reference camera from a frame pair of the same subject: frames extracted by "
                     "ffmpeg-skill/look, a correction fitted (per-channel curves or a 3x3 matrix + offset), written as a 3D .cube LUT and "
                     "applied by ffmpeg-skill/color --lut; the residual CIEDE2000 against the reference is measured before and after "
                     "(docs/decisions.md ADR-23)", "parameters": {
        "reference_path": {"type": _STR, "required": True, "description": "the reference camera's video (camera A), resolved through the input PathPolicy"},
        "reference_at": {"type": _NUM, "required": False, "min": 0.0, "max": 86400.0, "default": 0.0, "description": "time of the reference frame in the reference video, seconds"},
        "at": {"type": _NUM, "required": False, "min": 0.0, "max": 86400.0, "default": 0.0, "description": "time of the matching frame in this operation's input, seconds"},
        "model": {"type": _STR, "required": False, "enum": list(MATCH_MODELS), "default": "curves", "description": "curves: one monotone curve per channel; "
                  "matrix: 3x3 matrix + offset (corrects cross-channel differences too)"},
        "patches": {"type": _PATCHES, "required": False, "default": None, "description": f"up to {MAX_PATCHES} stated patches, ';'-separated, each "
                    "'x,y,w,h' (the same rectangle in both frames) or 'x,y,w,h/x,y,w,h' (reference / input), fractions of the frame; omitted: the whole "
                    "frame, which needs the same framing"},
        "lut_size": {"type": _INT, "required": False, "enum": [17, 33, 65], "default": 33, "description": "edge length of the written 3D LUT"},
        **_ENCODE_PARAMS, **_AUDIO_STREAM_PARAM}},
}

# declared, not implemented: ffmpeg-skill's public contract has no typed filter for these (docs/ffmpeg-skill.md).
# This skill never adds one by writing a raw ffmpeg filter itself. Exposure / contrast / saturation / temperature /
# tint moved out of this table in favour of PRIMARY_CORRECTION once ffmpeg-skill 0.9.2 added typed --correct flags
# for them (docs/decisions.md ADR-15); gamma / lift / gain / levels / curves moved out the same way once ffmpeg-skill
# 0.12.3 added typed --correct flags for them too (ADR-18). WHITE_BALANCE left this table for OPERATION_ALIASES
# below (ADR-20): it was never blocked by an ffmpeg-skill gap, only by the absence of a single flag, and the two
# typed flags that achieve it are already emitted by PRIMARY_CORRECTION. The table stays (empty) as the honest place
# to declare a future operation that ffmpeg-skill really cannot execute (ADR-13).
UNSUPPORTED_OPERATIONS: Dict[str, str] = {}

# Request-level aliases (docs/decisions.md ADR-20): an alias is accepted as an operation `type`, takes only the
# listed parameters of its target operation type, and is resolved to that target at validation -- it is not an
# operation type of its own (never in OPERATION_TYPES, `operations[]` or the tool's `operations`), so the plan,
# the deterministic identity, the results and the provenance all name the target. A WHITE_BALANCE node therefore has
# exactly the identity of the equivalent PRIMARY_CORRECTION node and reuses its intermediate.
OPERATION_ALIASES: Dict[str, Dict[str, Any]] = {
    "WHITE_BALANCE": {"operation": "PRIMARY_CORRECTION", "parameters": ("temperature", "tint", *_ENCODE_PARAMS, *_AUDIO_STREAM_PARAM),
                      "description": "white balance only: PRIMARY_CORRECTION's temperature (Kelvin) and tint, every other correction "
                                     "parameter left at its identity default (ffmpeg-skill/color --correct --temperature --tint)"},
}


@dataclass
class ColorSource:
    source_id: str
    path: str


@dataclass
class ColorOperation:
    op_id: str
    type: str
    input: str                                # ref "source" or "op:<id>"
    parameters: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ColorOutput:
    output_id: str
    operation: str                            # ref "source" or "op:<id>"
    path: str
    format: str
    overwrite: bool = False
    expect: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ColorProject:
    project_id: str
    source: ColorSource
    operations: List[ColorOperation]
    outputs: List[ColorOutput]


@dataclass
class ColorRequest:
    project: ColorProject
    options: Dict[str, Any]


# ---- validation helpers
def _reject_forbidden(obj: Any, where: str) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ColorError("INVALID_REQUEST", f"{where}: object keys must be strings")
            if k.lower() in FORBIDDEN_KEYS:
                raise ColorError("INVALID_REQUEST", f"{where}: field {k!r} is not accepted (this skill never takes commands, argv, filters, executables or credentials)",
                                 {"field": k, "reason": "forbidden_field"})
            _reject_forbidden(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _reject_forbidden(v, f"{where}[{i}]")


def _obj(value: Any, where: str, allowed: Tuple[str, ...], required: Tuple[str, ...]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ColorError("INVALID_REQUEST", f"{where} must be an object", {"field": where})
    unknown = sorted(k for k in value if k not in allowed)
    if unknown:
        raise ColorError("INVALID_REQUEST", f"{where}: unknown field(s) {unknown}", {"field": where, "unknown": unknown, "allowed": list(allowed)})
    missing = [k for k in required if k not in value]
    if missing:
        raise ColorError("INVALID_REQUEST", f"{where}: missing required field(s) {missing}", {"field": where, "missing": missing})
    return value


def _id(value: Any, where: str) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ColorError("INVALID_REQUEST", f"{where} must match {ID_RE.pattern}", {"field": where})
    return value


def _number(value: Any, where: str, lo: Optional[float] = None, hi: Optional[float] = None, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ColorError("INVALID_REQUEST", f"{where} must be a number", {"field": where})
    if isinstance(value, float) and not math.isfinite(value):
        raise ColorError("INVALID_REQUEST", f"{where} must be finite", {"field": where})
    if integer and (isinstance(value, float) and not value.is_integer()):
        raise ColorError("INVALID_REQUEST", f"{where} must be an integer", {"field": where})
    if lo is not None and value < lo or hi is not None and value > hi:
        raise ColorError("INVALID_REQUEST", f"{where} must be within [{lo}, {hi}], got {value}", {"field": where, "min": lo, "max": hi})
    return float(value)


def resolve_alias(alias: str, params: Any, where: str) -> str:
    """An OPERATION_ALIASES entry -> its target operation type. Only the alias's own parameter
    subset is accepted (anything else is INVALID_REQUEST naming the field, exactly like an unknown parameter of a real
    operation type); the target's own validate_parameters then fills every other parameter with its identity default."""
    spec = OPERATION_ALIASES[alias]
    allowed = spec["parameters"]
    if isinstance(params, dict):
        for k in sorted(params):
            if k not in allowed:
                raise ColorError("INVALID_REQUEST", f"{where}.parameters.{k}: not accepted by {alias} (an alias of {spec['operation']} "
                                 f"taking only {list(allowed)}); use {spec['operation']} for it", {"field": f"{where}.parameters.{k}", "allowed": list(allowed)})
    return spec["operation"]


def validate_parameters(op_type: str, params: Any, where: str) -> Dict[str, Any]:
    """Validate and normalise parameters against the schema of op_type. Returns the effective parameters
    (defaults filled in), with every number in its declared type and nothing unknown."""
    spec = OPERATION_TYPES[op_type]["parameters"]
    d = _obj(params if params is not None else {}, where, tuple(spec), tuple(k for k, v in spec.items() if v["required"]))
    out: Dict[str, Any] = {}
    for name, ps in spec.items():
        w = f"{where}.{name}"
        if name not in d:
            if "default" in ps:
                out[name] = ps["default"]
            continue
        v = d[name]
        t = ps["type"]
        if t == _NUM:
            out[name] = _number(v, w, ps.get("min"), ps.get("max"))
        elif t == _INT:
            out[name] = int(_number(v, w, ps.get("min"), ps.get("max"), integer=True))
            if "enum" in ps and out[name] not in ps["enum"]:
                raise ColorError("INVALID_REQUEST", f"{w}={out[name]} is not supported; supported: {ps['enum']}", {"field": w, "allowed": list(ps["enum"])})
        elif t == _STR:
            if not isinstance(v, str) or not v or len(v) > 4096 or any(ord(c) < 32 for c in v):
                raise ColorError("INVALID_REQUEST", f"{w} must be a short printable string", {"field": w})
            if "enum" in ps and v not in ps["enum"]:
                raise ColorError("INVALID_REQUEST", f"{w}={v!r} is not supported; supported: {ps['enum']}", {"field": w, "allowed": list(ps["enum"])})
            out[name] = v
        elif t == _BOOL:
            if not isinstance(v, bool):
                raise ColorError("INVALID_REQUEST", f"{w} must be a boolean", {"field": w})
            out[name] = v
        elif t == _PATCHES:
            out[name] = _patches(v, w)
    if op_type == "PRIMARY_CORRECTION":
        _validate_levels_ordering(out, where)
    if op_type == "OCIO_TRANSFORM":
        _validate_ocio(out, where)
    return out


def _rect(text: str, where: str) -> List[float]:
    parts = text.split(",")
    if len(parts) != 4:
        raise ColorError("INVALID_REQUEST", f"{where}: a rectangle is x,y,w,h (fractions of the frame), got {text!r}", {"field": where})
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        raise ColorError("INVALID_REQUEST", f"{where}: a rectangle is four numbers x,y,w,h, got {text!r}", {"field": where})
    x, y, w, h = (_number(v, where, 0.0, 1.0) for v in vals)
    if w <= 0 or h <= 0 or x + w > 1.0 + 1e-9 or y + h > 1.0 + 1e-9:
        raise ColorError("INVALID_REQUEST", f"{where}: {text!r} is not a non-empty rectangle inside the frame (fractions 0..1)", {"field": where})
    return [x, y, w, h]


def parse_patches(text: str, where: str = "patches") -> List[Dict[str, List[float]]]:
    """MATCH_CAMERA's `patches` string: `;`-separated patches, each `x,y,w,h` (the same rectangle in both frames) or
    `x,y,w,h/x,y,w,h` (reference rectangle / input rectangle), in fractions of the frame. A string, not an array, so the
    parameter keeps one of the four scalar types every consumer of this contract validates (ADR-26)."""
    items = [p.strip() for p in text.split(";") if p.strip()]
    if not items or len(items) > MAX_PATCHES:
        raise ColorError("INVALID_REQUEST", f"{where}: 1 to {MAX_PATCHES} patches, ';'-separated", {"field": where})
    out = []
    for i, item in enumerate(items):
        halves = item.split("/")
        if len(halves) > 2:
            raise ColorError("INVALID_REQUEST", f"{where}[{i}]: x,y,w,h or x,y,w,h/x,y,w,h, got {item!r}", {"field": where})
        ref = _rect(halves[0].strip(), f"{where}[{i}].reference")
        inp = _rect(halves[-1].strip(), f"{where}[{i}].input")
        out.append({"reference": ref, "input": inp})
    return out


def _patches(value: Any, where: str) -> str:
    """Validated, in a canonical spelling (so two spellings of the same patches have one identity)."""
    if not isinstance(value, str):
        raise ColorError("INVALID_REQUEST", f"{where} must be a string: 'x,y,w,h[/x,y,w,h];...'", {"field": where})
    def r(v: List[float]) -> str:
        return ",".join(f"{c:g}" for c in v)
    return ";".join(f"{r(p['reference'])}/{r(p['input'])}" for p in parse_patches(value, where))


def _validate_ocio(params: Dict[str, Any], where: str) -> None:
    """OCIO_TRANSFORM's cross-field rules (ADR-22): one destination (a colour space, or a display with a view); a
    config that is a built-in URI or a .ocio path; and output colour tags that are known, never guessed. The names
    themselves are checked against the config when it is loaded (executor), not here: validation needs no OCIO."""
    has_space, has_display, has_view = (params[k] is not None for k in ("destination_space", "display", "view"))
    if has_space == has_display:
        raise ColorError("INVALID_REQUEST", f"{where}: give exactly one destination, destination_space or display + view", {"field": f"{where}.destination_space"})
    if has_display != has_view:
        raise ColorError("INVALID_REQUEST", f"{where}: display and view go together", {"field": f"{where}.{'view' if has_display else 'display'}"})
    cfg = params["config"]
    if not cfg.startswith(BUILTIN_PREFIX) and not cfg.lower().endswith(CONFIG_EXTENSIONS):
        raise ColorError("INVALID_REQUEST", f"{where}.config must be an {BUILTIN_PREFIX} built-in config or a path ending in {CONFIG_EXTENSIONS}",
                         {"field": f"{where}.config"})
    dest = params["display"] if has_display else params["destination_space"]
    known = KNOWN_DESTINATION_TAGS.get(dest)
    if params["output_tags"] is None:
        if known is None:
            raise ColorError("INVALID_REQUEST", f"{where}: the colour tags of destination {dest!r} are not known to this skill; name them with output_tags "
                             f"(one of {list(RETAG_TARGETS)})", {"field": f"{where}.output_tags", "destination": dest, "allowed": list(RETAG_TARGETS)})
        params["output_tags"] = known
    elif known is not None and params["output_tags"] != known:
        raise ColorError("INVALID_REQUEST", f"{where}.output_tags {params['output_tags']!r} contradicts destination {dest!r}, which is {known!r}",
                         {"field": f"{where}.output_tags", "destination": dest, "expected": known})


def _validate_levels_ordering(params: Dict[str, Any], where: str) -> None:
    """PRIMARY_CORRECTION's colorlevels parameters: the same in_black<in_white / out_black<out_white ordering
    ffmpeg-skill's own color.py --correct enforces (die()), duplicated here as an independent, fail-fast guard at
    the request boundary -- same convention as every other PRIMARY_CORRECTION range check (docs/decisions.md
    ADR-15/ADR-18), never relying on ffmpeg-skill's own check exclusively."""
    in_black, in_white = params["levels_in_black"], params["levels_in_white"]
    if in_black >= in_white:
        raise ColorError("INVALID_REQUEST", f"{where}.levels_in_black {in_black} must be less than levels_in_white {in_white}",
                         {"field": f"{where}.levels_in_black", "levels_in_black": in_black, "levels_in_white": in_white})
    out_black, out_white = params["levels_out_black"], params["levels_out_white"]
    if out_black >= out_white:
        raise ColorError("INVALID_REQUEST", f"{where}.levels_out_black {out_black} must be less than levels_out_white {out_white}",
                         {"field": f"{where}.levels_out_black", "levels_out_black": out_black, "levels_out_white": out_white})


def parse_ref(ref: Any, where: str) -> Tuple[str, str]:
    if not isinstance(ref, str):
        raise ColorError("INVALID_REQUEST", f"{where} must be a reference string 'source' or 'op:<id>'", {"field": where})
    m = REF_RE.match(ref)
    if not m:
        raise ColorError("INVALID_REQUEST", f"{where}: bad reference {ref!r} (expected 'source' or 'op:<id>')", {"field": where})
    return ("source", "") if m.group(1) == "source" else ("op", m.group(2))


def parse_request(doc: Any) -> ColorRequest:
    """Validate a request document (schema color-grading/request@1) into typed objects."""
    if not isinstance(doc, dict):
        raise ColorError("INVALID_REQUEST", "request document must be a JSON object")
    _reject_forbidden(doc, "request")
    d = _obj(doc, "request", ("schema", "project", "options"), ("schema", "project"))
    if d["schema"] != REQUEST_SCHEMA_ID:
        raise ColorError("INVALID_REQUEST", f"unsupported request schema {d['schema']!r}; expected {REQUEST_SCHEMA_ID!r}", {"field": "schema"})
    options = _obj(d.get("options", {}), "options", ("reuse_intermediates", "timeout"), ())
    opts: Dict[str, Any] = {"reuse_intermediates": True, "timeout": None}
    if "reuse_intermediates" in options:
        if not isinstance(options["reuse_intermediates"], bool):
            raise ColorError("INVALID_REQUEST", "options.reuse_intermediates must be a boolean")
        opts["reuse_intermediates"] = options["reuse_intermediates"]
    if "timeout" in options and options["timeout"] is not None:
        opts["timeout"] = _number(options["timeout"], "options.timeout", 1.0, 86400.0)

    p = _obj(d["project"], "project", ("project_id", "source", "operations", "outputs"), ("project_id", "source", "outputs"))
    project_id = _id(p["project_id"], "project.project_id")

    sd = _obj(p["source"], "project.source", ("source_id", "path"), ("source_id", "path"))
    source_id = _id(sd["source_id"], "project.source.source_id")
    if not isinstance(sd["path"], str) or not sd["path"]:
        raise ColorError("INVALID_REQUEST", "project.source.path must be a non-empty string")
    source = ColorSource(source_id, sd["path"])

    ops_raw = p.get("operations", [])
    if not isinstance(ops_raw, list):
        raise ColorError("INVALID_REQUEST", "project.operations must be an array")
    if len(ops_raw) > MAX_OPERATIONS:
        raise ColorError("INVALID_REQUEST", f"too many operations (max {MAX_OPERATIONS})")
    operations: List[ColorOperation] = []
    op_ids: set = set()
    for i, o in enumerate(ops_raw):
        w = f"project.operations[{i}]"
        od = _obj(o, w, ("op_id", "type", "input", "parameters"), ("op_id", "type", "input"))
        oid = _id(od["op_id"], f"{w}.op_id")
        if oid in op_ids:
            raise ColorError("DEPENDENCY_ERROR", f"duplicate op_id {oid!r}", {"field": w})
        op_ids.add(oid)
        typ = od["type"]
        if not isinstance(typ, str):
            raise ColorError("INVALID_REQUEST", f"{w}.type must be a string")
        if typ in OPERATION_ALIASES:
            typ = resolve_alias(typ, od.get("parameters"), w)
        if typ in UNSUPPORTED_OPERATIONS:
            raise ColorError("UNSUPPORTED_OPERATION", f"{w}: operation type {typ!r} is declared but not implemented: {UNSUPPORTED_OPERATIONS[typ]}",
                             {"field": w, "type": typ, "supported": sorted(OPERATION_TYPES)})
        if typ not in OPERATION_TYPES:
            raise ColorError("UNSUPPORTED_OPERATION", f"{w}: unknown operation type {typ!r}", {"field": w, "type": typ, "supported": sorted(OPERATION_TYPES)})
        parse_ref(od["input"], f"{w}.input")
        params = validate_parameters(typ, od.get("parameters"), f"{w}.parameters")
        operations.append(ColorOperation(oid, typ, od["input"], params))

    # reference existence (cycles / self-reference / ordering are checked by graph.py)
    for op in operations:
        kind, ident = parse_ref(op.input, "")
        if kind == "op" and ident not in op_ids:
            raise ColorError("MISSING_INPUT", f"operation {op.op_id!r} references unknown operation {ident!r}", {"op_id": op.op_id, "ref": op.input})
        if kind == "op" and ident == op.op_id:
            raise ColorError("DEPENDENCY_ERROR", f"operation {op.op_id!r} references itself", {"op_id": op.op_id})

    if not isinstance(p["outputs"], list) or not p["outputs"]:
        raise ColorError("INVALID_REQUEST", "project.outputs must be a non-empty array")
    outputs: List[ColorOutput] = []
    for i, o in enumerate(p["outputs"]):
        w = f"project.outputs[{i}]"
        od = _obj(o, w, ("output_id", "operation", "path", "format", "overwrite", "expect"), ("output_id", "operation", "path", "format"))
        oid = _id(od["output_id"], f"{w}.output_id")
        if any(x.output_id == oid for x in outputs):
            raise ColorError("DEPENDENCY_ERROR", f"duplicate output_id {oid!r}", {"field": w})
        kind, ident = parse_ref(od["operation"], f"{w}.operation")
        if kind == "op" and ident not in op_ids:
            raise ColorError("MISSING_INPUT", f"{w}: references unknown operation {ident!r}", {"field": w, "ref": od["operation"]})
        if not isinstance(od["path"], str) or not od["path"]:
            raise ColorError("INVALID_REQUEST", f"{w}.path must be a non-empty string")
        fmt = od["format"]
        if fmt not in OUTPUT_FORMATS:
            raise ColorError("UNSUPPORTED_FORMAT", f"{w}.format {fmt!r} is not supported; supported: {sorted(OUTPUT_FORMATS)}", {"field": w, "format": fmt})
        if not od["path"].lower().endswith(OUTPUT_FORMATS[fmt]["extension"]):
            raise ColorError("UNSUPPORTED_FORMAT", f"{w}.path must end with {OUTPUT_FORMATS[fmt]['extension']!r} for format {fmt!r}", {"field": w})
        overwrite = od.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise ColorError("INVALID_REQUEST", f"{w}.overwrite must be a boolean")
        ex = _obj(od.get("expect", {}), f"{w}.expect", ("width", "height", "duration", "duration_tolerance", "pix_fmt"), ())
        expect: Dict[str, Any] = {}
        if "width" in ex:
            expect["width"] = int(_number(ex["width"], f"{w}.expect.width", 1, 100000, integer=True))
        if "height" in ex:
            expect["height"] = int(_number(ex["height"], f"{w}.expect.height", 1, 100000, integer=True))
        if "duration" in ex:
            expect["duration"] = _number(ex["duration"], f"{w}.expect.duration", 0.0, 24 * 3600.0)
            expect["duration_tolerance"] = _number(ex.get("duration_tolerance", 0.2), f"{w}.expect.duration_tolerance", 0.0, 3600.0)
        elif "duration_tolerance" in ex:
            raise ColorError("INVALID_REQUEST", f"{w}.expect.duration_tolerance requires expect.duration")
        if "pix_fmt" in ex:
            if not isinstance(ex["pix_fmt"], str) or not ex["pix_fmt"]:
                raise ColorError("INVALID_REQUEST", f"{w}.expect.pix_fmt must be a non-empty string")
            expect["pix_fmt"] = ex["pix_fmt"]
        outputs.append(ColorOutput(oid, od["operation"], od["path"], fmt, overwrite, expect))
    if len({o.path for o in outputs}) != len(outputs):
        raise ColorError("OUTPUT_ERROR", "two outputs share the same path", {"reason": "duplicate_output_path"})

    return ColorRequest(ColorProject(project_id, source, operations, outputs), opts)
