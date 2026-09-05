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
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import RESPONSE_SCHEMA_VERSION, SKILL_ID, VERSION
from .adapter import FfmpegSkill, ToolRun, fmt_num
from .canonical import sha256_file, stable_hash
from .errors import ColorError
from .graph import Node, OperationGraph
from .model import CUBE_EXTENSIONS, MAX_LUT_BYTES, OUTPUT_FORMATS, RETAG_TAGS, ColorRequest, parse_request
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
}


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
    error: Optional[Dict[str, Any]] = None
    input_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"node_id": self.node.node_id, "operation_id": self.identity, "type": self.node.type,
                             "tool": f"ffmpeg-skill/{self.tool}" if self.tool else None, "required_capabilities": self.capabilities,
                             "input": self.node.inputs[0] if self.node.inputs else None, "parameters": self.node.parameters, "status": self.status,
                             "input_hash": self.input_hash, "seconds": self.seconds}
        d["artifact"] = self.artifact.to_dict() if self.artifact else None
        if self.lut is not None:
            d["lut"] = self.lut.to_dict()
        d["tool_commands_observed"] = list(self.tool_commands)
        d["measurements"] = self.measurements
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

        overrides = {nid: {"lut_path": info.sha256} for nid, info in luts.items()}
        identities = graph.identities(source["sha256"], self.tool_versions, overrides)
        states: Dict[str, NodeState] = {}
        for n in graph.order:
            node = graph.nodes[n]
            st = NodeState(node, identities[n])
            st.tool, st.capabilities = self._select_tool(node)
            if n in luts:
                st.lut = luts[n]
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
                "hdr": bool(video.get("hdr")), "dolby_vision": bool(video.get("dolby_vision"))}

    @staticmethod
    def _source_artifact(path: Path, source: Dict[str, Any]) -> Artifact:
        return Artifact(path, source["duration"], source["width"] or 0, source["height"] or 0, source["pix_fmt"], source["codec"],
                        source["color_space"], source["color_primaries"], source["color_transfer"], source["color_range"],
                        source["hdr"], source["dolby_vision"], source["size"], source["sha256"])

    # ---- planning
    def _select_tool(self, node: Node) -> Tuple[str, List[str]]:
        tool, extra = TOOL_FOR[node.type]
        caps = ["ffmpeg-skill", "ffmpeg", "ffprobe", *extra]
        missing = [c for c in caps if not self._capability_available(c)]
        if missing:
            raise ColorError("TOOL_ERROR", f"{node.type} needs capabilities that are not available: {missing}", {"node_id": node.node_id, "missing": missing}, retryable=False)
        return tool, caps

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
                  "lut": states[n].lut.to_dict() if states[n].lut else None} for n in graph.order if states[n].node.type != "SOURCE"]
        return {"plan_id": plan_id, "project_id": project.project_id, "work_dir": str(work_dir), "graph": graph.to_dict(),
                "source": {k: v for k, v in source.items()},
                "steps": steps,
                "outputs": [{"output_id": o.output_id, "node_id": graph.output_nodes[o.output_id], "path": str(outputs[o.output_id]), "format": o.format,
                             "expect": o.expect} for o in project.outputs],
                "required_capabilities": caps, "tool_versions": dict(self.tool_versions), "duration_tolerance": DURATION_TOLERANCE}

    def _intermediate_path(self, work_dir: Path, st: NodeState, ext: str) -> Path:
        return work_dir / f"{st.identity[:16]}{ext}"

    # ---- execution
    def _argv(self, st: NodeState, src: str, out_path: Path) -> List[str]:
        """One ffmpeg-skill/color invocation (argv without the script and --json). Every value is a fixed flag, a
        formatted number, an enum member already validated against the model schema, or a resolved absolute path;
        nothing from the request is passed through verbatim as a filter or shell fragment."""
        node, p, o = st.node, st.node.parameters, str(out_path)
        if node.type == "HDR_TO_SDR":
            args = [src, "--to-sdr", "--tonemap", p["tonemap"], "--peak", fmt_num(p["peak_nits"]), "--desat", fmt_num(p["desat"])]
            if p["force"]:
                args.append("--force")
            args += ["--crf", str(p["crf"]), "--preset", p["preset"], "-o", o]
            return args
        if node.type == "LUT_APPLY":
            assert st.lut is not None
            args = [src, "--lut", self._lut_arg(st.lut.path), "--lut-strength", fmt_num(p["lut_strength"]), "--crf", str(p["crf"]), "--preset", p["preset"], "-o", o]
            return args
        if node.type == "RETAG":
            return [src, "--retag", p["target"], "-o", o]
        if node.type == "STRIP_DOVI":
            return [src, "--strip-dovi", "-o", o]
        raise ColorError("INTERNAL_ERROR", f"no argv builder for {node.type}")

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
            return
        for stale in (out_path, manifest):
            if stale.exists():
                stale.unlink()
        try:
            argv = self._argv(st, self._artifact_path(upstream), out_path)
            # LUT_APPLY runs with cwd at the LUT's own directory so --lut can be a bare file name (see _lut_arg):
            # no drive letter, no colon, regardless of what drive the LUT/workspace/ffmpeg-skill checkout are on
            cwd = str(st.lut.path.parent) if st.lut is not None else None
            run = self.skill.run_tool("color", argv, timeout, cwd=cwd)
            st.seconds += run.seconds
            st.tool_commands += run.commands
            st.artifact = self._validate_artifact(out_path, st, source)
        except ColorError:
            self._remove_partial(out_path)
            raise
        manifest.write_text(json.dumps({"schema": MANIFEST_SCHEMA, "operation_id": st.identity, "type": node.type, "parameters": node.parameters,
                                        "input_hash": st.input_hash, "lut": st.lut.to_dict() if st.lut else None, "artifact": st.artifact.to_dict(),
                                        "measurements": st.measurements, "tool": f"ffmpeg-skill/{st.tool}", "tool_versions": dict(self.tool_versions),
                                        "skill": SKILL_ID, "skill_version": VERSION}, indent=2, sort_keys=True), encoding="utf-8")
        st.status = "completed"

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
        st.artifact = Artifact(out_path, float(art["duration"]), int(art["width"]), int(art["height"]), art.get("pix_fmt"), art.get("codec"),
                               art.get("color_space"), art.get("color_primaries"), art.get("color_transfer"), art.get("color_range"),
                               bool(art.get("hdr")), bool(art.get("dolby_vision")), int(art["size"]), art["sha256"])
        st.measurements = dict(m.get("measurements") or {})
        st.tool_commands = []
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
        hdr, dolby_vision = bool(video.get("hdr")), bool(video.get("dolby_vision"))
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
        if st.node.type == "LUT_APPLY" and video.get("pix_fmt") != "yuv420p":
            raise ColorError("VALIDATION_ERROR", f"{what}: output pixel format {video.get('pix_fmt')!r} is not 'yuv420p'", {"reason": "pix_fmt_mismatch", "path": str(path)})
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
                        bool(video.get("hdr")), bool(video.get("dolby_vision")), size, sha256_file(str(path)))

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
            chain.append(entry)
            stack.extend(self._states.get(i) for i in s.node.inputs if self._states.get(i) is not None)
        return chain

    @staticmethod
    def _tool_run_dict(r: ToolRun) -> Dict[str, Any]:
        return {"tool": f"ffmpeg-skill/{r.tool}", "exit_code": r.returncode, "seconds": r.seconds, "commands_observed": r.commands}
