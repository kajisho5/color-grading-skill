"""ffmpeg-skill compatibility by capability detection, not by version number (docs/decisions.md ADR-19).

Fake checkouts stand in for the layouts and contract shapes real releases have had: 0.12.x (`--crf`, a single
`_common.py`, probe `hdr` meaning BT.2020-or-PQ/HLG), 2.x (`--quality` only, a `_common/` package, `--overwrite`,
`bt2020_or_hdr`), a contract-less checkout read through `--help`, and a far-future version number. The real checkout
the suite runs against (any version) is checked too."""
import json
import sys
import textwrap
from pathlib import Path

import pytest

from color_grading.adapter import (FLAG_ALTERNATIVES, REQUIRED_FLAGS, FfmpegSkill, SkillInfo, _common_layout, check_tools, flags_from_help,
                                   probe_hdr)
from color_grading.contract import skill_contract
from color_grading.doctor import capability_status, doctor_report


def _props(*names):
    return {"input_schema": {"properties": {n: {} for n in names}}}


def _tools(color_extra=(), drop=(), video_required=True):
    color = [f for f in REQUIRED_FLAGS["color"] if f not in drop] + list(color_extra)
    spec = _props(*color)
    spec["name"] = "color"
    if video_required is not None:
        spec["video_required"] = video_required
    probe = _props(*REQUIRED_FLAGS["probe"])
    probe["name"] = "probe"
    return {"color": spec, "probe": probe}


# ---- pure detection

def test_old_release_with_crf_only_is_usable_and_gets_crf():
    problems, features = check_tools(_tools(color_extra=["crf"]))
    assert problems == []
    assert features["encoder_quality_flag"] == "--crf" and features["overwrite_flag"] is False


def test_new_release_with_quality_only_is_usable_and_gets_quality():
    problems, features = check_tools(_tools(color_extra=["quality", "overwrite"]))
    assert problems == []
    assert features["encoder_quality_flag"] == "--quality" and features["overwrite_flag"] is True


def test_quality_is_preferred_when_both_exist():
    """1.10 - 1.x offered both (--crf deprecated): the replacement is used, so no deprecation warning is triggered."""
    _, features = check_tools(_tools(color_extra=["crf", "quality"]))
    assert features["encoder_quality_flag"] == "--quality"
    assert FLAG_ALTERNATIVES["color"]["encoder_quality"][0] == "quality"


def test_neither_quality_nor_crf_is_a_named_problem():
    problems, features = check_tools(_tools())
    assert features["encoder_quality_flag"] is None
    assert len(problems) == 1 and "quality|crf" in problems[0]


@pytest.mark.parametrize("flag", ["gamma", "audio_stream", "correct", "levels_out_white"])
def test_a_missing_required_flag_is_reported_by_name(flag):
    problems, _ = check_tools(_tools(color_extra=["quality"], drop=[flag]))
    assert len(problems) == 1 and repr(flag) in problems[0]


def test_missing_tool_is_reported():
    tools = _tools(color_extra=["quality"])
    del tools["probe"]
    problems, _ = check_tools(tools)
    assert problems == ["ffmpeg-skill tool 'probe' is missing"]


def test_video_required_is_checked_only_when_declared():
    assert check_tools(_tools(color_extra=["crf"], video_required=None))[0] == []
    assert check_tools(_tools(color_extra=["crf"], video_required=False))[0] == ["ffmpeg-skill/color does not declare video_required"]


def test_flags_from_help_reads_long_flags_and_positionals():
    text = textwrap.dedent("""\
        usage: color.py [-h] [--to-sdr | --lut LUT] [--crf CRF] [-o OUTPUT] input

        positional arguments:
          input                 source video

        options:
          -h, --help            show this help message and exit
          --to-sdr              tone map
          --lut LUT             a .cube file
          --crf CRF             x264 CRF
          -o OUTPUT, --output OUTPUT
                                where to write
        """)
    props = flags_from_help(text)
    assert {"input", "to_sdr", "lut", "crf", "output", "help"} <= set(props)
    assert "usage" not in props and "source" not in props


@pytest.mark.parametrize("video,expected", [
    ({"hdr": True}, True),                                   # 0.x / 1.x: hdr already means BT.2020-or-PQ/HLG
    ({"hdr": False}, False),
    ({"hdr": False, "bt2020_or_hdr": True}, True),           # 2.x: BT.2020 SDR -- hdr narrowed, the old meaning kept
    ({"hdr": True, "hdr_signal": True, "bt2020_or_hdr": True}, True),
    ({"hdr": False, "bt2020_or_hdr": False}, False),
    ({}, False),
])
def test_probe_hdr_keeps_the_pre_2_0_meaning(video, expected):
    assert probe_hdr(video) is expected


def test_common_layout_both_shapes(tmp_path):
    (tmp_path / "scripts").mkdir()
    assert _common_layout(tmp_path) is None
    (tmp_path / "scripts" / "_common").mkdir()
    (tmp_path / "scripts" / "_common" / "__init__.py").write_text("")
    assert _common_layout(tmp_path) == "_common/"
    (tmp_path / "scripts" / "_common.py").write_text("")
    assert _common_layout(tmp_path) == "_common.py"


# ---- fake checkouts end to end (real subprocesses, no ffmpeg needed)

_CONTRACT_SCRIPT = """\
import json, sys
DOC = {doc}
if len(sys.argv) > 1 and sys.argv[1] == "doctor":
    print(json.dumps({{"ffmpeg": "6.1", "ffprobe": "6.1", "available": ["filter:zscale", "filter:tonemap", "filter:lut3d", "encoder:libx264"],
                      "missing": [], "missing_optional": [], "unknown": []}}))
else:
    print(json.dumps(DOC))
"""

_ARGPARSE_COLOR = """\
import argparse
ap = argparse.ArgumentParser()
ap.add_argument("input")
ap.add_argument("-o", "--output")
for flag in {flags}:
    ap.add_argument("--" + flag.replace("_", "-"))
ap.parse_args()
"""


def _fake_checkout(root: Path, version: str, quality: str, layout: str, with_contract: bool = True, overwrite: bool = False,
                   drop=()) -> Path:
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "probe.py").write_text("import sys\nprint('usage: probe.py [-h] inputs [inputs ...]')\n")
    if layout == "module":
        (scripts / "_common.py").write_text("")
    else:
        (scripts / "_common").mkdir()
        (scripts / "_common" / "__init__.py").write_text("")
    (root / "package.json").write_text(json.dumps({"name": "ffmpeg-skill", "version": version}))
    color_flags = [f for f in REQUIRED_FLAGS["color"] if f not in ("input", "output")] + [quality] + (["overwrite"] if overwrite else [])
    if with_contract:
        tools = _tools(color_extra=[quality] + (["overwrite"] if overwrite else []), drop=drop)
        doc = {"contract_version": "1.0", "skill": {"id": "ffmpeg-skill", "version": version}, "tools": list(tools.values())}
        (scripts / "_contract.py").write_text(_CONTRACT_SCRIPT.format(doc=repr(doc)))
        (scripts / "color.py").write_text("print('{}')\n")
    else:
        (scripts / "color.py").write_text(_ARGPARSE_COLOR.format(flags=repr([f for f in color_flags if f != "json"] + ["json"])))
        (scripts / "probe.py").write_text("import argparse\nap = argparse.ArgumentParser()\nap.add_argument('inputs', nargs='+')\nap.parse_args()\n")
    return root


@pytest.mark.parametrize("version,quality,layout,overwrite", [
    ("0.12.3", "crf", "module", False),     # the oldest release that has every flag (the former window's floor)
    ("1.0.0", "crf", "module", False),      # the former window's exclusive upper bound
    ("2.10.3", "quality", "package", True),
    ("17.4.0", "quality", "package", True),  # a release that does not exist yet: no upper bound
])
def test_any_version_with_the_flags_is_usable(tmp_path, version, quality, layout, overwrite):
    root = _fake_checkout(tmp_path / "ffs", version, quality, layout, overwrite=overwrite)
    info = FfmpegSkill.locate(str(root)).info()
    assert info.problems == [] and info.supported
    assert info.version == version
    assert info.quality_flag == "--" + quality
    assert info.features["overwrite_flag"] is overwrite
    assert info.features["common_layout"] == ("_common.py" if layout == "module" else "_common/")
    assert info.features["source"] == "contract"


def test_new_version_without_a_required_flag_is_refused_with_the_flag_named(tmp_path):
    root = _fake_checkout(tmp_path / "ffs", "3.0.0", "quality", "package", drop=["gamma"])
    info = FfmpegSkill.locate(str(root)).info()
    assert not info.supported and any("'gamma'" in p for p in info.problems)
    assert not any("window" in p or "version" in p for p in info.problems)


def test_checkout_without_a_contract_is_read_through_help(tmp_path):
    root = _fake_checkout(tmp_path / "ffs", "0.13.0", "crf", "module", with_contract=False)
    info = FfmpegSkill.locate(str(root)).info()
    assert info.features["source"] == "--help"
    assert info.problems == [], info.problems
    assert info.quality_flag == "--crf" and info.version == "0.13.0"
    # and with no doctor to ask, ffmpeg/ffprobe/filters are unknown, not unsupported
    d = doctor_report(str(root))
    assert d["checks"]["ffmpeg_skill"]["status"] == "ok"
    assert d["checks"]["capabilities"]["ffmpeg"] == "unknown"
    assert all(o["status"] == "unknown" for o in d["checks"]["operations"].values())
    assert d["status"] == "ok"


def test_doctor_reports_what_was_detected(tmp_path):
    root = _fake_checkout(tmp_path / "ffs", "2.10.3", "quality", "package", overwrite=True)
    d = doctor_report(str(root))
    fs = d["checks"]["ffmpeg_skill"]
    assert fs["status"] == "ok" and fs["version"] == "2.10.3"
    assert fs["detected"]["encoder_quality_flag"] == "--quality" and fs["detected"]["common_layout"] == "_common/"
    assert "window" not in json.dumps(d["problems"])


def test_executor_emits_the_detected_quality_flag(tmp_path):
    from color_grading.executor import Executor, NodeState
    from color_grading.graph import Node
    from color_grading.security import PathPolicy
    params = {"tonemap": "hable", "peak_nits": 1000.0, "desat": 0.0, "force": False, "crf": 21, "preset": "medium", "audio_stream": 0}
    for flag in ("--quality", "--crf"):
        skill = FfmpegSkill(tmp_path)
        skill._info = SkillInfo(tmp_path, "x", "1.0", {}, [], {"encoder_quality_flag": flag})
        ex = Executor(PathPolicy(str(tmp_path)), skill)
        argv = ex._argv(NodeState(Node("op:h", "HDR_TO_SDR", ["source"], params), "id"), "/in.mp4", tmp_path / "o.mp4")
        assert argv[argv.index(flag) + 1] == "21"
        assert ({"--quality", "--crf"} - {flag}).isdisjoint(argv)


def test_contract_declares_no_version_window():
    c = skill_contract()["ffmpeg_skill"]
    assert c["version_window"] is None
    assert set(c["flags_used"]["color"]) >= {"crf", "quality"}
    assert c["flag_alternatives"] == {"color": {"encoder_quality": ["quality", "crf"]}}


def test_capability_status_with_no_doctor_is_unknown():
    s = capability_status(SkillInfo(Path("."), "x", "1.0", {}, []), {"_unavailable": True})
    assert s["ffmpeg-skill"] == "supported" and s["ffmpeg"] == "unknown" and s["filter:zscale"] == "unknown"


# ---- the real checkout the suite runs against, whatever its version

def test_real_checkout_is_usable_and_detection_matches_its_contract(skill_dir):
    info = FfmpegSkill.locate(str(skill_dir)).info()
    assert info.problems == [], info.problems
    props = info.tools["color"]["input_schema"]["properties"]
    assert info.quality_flag == ("--quality" if "quality" in props else "--crf")
    assert info.features["overwrite_flag"] is ("overwrite" in props)
    assert info.features["common_layout"] in ("_common.py", "_common/")
    print(f"ffmpeg-skill {info.version}: {info.features}", file=sys.stderr)
