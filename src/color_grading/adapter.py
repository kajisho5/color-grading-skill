"""ffmpeg-skill adapter: the only module that starts a process.

- Locates an ffmpeg-skill checkout (explicit dir > COLOR_GRADING_FFMPEG_SKILL_DIR > VIDEO_AGENT_FFMPEG_SKILL_DIR >
  ~/.claude/skills/ffmpeg-skill > ./vendor/ffmpeg-skill > ../ffmpeg-skill) and detects what it can do: the flags each
  tool takes are read from its contract (`scripts/_contract.py --json --static`, falling back to `scripts/contract.py`
  and finally to each tool's own `--help`). There is no version window: a checkout is usable when the flags this
  skill emits are present, whatever its version number (docs/decisions.md ADR-19).
- Runs one named tool as  [sys.executable, <dir>/scripts/<tool>.py, <typed argv...>, --json]  with a minimal
  environment, in its own process group, with a timeout; never a shell, never a request-supplied string as a flag.
- Every argv value is produced here from validated numbers / enums / resolved paths; paths are absolute so they can
  never be parsed as options (ffmpeg-skill places the input as a positional argument, the LUT after `--lut`).
- Parses the tool's JSON document ({"status": "completed"|"failed", ...}); exit code != 0 or status failed -> TOOL_ERROR.

Which ffmpeg-skill tools are used, and for what (docs/ffmpeg-skill.md):
  probe   source facts and every artifact's validation
  color   HDR_TO_SDR (--to-sdr), LUT_APPLY (--lut), RETAG (--retag), STRIP_DOVI (--strip-dovi), PRIMARY_CORRECTION (--correct)"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
import time

from .errors import ColorError

# contract_version every ffmpeg-skill release so far declares (1.x and 2.x alike); reported, not gated on -- a
# different value only matters if the flags below can no longer be read from it, and that is detected directly
KNOWN_CONTRACT_VERSION = "1.0"
ENV_DIR_KEYS = ("COLOR_GRADING_FFMPEG_SKILL_DIR", "VIDEO_AGENT_FFMPEG_SKILL_DIR")
TOOLS_USED = ("probe", "color")
# argparse dests of the ffmpeg-skill tools this adapter emits on every call of some operation type; every one must be
# present in the located checkout (detected, never inferred from a version number). Together they imply ffmpeg-skill
# >= 0.12.3 (--gamma/--lift/--gain/--levels-*/--curves first exist there), which is also the first release where
# --to-sdr/--lut/--correct report dropped_non_av_streams honestly (0.12.1), so that guarantee needs no check of its own.
REQUIRED_FLAGS: Dict[str, Tuple[str, ...]] = {
    "probe": ("inputs",),
    "color": ("input", "output", "to_sdr", "lut", "retag", "strip_dovi", "correct", "tonemap", "peak", "desat", "lut_strength", "force",
              "exposure", "contrast", "saturation", "temperature", "tint", "gamma", "lift", "gain",
              "levels_in_black", "levels_in_white", "levels_out_black", "levels_out_white", "curves",
              "audio_stream", "preset", "json"),
}
# one of each group must be present; the first the checkout offers is used. ffmpeg-skill 1.10 added --quality (same
# CRF scale, same default) and 2.0 removed --crf from color.py, so a 0.x checkout has only --crf and a 2.x only --quality.
FLAG_ALTERNATIVES: Dict[str, Dict[str, Tuple[str, ...]]] = {"color": {"encoder_quality": ("quality", "crf")}}
# every flag this adapter may emit, for the contract document (`ffmpeg_skill.flags_used`)
FLAGS_USED: Dict[str, Tuple[str, ...]] = {
    tool: flags + tuple(f for alts in FLAG_ALTERNATIVES.get(tool, {}).values() for f in alts) for tool, flags in REQUIRED_FLAGS.items()
}
_ENV_KEEP = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "TERM",
             "SYSTEMROOT", "SYSTEMDRIVE", "PATHEXT", "COMSPEC", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA")
_HELP_FLAG_RE = re.compile(r"(?<![\w-])--([a-z][a-z0-9-]*)")


def _clean_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_KEEP}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _group_kwargs() -> Dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
    return {"start_new_session": True}


def kill_tree(proc: "subprocess.Popen") -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    try:
        proc.communicate(timeout=5)
    except Exception:
        pass


def fmt_num(value: float, what: str = "value") -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value or value in (float("inf"), float("-inf")):
        raise ColorError("INTERNAL_ERROR", f"bad numeric {what} {value!r}")
    return f"{float(value):.4f}"


@dataclass
class ToolRun:
    tool: str
    argv: List[str]
    returncode: int
    data: Dict[str, Any]
    stderr_tail: str
    seconds: float
    commands: List[str] = field(default_factory=list)


@dataclass
class SkillInfo:
    directory: Path
    version: str
    contract_version: str
    tools: Dict[str, Dict[str, Any]]
    problems: List[str]
    # what was detected, and how: {"source": "contract"|"contract (with detection)"|"contract.py"|"--help",
    # "encoder_quality_flag": "--quality"|"--crf"|None, "overwrite_flag": bool, "common_layout": "_common.py"|
    # "_common/"|None, "removed": [...]}; doctor prints it verbatim so the report says what was found
    features: Dict[str, Any] = field(default_factory=dict)

    @property
    def supported(self) -> bool:
        return not self.problems

    @property
    def quality_flag(self) -> str:
        """The x264 CRF flag of ffmpeg-skill/color in this checkout (`--quality` from 1.10, `--crf` before 2.0)."""
        return self.features.get("encoder_quality_flag") or "--crf"


def _common_layout(directory: Path) -> Optional[str]:
    """ffmpeg-skill's shared helpers: one `scripts/_common.py` before 2.0, a `scripts/_common/` package from 2.0 on.
    Reported by doctor only; this skill never imports either."""
    if (directory / "scripts" / "_common.py").is_file():
        return "_common.py"
    if (directory / "scripts" / "_common" / "__init__.py").is_file():
        return "_common/"
    return None


def flags_from_help(text: str) -> Dict[str, Any]:
    """argparse dests readable from a tool's `--help`, for a checkout whose contract cannot be read: every `--long-flag`
    (dashes -> underscores) and every name listed under "positional arguments". The same keys a contract's
    input_schema.properties carries, so the flag checks below work on either."""
    props: Dict[str, Any] = {m.group(1).replace("-", "_"): {} for m in _HELP_FLAG_RE.finditer(text)}
    in_positional = False
    for line in text.splitlines():
        if line.strip().lower().startswith("positional arguments"):
            in_positional = True
            continue
        if in_positional:
            if line and not line[0].isspace():
                in_positional = False
                continue
            words = line.split()
            if words and line.startswith("  ") and not line.startswith("    " * 2) and re.match(r"^[a-z_][a-z0-9_]*$", words[0]):
                props[words[0]] = {}
    return props


def check_tools(tools: Dict[str, Dict[str, Any]]) -> Tuple[List[str], Dict[str, Any]]:
    """Compare the flags this skill emits with what the located checkout declares. Returns (problems, features); an
    empty problem list means every operation type can be run. Pure: unit-testable against any contract document."""
    problems: List[str] = []
    features: Dict[str, Any] = {}
    for tool, flags in REQUIRED_FLAGS.items():
        spec = tools.get(tool)
        if spec is None:
            problems.append(f"ffmpeg-skill tool {tool!r} is missing")
            continue
        props = (spec.get("input_schema") or {}).get("properties") or {}
        missing = [f for f in flags if f not in props]
        for group, alternatives in FLAG_ALTERNATIVES.get(tool, {}).items():
            found = next((f for f in alternatives if f in props), None)
            features[f"{group}_flag"] = "--" + found.replace("_", "-") if found else None
            if found is None:
                missing.append("|".join(alternatives))
        if missing:
            problems.append(f"ffmpeg-skill/{tool} lacks flag(s) {missing}")
        if tool == "color":
            features["overwrite_flag"] = "overwrite" in props
            if "video_required" in spec and spec.get("video_required") is not True:
                problems.append("ffmpeg-skill/color does not declare video_required")
    return problems, features


def probe_hdr(video: Dict[str, Any]) -> bool:
    """HDR in the sense this skill has always used: a PQ/HLG transfer *or* BT.2020 primaries (or Dolby Vision).
    ffmpeg-skill 2.0 narrowed probe's `hdr` to the transfer alone and moved that meaning to `bt2020_or_hdr`; 1.x and
    0.x carry only `hdr`, with this meaning. Whichever key the installed version writes is read."""
    if "bt2020_or_hdr" in video:
        return bool(video.get("bt2020_or_hdr"))
    return bool(video.get("hdr"))


class FfmpegSkill:
    """Handle on one ffmpeg-skill checkout."""

    def __init__(self, directory: Path, timeout: float = 900.0):
        self.directory = directory
        self.timeout = timeout
        self._info: Optional[SkillInfo] = None
        self.runs: List[ToolRun] = []
        self._cancelled = False

    # ---- discovery
    @staticmethod
    def candidates(explicit: Optional[str] = None) -> List[Path]:
        out: List[Path] = []
        if explicit:
            return [Path(explicit)]      # an explicit directory is never silently replaced by a fallback
        for key in ENV_DIR_KEYS:
            v = os.environ.get(key)
            if v:
                out.append(Path(v))
        out.append(Path.home() / ".claude" / "skills" / "ffmpeg-skill")
        out.append(Path.cwd() / "vendor" / "ffmpeg-skill")
        out.append(Path.cwd().parent / "ffmpeg-skill")
        return out

    @classmethod
    def locate(cls, explicit: Optional[str] = None, timeout: float = 900.0) -> "FfmpegSkill":
        tried = []
        for c in cls.candidates(explicit):
            tried.append(str(c))
            if all((c / "scripts" / f"{t}.py").is_file() for t in TOOLS_USED):
                return cls(c.resolve(), timeout)
        raise ColorError("TOOL_ERROR", "ffmpeg-skill not found (need scripts/{probe,color}.py)",
                         {"reason": "ffmpeg_skill_missing", "tried": tried}, retryable=False)

    def script(self, tool: str) -> str:
        if tool not in TOOLS_USED:
            raise ColorError("INTERNAL_ERROR", f"tool {tool!r} is not on the adapter allowlist")
        return str(self.directory / "scripts" / f"{tool}.py")

    # ---- what the located skill can do
    def _read_contract(self, timeout: float) -> Tuple[Optional[Dict[str, Any]], str, List[str]]:
        """The first contract document one of ffmpeg-skill's contract entry points prints, newest first."""
        errors: List[str] = []
        scripts = self.directory / "scripts"
        attempts = [("contract", scripts / "_contract.py", ["--json", "--static"]),
                    ("contract (with detection)", scripts / "_contract.py", ["--json"]),
                    ("contract.py", scripts / "contract.py", ["--json"])]
        for label, path, extra in attempts:
            if not path.is_file():
                continue
            try:
                code, out, err, _ = self._popen([sys.executable, str(path), *extra], timeout)
                doc = json.loads(out or "{}")
            except (ColorError, ValueError, OSError) as e:
                errors.append(f"{path.name} {' '.join(extra)}: {getattr(e, 'message', str(e))}")
                continue
            if code == 0 and isinstance(doc, dict) and isinstance(doc.get("tools"), list):
                return doc, label, errors
            errors.append(f"{path.name} {' '.join(extra)}: exit {code}, no tools list")
        return None, "", errors

    def _tools_from_help(self, timeout: float) -> Dict[str, Dict[str, Any]]:
        tools: Dict[str, Dict[str, Any]] = {}
        for tool in TOOLS_USED:
            try:
                code, out, _, _ = self._popen([sys.executable, self.script(tool), "--help"], timeout)
            except (ColorError, OSError):
                continue
            if code == 0 and out.strip():
                tools[tool] = {"name": tool, "input_schema": {"properties": flags_from_help(out)}}
        return tools

    def info(self, timeout: float = 60.0) -> SkillInfo:
        if self._info is not None:
            return self._info
        problems: List[str] = []
        features: Dict[str, Any] = {}
        version, contract_version, tools = "unknown", "unknown", {}
        doc, source, errors = self._read_contract(timeout)
        if doc is not None:
            version = str((doc.get("skill") or {}).get("version", "unknown"))
            contract_version = str(doc.get("contract_version", "unknown"))
            tools = {t["name"]: t for t in doc.get("tools", []) if isinstance(t, dict) and "name" in t}
        else:
            tools = self._tools_from_help(timeout)
            source = "--help" if tools else "none"
            version = _package_version(self.directory)
        if not tools:
            problems.append("cannot read ffmpeg-skill's tool flags (no contract, no --help): " + ("; ".join(errors) or "nothing to run"))
        else:
            tool_problems, features = check_tools(tools)
            problems += tool_problems
        features.update({"source": source, "common_layout": _common_layout(self.directory),
                         "removed": [r.get("what") for r in (doc or {}).get("removed") or [] if isinstance(r, dict)]})
        self._info = SkillInfo(self.directory, version, contract_version, tools, problems, features)
        return self._info

    # ---- execution
    def cancel(self) -> None:
        self._cancelled = True

    def _popen(self, argv: Sequence[str], timeout: Optional[float], cwd: Optional[str] = None) -> Tuple[int, str, str, float]:
        for a in argv:
            if not isinstance(a, str) or "\x00" in a:
                raise ColorError("INTERNAL_ERROR", "argv element is not a clean string")
        if self._cancelled:
            raise ColorError("CANCELLED", "cancelled before the tool started")
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, errors="replace", env=_clean_env(), cwd=cwd or str(self.directory), **_group_kwargs())
        except FileNotFoundError as e:
            raise ColorError("TOOL_ERROR", f"cannot start {os.path.basename(argv[0])}: {e}", {"reason": "executable_missing"}, retryable=False)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            raise ColorError("TOOL_ERROR", f"{os.path.basename(argv[1]) if len(argv) > 1 else argv[0]} exceeded {timeout}s", {"reason": "timeout", "timeout": timeout})
        except KeyboardInterrupt:
            kill_tree(proc)
            self._cancelled = True
            raise ColorError("CANCELLED", "interrupted while a tool was running", {"reason": "signal"})
        return proc.returncode, out or "", err or "", round(time.monotonic() - t0, 3)

    def run_tool(self, tool: str, args: Sequence[str], timeout: Optional[float] = None, cwd: Optional[str] = None) -> ToolRun:
        argv = [sys.executable, self.script(tool), *args, "--json"]
        code, out, err, seconds = self._popen(argv, timeout or self.timeout, cwd)
        data = _parse_json(out)
        tail = "\n".join(err.strip().splitlines()[-12:])
        run = ToolRun(tool, argv, code, data, tail, seconds, list(data.get("commands", [])) if isinstance(data.get("commands"), list) else [])
        self.runs.append(run)
        if code != 0 or (isinstance(data, dict) and data.get("status") == "failed"):
            msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else None
            raise ColorError("TOOL_ERROR", f"ffmpeg-skill/{tool} failed (exit {code}): {msg or tail or 'no message'}",
                             {"reason": "tool_failed", "tool": f"ffmpeg-skill/{tool}", "exit_code": code, "stderr_tail": tail,
                              "error_kind": (data.get("error") or {}).get("kind") if isinstance(data.get("error"), dict) else None})
        return run

    # ---- typed helpers
    def probe(self, path: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        """ffmpeg-skill/probe prints its document directly (no status envelope)."""
        argv = [sys.executable, self.script("probe"), path]
        code, out, err, seconds = self._popen(argv, timeout or min(self.timeout, 120.0))
        data = _parse_json(out)
        self.runs.append(ToolRun("probe", argv, code, data, "\n".join(err.strip().splitlines()[-12:]), seconds))
        if code != 0 or not isinstance(data, dict) or "duration" not in data:
            raise ColorError("INVALID_INPUT", f"ffmpeg-skill/probe could not read {os.path.basename(path)}: {err.strip().splitlines()[-1] if err.strip() else 'no output'}",
                             {"reason": "unreadable_media", "path": path, "exit_code": code})
        return data


def _package_version(directory: Path) -> str:
    """ffmpeg-skill's version from package.json, for a checkout whose contract cannot be read; informational only."""
    try:
        return str(json.loads((directory / "package.json").read_text(encoding="utf-8")).get("version", "unknown"))
    except (OSError, ValueError, AttributeError):
        return "unknown"


def _parse_json(text: str) -> Dict[str, Any]:
    """ffmpeg-skill prints one JSON document on stdout under --json; tolerate a preceding plain line."""
    text = text.strip()
    if not text:
        return {}
    start = text.find("{")
    if start < 0:
        return {}
    try:
        doc = json.loads(text[start:])
    except ValueError:
        try:
            doc = json.loads(text.splitlines()[-1])
        except ValueError:
            return {}
    return doc if isinstance(doc, dict) else {}
