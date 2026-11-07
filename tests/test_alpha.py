"""Alpha sources (ECOSYSTEM_ROADMAP.md step 8, docs/decisions.md ADR-25, ADR-30).

Where the located ffmpeg-skill/color has --keep-alpha, LUT_APPLY and PRIMARY_CORRECTION grade an alpha source with the
alpha split off, untouched, and merged back into an alpha line; the tests read the written alpha and colour back
themselves (ffmpeg rawvideo). Every other re-encoding operation, and every one of them on an ffmpeg-skill without
--keep-alpha, is refused instead of losing the alpha without a word; stream copies keep it, and an output that lost it
fails validation."""
import json
import shutil
import subprocess

import pytest

from color_grading.errors import ColorError
from color_grading.executor import (ALPHA_GRADING_OPERATIONS, ALPHA_LINE_EXTENSIONS, Executor, NodeState, REENCODING_OPERATIONS, alpha_line,
                                    has_alpha)
from color_grading.graph import Node
from color_grading.security import PathPolicy
from conftest import one_json, request_doc, run_cli

OPS = {
    "HDR_TO_SDR": {"force": True},
    "LUT_APPLY": {"lut_path": "invert.cube"},
    "PRIMARY_CORRECTION": {"saturation": 0.5},
    "OCIO_TRANSFORM": {"source_space": "ACEScct", "destination_space": "ACEScg", "output_tags": "bt709"},
    "MATCH_CAMERA": {"reference_path": "camera_a.mp4"},
    "LEGALIZE": {},
}
KEEP = "ffmpeg-skill/color:keep-alpha"


@pytest.mark.parametrize("pix_fmt,alpha", [("yuva444p10le", True), ("yuva420p", True), ("gbrap16le", True), ("rgba", True), ("argb", True),
                                           ("ya8", True), ("rgba64be", True), ("yuv420p", False), ("yuv444p12le", False), ("gbrp", False),
                                           ("rgb24", False), ("0rgb", False), ("gray", False), (None, False)])
def test_alpha_pixel_formats(pix_fmt, alpha):
    assert has_alpha(pix_fmt) is alpha


@pytest.mark.parametrize("codec,ext,line", [("prores", ".mov", "prores4444"), ("prores", ".mkv", "prores4444"), ("qtrle", ".mov", "qtrle"),
                                            ("qtrle", ".mkv", None), ("png", ".mov", "png"), ("vp9", ".mkv", "vp9"), ("vp9", ".mov", None),
                                            ("ffv1", ".mkv", "prores4444"), (None, ".mov", "prores4444"), ("prores", ".mp4", None),
                                            ("hevc", ".mp4", None)])
def test_alpha_line_follows_the_input_codec_and_fits_the_container(codec, ext, line):
    assert alpha_line(codec, ext) == line
    if line is not None:
        assert ext in ALPHA_LINE_EXTENSIONS[line]


def _doc(op_type, params, path="alpha.mov", project_id="p1"):
    ext = path.rsplit(".", 1)[1]
    return request_doc([{"op_id": "x", "type": op_type, "input": "source", "parameters": params}], source={"source_id": "a", "path": path},
                       outputs=[{"output_id": "main", "operation": "op:x", "path": f"out/main.{ext}", "format": ext}], project_id=project_id)


def _run(doc, cmd="run"):
    shutil.rmtree("out", ignore_errors=True)
    code, out, _ = run_cli([cmd, "-", "--json"], json.dumps(doc))
    return code, one_json(out)


def test_every_reencoding_operation_is_listed():
    assert set(OPS) == set(REENCODING_OPERATIONS) and set(ALPHA_GRADING_OPERATIONS) < set(REENCODING_OPERATIONS)


@pytest.mark.parametrize("op_type", sorted(OPS))
@pytest.mark.parametrize("cmd", ["run", "plan"])
def test_reencoding_an_alpha_source_is_graded_with_its_alpha_or_refused(workspace, capabilities, op_type, cmd):
    assert subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", str(workspace / "alpha.mov")],
                          capture_output=True, text=True, check=True).stdout.strip().startswith("yuva")
    keep = capabilities.get(KEEP) == "supported"
    code, d = _run(_doc(op_type, OPS[op_type]), cmd)
    if keep and op_type in ALPHA_GRADING_OPERATIONS:
        assert code == 0 and d["ok"], d.get("error")
        step = next(s for s in d["plan"]["steps"] if s["type"] == op_type)
        assert step["alpha"] == {"keep_alpha": True, "alpha_codec": "prores4444", "alpha_mode": "straight"}
        if cmd == "run":
            r = next(x for x in d["results"] if x["type"] == op_type)
            assert has_alpha(r["artifact"]["pix_fmt"]) and r["artifact"]["codec"] == "prores"
            assert r["alpha"]["kept"] is True and r["alpha"]["alpha_codec"] == "prores4444" and r["alpha"]["alpha_mode"] == "straight"
            assert r["alpha"]["verification"]["step"] == "alpha" and r["alpha"]["verification"]["ok"] is True
            assert any("alphaextract" in c and "alphamerge" in c for c in r["tool_commands_observed"]), r["tool_commands_observed"]
            assert has_alpha(d["outputs"][0]["artifact"]["pix_fmt"])
            assert _alpha(workspace / "out" / "main.mov") and len(_alpha(workspace / "out" / "main.mov")) == len(_alpha(workspace / "alpha.mov"))
            # the 10-bit source alpha comes back through the grade's 8-bit split: within one 8-bit code, every sample
            assert max(abs(a - b) for a, b in zip(_alpha(workspace / "alpha.mov"), _alpha(workspace / "out" / "main.mov"))) <= 1
        else:
            assert not (workspace / "out").exists()
        return
    assert code != 0 and d["error"]["code"] == "UNSUPPORTED_FORMAT", d.get("error")
    det = d["error"]["details"]
    assert det["reason"] == "alpha_not_preserved" and det["op_ids"] == ["x"]
    assert det["keep_alpha"] == {"available": keep, "operations": list(ALPHA_GRADING_OPERATIONS), "alpha_codec": "prores4444" if keep else None}
    assert not (workspace / "out").exists() and not (workspace / ".color-grading").exists()


def _alpha(path):
    """Every frame's alpha plane, 8-bit, as bytes."""
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "alphaextract,format=gray", "-f", "rawvideo", "-"],
                          capture_output=True, check=True).stdout


def _rgba(path):
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba", "-"], capture_output=True, check=True).stdout


def _keep_alpha_or_refused(capabilities, code, d):
    if capabilities.get(KEEP) == "supported":
        assert code == 0 and d["ok"], d.get("error")
        return True
    assert code != 0 and d["error"]["details"]["reason"] == "alpha_not_preserved", d.get("error")
    return False


def test_a_lossless_alpha_is_bit_identical_after_a_lut_and_its_edges_have_no_fringe(workspace, capabilities):
    """The roadmap's Done when, on a straight source: the qtrle fixture stays qtrle, its alpha is bit-identical after
    the LUT, and the graded colour is the same in every pixel whatever its alpha (a fringe is a colour that depends on
    the alpha: darker or lighter towards the transparent edge)."""
    code, d = _run(_doc("LUT_APPLY", OPS["LUT_APPLY"], path="alpha_rle.mov"))
    if not _keep_alpha_or_refused(capabilities, code, d):
        return
    out = workspace / "out" / "main.mov"
    r = next(x for x in d["results"] if x["type"] == "LUT_APPLY")
    assert r["alpha"]["alpha_codec"] == "qtrle" and r["artifact"]["codec"] == "qtrle" and r["artifact"]["pix_fmt"] == "argb"
    src_alpha, out_alpha = _alpha(workspace / "alpha_rle.mov"), _alpha(out)
    assert len(src_alpha) == len(out_alpha) == 2 * 25 * 160 * 90 and src_alpha == out_alpha
    assert min(src_alpha) == 0 and max(src_alpha) == 255             # the ramp really covers transparent to opaque
    px = _rgba(out)
    colours = [tuple(px[i:i + 3]) for i in range(0, len(px), 4)]
    # one colour from fully transparent to opaque pixels (a rounding code allowed): no fringe
    assert all(max(c) - min(c) <= 1 for c in zip(*colours)), sorted(set(colours))[:8]
    # the LUT ran (0x8040c0 inverted is 0x7fbf3f: green up, red and blue down) ...
    r0, g0, b0 = colours[0]
    assert g0 > 150 and r0 < 0x80 + 10 and b0 < 0x80, colours[0]
    # ... and --lut's legs on an RGB alpha source are named in a warning: they convert back with another matrix on some
    # FFmpeg builds (ADR-30: an identity LUT moved 0x7f3ebf to 0x7a2fba with FFmpeg 6.1), a colour shift, not a fringe
    assert any("RGB alpha source" in w for w in d["warnings"]), d["warnings"]


def test_a_chain_keeps_the_alpha_through_every_grade(workspace, capabilities):
    ops = [{"op_id": "lut", "type": "LUT_APPLY", "input": "source", "parameters": OPS["LUT_APPLY"]},
           {"op_id": "pc", "type": "PRIMARY_CORRECTION", "input": "op:lut", "parameters": {"exposure": 0.3}}]
    doc = request_doc(ops, source={"source_id": "a", "path": "alpha_rle.mov"},
                      outputs=[{"output_id": "main", "operation": "op:pc", "path": "out/main.mov", "format": "mov"}])
    code, d = _run(doc)
    if not _keep_alpha_or_refused(capabilities, code, d):
        assert sorted(d["error"]["details"]["op_ids"]) == ["lut", "pc"]
        return
    for r in d["results"]:
        if r["type"] != "SOURCE":
            assert has_alpha(r["artifact"]["pix_fmt"]), r
    assert [r["alpha"]["alpha_codec"] for r in d["results"] if r.get("alpha")] == ["qtrle", "qtrle"]
    assert _alpha(workspace / "alpha_rle.mov") == _alpha(workspace / "out" / "main.mov")
    chain = d["outputs"][0]["provenance"]["operations"]
    assert {o["type"] for o in chain if o.get("alpha", {}).get("kept")} == {"LUT_APPLY", "PRIMARY_CORRECTION"}


def test_an_alpha_source_in_a_container_no_alpha_line_fits_is_refused(workspace, capabilities):
    """No alpha line goes in .mp4: the fixture's picture as PNG frames in an .mp4 (which holds PNG) is an alpha source
    this skill cannot write an alpha output for, since it does not convert containers."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(workspace / "alpha_rle.mov"), "-t", "0.2", "-c:v", "png", "-pix_fmt", "rgba",
                    str(workspace / "alpha_png.mp4")], check=True)
    code, d = _run(_doc("PRIMARY_CORRECTION", OPS["PRIMARY_CORRECTION"], path="alpha_png.mp4"), "plan")
    assert code != 0 and d["error"]["details"]["reason"] == "alpha_not_preserved"
    keep = capabilities.get(KEEP) == "supported"
    assert d["error"]["details"]["keep_alpha"]["available"] is keep and d["error"]["details"]["keep_alpha"]["alpha_codec"] is None
    if keep:
        assert ".mp4" in d["error"]["message"]


def test_a_stream_copy_keeps_the_alpha_or_fails_naming_it(workspace):
    code, out, _ = run_cli(["run", "-", "--json"], json.dumps(_doc("RETAG", {"target": "bt709"})))
    d = one_json(out)
    print("RETAG on alpha:", "kept" if d["ok"] else d["error"]["details"].get("reason"))
    if d["ok"]:
        r = next(x for x in d["results"] if x["type"] == "RETAG")
        assert has_alpha(r["artifact"]["pix_fmt"]) and has_alpha(d["outputs"][0]["artifact"]["pix_fmt"])
    else:   # an ffmpeg-skill whose --retag falls back to a re-encode: the loss is reported, never silent
        assert d["error"]["code"] == "VALIDATION_ERROR" and d["error"]["details"]["reason"] in ("alpha_lost", "alpha_mismatch"), d["error"]
        assert not (workspace / "out" / "main.mov").exists()


# ---- units (no ffmpeg): what this skill sends and how it reads ffmpeg-skill's verification
class _Run:
    def __init__(self, data):
        self.data = data


def _state(node_type="LUT_APPLY", line="prores4444"):
    st = NodeState(Node("op:x", node_type, ["source"], {}), "identity16")
    st.alpha_line = line
    return st


def test_keep_alpha_flags_come_from_the_tables_only():
    assert Executor._alpha_args(_state()) == ["--keep-alpha", "--alpha-codec", "prores4444", "--alpha-mode", "straight"]
    assert Executor._alpha_args(_state(line=None)) == []


@pytest.mark.parametrize("step,reason", [(None, "alpha_unverified"), ({"step": "alpha", "ok": False, "reason": "alpha_dropped"}, "alpha_lost"),
                                         ({"step": "alpha", "ok": False, "comparison": {"ok": False}}, "alpha_mismatch")])
def test_a_failed_or_missing_alpha_step_is_a_validation_error(tmp_path, step, reason):
    ex = Executor(PathPolicy(str(tmp_path)), skill=None)
    data = {"status": "completed", "verified": step is not None and step.get("ok", False), "verification": [{"step": "probe", "ok": True}] + ([step] if step else [])}
    with pytest.raises(ColorError) as e:
        ex._check_tool_verification(_state(), _Run(data), tmp_path / "x.mov")
    assert e.value.code == "VALIDATION_ERROR" and e.value.details["reason"] == reason


def test_an_alpha_step_failing_on_a_source_not_seen_as_alpha_is_still_caught(tmp_path):
    """A VP9 stream with an alpha layer probes as yuv420p: this skill does not see the alpha, ffmpeg-skill/color does."""
    ex = Executor(PathPolicy(str(tmp_path)), skill=None)
    data = {"verification": [{"step": "alpha", "ok": False, "reason": "alpha_dropped"}]}
    with pytest.raises(ColorError) as e:
        ex._check_tool_verification(_state("PRIMARY_CORRECTION", line=None), _Run(data), tmp_path / "x.mkv")
    assert e.value.details["reason"] == "alpha_lost"


def test_a_passing_alpha_step_is_recorded(tmp_path):
    ex = Executor(PathPolicy(str(tmp_path)), skill=None)
    step = {"step": "alpha", "ok": True, "comparison": {"ok": True, "worst_delta": 0.0}}
    st = _state()
    ex._check_tool_verification(st, _Run({"verification": [step], "alpha_codec": "prores4444", "alpha_mode": "straight"}), tmp_path / "x.mov")
    assert st.alpha == {"kept": True, "alpha_codec": "prores4444", "alpha_mode": "straight", "source_colour": "taken as straight (not un-premultiplied)",
                        "tool": "ffmpeg-skill/color --keep-alpha", "verification": step}
