"""OCIO_TRANSFORM (ECOSYSTEM_ROADMAP.md step 5, docs/decisions.md ADR-22): validation, the baked LUT's accuracy
against OpenColorIO's own CPU processor, the doctor without PyOpenColorIO, and real runs through ffmpeg-skill.
PyOpenColorIO is required here (CI installs the `ocio` extra on every job); nothing is skipped."""
import json
import subprocess
import sys

import pytest

import PyOpenColorIO as ocio_lib  # noqa: F401  (a missing extra fails this module, it is not skipped)

from color_grading import ocio
from color_grading.contract import skill_contract
from color_grading.doctor import doctor_report
from color_grading.errors import ColorError
from color_grading.executor import Executor
from color_grading.model import parse_request
from color_grading.security import PathPolicy
from conftest import one_json, request_doc, run_cli

REC709 = "Rec.1886 Rec.709 - Display"
PQ = "Rec.2100-PQ - Display"


def _cfg():
    return ocio_lib.Config.CreateFromFile(ocio.DEFAULT_CONFIG)


def _view(display, *words):
    """The config's own view whose name has every word (view names differ between OCIO releases' ACES configs)."""
    views = list(_cfg().getViews(display))
    return next(v for v in views if v.startswith("ACES") and all(w in v for w in words))


def _op(**params):
    return {"op_id": "t", "type": "OCIO_TRANSFORM", "input": "source", "parameters": params}


def _params(**params):
    return parse_request(request_doc([_op(**params)])).project.operations[0].parameters


# ---- validation (no OCIO needed)

@pytest.mark.parametrize("params,field", [
    ({}, "source_space"),
    ({"source_space": "ACEScct"}, "destination_space"),
    ({"source_space": "ACEScct", "destination_space": "ACEScg", "display": REC709, "view": "x"}, "destination_space"),
    ({"source_space": "ACEScct", "display": REC709}, "view"),
    ({"source_space": "ACEScct", "view": "x", "destination_space": "ACEScg", "output_tags": "bt709"}, "display"),
    ({"source_space": "ACEScct", "destination_space": "ACEScg"}, "output_tags"),
    ({"source_space": "ACEScct", "display": PQ, "view": "x", "output_tags": "bt709"}, "output_tags"),
    ({"source_space": "ACEScct", "display": REC709, "view": "x", "lut_size": 64}, "lut_size"),
    ({"source_space": "ACEScct", "display": REC709, "view": "x", "config": "/etc/passwd"}, "config"),
])
def test_invalid_parameters_are_rejected_naming_the_field(params, field):
    with pytest.raises(ColorError) as e:
        _params(**params)
    assert e.value.code == "INVALID_REQUEST" and field in (e.value.details.get("field") or "") + e.value.message


def test_output_tags_follow_a_known_destination_and_are_explicit_otherwise():
    assert _params(source_space="ACEScct", display=REC709, view="v")["output_tags"] == "bt709"
    assert _params(source_space="ACEScct", display=PQ, view="v")["output_tags"] == "bt2020-pq"
    p = _params(source_space="ACEScct", destination_space="ACEScg", output_tags="bt709")
    assert p["output_tags"] == "bt709" and p["lut_size"] == ocio.DEFAULT_LUT_SIZE and p["config"] == ocio.DEFAULT_CONFIG


# ---- the baked LUT against OpenColorIO's own processor (Done when: within 1/1023 on a test chart)

@pytest.mark.parametrize("source,display,words", [
    ("S-Log3 S-Gamut3.Cine", REC709, ("SDR", "Rec.709")),
    ("ARRI LogC4", PQ, ("HDR", "1000")),
])
def test_baked_lut_matches_ocio_cpu_processor_within_one_10bit_code_value(source, display, words):
    params = _params(source_space=source, display=display, view=_view(display, *words), lut_size=129)
    baked = ocio.bake(params)
    # read the LUT back from the text ffmpeg-skill will read, not from the in-memory values
    n, values = ocio.parse_cube(baked.text)
    assert n == 129 and baked.lut_size == 129
    cfg = _cfg()
    cpu = cfg.getProcessor(ocio_lib.DisplayViewTransform(src=source, display=display, view=params["view"])).getDefaultCPUProcessor()
    chart_name, chart = ocio.chart_points(ocio_lib, cfg, source)
    assert chart_name == ocio.CHART_NAME and len(chart) > 150
    m = ocio.measure(cpu, values, n, chart)
    assert m["max_error_1023"] <= 1.0, m
    assert baked.accuracy["within_1_1023"] is True and baked.accuracy["max_error_1023"] == m["max_error_1023"]


def test_smaller_lut_reports_its_measured_error_honestly():
    params = _params(source_space="ACEScct", display=REC709, view=_view(REC709, "SDR", "Rec.709"), lut_size=17)
    acc = ocio.bake(params).accuracy
    assert acc["max_error_1023"] > 1.0 and acc["within_1_1023"] is False and acc["points"] > 150


def test_bake_is_deterministic_and_identity_follows_content():
    params = _params(source_space="ACEScct", destination_space="ACEScg", output_tags="bt709", lut_size=17)
    a, b = ocio.bake(params), ocio.bake(params)
    assert a.text == b.text and a.sha256 == b.sha256
    assert a.config["resolved"].startswith("ocio://") and a.config["sha256"] and a.config["profile_version"]
    assert ocio.bake(dict(params, look=next(iter(_cfg().getLookNames())))).sha256 != a.sha256


def test_interpolation_reproduces_the_nodes_exactly():
    n = 5
    values = [v * v for v in ocio.grid(n)]
    for r, g, b in ((0, 0, 0), (1, 2, 3), (4, 4, 4), (2, 0, 4)):
        i = 3 * (r + n * (g + n * b))
        assert ocio.interpolate(values, n, [r / 4, g / 4, b / 4]) == pytest.approx(values[i:i + 3])


@pytest.mark.parametrize("field,value", [("source_space", "No Such Space"), ("view", "No Such View"), ("look", "No Such Look")])
def test_names_are_checked_against_the_config(field, value):
    params = _params(source_space="ACEScct", display=REC709, view=_view(REC709, "SDR", "Rec.709"))
    params[field] = value
    with pytest.raises(ColorError) as e:
        ocio.bake(params)
    assert e.value.code == "INVALID_REQUEST" and e.value.details["field"] == field and e.value.details["allowed"]


# ---- without PyOpenColorIO

def test_doctor_reports_ocio_missing_without_breaking_the_other_operations(skill_dir, monkeypatch):
    with_ocio = doctor_report(str(skill_dir))
    assert with_ocio["checks"]["ocio"]["status"] == "ok" and ocio.CAPABILITY not in with_ocio["checks"]["operations"]["OCIO_TRANSFORM"]["missing"]
    monkeypatch.setitem(sys.modules, "PyOpenColorIO", None)     # import now raises ImportError
    d = doctor_report(str(skill_dir))
    assert d["checks"]["ocio"]["status"] == "missing" and "pip install" in d["checks"]["ocio"]["install"]
    assert d["checks"]["capabilities"][ocio.CAPABILITY] == "unsupported"
    assert d["checks"]["operations"]["OCIO_TRANSFORM"]["status"] == "unsupported"
    assert d["checks"]["operations"]["OCIO_TRANSFORM"]["missing"] == [ocio.CAPABILITY] + with_ocio["checks"]["operations"]["OCIO_TRANSFORM"]["missing"]
    assert "OCIO_TRANSFORM" in d["unavailable_operations"] and d["status"] != "fail"
    others = {t: o["status"] for t, o in d["checks"]["operations"].items() if t != "OCIO_TRANSFORM"}
    assert others == {t: o["status"] for t, o in with_ocio["checks"]["operations"].items() if t != "OCIO_TRANSFORM"}
    assert any("OCIO_TRANSFORM is unavailable" in w for w in d["warnings"])


def test_run_without_ocio_fails_only_ocio_transform(workspace, skill_dir, monkeypatch):
    from color_grading.doctor import runtime_context
    monkeypatch.setitem(sys.modules, "PyOpenColorIO", None)
    skill, versions, caps = runtime_context(str(skill_dir), 120)
    ex = Executor(PathPolicy(str(workspace)), skill, tool_versions=versions, capabilities=caps)
    d = ex.response(request_doc([_op(source_space="ACEScct", display=REC709, view="v")]))
    assert d["ok"] is False and d["error"]["code"] == "TOOL_ERROR" and d["error"]["details"]["reason"] == "ocio_missing"
    skill, versions, caps = runtime_context(str(skill_dir), 120)
    ex = Executor(PathPolicy(str(workspace)), skill, tool_versions=versions, capabilities=caps)
    d = ex.response(request_doc([{"op_id": "c", "type": "PRIMARY_CORRECTION", "input": "source", "parameters": {"saturation": 0.5}}]))
    assert d["ok"] is True, d.get("error")


# ---- contract

def test_contract_declares_ocio_transform_and_its_optional_dependency():
    c = skill_contract()
    op = next(o for o in c["operations"] if o["type"] == "OCIO_TRANSFORM")
    assert ocio.CAPABILITY in op["required_capabilities"] and op["parameters"]["lut_size"]["enum"] == list(ocio.LUT_SIZES)
    assert {"id": "color.ocio_transform", "lifecycle": "EXPERIMENTAL", "tool_id": "color-grading/run", "operation": "OCIO_TRANSFORM"} in c["provides"]
    dep = c["optional_dependencies"][0]
    assert dep["extra"] == "ocio" and dep["licence"] == "BSD-3-Clause" and dep["needed_by"] == ["OCIO_TRANSFORM"]
    assert all(d["required"] for d in c["dependencies"])      # the required dependency list is unchanged: ffmpeg-skill only


# ---- real runs through ffmpeg-skill

def _run(doc):
    import shutil
    shutil.rmtree("out", ignore_errors=True)
    code, out, err = run_cli(["run", "-", "--json"], json.dumps(doc))
    return code, one_json(out)


def _avg_rgb(path, matrix="bt709"):
    """Average RGB of the first frame decoded with `matrix` (ffmpeg-skill's own --lut leg), in 0..1 (16-bit readout)."""
    import struct
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"scale=in_color_matrix={matrix},format=gbrpf32le,scale=1:1:flags=area,format=rgb48le",
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"], check=True, capture_output=True).stdout
    return [v / 65535 for v in struct.unpack("<3H", out[:6])]


def _refused_here(skill_dir, code, d, workspace):
    """On an ffmpeg-skill whose --retag cannot rewrite an H.264 colour description (0.12.3), OCIO_TRANSFORM is refused
    at planning for every destination (ADR-22): asserted here, and True so the caller stops."""
    from color_grading.adapter import FfmpegSkill
    if FfmpegSkill.locate(str(skill_dir)).info().features["retag_rewrites_h264_tags"] is not False:
        return False
    assert code != 0 and d["error"]["code"] == "TOOL_ERROR" and d["error"]["details"]["reason"] == "retag_cannot_rewrite_tags", d
    assert not (workspace / "out").exists() or not any((workspace / "out").iterdir())
    assert not list((workspace / ".color-grading").rglob("*.mp4")) if (workspace / ".color-grading").exists() else True
    return True


def _lut_apply_avg(workspace, cube_name, project_id, matrix):
    code, d = _run(request_doc([{"op_id": "l", "type": "LUT_APPLY", "input": "source", "parameters": {"lut_path": cube_name}}], project_id=project_id,
                               outputs=[{"output_id": "main", "operation": "op:l", "path": f"out/{project_id}.mp4", "format": "mp4"}]))
    assert code == 0 and d["ok"], d.get("error")
    return _avg_rgb(workspace / "out" / f"{project_id}.mp4", matrix)


def test_rec709_destination_matches_the_processor_and_keeps_bt709_tags(workspace, skill_dir):
    view = _view(REC709, "SDR", "Rec.709")
    code, d = _run(request_doc([_op(source_space="S-Log3 S-Gamut3.Cine", display=REC709, view=view)]))
    if _refused_here(skill_dir, code, d, workspace):
        return
    assert code == 0 and d["ok"], d.get("error")
    r = next(x for x in d["results"] if x["type"] == "OCIO_TRANSFORM")
    art = r["artifact"]
    assert (art["color_space"], art["color_primaries"], art["color_transfer"]) == ("bt709", "bt709", "bt709") and art["pix_fmt"] == "yuv420p"
    assert r["retagged"] is False and r["lut"]["sha256"] == r["ocio"]["lut_sha256"]
    prov = next(e for e in d["outputs"][0]["provenance"]["operations"] if e["type"] == "OCIO_TRANSFORM")
    assert prov["ocio"]["config"]["name"] and prov["ocio"]["config"]["sha256"] and prov["ocio"]["lut_size"] == 65
    assert prov["ocio"]["transform"] == {"source_space": "S-Log3 S-Gamut3.Cine", "destination_space": None, "display": REC709, "view": view, "look": None}
    cpu = _cfg().getProcessor(ocio_lib.DisplayViewTransform(src="S-Log3 S-Gamut3.Cine", display=REC709, view=view)).getDefaultCPUProcessor()
    # the result is OCIO's own on the RGB the picture shows (BT.709, ffmpeg-skill >= 3.1.6); an older ffmpeg-skill feeds
    # lut3d the RGB of the BT.601 matrix (its #410, which doctor warns about), and then it is OCIO's own on that
    from color_grading.adapter import FfmpegSkill
    from color_grading.doctor import LUT_LEGS_FIXED_IN, _version_tuple
    version = _version_tuple(FfmpegSkill.locate(str(skill_dir)).info().version)
    matrix = "bt709" if version is None or version >= LUT_LEGS_FIXED_IN else "bt601"
    want = cpu.applyRGB(_avg_rgb(workspace / "sdr.mp4", matrix))
    got = _avg_rgb(workspace / "out" / "main.mp4", matrix)
    # OCIO_TRANSFORM adds nothing of its own to ffmpeg-skill's --lut: the same cube through LUT_APPLY gives the same colour
    import shutil
    shutil.copy(r["lut"]["path"], workspace / "baked.cube")
    assert max(abs(a - b) for a, b in zip(_lut_apply_avg(workspace, "baked.cube", "same", matrix), got)) <= 1.0 / 255
    # 2.5/255: 8-bit 4:2:0 H.264 on both sides of the comparison; the pre-3.1.6 legs add one 8-bit RGB round trip more
    assert max(abs(a - b) for a, b in zip(got, want)) <= (2.5 if matrix == "bt709" else 3.5) / 255, (matrix, got, want)


def test_pq_destination_is_retagged_and_reused_on_rerun(workspace, skill_dir):
    doc = request_doc([_op(source_space="ARRI LogC4", display=PQ, view=_view(PQ, "HDR", "1000"), lut_size=33)])
    code, d = _run(doc)
    if _refused_here(skill_dir, code, d, workspace):
        return
    assert code == 0 and d["ok"], d.get("error")
    r = next(x for x in d["results"] if x["type"] == "OCIO_TRANSFORM")
    art = r["artifact"]
    assert (art["color_space"], art["color_primaries"], art["color_transfer"]) == ("bt2020nc", "bt2020", "smpte2084") and r["retagged"] is True
    assert r["parameters"]["output_tags"] == "bt2020-pq"
    code, d2 = _run(doc)
    r2 = next(x for x in d2["results"] if x["type"] == "OCIO_TRANSFORM")
    assert code == 0 and r2["status"] == "reused" and r2["artifact"]["sha256"] == art["sha256"] and r2["retagged"] is True


def test_config_file_is_hashed_and_resolved_through_the_lut_policy(workspace, skill_dir):
    (workspace / "studio.ocio").write_text(_cfg().serialize(), encoding="utf-8")
    code, d = _run(request_doc([_op(config="missing.ocio", source_space="ACEScct", destination_space="Gamma 2.4 Encoded Rec.709")]))
    assert code != 0 and d["error"]["code"] == "INVALID_INPUT"
    code, d = _run(request_doc([_op(config="studio.ocio", source_space="ACEScct", destination_space="Gamma 2.4 Encoded Rec.709", lut_size=17)]))
    if _refused_here(skill_dir, code, d, workspace):
        return
    assert code == 0 and d["ok"], d.get("error")
    prov = next(e for e in d["outputs"][0]["provenance"]["operations"] if e["type"] == "OCIO_TRANSFORM")["ocio"]
    assert prov["config"]["kind"] == "file" and prov["config"]["file_sha256"] and prov["config"]["path"].endswith("studio.ocio")


def test_dry_run_bakes_nothing_to_disk(workspace, skill_dir):
    code, out, _ = run_cli(["plan", "-", "--json"], json.dumps(request_doc([_op(source_space="ACEScct", display=REC709, view=_view(REC709, "SDR", "Rec.709"), lut_size=17)])))
    d = one_json(out)
    if _refused_here(skill_dir, code, d, workspace):
        assert not (workspace / ".color-grading").exists()
        return
    assert code == 0 and d["ok"] and d["dry_run"]
    step = d["plan"]["steps"][0]
    assert step["ocio"]["lut_size"] == 17 and step["lut"]["path"].endswith(".cube")
    assert not (workspace / ".color-grading").exists()
