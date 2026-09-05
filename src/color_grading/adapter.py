"""ffmpeg-skill adapter: the only module that starts a process.

- Locates an ffmpeg-skill checkout (explicit dir > COLOR_GRADING_FFMPEG_SKILL_DIR > VIDEO_AGENT_FFMPEG_SKILL_DIR >
  ~/.claude/skills/ffmpeg-skill > ./vendor/ffmpeg-skill > ../ffmpeg-skill) and reads its contract (`scripts/_contract.py
  --json --static`) to check contract_version and the flags this skill relies on.
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

SUPPORTED_CONTRACT_VERSION = "1.0"
# verified: scripts/color.py --to-sdr/--lut/--retag/--strip-dovi/--correct with
# --crf/--preset/--tonemap/--peak/--desat/--lut-strength/--force/--exposure/--contrast/--saturation/--temperature/--tint
SUPPORTED_MIN = (0, 9, 2)
SUPPORTED_MAX_EXCLUSIVE = (1, 0, 0)
ENV_DIR_KEYS = ("COLOR_GRADING_FFMPEG_SKILL_DIR", "VIDEO_AGENT_FFMPEG_SKILL_DIR")
TOOLS_USED = ("probe", "color")
# flags of the ffmpeg-skill input_schema this adapter emits; checked against the live contract in doctor
FLAGS_USED: Dict[str, Tuple[str, ...]] = {
    "probe": ("inputs",),
    "color": ("input", "output", "to_sdr", "lut", "retag", "strip_dovi", "correct", "tonemap", "peak", "desat", "lut_strength", "force",
              "exposure", "contrast", "saturation", "temperature", "tint", "crf", "preset", "json"),
}
_ENV_KEEP = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "TERM",
             "SYSTEMROOT", "SYSTEMDRIVE", "PATHEXT", "COMSPEC", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


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

    @property
    def supported(self) -> bool:
        return not self.problems


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
            if (c / "scripts" / "_contract.py").is_file() and all((c / "scripts" / f"{t}.py").is_file() for t in TOOLS_USED):
                return cls(c.resolve(), timeout)
        raise ColorError("TOOL_ERROR", "ffmpeg-skill not found (need scripts/_contract.py and scripts/{probe,color}.py)",
                         {"reason": "ffmpeg_skill_missing", "tried": tried}, retryable=False)

    def script(self, tool: str) -> str:
        if tool not in TOOLS_USED:
            raise ColorError("INTERNAL_ERROR", f"tool {tool!r} is not on the adapter allowlist")
        return str(self.directory / "scripts" / f"{tool}.py")

    # ---- contract of the located skill
    def info(self, timeout: float = 60.0) -> SkillInfo:
        if self._info is not None:
            return self._info
        problems: List[str] = []
        version, contract_version, tools = "unknown", "unknown", {}
        argv = [sys.executable, str(self.directory / "scripts" / "_contract.py"), "--json", "--static"]
        try:
            r = self._popen(argv, timeout)
            doc = json.loads(r[1] or "{}")
            version = str(doc.get("skill", {}).get("version", "unknown"))
            contract_version = str(doc.get("contract_version", "unknown"))
            tools = {t["name"]: t for t in doc.get("tools", []) if isinstance(t, dict) and "name" in t}
        except (ColorError, ValueError, OSError) as e:
            problems.append(f"cannot read ffmpeg-skill contract: {getattr(e, 'message', str(e))}")
        if contract_version != SUPPORTED_CONTRACT_VERSION:
            problems.append(f"ffmpeg-skill contract_version {contract_version} is not {SUPPORTED_CONTRACT_VERSION}")
        m = _VERSION_RE.match(version)
        if not m:
            problems.append(f"cannot parse ffmpeg-skill version {version!r}")
        else:
            v = tuple(int(x) for x in m.groups())
            if not (SUPPORTED_MIN <= v < SUPPORTED_MAX_EXCLUSIVE):
                problems.append(f"ffmpeg-skill {version} is outside the supported window [{'.'.join(map(str, SUPPORTED_MIN))}, {'.'.join(map(str, SUPPORTED_MAX_EXCLUSIVE))})")
        for tool, flags in FLAGS_USED.items():
            spec = tools.get(tool)
            if spec is None:
                problems.append(f"ffmpeg-skill tool {tool!r} is missing from its contract")
                continue
            props = spec.get("input_schema", {}).get("properties", {})
            missing = [f for f in flags if f not in props]
            if missing:
                problems.append(f"ffmpeg-skill/{tool} lacks flag(s) {missing}")
            if tool == "color" and spec.get("video_required") is not True:
                problems.append("ffmpeg-skill/color does not declare video_required")
        self._info = SkillInfo(self.directory, version, contract_version, tools, problems)
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
