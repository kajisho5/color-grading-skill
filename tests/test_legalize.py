"""LEGALIZE (ECOSYSTEM_ROADMAP.md step 7, docs/decisions.md ADR-24): the soft clip, request validation, and real runs
through ffmpeg-skill/color --lut on a fixture with super-whites and sub-blacks. The tests measure the written file's
Y'CbCr themselves (ffmpeg rawvideo): the skill cannot, ffmpeg-skill exposes no such measurement (ADR-24)."""
import json
import subprocess

import pytest

from color_grading import legalize
from color_grading.errors import ColorError
from color_grading.model import parse_request
from color_grading.ocio import parse_cube
from conftest import one_json, request_doc, run_cli


def out_of_range(path, luma=(16, 235), chroma=(16, 240)):
    """(share of luma samples, share of chroma samples) outside the limits, every frame, 8-bit 4:2:0."""
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(path)],
                                      capture_output=True, text=True, check=True).stdout)["streams"][0]
    w, h = probe["width"], probe["height"]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"], capture_output=True, check=True).stdout
    fs = w * h * 3 // 2
    frames = len(raw) // fs
    bad_y = bad_c = 0
    for k in range(frames):
        f = raw[k * fs:(k + 1) * fs]
        bad_y += sum(1 for v in f[:w * h] if not luma[0] <= v <= luma[1])
        bad_c += sum(1 for v in f[w * h:] if not chroma[0] <= v <= chroma[1])
    return bad_y / (frames * w * h), bad_c / (frames * w * h // 2)


# ---- the curve and the LUT
@pytest.mark.parametrize("profile", sorted(legalize.PROFILES))
def test_soft_clip_is_identity_below_the_knee_monotone_and_inside_the_limits(profile):
    lo, hi = legalize.PROFILES[profile]["rgb"]
    knee = 0.95
    k = lo + knee * (hi - lo)
    xs = [i / 1000 * 1.25 for i in range(1001)]
    ys = [legalize.soft_clip(x, lo, hi, knee) for x in xs]
    assert all(b >= a for a, b in zip(ys, ys[1:])) and all(lo <= y < hi for y in ys)
    assert all(y == pytest.approx(max(lo, x)) for x, y in zip(xs, ys) if x <= k)
    assert legalize.soft_clip(1.091, lo, hi, knee) < hi       # 8-bit super-white Y' 255 stays below the limit


def test_lut_is_deterministic_declares_its_domain_and_reads_back():
    params = parse_request(request_doc([{"op_id": "l", "type": "LEGALIZE", "input": "source"}])).project.operations[0].parameters
    assert params == {"profile": "bt709", "knee": 0.95, "lut_size": 65, "crf": 18, "preset": "medium", "audio_stream": 0}
    a, b = legalize.bake(params), legalize.bake(params)
    assert a.text == b.text and a.sha256 == b.sha256
    assert "DOMAIN_MIN 0 0 0" in a.text and "DOMAIN_MAX 1.25 1.25 1.25" in a.text
    n, values = parse_cube("\n".join(line for line in a.text.splitlines() if not line.startswith("DOMAIN")))
    assert n == 65 and max(values) < 1.0 and min(values) == 0.0
    prov = a.provenance()
    assert prov["out_of_range_share"]["measured"] is False and prov["rgb_limits"] == [0.0, 1.0]


def test_profile_limits_in_code_values():
    assert legalize.limits_8bit("bt709") == ((16.0, 235.0), (16.0, 240.0))
    (ylo, yhi), _ = legalize.limits_8bit("ebu-r103")
    assert ylo == pytest.approx(16 - 2.19) and yhi == pytest.approx(16 + 219 * 1.03)


@pytest.mark.parametrize("params,field", [({"profile": "smpte"}, "profile"), ({"knee": 0.5}, "knee"), ({"knee": 1.0}, "knee"), ({"lut_size": 17}, "lut_size")])
def test_invalid_parameters_are_rejected(params, field):
    with pytest.raises(ColorError) as e:
        parse_request(request_doc([{"op_id": "l", "type": "LEGALIZE", "input": "source", "parameters": params}]))
    assert e.value.code == "INVALID_REQUEST" and field in e.value.details["field"]


# ---- real runs
def _run(doc):
    import shutil
    shutil.rmtree("out", ignore_errors=True)
    code, out, _ = run_cli(["run", "-", "--json"], json.dumps(doc))
    return code, one_json(out)


def _refused_here(skill_dir, code, d):
    """Before ffmpeg-skill 3.1.6 --lut converts through 8-bit RGB with mismatched matrices (its #410): super-whites never
    reach the LUT and colours shift, so LEGALIZE is unsupported there and refused before anything runs (ADR-24)."""
    from color_grading.adapter import FfmpegSkill
    if FfmpegSkill.locate(str(skill_dir)).info().features["lut_legs_bt709"] is not False:
        return False
    assert code != 0 and d["error"]["code"] == "TOOL_ERROR" and d["error"]["details"]["missing"] == ["ffmpeg-skill/color:lut-bt709-legs"], d
    return True


def _doc(**params):
    return request_doc([{"op_id": "l", "type": "LEGALIZE", "input": "source", "parameters": params}], source={"source_id": "s", "path": "super_white.mp4"})


def test_super_whites_end_within_bt709_legal_range(workspace, skill_dir):
    before = out_of_range(workspace / "super_white.mp4")
    assert before[0] > 0.05                      # the fixture really has super-whites / sub-blacks
    code, d = _run(_doc())
    if _refused_here(skill_dir, code, d):
        return
    assert code == 0 and d["ok"], d.get("error")
    assert out_of_range(workspace / "out" / "main.mp4") == (0.0, 0.0)
    r = next(x for x in d["results"] if x["type"] == "LEGALIZE")
    assert r["legalize"]["profile"] == "bt709" and r["lut"]["sha256"] == r["legalize"]["lut_sha256"]
    # what the skill could not measure, it says so instead of reporting a number
    assert r["legalize"]["out_of_range_share"] == {"measured": False, "reason": legalize.SHARES_NOT_MEASURED}


def test_ebu_r103_profile_keeps_its_wider_tolerance(workspace, skill_dir):
    code, d = _run(_doc(profile="ebu-r103"))
    if _refused_here(skill_dir, code, d):
        return
    assert code == 0 and d["ok"], d.get("error")
    (ylo, yhi), (clo, chi) = legalize.limits_8bit("ebu-r103")
    assert out_of_range(workspace / "out" / "main.mp4", (ylo, yhi), (clo, chi)) == (0.0, 0.0)
    # ffmpeg-skill's way back from float RGB clamps to 0..1 (measured, ADR-24), so the R 103 headroom above 100 % is never
    # written: the result is inside BT.709's range too. Pinned, so a change of that leg shows up here
    assert out_of_range(workspace / "out" / "main.mp4") == (0.0, 0.0)


def test_hdr_input_is_refused(workspace, skill_dir):
    code, d = _run(request_doc([{"op_id": "l", "type": "LEGALIZE", "input": "source"}], source={"source_id": "h", "path": "hdr.mp4"}))
    if _refused_here(skill_dir, code, d):
        return
    assert code != 0 and d["error"]["code"] == "INVALID_INPUT" and d["error"]["details"]["reason"] == "hdr_input"
