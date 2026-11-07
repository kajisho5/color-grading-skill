"""Executor: request -> validated project -> operation graph -> plan -> ffmpeg-skill calls -> validated artifacts.

Pipeline (docs/architecture.md):
  parse_request (model)  ->  PathPolicy (security)  ->  OperationGraph (graph)  ->  probe + fingerprint the source
  ->  resolve/hash any LUTs  ->  deterministic identities  ->  plan (tool selection, argv, expected geometry)
  ->  execute in order  ->  validate every artifact (exists, size, video stream, duration, resolution, colour tags,
  sha256)  ->  materialise outputs (copy the validated bytes to the requested path, re-validate)
  ->  response document (schema color-grading/response@1)

Every intermediate is written next to <workspace>/.color-grading/<project_id>/<identity16><source-extension> with a
manifest (<identity16>.json) so a re-run with identical identity reuses it instead of re-processing (idempotency).
Colour operations never change duration or frame geometry: expected duration/width/height for every intermediate is
the source's, and RETAG / HDR_TO_SDR / STRIP_DOVI's effect on colour tags is verified by re-probing the output,
never assumed from the request."""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import RESPONSE_SCHEMA_VERSION, SKILL_ID, VERSION
from .adapter import FfmpegSkill, ToolRun, fmt_num, probe_hdr
from .canonical import sha256_file, stable_hash
from .errors import ColorError
from .graph import Node, OperationGraph
from . import legalize as legalize_mod
from . import match as match_mod
from . import ocio as ocio_mod
from .model import CUBE_EXTENSIONS, MAX_LUT_BYTES, OUTPUT_FORMATS, RETAG_TAGS, ColorRequest, parse_patches, parse_request, select_tonemap
from .security import PathPolicy

RESPONSE_SCHEMA_ID = f"{SKILL_ID}/response@{RESPONSE_SCHEMA_VERSION}"
WORK_DIR_NAME = ".color-grading"
DURATION_TOLERANCE = 0.2    # seconds; colour operations never trim, but a re-encode may round to the nearest frame
MANIFEST_SCHEMA = f"{SKILL_ID}/manifest@1"

# tool selection per node type (ffmpeg-skill tool, extra capabilities beyond ffmpeg/ffprobe)
TOOL_FOR: Dict[str, Tuple[str, List[str]]] = {
    "SOURCE": ("probe", []),
    "HDR_TO_SDR": ("color", ["filter:zscale", "filter:tonemap", "encoder:libx264"]),
    "LUT_APPLY": ("color", ["filter:lut3d", "encoder:libx264"]),
    "RETAG": ("color", []),
    "STRIP_DOVI": ("color", ["bsf:filter_units"]),
    "PRIMARY_CORRECTION": ("color", ["filter:exposure", "filter:eq", "filter:colorbalance", "filter:colortemperature", "filter:colorlevels",
                                     "filter:curves", "encoder:libx264"]),
    # baked by PyOpenColorIO (a Python capability of this skill, not of ffmpeg-skill), applied by color --lut, tagged by
    # color --retag when the --lut output does not already carry the destination's tags (ADR-22)
    # frames by ffmpeg-skill/look, the fit computed here (match.py), the LUT applied by color --lut (ADR-23)
    # baked here (legalize.py), applied by color --lut over its float RGB legs (3.1.6 on), ADR-24
    "LEGALIZE": ("color", ["ffmpeg-skill/color:lut-bt709-legs", "filter:lut3d", "encoder:libx264"]),
    "MATCH_CAMERA": ("color", ["ffmpeg-skill/look", "ffmpeg-skill/color:lut-bt709-legs", "filter:lut3d", "encoder:libx264"]),
    "OCIO_TRANSFORM": ("color", [ocio_mod.CAPABILITY, "ffmpeg-skill/color:retag-h264-vui", "filter:lut3d", "encoder:libx264"]),
}
# capabilities this skill provides itself (Python modules), not detected through ffmpeg-skill's doctor
PYTHON_CAPABILITIES = (ocio_mod.CAPABILITY,)
# capabilities read from the located ffmpeg-skill's contract by the adapter (SkillInfo.features), not from its doctor:
# capability -> feature key
CHECKOUT_CAPABILITIES = {"ffmpeg-skill/color:retag-h264-vui": "retag_rewrites_h264_tags", "ffmpeg-skill/look": "look_tool",
                         "ffmpeg-skill/color:lut-bt709-legs": "lut_legs_bt709",
                         # optional: used where supported, never required by an operation (TOOL_FOR); ADR-29, ADR-30
                         "ffmpeg-skill/check:levels": "check_tool", "ffmpeg-skill/color:legalize": "color_legalize",
                         "ffmpeg-skill/color:keep-alpha": "color_keep_alpha"}
OPTIONAL_CHECKOUT_CAPABILITIES = ("ffmpeg-skill/check:levels", "ffmpeg-skill/color:legalize", "ffmpeg-skill/color:keep-alpha")


# pixel formats that carry an alpha plane (FFmpeg's names): yuva*, gbrap*, ya8/ya16, rgba/bgra/argb/abgr and their 64-bit forms
ALPHA_PIX_FMT_RE = re.compile(r"^(yuva|gbrap|ya(8|16)|rgba|bgra|argb|abgr)")
# operations that re-encode through ffmpeg-skill/color's H.264 4:2:0 path (no alpha plane exists there, ADR-25); RETAG
# and STRIP_DOVI are stream copies, checked on their output instead
REENCODING_OPERATIONS = ("HDR_TO_SDR", "LUT_APPLY", "PRIMARY_CORRECTION", "OCIO_TRANSFORM", "MATCH_CAMERA", "LEGALIZE")
# the ones ffmpeg-skill/color --keep-alpha can run on an alpha source (its --lut and --correct), ADR-30: the alpha is
# split off, never graded, and merged back into an alpha line. The others stay refused on an alpha source
ALPHA_GRADING_OPERATIONS = ("LUT_APPLY", "PRIMARY_CORRECTION")
# the alpha line (color --alpha-codec) per codec of the operation's input, so an editing master stays in its own family
# (lossless qtrle / PNG stay lossless); anything else becomes ProRes 4444. Each line's containers among this skill's
# output formats (a .webm is not one, so VP9 with alpha is kept only in .mkv)
ALPHA_LINE_FOR_CODEC = {"prores": "prores4444", "qtrle": "qtrle", "png": "png", "vp9": "vp9"}
ALPHA_LINE_DEFAULT = "prores4444"
ALPHA_LINE_EXTENSIONS = {"prores4444": (".mov", ".mkv"), "qtrle": (".mov",), "png": (".mov", ".mkv"), "vp9": (".mkv",)}
# the encoder each line needs: it replaces encoder:libx264 in an alpha grade's required capabilities
ALPHA_LINE_ENCODERS = {"prores4444": "prores_ks", "qtrle": "qtrle", "png": "png", "vp9": "libvpx-vp9"}
# ffmpeg-skill/color --keep-alpha splits the alpha off at 8 bits (measured, ADR-30): a deeper source alpha comes back
# within one 8-bit code, not bit-identical. Pixel formats whose alpha is deeper than 8 bits
DEEP_ALPHA_PIX_FMT_RE = re.compile(r"^(yuva\d+p(9|10|12|16)|gbrap(10|12|14|16)|ya16|rgba64|bgra64)")
# how the source's colour is taken and written beside its alpha: straight, as ffmpeg-skill/color takes every source.
# A premultiplied source is not un-premultiplied before the grade (ffmpeg-skill has no such step), ADR-30
ALPHA_MODE = "straight"


def alpha_line(codec: Optional[str], ext: str) -> Optional[str]:
    """The alpha line an alpha source of this codec is graded into, or None when no line fits the container."""
    line = ALPHA_LINE_FOR_CODEC.get(str(codec or ""), ALPHA_LINE_DEFAULT)
    return line if ext.lower() in ALPHA_LINE_EXTENSIONS[line] else None


def has_alpha(pix_fmt: Optional[str]) -> bool:
    return bool(pix_fmt) and bool(ALPHA_PIX_FMT_RE.match(str(pix_fmt)))


@dataclass
class Artifact:
    path: Path
    duration: float
    width: int
    height: int
    pix_fmt: Optional[str]
    codec: Optional[str]
    color_space: Optional[str]
    color_primaries: Optional[str]
    color_transfer: Optional[str]
    color_range: Optional[str]
    hdr: bool
    dolby_vision: bool
    size: int
    sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return {"path": str(self.path), "duration": self.duration, "width": self.width, "height": self.height,
                "pix_fmt": self.pix_fmt, "codec": self.codec, "color_space": self.color_space, "color_primaries": self.color_primaries,
                "color_transfer": self.color_transfer, "color_range": self.color_range, "hdr": self.hdr, "dolby_vision": self.dolby_vision,
                "size": self.size, "sha256": self.sha256}


@dataclass
class LutInfo:
    path: Path
    size: int
    sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return {"path": str(self.path), "size": self.size, "sha256": self.sha256}


@dataclass
class NodeState:
    node: Node
    identity: str = ""
    tool: str = ""
    capabilities: List[str] = field(default_factory=list)
    artifact: Optional[Artifact] = None
    lut: Optional[LutInfo] = None
    status: str = "planned"       # planned | reused | completed | failed | skipped | cancelled
    seconds: float = 0.0
    tool_commands: List[str] = field(default_factory=list)
    measurements: Dict[str, Any] = field(default_factory=dict)
    reencoded: Optional[bool] = None               # ffmpeg-skill/color's own "did I have to re-encode" signal (RETAG only; >= 0.12.0)
    dropped_non_av_streams: Optional[bool] = None  # ffmpeg-skill/color's own "did I drop subtitle/data streams" signal (>= 0.12.0)
    error: Optional[Dict[str, Any]] = None
    input_hash: Optional[str] = None
    baked: Optional[Any] = None                    # a LUT this skill computed at planning: ocio.BakedLut (kind "ocio"), legalize.LegalLut (kind "legalize")
    reference: Optional[LutInfo] = None            # MATCH_CAMERA: the reference video (path, size, sha256)
    retagged: Optional[bool] = None                # OCIO_TRANSFORM: whether a --retag pass was needed for the destination's tags
    tonemap_selection: Optional[Dict[str, Any]] = None   # HDR_TO_SDR: the curve run and the rule that chose it (ADR-28)
    alpha_line: Optional[str] = None               # LUT_APPLY / PRIMARY_CORRECTION on an alpha source: color --alpha-codec (ADR-30)
    alpha: Optional[Dict[str, Any]] = None         # ... and what was kept and verified (provenance `alpha`)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"node_id": self.node.node_id, "operation_id": self.identity, "type": self.node.type,
                             "tool": f"ffmpeg-skill/{self.tool}" if self.tool else None, "required_capabilities": self.capabilities,
                             "input": self.node.inputs[0] if self.node.inputs else None, "parameters": self.node.parameters, "status": self.status,
                             "input_hash": self.input_hash, "seconds": self.seconds}
        d["artifact"] = self.artifact.to_dict() if self.artifact else None
        if self.lut is not None:
            d["lut"] = self.lut.to_dict()
        if self.baked is not None:
            d[self.baked.kind] = self.baked.provenance()
        if self.reference is not None:
            d["reference"] = self.reference.to_dict()
        if self.retagged is not None:
            d["retagged"] = self.retagged
        if self.tonemap_selection is not None:
            d["tonemap_selection"] = self.tonemap_selection
        if self.alpha is not None:
            d["alpha"] = self.alpha
        d["tool_commands_observed"] = list(self.tool_commands)
        d["measurements"] = self.measurements
        if self.reencoded is not None:
            d["reencoded"] = self.reencoded
        if self.dropped_non_av_streams is not None:
            d["dropped_non_av_streams"] = self.dropped_non_av_streams
        if self.error:
            d["error"] = self.error
        return d


class Executor:
    def __init__(self, policy: PathPolicy, skill: FfmpegSkill, dry_run: bool = False, reuse: bool = True, timeout: Optional[float] = None,
                 tool_versions: Optional[Dict[str, str]] = None, capabilities: Optional[Dict[str, str]] = None):
        self.policy = policy
        self.skill = skill
        self.dry_run = dry_run
        self.reuse = reuse
        self.timeout = timeout
        self.tool_versions = dict(tool_versions or {})
        self.capabilities = dict(capabilities or {})   # capability -> supported | unsupported | unknown (doctor)
        self.warnings: List[str] = []
        self._states: Dict[str, NodeState] = {}

    # ---- entry points
    def response(self, document: Any, validate_only: bool = False) -> Dict[str, Any]:
        """Always returns one response document; never raises."""
        try:
            req = parse_request(document)
            if validate_only:
                graph = OperationGraph(req.project)
                return self._envelope(True, "ok", {"validation": {"ok": True, "graph": graph.to_dict()}, "dry_run": True})
            return self._run(req)
        except ColorError as e:
            return self._envelope(False, "cancelled" if e.code == "CANCELLED" else "error", {"error": e.to_dict(), "dry_run": self.dry_run})
        except Exception as e:  # a bug in this skill: still one document
            err = ColorError("INTERNAL_ERROR", f"{type(e).__name__}: {e}")
            return self._envelope(False, "error", {"error": err.to_dict(), "dry_run": self.dry_run})

    def _envelope(self, ok: bool, status: str, body: Dict[str, Any]) -> Dict[str, Any]:
        doc: Dict[str, Any] = {"schema": RESPONSE_SCHEMA_ID, "skill": {"id": SKILL_ID, "version": VERSION}, "ok": ok, "status": status}
        doc.update(body)
        doc.setdefault("warnings", list(self.warnings))
        return doc

    # ---- run
    def _run(self, req: ColorRequest) -> Dict[str, Any]:
        project = req.project
        timeout = req.options.get("timeout") or self.timeout
        graph = OperationGraph(project)

        resolved_source = self.policy.resolve_input(project.source.path, f"source {project.source.source_id!r}")
        meta = self.skill.probe(str(resolved_source), timeout)
        source = self._source_facts(resolved_source, meta)
        if not source["has_video"]:
            raise ColorError("INVALID_INPUT", f"source {project.source.source_id!r} has no video stream", {"source_id": project.source.source_id, "reason": "no_video_stream"})

        source_ext = resolved_source.suffix.lower()
        line: Optional[str] = None
        if source["alpha"]:
            # Without --keep-alpha ffmpeg-skill/color writes H.264 4:2:0 (or ProRes 422) for every re-encoding mode: the
            # alpha plane would be dropped (ADR-25). With it, LUT_APPLY / PRIMARY_CORRECTION grade the colour and keep the
            # alpha in an alpha line (ADR-30); every other re-encoding operation is still refused before anything runs
            keep = self._supported("ffmpeg-skill/color:keep-alpha")
            line = alpha_line(source["codec"], source_ext) if keep else None
            graded = ALPHA_GRADING_OPERATIONS if line else ()
            lossy = sorted(op.op_id for op in project.operations if op.type in REENCODING_OPERATIONS and op.type not in graded)
            if lossy:
                if not keep:
                    why = ("re-encode through ffmpeg-skill/color, which writes no alpha here (this ffmpeg-skill has no --keep-alpha), so the alpha "
                           "would be lost. Grade the fill without its alpha deliberately (e.g. export it first), or update ffmpeg-skill")
                elif line is None:
                    why = (f"would need an alpha line, and none fits the {source_ext} container ({source['codec']} source); this skill does not "
                           "convert containers: rewrap it as .mov or .mkv first (ffmpeg-skill/export)")
                else:
                    why = (f"cannot keep an alpha: only {list(ALPHA_GRADING_OPERATIONS)} grade with the alpha kept (ffmpeg-skill/color "
                           "--keep-alpha); the others write an opaque picture. Export the fill without its alpha deliberately first")
                raise ColorError("UNSUPPORTED_FORMAT", f"source {project.source.source_id!r} has an alpha channel ({source['pix_fmt']}); operation(s) {lossy} {why}",
                                 {"reason": "alpha_not_preserved", "pix_fmt": source["pix_fmt"], "op_ids": lossy,
                                  "keep_alpha": {"available": keep, "operations": list(ALPHA_GRADING_OPERATIONS), "alpha_codec": line}})
            luts_on_rgb = sorted(op.op_id for op in project.operations if op.type == "LUT_APPLY")
            if line and luts_on_rgb and not str(source["pix_fmt"]).startswith("yuva"):
                # measured (ADR-30): ffmpeg-skill/color's --lut legs write BT.709 limited-range Y'CbCr, and on an RGB alpha
                # source the alpha line converts that back to RGB with another matrix on some FFmpeg builds (6.1: an
                # identity LUT moved 0x7f3ebf to 0x7a2fba). The alpha is kept and no fringe appears; the colour can shift
                self.warnings.append(f"LUT_APPLY {luts_on_rgb} on an RGB alpha source ({source['pix_fmt']}): ffmpeg-skill/color --lut's colour legs can "
                                     "shift colours on the way back to the RGB alpha line with some FFmpeg builds (BT.709 written, another matrix read); "
                                     "the alpha is kept. Check the colour, or grade a Y'CbCr alpha master (ProRes 4444) instead")
            if line and DEEP_ALPHA_PIX_FMT_RE.match(str(source["pix_fmt"])) and any(op.type in ALPHA_GRADING_OPERATIONS for op in project.operations):
                self.warnings.append(f"the source's alpha is deeper than 8 bits ({source['pix_fmt']}): ffmpeg-skill/color --keep-alpha carries it at 8 bits, "
                                     "so it comes back within one 8-bit code, not bit-identical (alpha.precision_bits in provenance)")

        input_paths = {str(resolved_source)}
        outputs: Dict[str, Path] = {}
        for o in project.outputs:
            target = self.policy.resolve_write_path(o.path, f"output {o.output_id!r}")
            if str(target) in input_paths:
                raise ColorError("OUTPUT_ERROR", f"output {o.output_id!r} would overwrite the input", {"reason": "input_output_collision", "path": str(target)})
            if target.exists() and not o.overwrite:
                raise ColorError("OUTPUT_ERROR", f"output {o.output_id!r} already exists (set overwrite: true to replace it)", {"reason": "exists", "path": str(target)})
            ext = OUTPUT_FORMATS[o.format]["extension"]
            if ext != source_ext:
                raise ColorError("UNSUPPORTED_FORMAT",
                                 f"output {o.output_id!r}: format {o.format!r} ({ext}) does not match the source container ({source_ext}); "
                                 "this skill does not convert containers, use ffmpeg-skill/export for that",
                                 {"output_id": o.output_id, "requested": ext, "source": source_ext})
            outputs[o.output_id] = target
        work_dir = self.policy.resolve_work_dir(os.path.join(WORK_DIR_NAME, project.project_id))
        if str(work_dir) in input_paths or any(str(t).startswith(str(work_dir) + os.sep) for t in outputs.values()):
            raise ColorError("OUTPUT_ERROR", "outputs may not live inside the work directory", {"reason": "output_in_work_dir", "work_dir": str(work_dir)})

        # LUTs: resolved and hashed up front (read-only, also under dry-run) so identities can depend on their content
        luts: Dict[str, LutInfo] = {}
        for op in project.operations:
            if op.type != "LUT_APPLY":
                continue
            lut_path = self.policy.resolve_lut(op.parameters["lut_path"], f"operation {op.op_id!r} lut_path")
            if lut_path.suffix.lower() not in CUBE_EXTENSIONS:
                raise ColorError("UNSUPPORTED_FORMAT", f"operation {op.op_id!r}: LUT must be a .cube file, got {lut_path.suffix!r}",
                                 {"op_id": op.op_id, "path": str(lut_path)})
            size = lut_path.stat().st_size
            if size <= 0:
                raise ColorError("INVALID_INPUT", f"operation {op.op_id!r}: LUT is empty", {"op_id": op.op_id, "path": str(lut_path)})
            if size > MAX_LUT_BYTES:
                raise ColorError("INVALID_INPUT", f"operation {op.op_id!r}: LUT is larger than {MAX_LUT_BYTES} bytes", {"op_id": op.op_id, "path": str(lut_path), "size": size})
            luts[f"op:{op.op_id}"] = LutInfo(lut_path, size, sha256_file(str(lut_path)))

        overrides: Dict[str, Dict[str, Any]] = {nid: {"lut_path": info.sha256} for nid, info in luts.items()}
        # OCIO_TRANSFORM: baked up front (pure computation, also under dry-run, nothing written) so the identity can
        # depend on the LUT's content and the config's, not on a config's path or the "latest" alias (ADR-22)
        baked: Dict[str, Any] = {}
        for op in project.operations:
            if op.type != "OCIO_TRANSFORM":
                continue
            config_path = None
            if not op.parameters["config"].startswith(ocio_mod.BUILTIN_PREFIX):
                config_path = self.policy.resolve_lut(op.parameters["config"], f"operation {op.op_id!r} config")
            try:
                baked[f"op:{op.op_id}"] = ocio_mod.bake(op.parameters, config_path)
            except ColorError as e:
                e.details.setdefault("op_id", op.op_id)
                raise
            b = baked[f"op:{op.op_id}"]
            info = getattr(self.skill, "_info", None)
            if info is not None and info.features.get("retag_rewrites_h264_tags") is False:
                # neither step can guarantee the destination's tags on this checkout (0.12.3): its --lut output's tags
                # depend on the FFmpeg build (all three BT.709 on 6.1, only the matrix on 8.0) and its --retag only writes a
                # container hint that a tagged H.264 stream overrides. Refused before anything is encoded (ADR-22)
                raise ColorError("TOOL_ERROR", f"operation {op.op_id!r}: OCIO_TRANSFORM needs ffmpeg-skill/color --retag to rewrite an H.264 stream's "
                                 "colour description (bsf:h264_metadata), which this ffmpeg-skill does not declare, so the destination's colour tags "
                                 f"({op.parameters['output_tags']}) cannot be guaranteed; update ffmpeg-skill", {"op_id": op.op_id, "reason": "retag_cannot_rewrite_tags",
                                 "ffmpeg_skill": info.version}, retryable=False)
            overrides[f"op:{op.op_id}"] = {"config": b.config["sha256"], "lut_sha256": b.sha256}
        clamp = self._supported("ffmpeg-skill/color:legalize")
        measure = self._supported("ffmpeg-skill/check:levels")
        for op in project.operations:
            if op.type == "LEGALIZE":
                b = legalize_mod.bake(op.parameters)
                # ADR-29: the soft clip and, where color has --legalize, the Y'CbCr clamp after 4:2:0 in the same encode;
                # the clamp changes the written pixels, so it is part of the identity
                b.clamp = ({"applied": True, "tool": "ffmpeg-skill/color --legalize", "standard": b.profile, "verification": None}
                           if clamp else None)
                if measure:
                    b.share = {"measured": False, "reason": legalize_mod.SHARES_PLANNED}
                baked[f"op:{op.op_id}"] = b
                overrides[f"op:{op.op_id}"] = {"lut_sha256": b.sha256, "legal_clamp": b.profile if clamp else None}
        # MATCH_CAMERA: the reference video is an input like the source (input PathPolicy), identified by its content
        references: Dict[str, LutInfo] = {}
        for op in project.operations:
            if op.type != "MATCH_CAMERA":
                continue
            ref = self.policy.resolve_input(op.parameters["reference_path"], f"operation {op.op_id!r} reference_path")
            references[f"op:{op.op_id}"] = LutInfo(ref, ref.stat().st_size, sha256_file(str(ref)))
            overrides[f"op:{op.op_id}"] = {"reference_path": references[f"op:{op.op_id}"].sha256}
            if any(str(t) == str(ref) for t in outputs.values()):
                raise ColorError("OUTPUT_ERROR", f"an output would overwrite the reference of operation {op.op_id!r}", {"reason": "input_output_collision", "path": str(ref)})
        # HDR_TO_SDR without a tonemap: the curve follows the input's transfer (ADR-28), resolved before the identities so
        # an HLG default and an explicit mobius share one identity (same output), and a pre-0.5.1 hable intermediate is not reused
        selections: Dict[str, Dict[str, Any]] = {}
        for n in graph.order:
            node = graph.nodes[n]
            if node.type == "HDR_TO_SDR":
                selections[n] = select_tonemap(node.parameters.get("tonemap"), self._input_transfer(graph, n, source))
                node.parameters["tonemap"] = selections[n]["tonemap"]
        identities = graph.identities(source["sha256"], self.tool_versions, overrides)
        states: Dict[str, NodeState] = {}
        for n in graph.order:
            node = graph.nodes[n]
            st = NodeState(node, identities[n])
            st.tool, st.capabilities = self._select_tool(node, line if line is not None and node.type in ALPHA_GRADING_OPERATIONS else None)
            if n in luts:
                st.lut = luts[n]
            if n in selections:
                st.tonemap_selection = selections[n]
            if line is not None and node.type in ALPHA_GRADING_OPERATIONS:
                st.alpha_line = line
            if n in references:
                st.reference = references[n]
                st.lut = LutInfo(work_dir / f"{st.identity[:16]}.cube", 0, "")    # written at execution, from the fitted frames
            if n in baked:
                st.baked = baked[n]
                st.lut = LutInfo(work_dir / f"{st.identity[:16]}.cube", len(baked[n].text.encode("utf-8")), baked[n].sha256)
            states[n] = st
        self._states = states

        plan = self._plan_document(graph, states, source, outputs, project, work_dir, resolved_source)
        if self.dry_run:
            return self._envelope(True, "ok", {"dry_run": True, "plan": plan, "results": [states[n].to_dict() for n in graph.order], "outputs": plan["outputs"]})

        work_dir.mkdir(parents=True, exist_ok=True)
        cancelled = False
        failure: Optional[ColorError] = None
        for n in graph.order:
            st = states[n]
            if st.node.type == "SOURCE":
                st.artifact = self._source_artifact(resolved_source, source)
                st.status = "completed"
                continue
            try:
                self._execute_node(states, st, source, resolved_source, source_ext, work_dir, timeout, req.options.get("reuse_intermediates", True) and self.reuse)
            except ColorError as e:
                st.status = "cancelled" if e.code == "CANCELLED" else "failed"
                st.error = e.to_dict()
                failure = e
                cancelled = e.code == "CANCELLED"
                break
        out_results: List[Dict[str, Any]] = []
        if failure is None:
            for o in project.outputs:
                try:
                    out_results.append(self._materialize(states[graph.output_nodes[o.output_id]], o, outputs[o.output_id], timeout))
                except ColorError as e:
                    failure = e
                    cancelled = e.code == "CANCELLED"
                    out_results.append({"output_id": o.output_id, "status": "cancelled" if cancelled else "failed", "path": str(outputs[o.output_id]), "error": e.to_dict()})
                    break
        for n in graph.order:
            if states[n].status == "planned":
                states[n].status = "skipped"
        body: Dict[str, Any] = {"dry_run": False, "plan": plan, "results": [states[n].to_dict() for n in graph.order], "outputs": out_results,
                                "tool_runs": [self._tool_run_dict(r) for r in self.skill.runs]}
        if failure is not None:
            body["error"] = failure.to_dict()
            return self._envelope(False, "cancelled" if cancelled else "error", body)
        return self._envelope(True, "ok", body)

    # ---- source facts
    @staticmethod
    def _source_facts(path: Path, meta: Dict[str, Any]) -> Dict[str, Any]:
        video = meta.get("video") or {}
        duration = float(meta.get("duration") or 0.0)
        return {"path": str(path), "sha256": sha256_file(str(path)), "size": os.path.getsize(str(path)), "duration": duration,
                "has_video": bool(video), "has_audio": bool(meta.get("audio")), "width": video.get("width"), "height": video.get("height"),
                "pix_fmt": video.get("pix_fmt"), "codec": video.get("codec"), "color_space": video.get("color_space"),
                "color_primaries": video.get("color_primaries"), "color_transfer": video.get("color_transfer"), "color_range": video.get("color_range"),
                "hdr": probe_hdr(video), "dolby_vision": bool(video.get("dolby_vision")), "alpha": has_alpha(video.get("pix_fmt")),
                "subtitle_streams": int(meta.get("subtitle_streams") or 0), "data_streams": int(meta.get("data_streams") or 0)}

    @staticmethod
    def _input_transfer(graph: OperationGraph, node_id: str, source: Dict[str, Any]) -> Optional[str]:
        """The transfer an operation's input will carry, known before anything runs: the source's measured transfer,
        through STRIP_DOVI (a stream copy that keeps the base layer's tags), or the transfer a RETAG writes. After any
        other operation (one that re-encodes) it is not known up front: None, which takes the general default."""
        nid = graph.nodes[node_id].inputs[0]
        while True:
            node = graph.nodes[nid]
            if node.type == "SOURCE":
                return source.get("color_transfer")
            if node.type == "RETAG":
                return RETAG_TAGS[node.parameters["target"]][2]
            if node.type != "STRIP_DOVI":
                return None
            nid = node.inputs[0]

    @staticmethod
    def _source_artifact(path: Path, source: Dict[str, Any]) -> Artifact:
        return Artifact(path, source["duration"], source["width"] or 0, source["height"] or 0, source["pix_fmt"], source["codec"],
                        source["color_space"], source["color_primaries"], source["color_transfer"], source["color_range"],
                        source["hdr"], source["dolby_vision"], source["size"], source["sha256"])

    # ---- planning
    def _select_tool(self, node: Node, alpha_line_: Optional[str] = None) -> Tuple[str, List[str]]:
        tool, extra = TOOL_FOR[node.type]
        if alpha_line_ is not None:
            # an alpha grade writes the alpha line, not H.264: its own encoder is what must be available (ADR-30)
            extra = [f"encoder:{ALPHA_LINE_ENCODERS[alpha_line_]}" if c == "encoder:libx264" else c for c in extra]
        caps = ["ffmpeg-skill", "ffmpeg", "ffprobe", *extra]
        missing = [c for c in caps if not self._capability_available(c)]
        if missing:
            raise ColorError("TOOL_ERROR", f"{node.type} needs capabilities that are not available: {missing}", {"node_id": node.node_id, "missing": missing}, retryable=False)
        return tool, caps

    def _supported(self, cap: str) -> bool:
        """An optional capability (OPTIONAL_CHECKOUT_CAPABILITIES) is used only when detected as supported: unknown
        falls back to what works without it."""
        return self.capabilities.get(cap) == "supported"

    def _capability_available(self, cap: str) -> bool:
        """unknown counts as available: the tool run reports the truth; only a detected 'unsupported' blocks planning."""
        return self.capabilities.get(cap, "unknown") != "unsupported"

    def _plan_document(self, graph: OperationGraph, states: Dict[str, NodeState], source: Dict[str, Any], outputs: Dict[str, Path],
                       project: Any, work_dir: Path, resolved_source: Path) -> Dict[str, Any]:
        caps = sorted({c for st in states.values() for c in st.capabilities})
        plan_id = stable_hash({"identities": {n: states[n].identity for n in graph.order}, "outputs": [(o.output_id, o.format) for o in project.outputs]})
        steps = [{"node_id": n, "operation_id": states[n].identity, "type": states[n].node.type, "tool": f"ffmpeg-skill/{states[n].tool}",
                  "input": states[n].node.inputs[0] if states[n].node.inputs else None, "parameters": states[n].node.parameters,
                  "intermediate": str(self._intermediate_path(work_dir, states[n], resolved_source.suffix)),
                  "lut": states[n].lut.to_dict() if states[n].lut else None,
                  **({states[n].baked.kind: states[n].baked.provenance()} if states[n].baked is not None else {}),
                  **({"tonemap_selection": states[n].tonemap_selection} if states[n].tonemap_selection is not None else {}),
                  **({"alpha": {"keep_alpha": True, "alpha_codec": states[n].alpha_line, "alpha_mode": ALPHA_MODE}} if states[n].alpha_line else {})}
                 for n in graph.order if states[n].node.type != "SOURCE"]
        return {"plan_id": plan_id, "project_id": project.project_id, "work_dir": str(work_dir), "graph": graph.to_dict(),
                "source": {k: v for k, v in source.items()},
                "steps": steps,
                "outputs": [{"output_id": o.output_id, "node_id": graph.output_nodes[o.output_id], "path": str(outputs[o.output_id]), "format": o.format,
                             "expect": o.expect} for o in project.outputs],
                "required_capabilities": caps, "tool_versions": dict(self.tool_versions), "duration_tolerance": DURATION_TOLERANCE}

    def _intermediate_path(self, work_dir: Path, st: NodeState, ext: str) -> Path:
        return work_dir / f"{st.identity[:16]}{ext}"

    # ---- execution
    def _quality_flag(self) -> str:
        """`--quality` on an ffmpeg-skill that offers it (1.10+; 2.0 removed `--crf` from color.py), else `--crf`: the
        same CRF value either way. Read from the capabilities the adapter detected (runtime_context reads them before
        any run); with nothing detected yet the long-standing `--crf` is assumed."""
        info = getattr(self.skill, "_info", None)
        return info.quality_flag if info is not None else "--crf"

    def _argv(self, st: NodeState, src: str, out_path: Path, hdr_input: bool = False) -> List[str]:
        """One ffmpeg-skill/color invocation (argv without the script and --json). Every value is a fixed flag, a
        formatted number, an enum member already validated against the model schema, or a resolved absolute path;
        nothing from the request is passed through verbatim as a filter or shell fragment."""
        node, p, o = st.node, st.node.parameters, str(out_path)
        if node.type == "HDR_TO_SDR":
            args = [src, "--to-sdr", "--tonemap", p["tonemap"], "--peak", fmt_num(p["peak_nits"]), "--desat", fmt_num(p["desat"])]
            if p["force"]:
                args.append("--force")
            args += ["--audio-stream", str(p["audio_stream"]), self._quality_flag(), str(p["crf"]), "--preset", p["preset"], "-o", o]
            return args
        if node.type in ("OCIO_TRANSFORM", "MATCH_CAMERA", "LEGALIZE"):
            assert st.lut is not None
            args = [src, "--lut", self._lut_arg(st.lut.path)]
            if node.type == "LEGALIZE" and st.baked is not None and st.baked.clamp is not None:
                args += ["--legalize", st.baked.profile]     # the clamp after 4:2:0, same encode (ADR-29)
            if hdr_input:
                # the LUT was baked for the source space the caller named (e.g. Rec.2100-PQ): ffmpeg-skill refuses an
                # HDR-tagged input to --lut without --force, which exists for exactly this case
                args.append("--force")
            args += ["--audio-stream", str(p["audio_stream"]), self._quality_flag(), str(p["crf"]), "--preset", p["preset"], "-o", o]
            return args
        if node.type == "LUT_APPLY":
            assert st.lut is not None
            args = [src, "--lut", self._lut_arg(st.lut.path), "--lut-strength", fmt_num(p["lut_strength"]),
                    "--audio-stream", str(p["audio_stream"]), self._quality_flag(), str(p["crf"]), "--preset", p["preset"], *self._alpha_args(st), "-o", o]
            return args
        if node.type == "RETAG":
            return [src, "--retag", p["target"], "--audio-stream", str(p["audio_stream"]), "-o", o]
        if node.type == "STRIP_DOVI":
            return [src, "--strip-dovi", "-o", o]
        if node.type == "PRIMARY_CORRECTION":
            args = [src, "--correct", "--exposure", fmt_num(p["exposure"]), "--contrast", fmt_num(p["contrast"]),
                    "--saturation", fmt_num(p["saturation"]), "--temperature", fmt_num(p["temperature"]), "--tint", fmt_num(p["tint"]),
                    "--gamma", fmt_num(p["gamma"]), "--lift", fmt_num(p["lift"]), "--gain", fmt_num(p["gain"]),
                    "--levels-in-black", str(p["levels_in_black"]), "--levels-in-white", str(p["levels_in_white"]),
                    "--levels-out-black", str(p["levels_out_black"]), "--levels-out-white", str(p["levels_out_white"])]
            if p["curves"] is not None:
                args += ["--curves", p["curves"]]
            args += ["--audio-stream", str(p["audio_stream"]), self._quality_flag(), str(p["crf"]), "--preset", p["preset"], *self._alpha_args(st), "-o", o]
            return args
        raise ColorError("INTERNAL_ERROR", f"no argv builder for {node.type}")

    @staticmethod
    def _alpha_args(st: NodeState) -> List[str]:
        """color --keep-alpha for an operation on an alpha source (ADR-30): the line picked from the input's codec, the
        colour taken and written straight. Every value is a table member, never request text."""
        if not st.alpha_line:
            return []
        return ["--keep-alpha", "--alpha-codec", st.alpha_line, "--alpha-mode", ALPHA_MODE]

    @staticmethod
    def _lut_arg(lut_path: Path) -> str:
        """The value passed to ffmpeg-skill's --lut: always just the LUT's own file name. `_execute_node` runs
        this call with `cwd` set to `lut_path.parent`, so the bare name is the whole path the subprocess needs --
        never a drive letter, never a colon, regardless of what drive the LUT happens to live on relative to the
        ffmpeg-skill checkout or the workspace.

        Measured reason this matters: ffmpeg-skill's own `escape_filter_path` backslash-escapes a Windows
        drive-letter colon for the `-vf lut3d=file=...` filter graph value (`C:\\...` -> `C\\:/...`), and at least
        one Windows ffmpeg build (gyan.dev 9.0.1 essentials, Windows Server 2025) rejects that escaping in a filter
        *option* value with "No option name near ..." -- an absolute Windows LUT path breaks LUT_APPLY outright.
        A relative-to-ffmpeg-skill's-own-directory path was tried first and still fails on GitHub Actions Windows
        runners specifically, because the repository checkout and the OS temp directory (where a workspace under
        pytest's tmp_path, or any caller's own temp workspace, typically lives) are on *different drives* there --
        no relative path can cross a Windows drive at all. Running the subprocess with `cwd` at the LUT's own
        directory has no such limitation: this is unrelated to ffmpeg-skill's own code (nothing there is changed)
        and is inert on POSIX (a bare file name in the process's own directory was already unambiguous there)."""
        return lut_path.name

    def _artifact_path(self, st: NodeState) -> str:
        if st.artifact is None:
            raise ColorError("DEPENDENCY_ERROR", f"input {st.node.node_id!r} has no artifact (status {st.status})", {"node_id": st.node.node_id})
        return str(st.artifact.path)

    def _execute_node(self, states: Dict[str, NodeState], st: NodeState, source: Dict[str, Any], resolved_source: Path, source_ext: str,
                      work_dir: Path, timeout: Optional[float], reuse: bool) -> None:
        node = st.node
        upstream = states[node.inputs[0]]
        st.input_hash = upstream.artifact.sha256 if upstream.artifact else None
        out_path = self._intermediate_path(work_dir, st, source_ext)
        manifest = out_path.with_suffix(out_path.suffix + ".json")
        if reuse and self._reusable(st, out_path, manifest):
            st.status = "reused"
            if (node.type == "LEGALIZE" and st.baked is not None and not st.baked.share.get("measured")
                    and self._supported("ffmpeg-skill/check:levels")):
                # a report left unmeasured (no check then, or a file it could not measure) is measured again, and kept
                self._measure_levels(st, upstream, timeout)
                m = json.loads(manifest.read_text(encoding="utf-8"))
                m["legalize"] = st.baked.provenance()
                manifest.write_text(json.dumps(m, indent=2, sort_keys=True), encoding="utf-8")
            return
        graded = out_path.with_name(out_path.stem + ".graded" + out_path.suffix) if node.type == "OCIO_TRANSFORM" else out_path
        for stale in (out_path, manifest, graded):
            if stale.exists():
                stale.unlink()
        try:
            if st.baked is not None:
                self._write_baked_lut(st)
            hdr_input = bool(upstream.artifact.hdr) if upstream.artifact else False
            if node.type == "MATCH_CAMERA":
                self._fit_match(st, upstream, work_dir, timeout)
            if node.type == "LEGALIZE" and hdr_input:
                raise ColorError("INVALID_INPUT", f"LEGALIZE {node.node_id}: the input is HDR; --lut writes BT.709 and the limits here are BT.709's, "
                                 "run HDR_TO_SDR first", {"reason": "hdr_input", "node_id": node.node_id})
            argv = self._argv(st, self._artifact_path(upstream), graded, hdr_input)
            # LUT_APPLY runs with cwd at the LUT's own directory so --lut can be a bare file name (see _lut_arg):
            # no drive letter, no colon, regardless of what drive the LUT/workspace/ffmpeg-skill checkout are on
            cwd = str(st.lut.path.parent) if st.lut is not None else None
            run = self.skill.run_tool("color", argv, timeout, cwd=cwd)
            self._absorb(st, run)
            self._check_tool_verification(st, run, graded)
            if node.type == "OCIO_TRANSFORM":
                self._tag_destination(st, graded, out_path, timeout)
            st.artifact = self._validate_artifact(out_path, st, source)
            if node.type == "MATCH_CAMERA":
                self._match_residual(st, out_path, work_dir, timeout)
            if node.type == "LEGALIZE":
                self._measure_levels(st, upstream, timeout)
        except ColorError:
            self._remove_partial(out_path)
            if graded != out_path:
                self._remove_partial(graded)
            raise
        manifest.write_text(json.dumps({"schema": MANIFEST_SCHEMA, "operation_id": st.identity, "type": node.type, "parameters": node.parameters,
                                        "input_hash": st.input_hash, "lut": st.lut.to_dict() if st.lut else None, "artifact": st.artifact.to_dict(),
                                        "measurements": st.measurements, "reencoded": st.reencoded, "dropped_non_av_streams": st.dropped_non_av_streams,
                                        "retagged": st.retagged, **({st.baked.kind: st.baked.provenance()} if st.baked is not None else {}),
                                        **({"alpha": st.alpha} if st.alpha is not None else {}),
                                        "tool": f"ffmpeg-skill/{st.tool}", "tool_versions": dict(self.tool_versions),
                                        "skill": SKILL_ID, "skill_version": VERSION}, indent=2, sort_keys=True), encoding="utf-8")
        st.status = "completed"

    def _absorb(self, st: NodeState, run: ToolRun) -> None:
        st.seconds += run.seconds
        st.tool_commands += run.commands
        if isinstance(run.data.get("measurements"), dict):
            st.measurements = dict(run.data["measurements"])
        if "reencoded" in run.data:
            st.reencoded = bool(run.data["reencoded"])
        if "dropped_non_av_streams" in run.data:
            st.dropped_non_av_streams = bool(run.data["dropped_non_av_streams"])

    def _check_tool_verification(self, st: NodeState, run: ToolRun, written: Path) -> None:
        """Read the verification steps ffmpeg-skill/color reports for what this skill relies on: `alpha` (the written
        alpha against the source's, ADR-30) and `legal_levels` (the clamp's measurement of the written file, ADR-29).
        color completes with `verified: false` when one fails; here that is a VALIDATION_ERROR, never a pass."""
        steps = [s for s in run.data.get("verification") or [] if isinstance(s, dict)]
        alpha_step = next((s for s in steps if s.get("step") == "alpha"), None)
        legal = next((s for s in steps if s.get("step") == "legal_levels"), None)
        what = f"{st.node.type} {st.node.node_id}"
        if st.alpha_line:
            st.alpha = {"kept": bool(alpha_step and alpha_step.get("ok")), "precision_bits": 8, "alpha_codec": run.data.get("alpha_codec", st.alpha_line),
                        "alpha_mode": run.data.get("alpha_mode", ALPHA_MODE), "source_colour": "taken as straight (not un-premultiplied)",
                        "tool": "ffmpeg-skill/color --keep-alpha", "verification": alpha_step}
            if alpha_step is None:
                raise ColorError("VALIDATION_ERROR", f"{what}: ffmpeg-skill/color --keep-alpha reported no alpha verification step",
                                 {"reason": "alpha_unverified", "path": str(written)})
        if alpha_step is not None and not alpha_step.get("ok"):
            hint = ("" if st.alpha_line else "; the source was not seen as having alpha (a VP9 stream with an alpha layer probes as yuv420p through "
                                              "ffmpeg-skill/probe), so it was graded without --keep-alpha: export it to ProRes 4444 / qtrle / PNG first")
            raise ColorError("VALIDATION_ERROR", f"{what}: ffmpeg-skill/color's alpha check failed ({alpha_step.get('reason') or 'alpha differs from the source'}){hint}",
                             {"reason": "alpha_lost" if alpha_step.get("reason") == "alpha_dropped" else "alpha_mismatch", "path": str(written),
                              "step": alpha_step})
        if st.baked is not None and getattr(st.baked, "clamp", None) is not None:
            st.baked.clamp = {**st.baked.clamp, "verification": legal, "residual": run.data.get("legalize", {}).get("output", {}).get("out_of_range_pct")
                              if isinstance(run.data.get("legalize"), dict) and isinstance(run.data["legalize"].get("output"), dict) else None}
            if legal is None or not legal.get("ok"):
                raise ColorError("VALIDATION_ERROR", f"{what}: the clamped file is not within {st.baked.profile} legal levels by ffmpeg-skill/color's own "
                                 f"measurement ({(legal or {}).get('reason') or (legal or {}).get('worst_frame') or 'no legal_levels step reported'})",
                                 {"reason": "legal_levels_failed", "path": str(written), "step": legal})

    def _measure_levels(self, st: NodeState, upstream: NodeState, timeout: Optional[float]) -> None:
        """LEGALIZE's report (ADR-29): the shares of luma / chroma samples outside the profile's limits on the input and
        on the written file, both by ffmpeg-skill/check --levels. Not measured (with the reason) on a checkout without it."""
        assert st.baked is not None and st.artifact is not None and upstream.artifact is not None
        if not self._supported("ffmpeg-skill/check:levels"):
            st.baked.share = {"measured": False, "reason": legalize_mod.SHARES_NOT_MEASURED}
            return
        before = self.skill.levels(str(upstream.artifact.path), st.baked.profile, timeout)
        after = self.skill.levels(str(st.artifact.path), st.baked.profile, timeout)
        st.baked.share = legalize_mod.share_report(st.baked.profile, before, after)

    @staticmethod
    def _write_baked_lut(st: NodeState) -> None:
        """The baked .cube next to the intermediates, written once per identity; its bytes are checked against the
        hash the identity was computed from, so what ffmpeg-skill reads is exactly what provenance names."""
        assert st.lut is not None and st.baked is not None
        path = st.lut.path
        if path.is_file() and sha256_file(str(path)) == st.baked.sha256:
            return
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(st.baked.text.encode("utf-8"))
        os.replace(str(tmp), str(path))
        if sha256_file(str(path)) != st.baked.sha256:
            raise ColorError("INTERNAL_ERROR", "baked LUT on disk does not match its hash", {"path": str(path)})

    def _match_frames_dir(self, st: NodeState, work_dir: Path) -> Path:
        return work_dir / f"{st.identity[:16]}.match"

    def _frame(self, video: str, at: float, base: Path, timeout: Optional[float]) -> Any:
        path = self.skill.look_frame(video, at, base, match_mod.ANALYSIS_WIDTH, timeout)
        return path, match_mod.read_png(path.read_bytes())

    def _fit_match(self, st: NodeState, upstream: NodeState, work_dir: Path, timeout: Optional[float]) -> None:
        """MATCH_CAMERA, before --lut: the reference frame and this input's frame (ffmpeg-skill/look), the fit, the LUT."""
        assert st.reference is not None and st.lut is not None and upstream.artifact is not None
        p = st.node.parameters
        if upstream.artifact.hdr:
            raise ColorError("INVALID_INPUT", f"MATCH_CAMERA {st.node.node_id}: the input is HDR; the fit works on display-referred SDR frames, "
                             "run HDR_TO_SDR (or OCIO_TRANSFORM) first", {"reason": "hdr_input", "node_id": st.node.node_id})
        frames = self._match_frames_dir(st, work_dir)
        ref_png, ref = self._frame(str(st.reference.path), p["reference_at"], frames / "reference", timeout)
        in_png, inp = self._frame(str(upstream.artifact.path), p["at"], frames / "input", timeout)
        ref_s, in_s, how = match_mod.samples(ref, inp, parse_patches(p["patches"]) if p["patches"] else None)
        f = match_mod.fit(p["model"], ref_s, in_s)
        text = ocio_mod.cube_text(match_mod.lut_values(f, p["lut_size"]), p["lut_size"], title="color-grading MATCH_CAMERA")
        data = text.encode("utf-8")
        tmp = st.lut.path.with_name(st.lut.path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(str(tmp), str(st.lut.path))
        st.lut = LutInfo(st.lut.path, len(data), sha256_file(str(st.lut.path)))
        predicted = [match_mod.apply_fit(f, c) for c in in_s]
        st.measurements = {"sampling": how, "analysis_width": match_mod.ANALYSIS_WIDTH, "fit": f,
                           "frames": {"reference": {"path": str(ref_png), "sha256": sha256_file(str(ref_png)), "at": p["reference_at"]},
                                      "input": {"path": str(in_png), "sha256": sha256_file(str(in_png)), "at": p["at"]}},
                           "delta_e2000": {"before": match_mod.residual(ref_s, in_s), "fit": match_mod.residual(ref_s, predicted)}}

    def _match_residual(self, st: NodeState, out_path: Path, work_dir: Path, timeout: Optional[float]) -> None:
        """MATCH_CAMERA, after --lut: the same frame of the written output against the reference -- what was achieved,
        measured on the encoded file, not predicted by the fit."""
        p = st.node.parameters
        frames = self._match_frames_dir(st, work_dir)
        ref = match_mod.read_png(Path(st.measurements["frames"]["reference"]["path"]).read_bytes())   # the frame the fit used
        out_png, out = self._frame(str(out_path), p["at"], frames / "output", timeout)
        ref_s, out_s, _ = match_mod.samples(ref, out, parse_patches(p["patches"]) if p["patches"] else None)
        st.measurements["frames"]["output"] = {"path": str(out_png), "sha256": sha256_file(str(out_png)), "at": p["at"]}
        st.measurements["delta_e2000"]["after"] = match_mod.residual(ref_s, out_s)

    def _tag_destination(self, st: NodeState, graded: Path, out_path: Path, timeout: Optional[float]) -> None:
        """OCIO_TRANSFORM's second step: the --lut output carries ffmpeg-skill's BT.709 tags; when the destination
        needs other tags (Rec.2100 PQ / HLG), ffmpeg-skill/color --retag rewrites them (a stream copy). The tags are
        measured on the result by _validate_artifact either way."""
        want = RETAG_TAGS[st.node.parameters["output_tags"]]
        video = self.skill.probe(str(graded)).get("video") or {}
        got = (video.get("color_space"), video.get("color_primaries"), video.get("color_transfer"))
        if got == want:
            os.replace(str(graded), str(out_path))
            st.retagged = False
            return
        run = self.skill.run_tool("color", [str(graded), "--retag", st.node.parameters["output_tags"], "--audio-stream",
                                            str(st.node.parameters["audio_stream"]), "-o", str(out_path)], timeout)
        self._absorb(st, run)
        st.reencoded = None
        st.retagged = True
        self._remove_partial(graded)

    def _reusable(self, st: NodeState, out_path: Path, manifest: Path) -> bool:
        if not (out_path.is_file() and manifest.is_file()):
            return False
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if m.get("schema") != MANIFEST_SCHEMA or m.get("operation_id") != st.identity:
            return False
        art = m.get("artifact") or {}
        if art.get("path") != str(out_path) or out_path.stat().st_size != art.get("size") or sha256_file(str(out_path)) != art.get("sha256"):
            return False
        prev = m.get("legalize") or {}
        if st.node.type == "LEGALIZE" and not (isinstance(prev.get("out_of_range_share"), dict) and isinstance(prev.get("clamp"), dict)):
            return False   # written before the shares and the clamp were recorded (ADR-29): run it again rather than report less
        st.artifact = Artifact(out_path, float(art["duration"]), int(art["width"]), int(art["height"]), art.get("pix_fmt"), art.get("codec"),
                               art.get("color_space"), art.get("color_primaries"), art.get("color_transfer"), art.get("color_range"),
                               bool(art.get("hdr")), bool(art.get("dolby_vision")), int(art["size"]), art["sha256"])
        st.measurements = dict(m.get("measurements") or {})
        st.reencoded = m.get("reencoded")
        st.dropped_non_av_streams = m.get("dropped_non_av_streams")
        st.retagged = m.get("retagged")
        st.alpha = m.get("alpha")
        if st.node.type == "LEGALIZE" and st.baked is not None:
            st.baked.share = dict(prev["out_of_range_share"])
            st.baked.clamp = dict(prev["clamp"]) if prev["clamp"].get("applied") else None
        if st.node.type == "MATCH_CAMERA" and isinstance(m.get("lut"), dict):
            st.lut = LutInfo(Path(m["lut"]["path"]), int(m["lut"]["size"]), m["lut"]["sha256"])
        st.tool_commands = []
        if st.baked is not None:
            self._write_baked_lut(st)
        return True

    @staticmethod
    def _remove_partial(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    # ---- validation
    def _validate_artifact(self, path: Path, st: NodeState, source: Dict[str, Any]) -> Artifact:
        what = f"{st.node.type} {st.node.node_id}"
        if not path.is_file():
            raise ColorError("OUTPUT_ERROR", f"{what}: tool reported success but wrote no file", {"reason": "missing_output", "path": str(path)})
        size = path.stat().st_size
        if size <= 0:
            raise ColorError("OUTPUT_ERROR", f"{what}: output is empty", {"reason": "empty_output", "path": str(path)})
        if not os.access(str(path), os.R_OK):
            raise ColorError("OUTPUT_ERROR", f"{what}: output is not readable", {"reason": "unreadable_output", "path": str(path)})
        try:
            meta = self.skill.probe(str(path))
        except ColorError as e:
            if e.code != "INVALID_INPUT":      # timeout / cancellation / tool failure keep their own code
                raise
            raise ColorError("VALIDATION_ERROR", f"{what}: output is not readable media: {e.message}", {"reason": "corrupt_output", "path": str(path)})
        video = meta.get("video") or {}
        if not video:
            raise ColorError("VALIDATION_ERROR", f"{what}: output has no video stream", {"reason": "no_video_stream", "path": str(path)})
        duration = float(meta.get("duration") or 0.0)
        if source["duration"] > 0 and abs(duration - source["duration"]) > DURATION_TOLERANCE:
            raise ColorError("VALIDATION_ERROR", f"{what}: output duration {duration:.3f}s differs from the source's {source['duration']:.3f}s by more than {DURATION_TOLERANCE}s",
                             {"reason": "duration_mismatch", "duration": duration, "expected": source["duration"], "tolerance": DURATION_TOLERANCE, "path": str(path)})
        width, height = video.get("width"), video.get("height")
        if source["width"] and width != source["width"] or source["height"] and height != source["height"]:
            raise ColorError("VALIDATION_ERROR", f"{what}: output resolution {width}x{height} differs from the source's {source['width']}x{source['height']}",
                             {"reason": "resolution_mismatch", "path": str(path)})
        if source.get("alpha") and not has_alpha(video.get("pix_fmt")):
            raise ColorError("VALIDATION_ERROR", f"{what}: the source's alpha channel ({source.get('pix_fmt')}) is gone from the output ({video.get('pix_fmt')})",
                             {"reason": "alpha_lost", "path": str(path), "source_pix_fmt": source.get("pix_fmt"), "pix_fmt": video.get("pix_fmt")})
        hdr, dolby_vision = probe_hdr(video), bool(video.get("dolby_vision"))
        if st.node.type == "HDR_TO_SDR" and hdr:
            raise ColorError("VALIDATION_ERROR", f"{what}: output is still tagged HDR (transfer={video.get('color_transfer')}, primaries={video.get('color_primaries')}) after tone mapping",
                             {"reason": "still_hdr", "path": str(path)})
        if st.node.type == "STRIP_DOVI" and dolby_vision:
            raise ColorError("VALIDATION_ERROR", f"{what}: output still carries Dolby Vision metadata after --strip-dovi", {"reason": "dolby_vision_remains", "path": str(path)})
        if st.node.type == "RETAG":
            want_space, want_prim, want_trc = RETAG_TAGS[st.node.parameters["target"]]
            got = (video.get("color_space"), video.get("color_primaries"), video.get("color_transfer"))
            if got != (want_space, want_prim, want_trc):
                raise ColorError("VALIDATION_ERROR", f"{what}: output colour tags {got} do not match the requested target {(want_space, want_prim, want_trc)}",
                                 {"reason": "retag_mismatch", "path": str(path), "got": list(got), "want": [want_space, want_prim, want_trc]})
        if st.node.type == "OCIO_TRANSFORM":
            want = RETAG_TAGS[st.node.parameters["output_tags"]]
            got = (video.get("color_space"), video.get("color_primaries"), video.get("color_transfer"))
            if got != want:
                raise ColorError("VALIDATION_ERROR", f"{what}: output colour tags {got} do not match the destination's {want} ({st.node.parameters['output_tags']})",
                                 {"reason": "destination_tags_mismatch", "path": str(path), "got": list(got), "want": list(want)})
        if st.alpha_line and not has_alpha(video.get("pix_fmt")):
            raise ColorError("VALIDATION_ERROR", f"{what}: output pixel format {video.get('pix_fmt')!r} has no alpha plane (alpha line {st.alpha_line})",
                             {"reason": "alpha_lost", "path": str(path), "pix_fmt": video.get("pix_fmt")})
        if not st.alpha_line and st.node.type in ("LUT_APPLY", "OCIO_TRANSFORM", "MATCH_CAMERA", "LEGALIZE") and video.get("pix_fmt") != "yuv420p":
            raise ColorError("VALIDATION_ERROR", f"{what}: output pixel format {video.get('pix_fmt')!r} is not 'yuv420p'", {"reason": "pix_fmt_mismatch", "path": str(path)})
        # subtitle/data-stream survival: ffmpeg-skill >= 0.12.1 tries to keep a source's subtitle/data streams
        # through every re-encoding colour operation and says so honestly (dropped_non_av_streams) when it could
        # not; independently re-probing the output (rather than trusting that flag alone) catches the case this
        # skill's own philosophy exists for -- a tool reporting "completed" while a stream silently vanished.
        src_subs, src_data = int(source.get("subtitle_streams") or 0), int(source.get("data_streams") or 0)
        if src_subs or src_data:
            out_subs, out_data = int(meta.get("subtitle_streams") or 0), int(meta.get("data_streams") or 0)
            if (out_subs < src_subs or out_data < src_data) and not st.dropped_non_av_streams:
                raise ColorError("VALIDATION_ERROR",
                                 f"{what}: source had {src_subs} subtitle stream(s)/{src_data} data stream(s) but output has {out_subs}/{out_data}, "
                                 "and ffmpeg-skill did not report dropping them",
                                 {"reason": "stream_loss_unreported", "path": str(path), "source_subtitle_streams": src_subs, "output_subtitle_streams": out_subs,
                                  "source_data_streams": src_data, "output_data_streams": out_data})
        return Artifact(path, duration, int(width or 0), int(height or 0), video.get("pix_fmt"), video.get("codec"),
                        video.get("color_space"), video.get("color_primaries"), video.get("color_transfer"), video.get("color_range"),
                        hdr, dolby_vision, size, sha256_file(str(path)))

    # ---- outputs
    def _materialize(self, st: NodeState, out: Any, target: Path, timeout: Optional[float]) -> Dict[str, Any]:
        """The requested output is the validated artifact's bytes at the requested path: colour operations do not
        change format, so no further ffmpeg-skill call is needed; the copy is re-validated like any other artifact."""
        assert st.artifact is not None
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        if existed and not out.overwrite:
            raise ColorError("OUTPUT_ERROR", f"output {out.output_id!r} appeared during execution", {"reason": "exists", "path": str(target)})
        try:
            shutil.copy2(str(st.artifact.path), str(target))
            art = self._validate_export(target, st.artifact, out.expect)
        except ColorError:
            self._remove_partial(target)
            raise
        if art.sha256 != st.artifact.sha256:
            self._remove_partial(target)
            raise ColorError("OUTPUT_ERROR", f"output {out.output_id!r}: copied file hash does not match the source artifact", {"reason": "copy_hash_mismatch", "path": str(target)})
        chain = self._provenance_chain(st)
        source_entry = next((c for c in chain if c["type"] == "SOURCE"), None)
        return {"output_id": out.output_id, "status": "completed", "path": str(target), "format": out.format, "artifact": art.to_dict(),
                "seconds": 0.0, "tool_commands_observed": [],
                "provenance": {"skill": SKILL_ID, "skill_version": VERSION, "tool_versions": dict(self.tool_versions),
                               "output_hash": art.sha256, "node_id": st.node.node_id, "operation_id": st.identity, "operations": chain,
                               "source": {"sha256": source_entry["output_hash"]} if source_entry else {}}}

    def _validate_export(self, path: Path, expected: Artifact, expect: Dict[str, Any]) -> Artifact:
        what = f"output {path.name}"
        if not path.is_file() or path.stat().st_size <= 0:
            raise ColorError("OUTPUT_ERROR", f"{what}: copy did not produce a non-empty file", {"reason": "missing_output", "path": str(path)})
        meta = self.skill.probe(str(path))
        video = meta.get("video") or {}
        if not video:
            raise ColorError("VALIDATION_ERROR", f"{what}: output has no video stream", {"reason": "no_video_stream", "path": str(path)})
        duration = float(meta.get("duration") or 0.0)
        exp_duration, tol = expect.get("duration", expected.duration), expect.get("duration_tolerance", DURATION_TOLERANCE)
        if exp_duration and abs(duration - exp_duration) > tol:
            raise ColorError("VALIDATION_ERROR", f"{what}: duration {duration:.3f}s differs from expected {exp_duration:.3f}s by more than {tol}s",
                             {"reason": "duration_mismatch", "path": str(path)})
        width, height = video.get("width"), video.get("height")
        exp_w, exp_h = expect.get("width", expected.width), expect.get("height", expected.height)
        if exp_w and width != exp_w or exp_h and height != exp_h:
            raise ColorError("VALIDATION_ERROR", f"{what}: resolution {width}x{height} differs from expected {exp_w}x{exp_h}", {"reason": "resolution_mismatch", "path": str(path)})
        if "pix_fmt" in expect and video.get("pix_fmt") != expect["pix_fmt"]:
            raise ColorError("VALIDATION_ERROR", f"{what}: pixel format {video.get('pix_fmt')!r} differs from expected {expect['pix_fmt']!r}", {"reason": "pix_fmt_mismatch", "path": str(path)})
        size = path.stat().st_size
        return Artifact(path, duration, int(width or 0), int(height or 0), video.get("pix_fmt"), video.get("codec"),
                        video.get("color_space"), video.get("color_primaries"), video.get("color_transfer"), video.get("color_range"),
                        probe_hdr(video), bool(video.get("dolby_vision")), size, sha256_file(str(path)))

    def _provenance_chain(self, st: NodeState) -> List[Dict[str, Any]]:
        """output -> operation -> ... -> source, as the executor observed it (status, hashes, tool)."""
        chain: List[Dict[str, Any]] = []
        seen: set = set()
        stack = [st]
        while stack:
            s = stack.pop()
            if s.node.node_id in seen:
                continue
            seen.add(s.node.node_id)
            entry = {"node_id": s.node.node_id, "operation_id": s.identity, "type": s.node.type, "status": s.status, "tool": f"ffmpeg-skill/{s.tool}" if s.tool else None,
                     "parameters": s.node.parameters, "input_hash": s.input_hash, "output_hash": s.artifact.sha256 if s.artifact else None}
            if s.lut is not None:
                entry["lut"] = s.lut.to_dict()
            if s.baked is not None:
                entry[s.baked.kind] = s.baked.provenance()
            if s.reference is not None:
                entry["reference"] = s.reference.to_dict()
            if s.retagged is not None:
                entry["retagged"] = s.retagged
            if s.tonemap_selection is not None:
                entry["tonemap_selection"] = s.tonemap_selection
            if s.alpha is not None:
                entry["alpha"] = s.alpha
            if s.measurements:
                entry["measurements"] = s.measurements
            if s.reencoded is not None:
                entry["reencoded"] = s.reencoded
            if s.dropped_non_av_streams is not None:
                entry["dropped_non_av_streams"] = s.dropped_non_av_streams
            chain.append(entry)
            stack.extend(self._states.get(i) for i in s.node.inputs if self._states.get(i) is not None)
        return chain

    @staticmethod
    def _tool_run_dict(r: ToolRun) -> Dict[str, Any]:
        return {"tool": f"ffmpeg-skill/{r.tool}", "exit_code": r.returncode, "seconds": r.seconds, "commands_observed": r.commands}
