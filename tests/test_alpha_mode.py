"""How an alpha source's colour is stored, and an opaque result on request (docs/decisions.md ADR-33).

`project.source.alpha_mode` (straight | premultiplied) says how the colour beside the alpha is stored; without it the
format decides where its specification fixes it (PNG: straight), and anything else is taken as straight with a
warning, never silently. ffmpeg-skill/color --keep-alpha grades the colour as stored and cannot un-premultiply it, so a
premultiplied source is refused for a grade that keeps the alpha; `project.source.drop_alpha` asks for an opaque
result instead, and is the only way an alpha source's alpha is given up.

The real runs use small synthetic sources made here with ffmpeg's lavfi: one fill colour (0x4a7fb5) in every pixel
under three bands of alpha, 0 | 128 | 255, stored straight or premultiplied, as ProRes 4444 (Y'CbCr, BT.709 tagged) and
QuickTime Animation (RGB, lossless), plus an opaque twin of each with the same colour and no alpha (the non-alpha path).
The tests read the written alpha and colour back themselves (ffmpeg rawvideo)."""
import json
import shutil
import subprocess

import pytest

from color_grading.errors import ColorError
from color_grading.executor import REENCODING_OPERATIONS, TOOL_FOR, Executor, NodeState, has_alpha
from color_grading.graph import Node
from color_grading.model import ALPHA_MODES, parse_request, select_alpha_mode
from color_grading.security import PathPolicy
from conftest import one_json, request_doc, run_cli

KEEP = "ffmpeg-skill/color:keep-alpha"
DROP = "ffmpeg-skill/color:drop-alpha"
W, H, BAND, FRAMES = 96, 32, 32, 10
FILL = (0x4a, 0x7f, 0xb5)
ALPHAS = (0, 128, 255)
# opaque pixels of an alpha grade against the same grade through the non-alpha path, in 8-bit codes: that path writes
# H.264 4:2:0 at CRF 18 and the alpha line 4:4:4, so a flat colour lands a code or two apart (measured with FFmpeg 6.1:
# LUT_APPLY up to 1.0, PRIMARY_CORRECTION up to 2.4)
ACROSS_LINES = 3.0
_BANDS = (f"color=c=0x4a7fb5:s={W}x{H}:d=1:r={FRAMES},format=rgba,"
          f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lt(X,{BAND}),0,if(lt(X,{2 * BAND}),128,255))'")
_TO_709 = "scale=out_color_matrix=bt709:out_range=tv"
_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
_PRORES = ["-c:v", "prores_ks", "-profile:v", "4"]
OPS = {"LUT_APPLY": {"lut_path": "invert.cube"}, "PRIMARY_CORRECTION": {"exposure": 0.5, "saturation": 1.3}}
DROP_OPS = {"HDR_TO_SDR": {"force": True}, "LUT_APPLY": {"lut_path": "invert.cube"}, "PRIMARY_CORRECTION": {"saturation": 0.5},
            "OCIO_TRANSFORM": {"source_space": "ACEScct", "destination_space": "ACEScg", "output_tags": "bt709"},
            "MATCH_CAMERA": {"reference_path": "bands_4444.mov"}, "LEGALIZE": {}}


def _ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", *args], check=True)


@pytest.fixture(scope="module")
def bands(tmp_path_factory):
    """The synthetic band sources (a few KB each)."""
    d = tmp_path_factory.mktemp("bands")
    _ff("-f", "lavfi", "-i", f"{_BANDS},{_TO_709},format=yuva444p10le", *_PRORES, "-pix_fmt", "yuva444p10le", *_TAGS, str(d / "bands_4444.mov"))
    _ff("-f", "lavfi", "-i", f"{_BANDS},premultiply=inplace=1,{_TO_709},format=yuva444p10le", *_PRORES, "-pix_fmt", "yuva444p10le", *_TAGS,
        str(d / "bands_4444_premult.mov"))
    _ff("-f", "lavfi", "-i", f"{_BANDS},{_TO_709},format=yuv444p10le", *_PRORES, "-pix_fmt", "yuv444p10le", *_TAGS, str(d / "bands_4444_opaque.mov"))
    _ff("-f", "lavfi", "-i", _BANDS, "-c:v", "qtrle", "-pix_fmt", "argb", str(d / "bands_rle.mov"))
    _ff("-f", "lavfi", "-i", f"{_BANDS},premultiply=inplace=1", "-c:v", "qtrle", "-pix_fmt", "argb", str(d / "bands_rle_premult.mov"))
    _ff("-f", "lavfi", "-i", f"{_BANDS},format=rgb24", "-c:v", "qtrle", "-pix_fmt", "rgb24", str(d / "bands_rle_opaque.mov"))
    _ff("-f", "lavfi", "-i", _BANDS, "-c:v", "png", "-pix_fmt", "rgba", str(d / "bands_png.mkv"))
    return d


@pytest.fixture
def ws(workspace, bands):
    for p in bands.iterdir():
        shutil.copy(p, workspace / p.name)
    return workspace


def _doc(src, op_type, params, alpha_mode=None, drop_alpha=None, expect=None, out_dir="out"):
    source = {"source_id": "a", "path": src}
    if alpha_mode is not None:
        source["alpha_mode"] = alpha_mode
    if drop_alpha is not None:
        source["drop_alpha"] = drop_alpha
    ext = src.rsplit(".", 1)[1]
    out = {"output_id": "main", "operation": "op:x", "path": f"{out_dir}/main.{ext}", "format": ext, **({"expect": expect} if expect else {})}
    return request_doc([{"op_id": "x", "type": op_type, "input": "source", "parameters": params}], source=source, outputs=[out])


def _run(doc, cmd="run", out="out"):
    shutil.rmtree(out, ignore_errors=True)
    code, stdout, _ = run_cli([cmd, "-", "--json"], json.dumps(doc))
    return code, one_json(stdout)


def _rgba(path):
    """Every frame as 8-bit RGBA (the decoder converts with the stream's own colour tags)."""
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba", "-"], capture_output=True, check=True).stdout


def _alpha(path):
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "alphaextract,format=gray", "-f", "rawvideo", "-"],
                          capture_output=True, check=True).stdout


def _band_colour(px, band, inset=4):
    """The mean RGB of one band's interior over every frame (inset from the band edges, where 4:2:0 chroma would
    bleed in an opaque H.264 output)."""
    sums, n = [0, 0, 0], 0
    for f in range(len(px) // (W * H * 4)):
        for y in range(H):
            for x in range(band * BAND + inset, (band + 1) * BAND - inset):
                i = ((f * H + y) * W + x) * 4
                for c in range(3):
                    sums[c] += px[i + c]
                n += 1
    return tuple(s / n for s in sums)


def _near(a, b, tol):
    return max(abs(x - y) for x, y in zip(a, b)) <= tol


def _bt709_read_as_bt601(rgb):
    """An RGB colour written as BT.709 limited-range Y'CbCr and read back with BT.601's coefficients (ADR-30)."""
    r, g, b = (v / 255 for v in rgb)
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    cb, cr = (b - y) / 1.8556, (r - y) / 1.5748
    r2, b2 = y + 1.402 * cr, y + 1.772 * cb
    g2 = (y - 0.299 * r2 - 0.114 * b2) / 0.587
    return tuple(255 * min(max(v, 0.0), 1.0) for v in (r2, g2, b2))


def _required_unsupported(capabilities, op_type):
    return [c for c in TOOL_FOR[op_type][1] if capabilities.get(c) == "unsupported"]


# ---- the fixtures are what they claim to be
def test_the_band_sources_hold_alpha_0_partial_and_1_straight_and_premultiplied(ws):
    for name in ("bands_rle.mov", "bands_rle_premult.mov"):
        a = _alpha(ws / name)
        assert len(a) == FRAMES * W * H
        assert [a[16 * W + b * BAND + 16] for b in range(3)] == list(ALPHAS)
    straight, premult = _rgba(ws / "bands_rle.mov"), _rgba(ws / "bands_rle_premult.mov")
    i = [(16 * W + b * BAND + 16) * 4 for b in range(3)]
    assert all(tuple(straight[j:j + 3]) == FILL for j in i)                                     # straight: one colour under every alpha
    got = [tuple(premult[j:j + 3]) for j in i]
    # colour x alpha, within the one code FFmpeg's premultiply rounds by (8.0 and 9.0 write 180 for 181 under alpha 255)
    assert got[0] == (0, 0, 0) and _near(got[2], FILL, 1) and _near(got[1], tuple(c * 128 / 255 for c in FILL), 1)
    assert _near(_band_colour(_rgba(ws / "bands_4444.mov"), 2), FILL, 1) and _near(_band_colour(_rgba(ws / "bands_4444_opaque.mov"), 0), FILL, 1)


# ---- straight: the alpha is kept, bit-identical (or within its stated tolerance), and opaque pixels match the non-alpha path
@pytest.mark.parametrize("family", ["rle", "4444"])
@pytest.mark.parametrize("op_type", sorted(OPS))
def test_a_straight_source_keeps_its_alpha_and_its_opaque_colour_matches_the_non_alpha_path(ws, capabilities, family, op_type):
    code, d = _run(_doc(f"bands_{family}.mov", op_type, OPS[op_type], alpha_mode="straight"))
    if capabilities.get(KEEP) != "supported":
        assert code != 0 and d["error"]["details"]["reason"] == "alpha_not_preserved", d.get("error")
        return
    assert code == 0 and d["ok"], d.get("error")
    assert not any("neither the request" in w for w in d["warnings"]), d["warnings"]
    r = next(x for x in d["results"] if x["type"] == op_type)
    assert r["alpha"]["kept"] is True and r["alpha"]["verification"]["ok"] is True
    assert r["alpha"]["alpha_mode_selection"] == {"alpha_mode": "straight", "rule": "request", "format_states": None,
                                                  "codec": "qtrle" if family == "rle" else "prores"}
    assert d["plan"]["alpha"]["alpha_mode_selection"]["rule"] == "request" and d["plan"]["alpha"]["drop_alpha"] is False
    out = ws / "out" / "main.mov"
    src_alpha, out_alpha = _alpha(ws / f"bands_{family}.mov"), _alpha(out)
    assert len(src_alpha) == len(out_alpha) == FRAMES * W * H
    if family == "rle":
        assert src_alpha == out_alpha                                              # 8-bit alpha: bit-identical
    else:
        # ProRes 4444's 10-bit alpha comes back through color's 8-bit split: within one 8-bit code in every sample
        assert max(abs(a - b) for a, b in zip(src_alpha, out_alpha)) <= 1
    # 0, partial and 1 are still 0, partial and 1 (the 10-bit alpha within its one 8-bit code)
    assert _near([out_alpha[16 * W + b * BAND + 16] for b in range(3)], ALPHAS, 0 if family == "rle" else 1)
    px = _rgba(out)
    bands_out = [_band_colour(px, b) for b in range(3)]
    # straight colour stays one colour under every alpha: no fringe at the partial band, none under the transparent one
    assert all(_near(c, bands_out[2], 1.5) for c in bands_out), bands_out
    # the same grade on the opaque twin, through the ordinary (non-alpha) path: H.264 4:2:0
    code, ref = _run(_doc(f"bands_{family}_opaque.mov", op_type, OPS[op_type], out_dir="ref"), out="ref")
    assert code == 0 and ref["ok"], ref.get("error")
    assert not any("alpha" in w for w in ref["warnings"]) and "alpha" not in ref["plan"]
    opaque, non_alpha = bands_out[2], _band_colour(_rgba(ws / "ref" / "main.mov"), 2)
    print(f"{family} {op_type}: opaque band {opaque}, non-alpha path {non_alpha}")
    if family == "4444" and op_type == "PRIMARY_CORRECTION":
        assert any("Y'CbCr alpha source" in w for w in d["warnings"]), d["warnings"]     # always said (ADR-33)
    if family == "rle" and op_type == "LUT_APPLY" and not _near(opaque, non_alpha, ACROSS_LINES):
        # a measured exception (ADR-30): --lut's legs write BT.709 Y'CbCr and the RGB alpha line reads it back with
        # BT.601's matrix on some FFmpeg builds (6.1). Exactly that shift, within 2 codes, and named in a warning
        assert _near(opaque, _bt709_read_as_bt601(non_alpha), 2.0), (opaque, non_alpha)
        assert any("RGB alpha source" in w for w in d["warnings"])
    elif family == "4444" and op_type == "PRIMARY_CORRECTION" and not _near(opaque, non_alpha, ACROSS_LINES):
        # the other measured exception (ADR-33): with FFmpeg 8.0.1, --correct through --keep-alpha landed 11.4 codes from the
        # opaque path (6.1 and 9.0.2: within 3). Bounded here, and named in the warning above
        assert _near(opaque, non_alpha, 12.0), (opaque, non_alpha)
    else:
        assert _near(opaque, non_alpha, ACROSS_LINES), (opaque, non_alpha)


def test_an_unstated_mode_is_taken_as_straight_with_a_warning_and_the_same_identity(ws, capabilities):
    keep = capabilities.get(KEEP) == "supported"
    for cmd in ("plan", "run"):
        code, d = _run(_doc("bands_4444.mov", "LUT_APPLY", OPS["LUT_APPLY"]), cmd)
        if not keep:
            assert code != 0 and d["error"]["details"]["reason"] == "alpha_not_preserved"
            return
        assert code == 0 and d["ok"], d.get("error")
        (w,) = [w for w in d["warnings"] if "neither the request" in w]
        assert "grade it as straight" in w and "alpha_mode" in w and "yuva444p" in w
        step = d["plan"]["steps"][0]
        assert step["alpha_mode_selection"] == {"alpha_mode": "straight", "rule": "assumed", "format_states": None, "codec": "prores"}
        assumed_id = step["operation_id"]
    r = next(x for x in d["results"] if x["type"] == "LUT_APPLY")
    assert r["status"] == "completed" and r["alpha"]["alpha_mode_selection"]["rule"] == "assumed"
    # stating straight writes the same file: one identity, so the intermediate is reused, with this request's rule recorded
    code, d = _run(_doc("bands_4444.mov", "LUT_APPLY", OPS["LUT_APPLY"], alpha_mode="straight"))
    assert code == 0 and d["plan"]["steps"][0]["operation_id"] == assumed_id
    r = next(x for x in d["results"] if x["type"] == "LUT_APPLY")
    assert r["status"] == "reused" and r["alpha"]["alpha_mode_selection"]["rule"] == "request"
    assert not any("neither the request" in w for w in d["warnings"])


def test_png_states_straight_so_nothing_is_assumed(ws, capabilities):
    code, d = _run(_doc("bands_png.mkv", "PRIMARY_CORRECTION", OPS["PRIMARY_CORRECTION"]), "plan")
    if capabilities.get(KEEP) != "supported":
        assert code != 0 and d["error"]["details"]["reason"] == "alpha_not_preserved"
        return
    assert code == 0 and d["ok"], d.get("error")
    assert d["plan"]["steps"][0]["alpha_mode_selection"] == {"alpha_mode": "straight", "rule": "format", "format_states": "straight", "codec": "png"}
    assert d["plan"]["steps"][0]["alpha"]["alpha_codec"] == "png" and not d["warnings"]


# ---- premultiplied: refused where the alpha would be kept, flattened over black on request
@pytest.mark.parametrize("family", ["rle", "4444"])
@pytest.mark.parametrize("op_type", sorted(OPS))
@pytest.mark.parametrize("cmd", ["run", "plan"])
def test_a_premultiplied_source_is_refused_for_a_grade_that_keeps_the_alpha(ws, family, op_type, cmd):
    """On every ffmpeg-skill: color takes the colour as stored and cannot un-premultiply it, so the edges would be graded
    at the wrong colour; updating ffmpeg-skill would not help, so this is said before the missing --keep-alpha is."""
    code, d = _run(_doc(f"bands_{family}_premult.mov", op_type, OPS[op_type], alpha_mode="premultiplied"), cmd)
    assert code == 6 and d["error"]["code"] == "UNSUPPORTED_FORMAT", d.get("error")
    det = d["error"]["details"]
    assert det["reason"] == "alpha_premultiplied" and det["op_ids"] == ["x"]
    assert det["alpha_mode_selection"]["alpha_mode"] == "premultiplied" and det["alpha_mode_selection"]["rule"] == "request"
    assert "un-premultiply" in d["error"]["message"] and "drop_alpha" in d["error"]["message"] and "alpha_mode: straight" in d["error"]["message"]
    assert not (ws / "out").exists() and not (ws / ".color-grading").exists()


def test_a_premultiplied_source_is_copied_unchanged_by_a_stream_copy(ws):
    code, d = _run(_doc("bands_4444_premult.mov", "RETAG", {"target": "bt709"}, alpha_mode="premultiplied"))
    if not d["ok"]:     # an ffmpeg-skill whose --retag falls back to a re-encode: the loss is reported (test_alpha.py)
        assert d["error"]["details"]["reason"] in ("alpha_lost", "alpha_mismatch"), d["error"]
        return
    assert "alpha" not in d["plan"]["steps"][0] and not d["warnings"]
    assert _alpha(ws / "out" / "main.mov") == _alpha(ws / "bands_4444_premult.mov")
    assert _rgba(ws / "out" / "main.mov") == _rgba(ws / "bands_4444_premult.mov")


@pytest.mark.parametrize("family", ["rle", "4444"])
def test_drop_alpha_flattens_a_premultiplied_source_over_black(ws, capabilities, family):
    code, d = _run(_doc(f"bands_{family}_premult.mov", "LUT_APPLY", OPS["LUT_APPLY"], alpha_mode="premultiplied", drop_alpha=True))
    assert code == 0 and d["ok"], d.get("error")
    r = next(x for x in d["results"] if x["type"] == "LUT_APPLY")
    assert r["artifact"]["pix_fmt"] == "yuv420p" and not has_alpha(d["outputs"][0]["artifact"]["pix_fmt"])
    assert r["alpha"]["kept"] is False and r["alpha"]["dropped"] is True and r["alpha"]["requested_by"] == "project.source.drop_alpha"
    assert r["alpha"]["alpha_mode_selection"]["alpha_mode"] == "premultiplied"
    flag = capabilities.get(DROP) == "supported"
    assert r["alpha"]["tool"] == ("ffmpeg-skill/color --drop-alpha" if flag else "ffmpeg-skill/color (opaque line)")
    assert d["plan"]["steps"][0]["alpha"] == {"keep_alpha": False, "drop_alpha": True, "drop_alpha_flag": flag}
    px = _rgba(ws / "out" / "main.mov")
    # the colour as stored, inverted: premultiplied black under alpha 0 -> white; half the fill under alpha 128 -> its inverse.
    # 3 codes; 6 on an ffmpeg-skill before 3.1.6, whose --lut legs read BT.709 and write BT.601 (its #410, ADR-23)
    tol = 3 if capabilities.get("ffmpeg-skill/color:lut-bt709-legs") == "supported" else 6
    half = tuple(c * 128 / 255 for c in FILL)
    assert _near(_band_colour(px, 0), (255, 255, 255), tol), _band_colour(px, 0)
    assert _near(_band_colour(px, 1), tuple(255 - c for c in half), tol), _band_colour(px, 1)
    # fully opaque pixels: the same as the non-alpha path on the opaque twin
    code, ref = _run(_doc(f"bands_{family}_opaque.mov", "LUT_APPLY", OPS["LUT_APPLY"], out_dir="ref"), out="ref")
    assert code == 0, ref.get("error")
    print(f"{family} premultiplied, dropped: bands {[_band_colour(px, b) for b in range(3)]}, non-alpha path {_band_colour(_rgba(ws / 'ref' / 'main.mov'), 2)}")
    assert _near(_band_colour(px, 2), _band_colour(_rgba(ws / "ref" / "main.mov"), 2), 2.0)


@pytest.mark.parametrize("op_type", sorted(DROP_OPS))
def test_drop_alpha_lets_every_reencoding_operation_write_an_opaque_picture(ws, capabilities, op_type):
    assert set(DROP_OPS) == set(REENCODING_OPERATIONS)
    code, d = _run(_doc("bands_4444.mov", op_type, DROP_OPS[op_type], drop_alpha=True))
    if op_type == "HDR_TO_SDR":
        # the one exception, on every build (ADR-33): --to-sdr hands the alpha plane to zscale, which fails with FFmpeg 8.0.1
        # and 9.0.2 (measured in CI; 6.1 runs it), so it is refused before anything runs
        assert code == 6 and d["error"]["details"] == {"reason": "alpha_input_not_supported", "pix_fmt": d["error"]["details"]["pix_fmt"],
                                                       "op_ids": ["x"]}, d.get("error")
        assert "OCIO_TRANSFORM" in d["error"]["message"] and not (ws / "out").exists() and not (ws / ".color-grading").exists()
        return
    missing = _required_unsupported(capabilities, op_type)
    if missing:     # this checkout cannot run the operation at all (e.g. MATCH_CAMERA before ffmpeg-skill 3.1.6)
        assert code != 0 and d["error"]["code"] == "TOOL_ERROR", d.get("error")
        det = d["error"]["details"]
        assert set(det.get("missing", [])) == set(missing) or det.get("reason") == "retag_cannot_rewrite_tags", det
        return
    assert code == 0 and d["ok"], d.get("error")
    r = next(x for x in d["results"] if x["type"] == op_type)
    assert r["alpha"]["dropped"] is True and r["alpha"]["kept"] is False
    assert not has_alpha(r["artifact"]["pix_fmt"]) and not has_alpha(d["outputs"][0]["artifact"]["pix_fmt"])
    assert not any("neither the request" in w for w in d["warnings"])       # nothing is graded with the alpha kept


def test_the_same_picture_without_alpha_is_tone_mapped(ws, capabilities):
    """The control for HDR_TO_SDR's refusal above: the opaque twin (the same colour planes, no alpha) runs through --to-sdr
    wherever this checkout can tone-map at all, so the refusal is about the alpha plane, not the picture."""
    code, d = _run(_doc("bands_4444_opaque.mov", "HDR_TO_SDR", {"force": True}))
    missing = _required_unsupported(capabilities, "HDR_TO_SDR")
    if missing:
        assert code != 0 and set(d["error"]["details"]["missing"]) == set(missing), d.get("error")
        return
    assert code == 0 and d["ok"], d.get("error")
    assert d["outputs"][0]["artifact"]["pix_fmt"] == "yuv420p"


def test_without_drop_alpha_the_refusal_names_it(ws):
    code, d = _run(_doc("bands_4444.mov", "LEGALIZE", {}), "plan")
    assert code == 6 and d["error"]["details"]["reason"] == "alpha_not_preserved"
    assert "project.source.drop_alpha: true" in d["error"]["message"]


# ---- output settings that would drop the alpha
def test_an_output_setting_that_would_drop_the_alpha_is_refused(ws, capabilities):
    # a copy of the source itself, expected opaque: refused on every checkout, nothing written
    copy = request_doc([], source={"source_id": "a", "path": "bands_rle.mov"},
                       outputs=[{"output_id": "main", "operation": "source", "path": "out/main.mov", "format": "mov", "expect": {"pix_fmt": "rgb24"}}])
    code, d = _run(copy)
    assert code == 6 and d["error"]["details"] == {"reason": "alpha_not_preserved", "output_id": "main", "expect_pix_fmt": "rgb24", "pix_fmt": "argb"}
    assert "drop_alpha" in d["error"]["message"] and not (ws / "out").exists()
    if capabilities.get(KEEP) == "supported":
        code, d = _run(_doc("bands_4444.mov", "LUT_APPLY", OPS["LUT_APPLY"], alpha_mode="straight", expect={"pix_fmt": "yuv420p"}), "plan")
        assert code == 6 and d["error"]["details"]["output_id"] == "main" and d["error"]["details"]["reason"] == "alpha_not_preserved"
    # asked for: the opaque expectation holds
    code, d = _run(_doc("bands_4444.mov", "LUT_APPLY", OPS["LUT_APPLY"], drop_alpha=True, expect={"pix_fmt": "yuv420p"}))
    assert code == 0 and d["outputs"][0]["artifact"]["pix_fmt"] == "yuv420p", d.get("error")


# ---- units (no ffmpeg)
@pytest.mark.parametrize("value", ["straight", "premultiplied"])
def test_alpha_mode_is_parsed(value):
    req = parse_request(request_doc([], source={"source_id": "a", "path": "a.mov", "alpha_mode": value, "drop_alpha": True},
                                    outputs=[{"output_id": "o", "operation": "source", "path": "o.mov", "format": "mov"}]))
    assert req.project.source.alpha_mode == value and req.project.source.drop_alpha is True
    req = parse_request(request_doc([], source={"source_id": "a", "path": "a.mov"}, outputs=[{"output_id": "o", "operation": "source", "path": "o.mov", "format": "mov"}]))
    assert req.project.source.alpha_mode is None and req.project.source.drop_alpha is False


@pytest.mark.parametrize("field,value", [("alpha_mode", "Straight"), ("alpha_mode", "associated"), ("alpha_mode", 1), ("alpha_mode", ["straight"]),
                                         ("drop_alpha", "true"), ("drop_alpha", 1), ("drop_alpha", None), ("alpha", "straight")])
def test_bad_alpha_fields_are_rejected(field, value):
    with pytest.raises(ColorError) as e:
        parse_request(request_doc([], source={"source_id": "a", "path": "a.mov", field: value},
                                  outputs=[{"output_id": "o", "operation": "source", "path": "o.mov", "format": "mov"}]))
    assert e.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("requested,codec,mode,rule", [(None, "png", "straight", "format"), (None, "prores", "straight", "assumed"),
                                                       (None, "qtrle", "straight", "assumed"), (None, "vp9", "straight", "assumed"),
                                                       (None, "ffv1", "straight", "assumed"), (None, None, "straight", "assumed"),
                                                       ("premultiplied", "prores", "premultiplied", "request"),
                                                       ("premultiplied", "png", "premultiplied", "request"), ("straight", "qtrle", "straight", "request")])
def test_the_alpha_mode_comes_from_the_request_then_the_format_then_is_assumed(requested, codec, mode, rule):
    sel = select_alpha_mode(requested, codec)
    assert sel["alpha_mode"] == mode and sel["rule"] == rule and sel["codec"] == codec
    assert sel["format_states"] == ("straight" if codec == "png" else None)       # kept beside a request that disagrees
    assert sel["alpha_mode"] in ALPHA_MODES


def test_drop_alpha_flags_come_from_the_tables_only():
    st = NodeState(Node("op:x", "HDR_TO_SDR", ["source"], {}), "id")
    assert Executor._alpha_args(st) == []
    st.drop_alpha = True
    assert Executor._alpha_args(st) == []                    # no --drop-alpha in this checkout: the opaque line is written anyway
    st.drop_alpha_flag = True
    assert Executor._alpha_args(st) == ["--drop-alpha"]


class _Run:
    def __init__(self, data):
        self.data = data


def test_an_alpha_dropped_step_is_what_drop_alpha_asked_for(tmp_path):
    step = {"step": "alpha", "ok": False, "reason": "alpha_dropped"}
    ex = Executor(PathPolicy(str(tmp_path)), skill=None)
    st = NodeState(Node("op:x", "LUT_APPLY", ["source"], {}), "id")
    st.drop_alpha = True
    ex._check_tool_verification(st, _Run({"verification": [step], "alpha_dropped": True}), tmp_path / "x.mov")
    assert st.alpha == {"kept": False, "dropped": True, "requested_by": "project.source.drop_alpha", "tool": "ffmpeg-skill/color (opaque line)",
                        "reported_dropped": True, "verification": step}
    # ... and still a validation error when nothing asked for it
    with pytest.raises(ColorError) as e:
        ex._check_tool_verification(NodeState(Node("op:x", "LUT_APPLY", ["source"], {}), "id"), _Run({"verification": [step]}), tmp_path / "x.mov")
    assert e.value.details["reason"] == "alpha_lost"
    # ... or when the alpha step failed for another reason than the drop that was asked for
    with pytest.raises(ColorError) as e:
        ex._check_tool_verification(st, _Run({"verification": [{"step": "alpha", "ok": False, "comparison": {"ok": False}}]}), tmp_path / "x.mov")
    assert e.value.details["reason"] == "alpha_mismatch"
    # ... or when a stream copy, which keeps the alpha under drop_alpha too, reports it dropped
    ex._drop_requested = True
    copy = NodeState(Node("op:r", "RETAG", ["source"], {}), "id")
    copy.alpha_required = True
    with pytest.raises(ColorError) as e:
        ex._check_tool_verification(copy, _Run({"verification": [step]}), tmp_path / "x.mov")
    assert e.value.details["reason"] == "alpha_lost"
    # an alpha step reporting the drop on a source this skill did not see as alpha (VP9) is accepted when it was asked for
    vp9 = NodeState(Node("op:p", "PRIMARY_CORRECTION", ["source"], {}), "id")
    ex._check_tool_verification(vp9, _Run({"verification": [step]}), tmp_path / "x.mkv")
    assert vp9.alpha["dropped"] is True


class _ProbeStub:
    def __init__(self, pix_fmt):
        self.pix_fmt = pix_fmt

    def probe(self, path, timeout=None):
        return {"duration": 1.0, "video": {"width": W, "height": H, "pix_fmt": self.pix_fmt, "codec": "prores"}}


@pytest.mark.parametrize("drop,required,pix_fmt,reason", [(True, False, "yuva444p12le", "alpha_not_dropped"), (False, True, "yuv420p", "alpha_lost"),
                                                          (True, True, "yuv420p", "alpha_lost"), (True, False, "yuv420p", None),
                                                          (False, True, "yuva444p12le", None)])
def test_a_dropped_alpha_must_be_gone_and_a_kept_one_present(tmp_path, drop, required, pix_fmt, reason):
    out = tmp_path / "o.mov"
    out.write_bytes(b"x" * 64)
    st = NodeState(Node("op:x", "PRIMARY_CORRECTION", ["source"], {}), "id")
    st.drop_alpha, st.alpha_required = drop, required
    source = {"duration": 1.0, "width": W, "height": H, "pix_fmt": "yuva444p12le", "subtitle_streams": 0, "data_streams": 0}
    ex = Executor(PathPolicy(str(tmp_path)), _ProbeStub(pix_fmt))
    if reason is None:
        assert ex._validate_artifact(out, st, source).pix_fmt == pix_fmt
        return
    with pytest.raises(ColorError) as e:
        ex._validate_artifact(out, st, source)
    assert e.value.code == "VALIDATION_ERROR" and e.value.details["reason"] == reason


class _StubSkill:
    def __init__(self, video):
        self.video, self.runs = video, []

    def probe(self, path, timeout=None):
        return {"duration": 1.0, "video": dict(self.video), "audio": None}


ALL = {KEEP: "supported", DROP: "supported", "ffmpeg-skill/color:legalize": "supported", "ffmpeg-skill/check:levels": "supported"}
ALPHA_VIDEO = {"width": W, "height": H, "pix_fmt": "yuva444p12le", "codec": "prores"}


def _plan(tmp_path, video, caps=None, ops=None, **source):
    (tmp_path / "clip.mov").write_bytes(b"x")
    (tmp_path / "grade.cube").write_text("LUT_3D_SIZE 2\n" + "0 0 0\n" * 8)
    ops = ops or [{"op_id": "l", "type": "LUT_APPLY", "input": "source", "parameters": {"lut_path": "grade.cube"}}]
    doc = request_doc(ops, source={"source_id": "s", "path": str(tmp_path / "clip.mov"), **source},
                      outputs=[{"output_id": "o", "operation": f"op:{ops[-1]['op_id']}", "path": str(tmp_path / "out" / "o.mov"), "format": "mov"}])
    return Executor(PathPolicy(str(tmp_path)), _StubSkill(video), dry_run=True, capabilities=ALL if caps is None else caps).response(doc)


def _ids(d):
    return [s["operation_id"] for s in d["plan"]["steps"]]


def test_identity_follows_how_the_alpha_is_written_not_how_the_mode_was_chosen(tmp_path):
    assumed, stated = _plan(tmp_path, ALPHA_VIDEO), _plan(tmp_path, ALPHA_VIDEO, alpha_mode="straight")
    dropped = _plan(tmp_path, ALPHA_VIDEO, drop_alpha=True)
    assert assumed["ok"] and stated["ok"] and dropped["ok"]
    assert _ids(assumed) == _ids(stated) != _ids(dropped)
    # an opaque source: the alpha fields change nothing it writes, so nothing in its identity
    opaque = {**ALPHA_VIDEO, "pix_fmt": "yuv444p12le"}
    plain = _plan(tmp_path, opaque)
    assert _ids(plain) == _ids(_plan(tmp_path, opaque, drop_alpha=True)) == _ids(_plan(tmp_path, opaque, alpha_mode="straight"))
    assert "alpha" not in plain["plan"] and "alpha_mode_selection" not in plain["plan"]["steps"][0] and not plain["warnings"]


def test_alpha_mode_on_a_source_without_an_alpha_plane_is_said(tmp_path):
    d = _plan(tmp_path, {**ALPHA_VIDEO, "pix_fmt": "yuv420p", "codec": "vp9"}, alpha_mode="premultiplied")
    assert d["ok"]
    (w,) = d["warnings"]
    assert "shows no alpha plane" in w and "VP9" in w


def test_hdr_to_sdr_takes_no_alpha_input_even_with_drop_alpha_but_an_opaque_one_after_a_drop(tmp_path):
    hdr = {"op_id": "h", "type": "HDR_TO_SDR", "input": "source", "parameters": {"force": True}}
    d = _plan(tmp_path, ALPHA_VIDEO, ops=[hdr], drop_alpha=True)
    assert d["error"]["details"]["reason"] == "alpha_input_not_supported" and d["error"]["details"]["op_ids"] == ["h"]
    after = [{"op_id": "l", "type": "LUT_APPLY", "input": "source", "parameters": {"lut_path": "grade.cube"}}, {**hdr, "input": "op:l"}]
    d = _plan(tmp_path, ALPHA_VIDEO, ops=after, drop_alpha=True)
    assert d["ok"], d.get("error")
    assert [s.get("alpha", {}).get("drop_alpha") for s in d["plan"]["steps"]] == [True, None]


def test_drop_alpha_is_planned_for_the_reencodes_of_an_alpha_input_only(tmp_path):
    ops = [{"op_id": "r", "type": "RETAG", "input": "source", "parameters": {"target": "bt709"}},
           {"op_id": "l", "type": "LUT_APPLY", "input": "op:r", "parameters": {"lut_path": "grade.cube"}},
           {"op_id": "p", "type": "PRIMARY_CORRECTION", "input": "op:l", "parameters": {"gamma": 1.2}}]
    for flag in ("supported", "unknown"):
        d = _plan(tmp_path, ALPHA_VIDEO, caps={**ALL, DROP: flag}, ops=ops, drop_alpha=True, alpha_mode="premultiplied")
        assert d["ok"], d.get("error")
        steps = {s["node_id"]: s for s in d["plan"]["steps"]}
        assert "alpha" not in steps["op:r"]                                                    # a stream copy keeps it
        assert steps["op:l"]["alpha"] == {"keep_alpha": False, "drop_alpha": True, "drop_alpha_flag": flag == "supported"}
        assert "alpha" not in steps["op:p"]                                                    # its input is already opaque
        assert d["plan"]["alpha"] == {"pix_fmt": "yuva444p12le", "drop_alpha": True, "alpha_codec": None,
                                      "alpha_mode_selection": {"alpha_mode": "premultiplied", "rule": "request", "format_states": None, "codec": "prores"}}
        assert "encoder:libx264" in d["plan"]["required_capabilities"] and not d["warnings"]


def test_premultiplied_is_refused_before_a_missing_keep_alpha_but_after_an_operation_that_never_keeps_it(tmp_path):
    for keep in ("supported", "unsupported"):
        d = _plan(tmp_path, ALPHA_VIDEO, caps={**ALL, KEEP: keep}, alpha_mode="premultiplied")
        assert d["error"]["details"]["reason"] == "alpha_premultiplied"
    ops = [{"op_id": "h", "type": "HDR_TO_SDR", "input": "source", "parameters": {"force": True}},
           {"op_id": "l", "type": "LUT_APPLY", "input": "op:h", "parameters": {"lut_path": "grade.cube"}}]
    d = _plan(tmp_path, ALPHA_VIDEO, ops=ops, alpha_mode="premultiplied")
    assert d["error"]["details"]["reason"] == "alpha_not_preserved" and d["error"]["details"]["op_ids"] == ["h"]
