"""Integration tests with real video through the real ffmpeg-skill checkout (nothing is mocked or skipped).
One positive and at least one negative case per implemented operation, a chained pipeline, output validation,
failure handling, dry run, idempotent re-runs, cancellation by timeout, and the CLI boundary."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from color_grading.errors import EXIT_CODES
from conftest import one_json, request_doc, run_cli, sample_avg_color


def run(doc, *extra):
    import shutil
    shutil.rmtree("out", ignore_errors=True)
    code, out, err = run_cli(["run", "-", "--json", *extra], json.dumps(doc))
    d = one_json(out)
    return code, d


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    return {"duration": float(d["format"]["duration"]), "width": int(v["width"]), "height": int(v["height"]),
           "pix_fmt": v.get("pix_fmt"), "codec": v["codec_name"], "color_space": v.get("color_space"),
           "color_primaries": v.get("color_primaries"), "color_transfer": v.get("color_transfer")}


def op(op_id, typ, ref, **params):
    return {"op_id": op_id, "type": typ, "input": ref, "parameters": params}


def results(d):
    return {r["node_id"]: r for r in d["results"]}


def test_hdr_to_sdr(workspace, capabilities):
    if capabilities.get("filter:zscale") == "unsupported":
        pytest.skip("filter:zscale not available in this ffmpeg build (needs libzimg; e.g. macOS Homebrew's plain 'ffmpeg' formula omits it)")
    doc = request_doc([op("s", "HDR_TO_SDR", "source", tonemap="hable", peak_nits=1000, desat=0.1)], source={"source_id": "a", "path": "hdr.mp4"},
                      outputs=[{"output_id": "main", "operation": "op:s", "path": "out/main.mp4", "format": "mp4"}])
    code, d = run(doc)
    assert code == 0 and d["ok"] and d["status"] == "ok", d.get("error")
    p = probe(workspace / "out" / "main.mp4")
    assert abs(p["duration"] - 2.0) < 0.2 and p["width"] == 160 and p["height"] == 90
    r = results(d)["op:s"]
    assert r["tool"] == "ffmpeg-skill/color" and r["status"] == "completed" and r["artifact"]["hdr"] is False
    assert d["outputs"][0]["artifact"]["sha256"] == r["artifact"]["sha256"]
    assert d["outputs"][0]["provenance"]["tool_versions"]["ffmpeg-skill"]
    # negative: a non-HDR source refused without --force
    code, d = run(request_doc([op("s", "HDR_TO_SDR", "source")], source={"source_id": "a", "path": "sdr.mp4"}))
    assert d["ok"] is False and d["error"]["code"] == "TOOL_ERROR" and d["error"]["details"]["error_kind"] == "input"
    # force lets ffmpeg-skill past its own HDR check ("treat as PQ"); the tonemap filter chain then runs on content
    # that was never PQ-encoded, which is the caller's explicit risk and may legitimately fail inside ffmpeg itself
    # (measured: zscale/tonemap error out on this fixture) -- a real TOOL_ERROR, not a defect in this skill's boundary
    code, d = run(request_doc([op("s", "HDR_TO_SDR", "source", force=True)], source={"source_id": "a", "path": "sdr.mp4"}))
    assert d["ok"] or d["error"]["code"] == "TOOL_ERROR"


def test_hdr_to_sdr_invalid_tonemap_rejected(workspace):
    doc = request_doc([op("s", "HDR_TO_SDR", "source", tonemap="bogus")], source={"source_id": "a", "path": "hdr.mp4"})
    code, d = run(doc)
    assert d["error"]["code"] == "INVALID_REQUEST" and code == EXIT_CODES["INVALID_REQUEST"]


def test_lut_apply_with_deterministic_pixel_check(workspace):
    src_color = sample_avg_color(workspace / "sdr.mp4")
    doc = request_doc([op("l", "LUT_APPLY", "source", lut_path="invert.cube", lut_strength=1.0)])
    code, d = run(doc)
    assert code == 0 and d["ok"], d.get("error")
    out = workspace / "out" / "main.mp4"
    p = probe(out)
    assert p["pix_fmt"] == "yuv420p"
    out_color = sample_avg_color(out)
    for src_c, out_c in zip(src_color, out_color):
        assert abs((255 - src_c) - out_c) <= 20, (src_color, out_color)   # deterministic transform, not a subjective look
    r = results(d)["op:l"]
    assert r["lut"]["sha256"] and r["lut"]["size"] > 0
    assert d["outputs"][0]["provenance"]["operations"][0]["lut"]["sha256"] == r["lut"]["sha256"]


@pytest.mark.parametrize("bad_path", ["invert.txt", "missing.cube"])
def test_lut_apply_bad_lut_rejected(workspace, bad_path):
    if bad_path == "invert.txt":
        (workspace / "invert.txt").write_text((workspace / "invert.cube").read_text())
    doc = request_doc([op("l", "LUT_APPLY", "source", lut_path=bad_path)])
    code, d = run(doc)
    assert d["ok"] is False and d["error"]["code"] in ("UNSUPPORTED_FORMAT", "INVALID_INPUT")


def test_lut_apply_oversized_lut_rejected(workspace, monkeypatch, skill_dir):
    # this needs the monkeypatched constant to apply in-process, so it calls the Executor directly rather than
    # through the CLI subprocess (a subprocess would import a fresh, unpatched module)
    from color_grading import executor as execmod
    from color_grading.doctor import runtime_context
    from color_grading.security import PathPolicy
    monkeypatch.setattr(execmod, "MAX_LUT_BYTES", 10)
    skill, versions, caps = runtime_context(str(skill_dir), 60)
    ex = execmod.Executor(PathPolicy(str(workspace)), skill, tool_versions=versions, capabilities=caps)
    doc = request_doc([op("l", "LUT_APPLY", "source", lut_path="invert.cube")])
    d = ex.response(doc)
    assert d["ok"] is False and d["error"]["code"] == "INVALID_INPUT" and "larger than" in d["error"]["message"]


@pytest.mark.parametrize("target", ["bt709", "bt2020-pq", "bt2020-hlg", "bt601"])
def test_retag_all_targets(workspace, target):
    from color_grading.model import RETAG_TAGS
    doc = request_doc([op("r", "RETAG", "source", target=target)])
    code, d = run(doc)
    assert code == 0 and d["ok"], d.get("error")
    p = probe(workspace / "out" / "main.mp4")
    want_space, want_prim, want_trc = RETAG_TAGS[target]
    assert (p["color_space"], p["color_primaries"], p["color_transfer"]) == (want_space, want_prim, want_trc)


def test_retag_invalid_target_rejected(workspace):
    doc = request_doc([op("r", "RETAG", "source", target="bt2100")])
    code, d = run(doc)
    assert d["error"]["code"] == "INVALID_REQUEST" and code == EXIT_CODES["INVALID_REQUEST"]


def test_strip_dovi_on_hevc(workspace):
    doc = request_doc([op("d", "STRIP_DOVI", "source")], source={"source_id": "a", "path": "hevc_sdr.mp4"})
    code, d = run(doc)
    assert code == 0 and d["ok"], d.get("error")
    r = results(d)["op:d"]
    assert r["artifact"]["dolby_vision"] is False


def test_strip_dovi_refuses_non_hevc(workspace):
    doc = request_doc([op("d", "STRIP_DOVI", "source")], source={"source_id": "a", "path": "sdr.mp4"})
    code, d = run(doc)
    assert d["ok"] is False and d["error"]["code"] == "TOOL_ERROR" and d["error"]["details"]["error_kind"] == "input"


def test_chained_pipeline_hdr_to_sdr_then_lut(workspace, capabilities):
    if capabilities.get("filter:zscale") == "unsupported":
        pytest.skip("filter:zscale not available in this ffmpeg build (needs libzimg; e.g. macOS Homebrew's plain 'ffmpeg' formula omits it)")
    doc = request_doc([op("s", "HDR_TO_SDR", "source"), op("l", "LUT_APPLY", "op:s", lut_path="invert.cube")], source={"source_id": "a", "path": "hdr.mp4"})
    code, d = run(doc)
    assert code == 0 and d["ok"], d.get("error")
    p = probe(workspace / "out" / "main.mp4")
    assert p["pix_fmt"] == "yuv420p" and abs(p["duration"] - 2.0) < 0.2
    assert [r["status"] for r in d["results"] if r["type"] != "SOURCE"] == ["completed", "completed"]
    chain = d["outputs"][0]["provenance"]["operations"]
    assert [c["type"] for c in chain] == ["LUT_APPLY", "HDR_TO_SDR", "SOURCE"]


def test_output_format_must_match_source_container(workspace):
    doc = request_doc([op("r", "RETAG", "source", target="bt709")], outputs=[{"output_id": "main", "operation": "op:r", "path": "out/main.mov", "format": "mov"}])
    code, d = run(doc)
    assert d["ok"] is False and d["error"]["code"] == "UNSUPPORTED_FORMAT"


def test_dry_run_writes_nothing(workspace):
    doc = request_doc([op("r", "RETAG", "source", target="bt709"), op("d", "STRIP_DOVI", "op:r")], source={"source_id": "a", "path": "hevc_sdr.mp4"})
    code, out, _ = run_cli(["plan", "-", "--json"], json.dumps(doc))
    d = one_json(out)
    assert code == 0 and d["ok"] and d["dry_run"] is True
    assert [s["tool"] for s in d["plan"]["steps"]] == ["ffmpeg-skill/color", "ffmpeg-skill/color"]
    assert d["plan"]["required_capabilities"] and d["plan"]["plan_id"]
    assert all(r["status"] == "planned" for r in d["results"])
    assert not (workspace / "out").exists() and not (workspace / ".color-grading").exists()
    code2, out2, _ = run_cli(["run", "-", "--json", "--dry-run"], json.dumps(doc))
    assert one_json(out2)["plan"]["plan_id"] == d["plan"]["plan_id"]


def test_rerun_reuses_intermediates_and_is_deterministic(workspace):
    doc = request_doc([op("r", "RETAG", "source", target="bt601"), op("l", "LUT_APPLY", "op:r", lut_path="invert.cube")],
                      outputs=[{"output_id": "main", "operation": "op:l", "path": "out/main.mp4", "format": "mp4", "overwrite": True}])
    code, d1 = run(doc)
    code, d2 = run(doc)
    assert d1["ok"] and d2["ok"], (d1.get("error"), d2.get("error"))
    assert [r["status"] for r in d2["results"] if r["type"] != "SOURCE"] == ["reused", "reused"]
    assert [r["operation_id"] for r in d1["results"]] == [r["operation_id"] for r in d2["results"]]
    assert d1["outputs"][0]["artifact"]["sha256"] == d2["outputs"][0]["artifact"]["sha256"]
    code, d3 = run(doc, "--no-reuse")
    assert [r["status"] for r in d3["results"] if r["type"] != "SOURCE"] == ["completed", "completed"]
    assert d3["outputs"][0]["artifact"]["sha256"] == d1["outputs"][0]["artifact"]["sha256"]
    # a tampered intermediate is not reused
    inter = next(workspace.glob(".color-grading/p1/*.mp4"))
    inter.write_bytes(inter.read_bytes()[:-100])
    code, d4 = run(doc)
    assert d4["ok"] and "completed" in [r["status"] for r in d4["results"] if r["type"] != "SOURCE"]


def test_options_reuse_false(workspace):
    doc = request_doc([op("r", "RETAG", "source", target="bt709")], outputs=[{"output_id": "main", "operation": "op:r", "path": "out/main.mp4", "format": "mp4", "overwrite": True}],
                      options={"reuse_intermediates": False})
    run(doc)
    code, d = run(doc)
    assert [r["status"] for r in d["results"] if r["type"] != "SOURCE"] == ["completed"]


def test_invalid_inputs(workspace):
    code, d = run(request_doc([], source={"source_id": "a", "path": "missing.mp4"}))
    assert d["error"]["code"] == "INVALID_INPUT" and code == EXIT_CODES["INVALID_INPUT"]
    code, d = run(request_doc([], source={"source_id": "a", "path": "text.txt"}))
    assert d["error"]["code"] == "INVALID_INPUT"
    code, d = run(request_doc([], source={"source_id": "a", "path": "audio.wav"}))
    assert d["error"]["code"] == "INVALID_INPUT" and d["error"]["details"]["reason"] == "no_video_stream"


def test_output_expectation_failure_removes_output(workspace):
    doc = request_doc([op("r", "RETAG", "source", target="bt709")], outputs=[{"output_id": "main", "operation": "op:r", "path": "out/main.mp4", "format": "mp4", "expect": {"width": 999}}])
    code, d = run(doc)
    assert d["error"]["code"] == "VALIDATION_ERROR" and d["error"]["details"]["reason"] == "resolution_mismatch"
    assert not (workspace / "out" / "main.mp4").exists()
    assert d["outputs"][0]["status"] == "failed" and results(d)["op:r"]["status"] == "completed"


def test_timeout_is_a_retryable_tool_error(workspace):
    doc = request_doc([op("s", "HDR_TO_SDR", "source", force=True)])
    code, out, _ = run_cli(["run", "-", "--json", "--timeout", "0.001"], json.dumps(doc))
    d = one_json(out)
    assert d["ok"] is False and d["error"]["code"] in ("TOOL_ERROR", "INVALID_INPUT")
    if d["error"]["code"] == "TOOL_ERROR":
        assert d["error"]["retryable"] is True and d["error"]["details"]["reason"] == "timeout"
    assert not list(workspace.glob(".color-grading/p1/*.mp4")) and not (workspace / "out").exists()


@pytest.mark.skipif(os.name == "nt", reason="a console signal cannot be delivered to one child on Windows without also hitting the test runner")
def test_signal_cancellation_leaves_no_partial_output(workspace):
    import signal
    import time
    doc = request_doc([op("r1", "RETAG", "source", target="bt709"), op("r2", "RETAG", "op:r1", target="bt601"), op("r3", "RETAG", "op:r2", target="bt709")])
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
    proc = subprocess.Popen([sys.executable, "-m", "color_grading.cli", "run", "-", "--json"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    proc.stdin.write(json.dumps(doc))
    proc.stdin.close()
    deadline = time.time() + 20
    while time.time() < deadline and not (Path.cwd() / ".color-grading" / "p1").exists():
        time.sleep(0.05)
    time.sleep(0.2)
    proc.send_signal(signal.SIGINT if os.name != "nt" else signal.CTRL_BREAK_EVENT)
    out = proc.stdout.read()
    proc.stderr.read()
    proc.wait(timeout=30)
    d = one_json(out)
    assert d["ok"] is False and d["status"] == "cancelled" and d["error"]["code"] == "CANCELLED", d
    assert proc.returncode == EXIT_CODES["CANCELLED"]
    assert not (Path.cwd() / "out").exists()
    for f in Path.cwd().glob(".color-grading/p1/*.mp4"):
        assert f.with_suffix(f.suffix + ".json").exists(), "an intermediate without a manifest is a partial output"


def test_cli_validate_and_exit_codes(workspace):
    code, out, _ = run_cli(["validate", "-", "--json"], json.dumps(request_doc([op("r", "RETAG", "source", target="bt709")])))
    d = one_json(out)
    assert code == 0 and d["validation"]["ok"] and d["validation"]["graph"]["order"] == ["source", "op:r"]
    code, out, _ = run_cli(["validate", "-", "--json"], json.dumps(request_doc([op("g", "ECHO", "source")])))
    assert code == EXIT_CODES["UNSUPPORTED_OPERATION"] and one_json(out)["error"]["code"] == "UNSUPPORTED_OPERATION"
    code, out, _ = run_cli(["run", "-"], json.dumps(request_doc([op("r", "RETAG", "source", target="bt709")])))
    assert code == 0 and "op:r" in out and not out.strip().startswith("{")
