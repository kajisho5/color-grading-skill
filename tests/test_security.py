"""Security tests: the AI boundary (no command / argv / filter passthrough), path escapes through the whole
process boundary, malformed input, and source-level guarantees (no shell). Most of these need no ffmpeg."""
import json
import os
import re
from pathlib import Path

import pytest

from color_grading import adapter
from color_grading.adapter import FfmpegSkill, fmt_num
from color_grading.errors import ColorError
from conftest import one_json, request_doc, run_cli

SRC = Path(__file__).resolve().parent.parent / "src" / "color_grading"


def test_no_shell_or_eval_in_source():
    text = "\n".join(p.read_text(encoding="utf-8") for p in SRC.glob("*.py"))
    for pattern in (r"shell\s*=\s*True", r"\bos\.system\(", r"\bos\.popen\(", r"\beval\(", r"\bexec\(", r"subprocess\.getoutput", r"subprocess\.call\("):
        assert not re.search(pattern, text), pattern
    assert text.count("subprocess.Popen(") == 1  # exactly one place starts a process


def test_only_allowlisted_ffmpeg_skill_scripts_can_run(tmp_path):
    sk = FfmpegSkill(tmp_path)
    for name in ("render", "batch", "cut", "../../bin/sh", "color; rm -rf /", "verify"):
        with pytest.raises(ColorError) as ei:
            sk.script(name)
        assert ei.value.code == "INTERNAL_ERROR"
    assert sk.script("color").endswith(os.path.join("scripts", "color.py"))
    assert sk.script("probe").endswith(os.path.join("scripts", "probe.py"))


def test_argv_formatting_never_passes_strings_through():
    assert fmt_num(1) == "1.0000" and fmt_num(12.3456789) == "12.3457"
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ColorError):
            fmt_num(bad)
    with pytest.raises(ColorError):
        fmt_num("hable")  # type: ignore[arg-type]


def test_nul_in_argv_is_refused(tmp_path):
    with pytest.raises(ColorError):
        FfmpegSkill(tmp_path)._popen(["python3", "x\x00y"], 1)


@pytest.mark.parametrize("payload", [
    {"target": "3dB,volume=10dB"}, {"target": "$(rm -rf /)"}, {"target": "bt709; rm -rf /"}, {"target": ["bt709"]}, {"target": {"value": "bt709"}},
])
def test_retag_parameter_injection_is_rejected(payload):
    from color_grading.model import parse_request
    with pytest.raises(ColorError) as ei:
        parse_request(request_doc([{"op_id": "r", "type": "RETAG", "input": "source", "parameters": payload}]))
    assert ei.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("payload", [
    {"lut_path": "x.cube", "lut_strength": "1; rm -rf /"}, {"lut_path": ["x.cube"]}, {"lut_path": "x.cube", "preset": "medium; rm -rf /"},
])
def test_lut_apply_parameter_injection_is_rejected(payload):
    from color_grading.model import parse_request
    with pytest.raises(ColorError) as ei:
        parse_request(request_doc([{"op_id": "l", "type": "LUT_APPLY", "input": "source", "parameters": payload}]))
    assert ei.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("path", ["../secret.mp4", "/etc/passwd", "out/../../x.mp4", "CON.mp4", "-i.mp4", "x\x00.mp4", "a|b.mp4"])
def test_unsafe_output_paths_through_cli(workspace, path):
    doc = request_doc([], outputs=[{"output_id": "o", "operation": "source", "path": path, "format": "mp4"}])
    code, out, err = run_cli(["plan", "-", "--json", "--workspace", str(workspace)], json.dumps(doc))
    d = one_json(out)
    assert d["ok"] is False and d["error"]["code"] in ("PATH_NOT_ALLOWED", "UNSUPPORTED_FORMAT", "INVALID_REQUEST"), d["error"]
    assert code != 0 and not (workspace / "x.mp4").exists()


def test_input_outside_allowed_roots_through_cli(workspace, media, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "in.mp4").write_bytes((workspace / "sdr.mp4").read_bytes())
    doc = request_doc([], source={"source_id": "a", "path": str(other / "in.mp4")})
    code, out, _ = run_cli(["plan", "-", "--json", "--workspace", str(workspace), "--allowed-input", str(workspace)], json.dumps(doc))
    d = one_json(out)
    assert d["ok"] is False and d["error"]["code"] == "PATH_NOT_ALLOWED" and d["error"]["details"]["reason"] == "outside_allowed_roots"
    code, out, _ = run_cli(["plan", "-", "--json", "--workspace", str(workspace)], json.dumps(doc))
    assert one_json(out)["ok"] is True   # readable anywhere without roots, like media-analysis-skill / audio-production-skill


def test_lut_outside_allowed_lut_roots_through_cli(workspace, tmp_path):
    lut_root = tmp_path / "luts"
    lut_root.mkdir()
    outside_lut = tmp_path / "elsewhere" / "x.cube"
    outside_lut.parent.mkdir()
    outside_lut.write_text((workspace / "invert.cube").read_text())
    doc = request_doc([{"op_id": "l", "type": "LUT_APPLY", "input": "source", "parameters": {"lut_path": str(outside_lut)}}])
    code, out, _ = run_cli(["plan", "-", "--json", "--workspace", str(workspace), "--allowed-lut", str(lut_root)], json.dumps(doc))
    d = one_json(out)
    assert d["ok"] is False and d["error"]["code"] == "PATH_NOT_ALLOWED"


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_symlink_escape_through_cli(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside)
    doc = request_doc([], outputs=[{"output_id": "o", "operation": "source", "path": "escape/out.mp4", "format": "mp4"}])
    code, out, _ = run_cli(["run", "-", "--json", "--workspace", str(workspace)], json.dumps(doc))
    d = one_json(out)
    assert d["ok"] is False and d["error"]["code"] == "PATH_NOT_ALLOWED"
    assert not (outside / "out.mp4").exists()


def test_output_may_not_overwrite_input(workspace):
    doc = request_doc([{"op_id": "r", "type": "RETAG", "input": "source", "parameters": {"target": "bt709"}}],
                      outputs=[{"output_id": "o", "operation": "op:r", "path": "sdr.mp4", "format": "mp4", "overwrite": True}])
    before = (workspace / "sdr.mp4").read_bytes()
    code, out, _ = run_cli(["run", "-", "--json"], json.dumps(doc))
    d = one_json(out)
    assert d["ok"] is False and d["error"]["code"] == "OUTPUT_ERROR" and d["error"]["details"]["reason"] == "input_output_collision"
    assert (workspace / "sdr.mp4").read_bytes() == before


def test_existing_output_is_not_overwritten_by_default(workspace):
    (workspace / "out").mkdir()
    (workspace / "out" / "main.mp4").write_bytes(b"precious")
    doc = request_doc([{"op_id": "r", "type": "RETAG", "input": "source", "parameters": {"target": "bt709"}}])
    code, out, _ = run_cli(["run", "-", "--json"], json.dumps(doc))
    d = one_json(out)
    assert d["ok"] is False and d["error"]["code"] == "OUTPUT_ERROR" and d["error"]["details"]["reason"] == "exists"
    assert (workspace / "out" / "main.mp4").read_bytes() == b"precious"


@pytest.mark.parametrize("text", ["", "{", "[1,2]", "null", '{"schema": 1}', "\x00", '{"a": NaN}'])
def test_malformed_documents_yield_one_json_error(workspace, text):
    code, out, err = run_cli(["run", "-", "--json"], text)
    d = one_json(out)
    assert d["ok"] is False and d["error"]["code"] == "INVALID_REQUEST" and code == 2
    assert out.count('"schema"') >= 1 and out.strip().startswith("{") and out.strip().endswith("}")


def test_request_with_command_fields_never_reaches_a_tool(workspace):
    doc = request_doc([{"op_id": "r", "type": "RETAG", "input": "source", "parameters": {"target": "bt709"}}])
    doc["project"]["operations"][0]["argv"] = ["ffmpeg", "-i", "x"]
    code, out, _ = run_cli(["run", "-", "--json"], json.dumps(doc))
    d = one_json(out)
    assert d["ok"] is False and d["error"]["code"] == "INVALID_REQUEST" and d["error"]["details"]["reason"] == "forbidden_field"
    assert not (workspace / ".color-grading").exists()


def test_ffmpeg_skill_dir_is_not_taken_from_the_request(workspace):
    doc = request_doc([])
    doc["options"] = {"ffmpeg_skill": "/tmp/evil"}
    code, out, _ = run_cli(["plan", "-", "--json"], json.dumps(doc))
    assert one_json(out)["error"]["code"] == "INVALID_REQUEST"


def test_bogus_ffmpeg_skill_dir_is_rejected(workspace, tmp_path):
    fake = tmp_path / "fake-skill"
    (fake / "scripts").mkdir(parents=True)
    for n in ("_contract", "probe", "color"):
        (fake / "scripts" / f"{n}.py").write_text("import sys; print('{}'); sys.exit(0)\n")
    code, out, _ = run_cli(["plan", "-", "--json", "--ffmpeg-skill", str(fake)], json.dumps(request_doc([])))
    d = one_json(out)
    assert d["ok"] is False and d["error"]["code"] == "TOOL_ERROR" and d["error"]["details"]["reason"] == "ffmpeg_skill_incompatible"
    code, out, _ = run_cli(["plan", "-", "--json", "--ffmpeg-skill", str(tmp_path / "missing")], json.dumps(request_doc([])))
    assert one_json(out)["error"]["details"]["reason"] == "ffmpeg_skill_missing"


def test_child_environment_is_minimal(monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "abc")
    env = adapter._clean_env()
    assert "SECRET_TOKEN" not in env and "PATH" in env and env["PYTHONUTF8"] == "1"


def test_argv_builder_uses_only_fixed_flags_numbers_enums_and_resolved_paths(workspace):
    """Every argv element the executor builds for ffmpeg-skill/color is a fixed flag, a formatted number, an enum
    already validated against the model schema, or an absolute path -- never a raw request string."""
    from color_grading.executor import Executor
    from color_grading.graph import Node

    ex = Executor(object(), FfmpegSkill(workspace))  # policy unused by _argv
    lut_path = str((workspace / "invert.cube").resolve())

    from color_grading.executor import LutInfo, NodeState

    hdr_node = Node("op:h", "HDR_TO_SDR", ["source"], {"tonemap": "hable", "peak_nits": 1000.0, "desat": 0.0, "force": True, "crf": 18, "preset": "medium"})
    hdr_st = NodeState(hdr_node)
    lut_node = Node("op:l", "LUT_APPLY", ["source"], {"lut_path": "x.cube", "lut_strength": 0.75, "crf": 20, "preset": "slow"})
    lut_st = NodeState(lut_node)
    lut_st.lut = LutInfo(Path(lut_path), 100, "0" * 64)
    retag_node = Node("op:r", "RETAG", ["source"], {"target": "bt601"})
    retag_st = NodeState(retag_node)
    dovi_node = Node("op:d", "STRIP_DOVI", ["source"], {})
    dovi_st = NodeState(dovi_node)

    src = str((workspace / "sdr.mp4").resolve())
    out = workspace / "o.mp4"

    flag_or_enum = re.compile(r"^(--[a-z-]+|-o|hable|mobius|reinhard|bt2390|clip|linear|gamma|bt709|bt2020-pq|bt2020-hlg|bt601|ultrafast|superfast|veryfast|faster|fast|medium|slow|slower|veryslow|placebo|\d+)$")
    num = re.compile(r"^-?\d+\.\d{4}$")
    # the --lut value is deliberately a path relative to the ffmpeg-skill subprocess's own cwd when possible
    # (executor._lut_arg: no drive-letter colon to escape, sidesteps a Windows ffmpeg filter-parser defect); it must
    # still resolve, from that cwd, to exactly the resolved LUT file -- never a raw, unvalidated request string
    safe_path = re.compile(r"^[^;&|$`\n\x00]+$")

    for st, src_path in ((hdr_st, src), (lut_st, src), (retag_st, src), (dovi_st, src)):
        argv = ex._argv(st, src_path, out)
        for a in argv:
            assert flag_or_enum.match(a) or num.match(a) or os.path.isabs(a) or safe_path.match(a), (st.node.type, a)

    assert ex._argv(hdr_st, src, out) == [src, "--to-sdr", "--tonemap", "hable", "--peak", "1000.0000", "--desat", "0.0000", "--force", "--crf", "18", "--preset", "medium", "-o", str(out)]
    lut_argv = ex._argv(lut_st, src, out)
    assert lut_argv[:2] == [src, "--lut"] and lut_argv[3:] == ["--lut-strength", "0.7500", "--crf", "20", "--preset", "slow", "-o", str(out)]
    assert (Path(ex.skill.directory) / lut_argv[2]).resolve() == Path(lut_path).resolve()
    assert ex._argv(retag_st, src, out) == [src, "--retag", "bt601", "-o", str(out)]
    assert ex._argv(dovi_st, src, out) == [src, "--strip-dovi", "-o", str(out)]
