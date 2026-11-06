"""MATCH_CAMERA (ECOSYSTEM_ROADMAP.md step 6, docs/decisions.md ADR-23): the pieces (PNG decoding, CIEDE2000, the
two models), request validation, and real runs through ffmpeg-skill/look + color on the camera A / camera B fixture
(B = A with a known per-channel gain and tint)."""
import json
import struct
import subprocess
import zlib

import pytest

from color_grading import match
from color_grading.adapter import FfmpegSkill, check_tools
from color_grading.contract import skill_contract
from color_grading.doctor import capability_status, doctor_report
from color_grading.errors import ColorError
from color_grading.model import parse_request
from conftest import one_json, request_doc, run_cli
from fixtures.generate import CAMERA_B_GAIN, CAMERA_B_OFFSET


# ---- CIEDE2000: Sharma, Wu & Dalal (2005), Table 1
@pytest.mark.parametrize("lab1,lab2,de", [
    ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
    ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
    ((50.0, -1.3802, -84.2814), (50.0, 0.0, -82.7485), 1.0000),
    ((50.0, 2.5, 0.0), (50.0, 0.0, -2.5), 4.3065),
    ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
])
def test_ciede2000_matches_the_published_test_data(lab1, lab2, de):
    assert match.delta_e2000(lab1, lab2) == pytest.approx(de, abs=1e-4)


def test_lab_of_white_and_black():
    assert match.lab([1.0, 1.0, 1.0]) == pytest.approx((100.0, 0.0, 0.0), abs=1e-3)
    assert match.lab([0.0, 0.0, 0.0])[0] == pytest.approx(0.0, abs=1e-6)


# ---- PNG
def _png(width, height, rows, depth=8, ctype=2):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    raw = b"".join(bytes([f]) + r for f, r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, ctype, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_png_filters_are_undone():
    # 2x2 RGB; row 0 filter Sub, row 1 filter Paeth, values chosen so each filter matters
    r0 = bytes([10, 20, 30, 5, 5, 5])            # Sub: second pixel = (15, 25, 35)
    r1 = bytes([1, 1, 1, 2, 2, 2])               # Paeth on top of row 0
    w, h, px = match.read_png(_png(2, 2, [(1, r0), (4, r1)]))
    assert (w, h) == (2, 2)
    assert [round(c * 255) for c in px[0]] == [10, 20, 30] and [round(c * 255) for c in px[1]] == [15, 25, 35]
    assert [round(c * 255) for c in px[2]] == [11, 21, 31]
    # Paeth for the second pixel of row 1: a=left(11,21,31) b=up(15,25,35) c=upleft(10,20,30) -> p=16..: picks b
    assert [round(c * 255) for c in px[3]] == [17, 27, 37]


def test_png_from_ffmpeg_8_and_16_bit(tmp_path):
    # the same pixel format in and out, so ffmpeg only encodes the PNG and the decoded values are exactly the input's
    for pix_fmt, pixel, scale in (("rgb24", bytes([0x33, 0x66, 0x99]), 255), ("rgb48be", struct.pack(">3H", 13107, 26214, 39321), 65535)):
        out = tmp_path / f"{pix_fmt}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", pix_fmt, "-s", "4x2", "-i", "-", "-frames:v", "1", "-pix_fmt", pix_fmt, str(out)],
                       input=pixel * 8, check=True)
        w, h, px = match.read_png(out.read_bytes())
        assert (w, h) == (4, 2) and len(px) == 8
        assert px[5] == pytest.approx([0.2, 0.4, 0.6], abs=0.5 / scale), (pix_fmt, px[5])


def test_png_layouts_not_written_by_ffmpeg_are_refused():
    with pytest.raises(ColorError):
        match.read_png(b"not a png")
    with pytest.raises(ColorError) as e:
        match.read_png(_png(1, 1, [(0, bytes([1]))], ctype=0))      # greyscale
    assert e.value.details["reason"] == "frame_png_layout"


# ---- models
def _affine(samples, gain, offset):
    return [[min(1.0, max(0.0, s[c] * gain[c] + offset[c] / 255)) for c in range(3)] for s in samples]


def _grid_samples():
    return [[r / 7, g / 7, b / 7] for r in range(8) for g in range(8) for b in range(8)]


@pytest.mark.parametrize("model", match.MODELS)
def test_both_models_invert_a_known_gain_and_tint(model):
    reference = _grid_samples()
    camera_b = _affine(reference, CAMERA_B_GAIN, CAMERA_B_OFFSET)
    keep = [i for i, s in enumerate(camera_b) if all(0.0 < v < 1.0 for v in s)]         # no clipped samples
    reference, camera_b = [reference[i] for i in keep], [camera_b[i] for i in keep]
    f = match.fit(model, reference, camera_b)
    got = [match.apply_fit(f, s) for s in camera_b]
    assert max(abs(a - b) for g, r in zip(got, reference) for a, b in zip(g, r)) < 2e-3
    assert match.residual(reference, got)["mean"] < 0.2


def test_curves_are_monotone_even_from_noisy_samples():
    xs = [i / 100 for i in range(101)]
    ys = [x + (0.05 if i % 2 else -0.05) for i, x in enumerate(xs)]
    curve = match.fit_curve(xs, ys)
    values = [match.eval_curve(curve, x / 1000) for x in range(1001)]
    assert all(b >= a - 1e-12 for a, b in zip(values, values[1:]))


def test_matrix_needs_enough_distinct_samples():
    with pytest.raises(ColorError) as e:
        match.fit("matrix", [[0.5, 0.5, 0.5]] * 6, [[0.4, 0.4, 0.4]] * 6)
    assert e.value.details["reason"] == "matrix_singular"
    with pytest.raises(ColorError):
        match.fit("matrix", [[0.1, 0.2, 0.3]] * 3, [[0.1, 0.2, 0.3]] * 3)


# ---- validation
def _op(**params):
    return {"op_id": "m", "type": "MATCH_CAMERA", "input": "source", "parameters": params}


@pytest.mark.parametrize("params,field", [
    ({}, "reference_path"),
    ({"reference_path": "a.mp4", "model": "lut"}, "model"),
    ({"reference_path": "a.mp4", "patches": " ; "}, "patches"),
    ({"reference_path": "a.mp4", "patches": [{"reference": [0, 0, 0.5, 0.5]}]}, "patches"),
    ({"reference_path": "a.mp4", "patches": "0,0,0.5"}, "reference"),
    ({"reference_path": "a.mp4", "patches": "0,0,0.5,0.5/0.8,0,0.5,0.5"}, "input"),
    ({"reference_path": "a.mp4", "patches": "0,0,0,0.5/0,0,0.5,0.5"}, "reference"),
    ({"reference_path": "a.mp4", "patches": "0,0,a,0.5"}, "reference"),
    ({"reference_path": "a.mp4", "patches": "0,0,.5,.5/0,0,.5,.5/0,0,.5,.5"}, "patches"),
    ({"reference_path": "a.mp4", "lut_size": 129}, "lut_size"),
    ({"reference_path": "a.mp4", "at": -1}, "at"),
])
def test_invalid_parameters_are_rejected_naming_the_field(params, field):
    with pytest.raises(ColorError) as e:
        parse_request(request_doc([_op(**params)]))
    assert e.value.code == "INVALID_REQUEST" and field in (e.value.details.get("field") or "") + e.value.message


def test_patches_are_a_string_with_a_canonical_spelling():
    p = parse_request(request_doc([_op(reference_path="a.mp4", patches=" 0,0,0.25,0.25 ; 0.5,0.5,0.2,0.2/0.4,0.4,0.2,0.2")])).project.operations[0].parameters
    assert p["patches"] == "0,0,0.25,0.25/0,0,0.25,0.25;0.5,0.5,0.2,0.2/0.4,0.4,0.2,0.2"
    from color_grading.model import parse_patches
    assert parse_patches(p["patches"])[1] == {"reference": [0.5, 0.5, 0.2, 0.2], "input": [0.4, 0.4, 0.2, 0.2]}


def test_defaults():
    p = parse_request(request_doc([_op(reference_path="a.mp4")])).project.operations[0].parameters
    assert (p["model"], p["patches"], p["lut_size"], p["at"], p["reference_at"]) == ("curves", None, 33, 0.0, 0.0)


# ---- look as an optional tool
def test_a_checkout_without_look_loses_only_match_camera():
    color = {"input", "output", "to_sdr", "lut", "retag", "strip_dovi", "correct", "tonemap", "peak", "desat", "lut_strength", "force", "exposure",
             "contrast", "saturation", "temperature", "tint", "gamma", "lift", "gain", "levels_in_black", "levels_in_white", "levels_out_black",
             "levels_out_white", "curves", "audio_stream", "preset", "json", "quality"}
    tools = {"probe": {"input_schema": {"properties": {"inputs": {}}}}, "color": {"input_schema": {"properties": {k: {} for k in color}}}}
    problems, features = check_tools(tools)
    assert problems == [] and features["look_tool"] is False

    class Info:
        supported = True
    Info.features = features
    caps = capability_status(Info(), {"ffmpeg": "6.1", "ffprobe": "6.1", "available": ["filter:lut3d", "encoder:libx264"]})
    assert caps["ffmpeg-skill/look"] == "unsupported" and caps["ffmpeg-skill"] == "supported"


def test_contract_and_doctor_declare_match_camera(skill_dir):
    c = skill_contract()
    op = next(o for o in c["operations"] if o["type"] == "MATCH_CAMERA")
    assert "ffmpeg-skill/look" in op["required_capabilities"] and op["parameters"]["model"]["enum"] == list(match.MODELS)
    assert c["dependencies"][0]["optional_tools"] == ["ffmpeg-skill/look"] and "look" in c["ffmpeg_skill"]["flags_used"]
    d = doctor_report(str(skill_dir))
    assert d["checks"]["capabilities"]["ffmpeg-skill/look"] == "supported"
    legs = FfmpegSkill.locate(str(skill_dir)).info().features["lut_legs_bt709"]
    assert d["checks"]["capabilities"]["ffmpeg-skill/color:lut-bt709-legs"] == {True: "supported", False: "unsupported", None: "unknown"}[legs]
    assert d["checks"]["operations"]["MATCH_CAMERA"]["status"] in (("unsupported",) if legs is False else ("supported", "unknown"))


# ---- real runs
def _run(doc):
    import shutil
    shutil.rmtree("out", ignore_errors=True)
    code, out, _ = run_cli(["run", "-", "--json"], json.dumps(doc))
    return code, one_json(out)


def _refused_here(skill_dir, code, d):
    """Before ffmpeg-skill 3.1.6 its --lut reads RGB with another matrix than look's frames show (its #410), so a fit
    from those frames is not what --lut applies: MATCH_CAMERA is unsupported there and refused before anything runs
    (ADR-23). Asserted here; True so the caller stops."""
    from color_grading.adapter import FfmpegSkill
    if FfmpegSkill.locate(str(skill_dir)).info().features["lut_legs_bt709"] is not False:
        return False
    assert code != 0 and d["error"]["code"] == "TOOL_ERROR" and d["error"]["details"]["missing"] == ["ffmpeg-skill/color:lut-bt709-legs"], d
    return True


def _doc(**params):
    params.setdefault("reference_path", "camera_a.mp4")
    params.setdefault("at", 1.0)
    params.setdefault("reference_at", 1.0)
    return request_doc([_op(**params)], source={"source_id": "b", "path": "camera_b.mp4"})


def test_camera_b_matches_camera_a_within_delta_e_2(workspace, skill_dir):
    """Done when: a fixture whose camera B is camera A with a known tint and gain matches within ΔE 2 on average."""
    code, d = _run(_doc())
    if _refused_here(skill_dir, code, d):
        return
    assert code == 0 and d["ok"], d.get("error")
    r = next(x for x in d["results"] if x["type"] == "MATCH_CAMERA")
    de = r["measurements"]["delta_e2000"]
    assert de["after"]["metric"] == "CIEDE2000" and de["after"]["samples"] > 500
    assert de["before"]["mean"] > 3.0          # the fixture's difference is real
    assert de["after"]["mean"] < 2.0, de       # and the written file matches the reference within ΔE 2 on average
    assert r["lut"]["sha256"] and r["lut"]["path"].endswith(".cube") and r["reference"]["sha256"]
    assert r["artifact"]["pix_fmt"] == "yuv420p"
    fit = r["measurements"]["fit"]
    assert fit["model"] == "curves" and len(fit["curves"]) == 3
    # the red curve inverts the 0.86 gain: slope about 1/0.86
    assert fit["curves"][0]["line"][0] == pytest.approx(1 / CAMERA_B_GAIN[0], rel=0.05)
    prov = next(e for e in d["outputs"][0]["provenance"]["operations"] if e["type"] == "MATCH_CAMERA")
    assert prov["measurements"]["delta_e2000"]["after"] == de["after"] and prov["reference"] == r["reference"]
    # deterministic and reused on a re-run
    code, d2 = _run(_doc())
    r2 = next(x for x in d2["results"] if x["type"] == "MATCH_CAMERA")
    assert code == 0 and r2["status"] == "reused" and r2["artifact"]["sha256"] == r["artifact"]["sha256"] and r2["lut"] == r["lut"]
    assert r2["measurements"]["delta_e2000"] == de


def test_matrix_on_stated_patches(workspace, skill_dir):
    patches = ";".join(f"{x},{y},0.2,0.2" for x in (0.0, 0.4, 0.8) for y in (0.0, 0.4, 0.8))
    code, d = _run(_doc(model="matrix", patches=patches))
    if _refused_here(skill_dir, code, d):
        return
    assert code == 0 and d["ok"], d.get("error")
    m = next(x for x in d["results"] if x["type"] == "MATCH_CAMERA")["measurements"]
    assert m["sampling"] == "9 stated patches" and m["delta_e2000"]["after"]["samples"] == 9
    assert m["delta_e2000"]["after"]["mean"] < 2.0 < m["delta_e2000"]["before"]["mean"]
    assert m["fit"]["matrix"][0][0] == pytest.approx(1 / CAMERA_B_GAIN[0], rel=0.05)


def test_whole_frame_needs_the_same_framing(workspace, skill_dir):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:rate=25", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(workspace / "four_three.mp4")], check=True)
    code, d = _run(_doc(reference_path="four_three.mp4"))
    if _refused_here(skill_dir, code, d):
        return
    assert code != 0 and d["error"]["code"] == "INVALID_INPUT" and d["error"]["details"]["reason"] == "frame_size_mismatch"
    assert not (workspace / "out").exists() or not any((workspace / "out").iterdir())


def test_hdr_input_is_refused(workspace, skill_dir):
    doc = request_doc([_op(reference_path="camera_a.mp4")], source={"source_id": "h", "path": "hdr.mp4"})
    code, d = _run(doc)
    if _refused_here(skill_dir, code, d):
        return
    assert code != 0 and d["error"]["code"] == "INVALID_INPUT" and d["error"]["details"]["reason"] == "hdr_input"


def test_reference_follows_the_input_path_policy(workspace, tmp_path, skill_dir):
    code, out, _ = run_cli(["plan", "-", "--json", "--workspace", str(workspace), "--allowed-input", str(workspace)],
                           json.dumps(_doc(reference_path=str(tmp_path / "elsewhere.mp4"))))
    d = one_json(out)
    assert d["ok"] is False and d["error"]["code"] in ("INVALID_INPUT", "PATH_NOT_ALLOWED")
    code, out, _ = run_cli(["plan", "-", "--json"], json.dumps(_doc()))
    d = one_json(out)
    assert not (workspace / ".color-grading").exists()
    if _refused_here(skill_dir, code, d):
        return
    assert code == 0 and d["plan"]["steps"][0]["type"] == "MATCH_CAMERA" and not (workspace / ".color-grading").exists()
