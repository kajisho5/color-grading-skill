"""Alpha guard (ECOSYSTEM_ROADMAP.md step 8, docs/decisions.md ADR-25). ffmpeg-skill/color writes no alpha plane in any
re-encoding mode, so a source with alpha is refused for those operations instead of losing its alpha without a word;
stream copies keep it, and an output that lost it fails validation."""
import json
import subprocess

import pytest

from color_grading.executor import REENCODING_OPERATIONS, has_alpha
from conftest import one_json, request_doc, run_cli

OPS = {
    "HDR_TO_SDR": {"force": True},
    "LUT_APPLY": {"lut_path": "invert.cube"},
    "PRIMARY_CORRECTION": {"saturation": 0.5},
    "OCIO_TRANSFORM": {"source_space": "ACEScct", "destination_space": "ACEScg", "output_tags": "bt709"},
    "MATCH_CAMERA": {"reference_path": "camera_a.mp4"},
    "LEGALIZE": {},
}


@pytest.mark.parametrize("pix_fmt,alpha", [("yuva444p10le", True), ("yuva420p", True), ("gbrap16le", True), ("rgba", True), ("argb", True),
                                           ("ya8", True), ("rgba64be", True), ("yuv420p", False), ("yuv444p12le", False), ("gbrp", False),
                                           ("rgb24", False), ("0rgb", False), ("gray", False), (None, False)])
def test_alpha_pixel_formats(pix_fmt, alpha):
    assert has_alpha(pix_fmt) is alpha


def _doc(op_type, params):
    return request_doc([{"op_id": "x", "type": op_type, "input": "source", "parameters": params}], source={"source_id": "a", "path": "alpha.mov"},
                       outputs=[{"output_id": "main", "operation": "op:x", "path": "out/main.mov", "format": "mov"}])


def test_every_reencoding_operation_is_listed():
    assert set(OPS) == set(REENCODING_OPERATIONS)


@pytest.mark.parametrize("op_type", sorted(OPS))
@pytest.mark.parametrize("cmd", ["run", "plan"])
def test_reencoding_an_alpha_source_is_refused_before_anything_runs(workspace, op_type, cmd):
    assert subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", str(workspace / "alpha.mov")],
                          capture_output=True, text=True, check=True).stdout.strip().startswith("yuva")
    code, out, _ = run_cli([cmd, "-", "--json"], json.dumps(_doc(op_type, OPS[op_type])))
    d = one_json(out)
    assert code != 0 and d["error"]["code"] == "UNSUPPORTED_FORMAT", d.get("error")
    assert d["error"]["details"]["reason"] == "alpha_not_preserved" and d["error"]["details"]["op_ids"] == ["x"]
    assert not (workspace / "out").exists() and not (workspace / ".color-grading").exists()


def test_a_stream_copy_keeps_the_alpha_or_fails_naming_it(workspace):
    code, out, _ = run_cli(["run", "-", "--json"], json.dumps(_doc("RETAG", {"target": "bt709"})))
    d = one_json(out)
    print("RETAG on alpha:", "kept" if d["ok"] else d["error"]["details"].get("reason"))
    if d["ok"]:
        r = next(x for x in d["results"] if x["type"] == "RETAG")
        assert has_alpha(r["artifact"]["pix_fmt"]) and has_alpha(d["outputs"][0]["artifact"]["pix_fmt"])
    else:   # an ffmpeg-skill whose --retag falls back to a re-encode: the loss is reported, never silent
        assert d["error"]["code"] == "VALIDATION_ERROR" and d["error"]["details"]["reason"] == "alpha_lost", d["error"]
        assert not (workspace / "out" / "main.mov").exists()
