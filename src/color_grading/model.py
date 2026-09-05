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
flags and ranges (docs/ffmpeg-skill.md) -- never a raw filter string. White balance as its own operation type,
gamma, lift, gain, levels and curves remain in UNSUPPORTED_OPERATIONS because ffmpeg-skill's public contract has no
typed filter for them yet; they are not implemented here, and they never will be by improvising a raw ffmpeg filter
string in this package (that would cross the ffmpeg-skill boundary, see docs/architecture.md)."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import REQUEST_SCHEMA_VERSION, SKILL_ID
from .errors import ColorError

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
RETAG_TARGETS = ("bt709", "bt2020-pq", "bt2020-hlg", "bt601")
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

# common encode parameters accepted by every re-encoding mode of ffmpeg-skill/color (--crf / --preset); RETAG and
# STRIP_DOVI do not take them: RETAG is a stream copy that only re-encodes on an automatic ffmpeg-skill fallback
# (still using its own defaults, crf/preset are not forwarded for that path) and STRIP_DOVI is always a stream copy.
_ENCODE_PARAMS: Dict[str, Dict[str, Any]] = {
    "crf": {"type": _INT, "required": False, "min": 0, "max": 51, "default": 18, "description": "x264 constant rate factor (0=lossless, 51=worst); lower is higher quality / larger file"},
    "preset": {"type": _STR, "required": False, "enum": list(X264_PRESETS), "default": "medium", "description": "x264 encoding speed/efficiency preset"},
}

OPERATION_TYPES: Dict[str, Dict[str, Any]] = {
    "HDR_TO_SDR": {"description": "Tone-map HDR (PQ/HLG, BT.2020) to SDR BT.709 (ffmpeg-skill/color --to-sdr)", "parameters": {
        "tonemap": {"type": _STR, "required": False, "enum": list(TONEMAPS), "default": "hable", "description": "tone-mapping curve"},
        "peak_nits": {"type": _NUM, "required": False, "min": 1.0, "max": 10000.0, "default": 1000.0, "description": "source peak brightness in nits, used for PQ"},
        "desat": {"type": _NUM, "required": False, "min": 0.0, "max": 5.0, "default": 0.0, "description": "tonemap desaturation strength"},
        "force": {"type": _BOOL, "required": False, "default": False, "description": "tone-map even if the source is not tagged HDR (treat as PQ)"},
        **_ENCODE_PARAMS}},
    "LUT_APPLY": {"description": "Apply a 3D .cube LUT (ffmpeg-skill/color --lut); LUT is data, resolved and hashed by this skill, never a filter string", "parameters": {
        "lut_path": {"type": _STR, "required": True, "description": "path to a .cube LUT file, resolved through the LUT PathPolicy"},
        "lut_strength": {"type": _NUM, "required": False, "min": 0.0, "max": 1.0, "default": 1.0, "description": "blend of the LUT result with the original, 0..1 "
                          "(ffmpeg-skill applies the LUT at full strength for both 0.0 and 1.0; only a value strictly between them blends, see docs/ffmpeg-skill.md)"},
        **_ENCODE_PARAMS}},
    "RETAG": {"description": "Rewrite colour tags only, no re-encode when the container allows it (ffmpeg-skill/color --retag)", "parameters": {
        "target": {"type": _STR, "required": True, "enum": list(RETAG_TARGETS), "description": "colour tag set to write"}}},
    "STRIP_DOVI": {"description": "Remove the Dolby Vision RPU (profile 8.4 clips), keeping the HLG/HDR10 base layer; stream copy (ffmpeg-skill/color --strip-dovi)", "parameters": {}},
    "PRIMARY_CORRECTION": {"description": "Typed primary colour correction: exposure, contrast, saturation, white balance (temperature + tint) "
                           "(ffmpeg-skill/color --correct, requires ffmpeg-skill >= 0.9.2); each parameter is one option of one real ffmpeg filter "
                           "(exposure / eq / colortemperature / colorbalance), range-checked by ffmpeg-skill itself -- never a filter string", "parameters": {
        "exposure": {"type": _NUM, "required": False, "min": -3.0, "max": 3.0, "default": 0.0, "description": "exposure correction in stops; 0 is unchanged"},
        "contrast": {"type": _NUM, "required": False, "min": 0.0, "max": 2.0, "default": 1.0, "description": "contrast; 1 is unchanged, 0 is flat grey, 2 is double contrast"},
        "saturation": {"type": _NUM, "required": False, "min": 0.0, "max": 2.0, "default": 1.0, "description": "saturation; 1 is unchanged, 0 is grayscale, 2 is double saturation"},
        "temperature": {"type": _NUM, "required": False, "min": 2000.0, "max": 12000.0, "default": 6500.0, "description": "white-balance temperature in Kelvin; 6500 is unchanged"},
        "tint": {"type": _NUM, "required": False, "min": -1.0, "max": 1.0, "default": 0.0, "description": "green(-1)/magenta(+1) tint; 0 is unchanged"},
        **_ENCODE_PARAMS}},
}

# declared, not implemented: ffmpeg-skill's public contract has no typed filter for these (docs/ffmpeg-skill.md).
# This skill never adds one by writing a raw ffmpeg filter itself. Exposure / contrast / saturation / temperature /
# tint moved out of this table in favour of PRIMARY_CORRECTION once ffmpeg-skill 0.9.2 added typed --correct flags
# for them (docs/decisions.md ADR-15); WHITE_BALANCE stays declared because there is no single "white balance"
# operation type or flag -- only the two separate PRIMARY_CORRECTION parameters that together achieve it.
UNSUPPORTED_OPERATIONS: Dict[str, str] = {
    "WHITE_BALANCE": "no single white-balance operation or flag exists; use PRIMARY_CORRECTION's temperature and tint parameters",
    "GAMMA": "ffmpeg-skill exposes no typed gamma filter (no eq gamma control) in its public contract",
    "LIFT": "ffmpeg-skill exposes no typed lift (shadows) control in its public contract",
    "GAIN": "ffmpeg-skill exposes no typed gain (highlights) control in its public contract; not to be confused with audio-production's audio GAIN operation",
    "LEVELS": "ffmpeg-skill exposes no typed levels filter in its public contract",
    "CURVES": "ffmpeg-skill exposes no typed curves filter in its public contract",
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
    return out


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
