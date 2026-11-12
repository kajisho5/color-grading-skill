"""ffmpeg-skill's optional features (docs/decisions.md ADR-29, ADR-30, ADR-33): check --levels, color --legalize, color
--keep-alpha and color --drop-alpha are detected flag by flag, used only where detected as supported, and every other checkout keeps the
earlier behaviour (LEGALIZE's soft clip alone with the shares not measured; an alpha source refused). No ffmpeg here:
the checkout's contract and ffmpeg-skill's answers are stubbed; the real runs are in test_legalize.py / test_alpha.py."""
import json
from pathlib import Path

import pytest

from color_grading import legalize
from color_grading.adapter import FLAGS_USED, OPTIONAL_FLAGS, OPTIONAL_TOOL_FLAGS, REQUIRED_FLAGS, FfmpegSkill, SkillInfo, check_tools
from color_grading.contract import skill_contract
from color_grading.doctor import capability_status, doctor_report
from color_grading.errors import ColorError
from color_grading.executor import OPTIONAL_CHECKOUT_CAPABILITIES, Executor, NodeState
from color_grading.graph import Node
from color_grading.security import PathPolicy
from conftest import request_doc

ALL = {"ffmpeg-skill/check:levels": "supported", "ffmpeg-skill/color:legalize": "supported", "ffmpeg-skill/color:keep-alpha": "supported",
       "ffmpeg-skill/color:drop-alpha": "supported"}


def _tools(color_extra=(), check=None):
    def spec(name, flags):
        return {"name": name, "input_schema": {"properties": {f: {} for f in flags}}}
    tools = {"color": spec("color", list(REQUIRED_FLAGS["color"]) + ["quality", *color_extra]), "probe": spec("probe", REQUIRED_FLAGS["probe"])}
    if check is not None:
        tools["check"] = spec("check", check)
    return tools


# ---- detection
def test_a_checkout_without_the_optional_flags_is_still_usable_and_says_so():
    problems, f = check_tools(_tools())
    assert problems == [] and f["color_legalize"] is False and f["color_keep_alpha"] is False and f["check_tool"] is False
    assert f["color_drop_alpha"] is False


def test_each_optional_feature_needs_all_of_its_flags():
    _, f = check_tools(_tools(color_extra=["legalize", "keep_alpha", "alpha_codec"], check=["input", "levels"]))
    assert f["color_legalize"] is True and f["color_keep_alpha"] is False      # --alpha-mode missing
    assert f["check_tool"] is False and set(f["check_missing_flags"]) == {"platform", "no_loudness", "levels_window", "levels_tolerance", "json"}
    _, f = check_tools(_tools(color_extra=["legalize", "keep_alpha", "alpha_codec", "alpha_mode"], check=list(OPTIONAL_TOOL_FLAGS["check"])))
    assert f["color_legalize"] is f["color_keep_alpha"] is f["check_tool"] is True
    assert f["color_drop_alpha"] is False                                        # --drop-alpha is a group of its own
    _, f = check_tools(_tools(color_extra=["drop_alpha"]))
    assert f["color_drop_alpha"] is True and f["color_keep_alpha"] is False


@pytest.mark.parametrize("value,status", [(True, "supported"), (False, "unsupported"), (None, "unknown")])
def test_optional_capabilities_map_the_detected_features(value, status):
    info = SkillInfo(Path("."), "x", "2.0", {}, [], {"check_tool": value, "color_legalize": value, "color_keep_alpha": value, "color_drop_alpha": value})
    caps = capability_status(info, {"ffmpeg": "6.1", "ffprobe": "6.1", "available": ["filter:lut3d"]})
    assert {c: caps[c] for c in OPTIONAL_CHECKOUT_CAPABILITIES} == {c: status for c in OPTIONAL_CHECKOUT_CAPABILITIES}


def test_contract_names_the_optional_flags_and_tool():
    c = skill_contract()
    assert c["ffmpeg_skill"]["optional_flags"] == {"color": {"color_legalize": ["legalize"], "color_keep_alpha": ["keep_alpha", "alpha_codec", "alpha_mode"],
                                                             "color_drop_alpha": ["drop_alpha"]}}
    assert set(c["ffmpeg_skill"]["flags_used"]["color"]) >= {"legalize", "keep_alpha", "alpha_codec", "alpha_mode", "drop_alpha"}
    assert c["ffmpeg_skill"]["flags_used"]["check"] == list(OPTIONAL_TOOL_FLAGS["check"]) and set(FLAGS_USED["color"]) >= set(OPTIONAL_FLAGS["color"]["color_legalize"])
    assert c["optional_capabilities"] == list(OPTIONAL_CHECKOUT_CAPABILITIES)
    assert c["alpha"]["keep_alpha"]["operations"] == ["LUT_APPLY", "PRIMARY_CORRECTION"]
    assert c["alpha"]["refused_for"] == ["HDR_TO_SDR", "LUT_APPLY", "PRIMARY_CORRECTION", "OCIO_TRANSFORM", "MATCH_CAMERA", "LEGALIZE"]
    assert c["legalize"]["out_of_range_share"]["keys"] == list(legalize.LEVELS_KEYS)
    # every operation's required capabilities are unchanged: an optional feature never makes an operation unavailable
    for op in c["operations"]:
        assert not set(op["required_capabilities"]) & set(OPTIONAL_CHECKOUT_CAPABILITIES)


def test_doctor_lists_the_optional_capabilities(skill_dir):
    d = doctor_report(str(skill_dir))
    opt = d["checks"]["optional_capabilities"]
    assert set(opt) == set(OPTIONAL_CHECKOUT_CAPABILITIES)
    for cap, entry in opt.items():
        assert entry["status"] == d["checks"]["capabilities"][cap] and entry["used_for"]


# ---- check --levels through the adapter (its process stubbed)
class _LevelsSkill(FfmpegSkill):
    def __init__(self, code, doc):
        super().__init__(Path("."))
        self.answer = (code, json.dumps(doc), "", 0.1)
        self.argv = None

    def script(self, tool):
        return f"/x/{tool}.py"

    def _popen(self, argv, timeout, cwd=None):
        self.argv = list(argv)
        return self.answer


LEVELS = {"standard": "bt709", "frames": 3, "bit_depth": 8, "legal": {"y": [16, 235], "c": [16, 240]},
          "out_of_range_pct": {"y": 2.0, "u": 0.0, "v": 0.0, "chroma": 0.0}, "brng_pct": 2.0,
          "worst_frame": {"time": 0.0, "y_pct": 3.0, "chroma_pct": 0.0}, "min": {"y": 4, "u": 90, "v": 90}, "max": {"y": 250, "u": 160, "v": 160}}


def test_levels_never_lets_the_row_fail_and_reads_the_document_even_when_check_failed():
    s = _LevelsSkill(1, {"status": "failed", "levels": LEVELS, "checks": []})
    doc, why = s.levels("/a.mp4", "ebu-r103")
    assert doc == LEVELS and why == ""
    a = s.argv
    assert a[a.index("--levels") + 1] == "ebu-r103" and a[a.index("--levels-tolerance") + 1] == "100" and "--no-loudness" in a and a[2] == "/a.mp4"
    assert s.runs[-1].tool == "check"


class _RaisingSkill(_LevelsSkill):
    def __init__(self, error):
        super().__init__(0, {})
        self.error = error

    def _popen(self, argv, timeout, cwd=None):
        raise self.error


def test_a_measurement_that_times_out_leaves_the_shares_unmeasured():
    doc, why = _RaisingSkill(ColorError("TOOL_ERROR", "check.py exceeded 5s", {"reason": "timeout"})).levels("/a.mp4", "bt709", 5)
    assert doc is None and "did not finish" in why and "exceeded" in why


def test_a_cancelled_measurement_stays_cancelled():
    with pytest.raises(ColorError) as e:
        _RaisingSkill(ColorError("CANCELLED", "interrupted")).levels("/a.mp4", "bt709")
    assert e.value.code == "CANCELLED"


def test_levels_reports_why_nothing_was_measured():
    row = {"check": "legal levels", "status": "WARN", "value": "not measured: rgb24 is not a Y'CbCr picture", "reason": "an RGB or grey picture has no luma"}
    doc, why = _LevelsSkill(0, {"status": "completed", "checks": [row]}).levels("/a.mov", "bt709")
    assert doc is None and "rgb24" in why and "RGB" in why
    doc, why = _LevelsSkill(2, {"status": "failed", "error": {"message": "boom"}}).levels("/a.mov", "bt709")
    assert doc is None and "boom" in why


# ---- the executor's gates (probe stubbed, dry run: nothing is encoded)
class _StubSkill:
    def __init__(self, video):
        self.video = video
        self.runs = []

    def probe(self, path, timeout=None):
        return {"duration": 2.0, "video": dict(self.video), "audio": None}


def _plan(tmp_path, ops, caps, video, src="clip.mov", alpha_mode=None):
    (tmp_path / src).write_bytes(b"x")
    (tmp_path / "grade.cube").write_text("LUT_3D_SIZE 2\n" + "0 0 0\n" * 8)
    ext = src.rsplit(".", 1)[1]
    source = {"source_id": "s", "path": str(tmp_path / src), **({"alpha_mode": alpha_mode} if alpha_mode else {})}
    doc = request_doc(ops, source=source,
                      outputs=[{"output_id": "o", "operation": f"op:{ops[-1]['op_id']}", "path": str(tmp_path / "out" / f"o.{ext}"), "format": ext}])
    ex = Executor(PathPolicy(str(tmp_path)), _StubSkill(video), dry_run=True, capabilities=caps)
    return ex.response(doc)


ALPHA_VIDEO = {"width": 160, "height": 90, "pix_fmt": "yuva444p10le", "codec": "prores"}
LUT = {"op_id": "l", "type": "LUT_APPLY", "input": "source", "parameters": {"lut_path": "grade.cube"}}


@pytest.mark.parametrize("status", ["unsupported", "unknown"])
def test_an_alpha_source_is_refused_unless_keep_alpha_is_detected(tmp_path, status):
    d = _plan(tmp_path, [LUT], {**ALL, "ffmpeg-skill/color:keep-alpha": status}, ALPHA_VIDEO)
    assert d["ok"] is False and d["error"]["details"]["reason"] == "alpha_not_preserved"
    assert d["error"]["details"]["keep_alpha"]["available"] is False and "update ffmpeg-skill" in d["error"]["message"]


def test_keep_alpha_is_planned_with_the_line_of_the_input_codec(tmp_path):
    d = _plan(tmp_path, [LUT, {"op_id": "p", "type": "PRIMARY_CORRECTION", "input": "op:l", "parameters": {"gamma": 1.2}}], ALL,
              {**ALPHA_VIDEO, "codec": "qtrle", "pix_fmt": "argb"})
    assert d["ok"], d.get("error")
    assert [s["alpha"] for s in d["plan"]["steps"]] == [{"keep_alpha": True, "alpha_codec": "qtrle", "alpha_mode": "straight"}] * 2
    assert any("RGB alpha source" in w for w in d["warnings"])           # --lut on an RGB alpha line (ADR-30)


def test_no_rgb_warning_for_a_ycbcr_alpha_master_or_a_correction(tmp_path):
    assert not any("RGB alpha source" in w for w in _plan(tmp_path, [LUT], ALL, ALPHA_VIDEO)["warnings"])
    pc = {"op_id": "p", "type": "PRIMARY_CORRECTION", "input": "source", "parameters": {"gamma": 1.2}}
    # with the colour's alpha mode stated (ADR-33) a correction on an RGB alpha source warns about nothing at all ...
    assert not _plan(tmp_path, [pc], ALL, {**ALPHA_VIDEO, "codec": "qtrle", "pix_fmt": "argb"}, alpha_mode="straight")["warnings"]
    # ... and without it the only warning is that the mode was assumed, not the RGB one
    (w,) = _plan(tmp_path, [pc], ALL, {**ALPHA_VIDEO, "codec": "qtrle", "pix_fmt": "argb"})["warnings"]
    assert "neither the request" in w and "RGB alpha source" not in w


@pytest.mark.parametrize("pix_fmt,deep", [("yuva444p10le", True), ("yuva444p12le", True), ("gbrap16le", True), ("rgba64le", True),
                                          ("yuva420p", False), ("argb", False), ("rgba", False)])
def test_an_alpha_deeper_than_8_bits_is_named_in_a_warning(tmp_path, pix_fmt, deep):
    pc = {"op_id": "p", "type": "PRIMARY_CORRECTION", "input": "source", "parameters": {"gamma": 1.2}}
    d = _plan(tmp_path, [pc], ALL, {**ALPHA_VIDEO, "pix_fmt": pix_fmt})
    assert d["ok"], d.get("error")
    assert any("deeper than 8 bits" in w for w in d["warnings"]) is deep


def test_an_alpha_grade_needs_its_lines_encoder_not_libx264(tmp_path):
    d = _plan(tmp_path, [LUT], {**ALL, "encoder:libx264": "unsupported"}, ALPHA_VIDEO)
    assert d["ok"], d.get("error")
    step = d["plan"]["required_capabilities"]
    assert "encoder:prores_ks" in step and "encoder:libx264" not in step
    d = _plan(tmp_path, [LUT], {**ALL, "encoder:prores_ks": "unsupported"}, ALPHA_VIDEO)
    assert d["ok"] is False and d["error"]["code"] == "TOOL_ERROR" and d["error"]["details"]["missing"] == ["encoder:prores_ks"]
    # an opaque grade still needs libx264
    d = _plan(tmp_path, [LUT], {**ALL, "encoder:libx264": "unsupported"}, {**ALPHA_VIDEO, "pix_fmt": "yuv422p10le"})
    assert d["ok"] is False and d["error"]["details"]["missing"] == ["encoder:libx264"]


def test_an_opaque_source_gets_no_alpha_flags(tmp_path):
    d = _plan(tmp_path, [LUT], ALL, {**ALPHA_VIDEO, "pix_fmt": "yuv422p10le"})
    assert d["ok"] and "alpha" not in d["plan"]["steps"][0]


def _legal_plan(tmp_path, caps):
    d = _plan(tmp_path, [{"op_id": "g", "type": "LEGALIZE", "input": "source"}], caps,
              {"width": 160, "height": 90, "pix_fmt": "yuv420p", "codec": "h264"}, src="clip.mp4")
    assert d["ok"], d.get("error")
    return d["plan"]["steps"][0]


def test_legalize_plans_the_clamp_and_the_measurement_only_where_supported(tmp_path):
    with_all = _legal_plan(tmp_path, ALL)
    assert with_all["legalize"]["clamp"] == {"applied": True, "tool": "ffmpeg-skill/color --legalize", "standard": "bt709", "verification": None}
    assert with_all["legalize"]["out_of_range_share"] == {"measured": False, "reason": legalize.SHARES_PLANNED}
    none = _legal_plan(tmp_path, {})
    assert none["legalize"]["clamp"] == {"applied": False, "reason": legalize.CLAMP_UNAVAILABLE}
    assert none["legalize"]["out_of_range_share"] == {"measured": False, "reason": legalize.SHARES_NOT_MEASURED}
    # the clamp changes the written pixels, so it is part of the identity; the measurement is not
    assert with_all["operation_id"] != none["operation_id"]
    assert _legal_plan(tmp_path, {"ffmpeg-skill/color:legalize": "supported"})["operation_id"] == with_all["operation_id"]


def test_legalize_argv_carries_the_clamp_only_when_planned(tmp_path):
    params = {"profile": "ebu-r103", "knee": 0.95, "lut_size": 33, "crf": 18, "preset": "medium", "audio_stream": 0}
    ex = Executor(PathPolicy(str(tmp_path)), skill=None)
    st = NodeState(Node("op:g", "LEGALIZE", ["source"], params), "id")
    st.baked = legalize.bake(params)
    from color_grading.executor import LutInfo
    st.lut = LutInfo(tmp_path / "g.cube", 1, st.baked.sha256)
    assert "--legalize" not in ex._argv(st, "/in.mp4", tmp_path / "o.mp4")
    st.baked.clamp = {"applied": True}
    argv = ex._argv(st, "/in.mp4", tmp_path / "o.mp4")
    assert argv[argv.index("--legalize") + 1] == "ebu-r103" and argv.index("--legalize") == argv.index("--lut") + 2


class _Run:
    def __init__(self, data):
        self.data = data


@pytest.mark.parametrize("step", [None, {"step": "legal_levels", "ok": False, "worst_frame": {"y_pct": 4.0}}])
def test_a_failed_or_missing_legal_levels_step_is_a_validation_error(tmp_path, step):
    params = {"profile": "bt709", "knee": 0.95, "lut_size": 33}
    st = NodeState(Node("op:g", "LEGALIZE", ["source"], params), "id")
    st.baked = legalize.bake(params)
    st.baked.clamp = {"applied": True, "tool": "ffmpeg-skill/color --legalize", "standard": "bt709", "verification": None}
    with pytest.raises(ColorError) as e:
        Executor(PathPolicy(str(tmp_path)), skill=None)._check_tool_verification(st, _Run({"verification": [step] if step else []}), tmp_path / "o.mp4")
    assert e.value.code == "VALIDATION_ERROR" and e.value.details["reason"] == "legal_levels_failed"


def test_a_passing_legal_levels_step_and_its_residual_are_recorded(tmp_path):
    params = {"profile": "bt709", "knee": 0.95, "lut_size": 33}
    st = NodeState(Node("op:g", "LEGALIZE", ["source"], params), "id")
    st.baked = legalize.bake(params)
    st.baked.clamp = {"applied": True, "tool": "ffmpeg-skill/color --legalize", "standard": "bt709", "verification": None}
    step = {"step": "legal_levels", "ok": True, "out_of_range_pct": {"y": 0.01, "u": 0.0, "v": 0.0, "chroma": 0.0}}
    data = {"verification": [step], "legalize": {"standard": "bt709", "output": {"out_of_range_pct": step["out_of_range_pct"]}}}
    Executor(PathPolicy(str(tmp_path)), skill=None)._check_tool_verification(st, _Run(data), tmp_path / "o.mp4")
    assert st.baked.provenance()["clamp"] == {"applied": True, "tool": "ffmpeg-skill/color --legalize", "standard": "bt709", "verification": step,
                                              "residual": step["out_of_range_pct"]}
