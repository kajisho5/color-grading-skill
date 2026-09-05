"""Contract tests: the printed contract matches the implementation, doctor is honest, stdout is one document."""
import json

from color_grading import VERSION
from color_grading.contract import skill_contract
from color_grading.doctor import capability_status, doctor_report
from color_grading.errors import ERROR_CODES, EXIT_CODES
from color_grading.executor import TOOL_FOR
from color_grading.model import OPERATION_TYPES, OUTPUT_FORMATS, UNSUPPORTED_OPERATIONS
from conftest import one_json, run_cli


def test_contract_matches_implementation():
    c = skill_contract()
    assert c["schema"] == "color-grading/contract@1" and c["skill_id"] == "color-grading" and c["version"] == VERSION
    assert {o["type"] for o in c["operations"]} == set(OPERATION_TYPES)
    assert {o["type"] for o in c["unsupported_operations"]} == set(UNSUPPORTED_OPERATIONS)
    assert not ({o["type"] for o in c["operations"]} & set(UNSUPPORTED_OPERATIONS))
    for o in c["operations"]:
        assert o["tool"] == f"ffmpeg-skill/{TOOL_FOR[o['type']][0]}"
        for name, ps in o["parameters"].items():
            assert "type" in ps and "required" in ps and "description" in ps, (o["type"], name)
    assert set(c["output_formats"]) == set(OUTPUT_FORMATS)
    assert c["errors"]["codes"] == list(ERROR_CODES) and c["errors"]["exit_codes"] == EXIT_CODES
    assert c["execution"]["shell"] is False and c["execution"]["arbitrary_filters"] is False and c["execution"]["ai"] is False
    assert c["hdr_sdr"]["tone_mapping_selection"].startswith("always an explicit")
    text = json.dumps(c, sort_keys=True)
    assert text == json.dumps(skill_contract(), sort_keys=True)     # deterministic
    # generic core: no production-house / camera-brand vocabulary
    for word in ("cinematic", "teal-and-orange", "arri", "red camera", "blackmagic"):
        assert word not in text.lower()


def test_contract_and_skill_cli_are_identical():
    c1, o1, _ = run_cli(["skill", "--json"])
    c2, o2, _ = run_cli(["contract", "--json"])
    assert c1 == c2 == 0 and one_json(o1) == one_json(o2)


def test_doctor_reports_supported_unsupported_unknown(skill_dir):
    d = doctor_report(str(skill_dir))
    assert d["schema"] == "color-grading/doctor@1" and d["status"] in ("ok", "degraded") and d["secrets_shown"] is False
    assert d["checks"]["ffmpeg_skill"]["status"] == "ok" and d["checks"]["ffmpeg"]["version"]
    ops = d["checks"]["operations"]
    assert set(ops) == set(OPERATION_TYPES)
    assert all(o["status"] in ("supported", "unsupported", "unknown") for o in ops.values())
    # FFmpeg >= 8.0 defeats ffmpeg-skill's filter parser: filters are then unknown, never unsupported
    assert ops["RETAG"]["status"] == "supported"
    assert ops["HDR_TO_SDR"]["status"] in ("supported", "unknown")
    assert d["checks"]["filter_detection"]["status"] in ("ok", "unknown")
    assert d["checks"]["unsupported_operations"] == UNSUPPORTED_OPERATIONS
    caps = d["checks"]["capabilities"]
    assert caps["ffmpeg"] == "supported"
    assert all(v in ("supported", "unsupported", "unknown") for v in caps.values())
    code, out, _ = run_cli(["doctor", "--json", "--ffmpeg-skill", str(skill_dir)])
    assert code == 0 and one_json(out)["status"] == d["status"]


def test_doctor_without_ffmpeg_skill_is_a_failure_not_a_guess(tmp_path, monkeypatch):
    monkeypatch.delenv("COLOR_GRADING_FFMPEG_SKILL_DIR", raising=False)
    monkeypatch.delenv("VIDEO_AGENT_FFMPEG_SKILL_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    d = doctor_report(str(tmp_path / "nowhere"))
    assert d["status"] == "fail" and d["checks"]["ffmpeg_skill"]["status"] == "missing"
    assert all(o["status"] == "unsupported" for o in d["checks"]["operations"].values())
    assert d["checks"]["ffmpeg"]["status"] == "unknown"


def test_capability_status_table():
    s = capability_status(None, {})
    assert s["ffmpeg"] == "unsupported" and s["filter:zscale"] == "unsupported"

    class Info:
        supported = True
    s = capability_status(Info(), {"ffmpeg": "6.1", "ffprobe": "6.1", "available": ["filter:zscale", "filter:tonemap", "filter:lut3d", "encoder:libx264"], "missing_optional": ["bsf:filter_units"]})
    assert s["filter:zscale"] == "supported" and s["encoder:libx264"] == "supported" and s["bsf:filter_units"] == "unsupported"
    s = capability_status(Info(), {"ffmpeg": "6.1", "ffprobe": "6.1", "available": ["encoder:libx264"], "missing": ["filter:zscale"], "missing_optional": ["filter:tonemap", "bsf:filter_units"]})
    assert s["filter:zscale"] == "unknown"     # zero filters detected: parser failure, not absence
    assert s["filter:tonemap"] == "unknown" and s["bsf:filter_units"] == "unsupported"  # not a filter: capability, the detection defect does not apply
    s = capability_status(Info(), {"ffmpeg": "6.1", "ffprobe": "6.1", "available": ["filter:lut3d"], "missing": ["filter:zscale"], "missing_optional": []})
    assert s["filter:zscale"] == "unsupported"                                                     # some filters detected: a missing one is really missing
