"""Contract tests: the printed contract matches the implementation, doctor is honest, stdout is one document."""
import json

from color_grading import VERSION
from color_grading.contract import skill_contract
from color_grading.doctor import capability_status, doctor_report
from color_grading.errors import ERROR_CODES, EXIT_CODES
from color_grading.executor import TOOL_FOR
from color_grading.model import (HLG_TRANSFER, OPERATION_ALIASES, OPERATION_TYPES, OUTPUT_FORMATS, TONEMAP_DEFAULT, TONEMAP_HLG_DEFAULT,
                                 UNSUPPORTED_OPERATIONS)
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
    # ADR-31: the line says what the executor does (ADR-28): an explicit curve wins, else mobius for HLG, hable otherwise
    sel = c["hdr_sdr"]["tone_mapping_selection"]
    assert sel.startswith("an explicit typed parameter") and "always wins" in sel and "never picks" not in sel
    assert f"{TONEMAP_HLG_DEFAULT} for HLG ({HLG_TRANSFER})" in sel and f"{TONEMAP_DEFAULT} for PQ and any other or unknown transfer" in sel
    text = json.dumps(c, sort_keys=True)
    assert text == json.dumps(skill_contract(), sort_keys=True)     # deterministic
    # generic core: no production-house / camera-brand vocabulary
    for word in ("cinematic", "teal-and-orange", "arri", "red camera", "blackmagic"):
        assert word not in text.lower()


def test_contract_os_registry_contract_version_and_dependencies():
    c = skill_contract()
    assert c["contract_version"] == 1
    (dep,) = c["dependencies"]
    assert dep["skill_id"] == "ffmpeg-skill" and dep["version_range"] == ">=1.0 <3.0" and dep["range_of"] == "contract_version"
    assert dep["required"] is True and dep["tools"] == ["ffmpeg-skill/probe", "ffmpeg-skill/color"]
    assert dep["version_range"].startswith(">=" + c["ffmpeg_skill"]["contract_version"])
    # ffmpeg-skill 1.x/2.x declare contract_version 1.0, 3.x declares 2.0 (ADR-21): both inside the range
    assert c["ffmpeg_skill"]["contract_versions_known"] == ["1.0", "2.0"]


def test_contract_white_balance_is_an_explicit_alias_not_unsupported():
    c = skill_contract()
    assert c["unsupported_operations"] == []
    (alias,) = c["operation_aliases"]
    assert alias["type"] == "WHITE_BALANCE" and alias["alias_of"] == "PRIMARY_CORRECTION"
    assert alias["parameters"] == list(OPERATION_ALIASES["WHITE_BALANCE"]["parameters"])
    assert "WHITE_BALANCE" not in {o["type"] for o in c["operations"]} and "WHITE_BALANCE" not in c["tools"][0]["operations"]


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
    # RETAG needs only ffmpeg/ffprobe (no optional filter), so it is always supported once those are present
    assert ops["RETAG"]["status"] == "supported"
    # HDR_TO_SDR needs the optional zscale/tonemap filters: FFmpeg >= 8.0 can defeat ffmpeg-skill's filter parser
    # (then unknown, never unsupported), and some builds (e.g. macOS Homebrew's plain "ffmpeg" formula) do not
    # compile libzimg/zscale in at all (then genuinely unsupported, correctly detected) -- all three are legitimate
    assert ops["HDR_TO_SDR"]["status"] in ("supported", "unsupported", "unknown")
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


# ---- what a pinned consumer compares (ADR-26)
def test_blocks_a_consumer_pins_are_unchanged_since_0_4_0():
    """video-production-agent pins this contract and makes the skill unavailable when one of these blocks changes
    (its ADR-045: protocol / execution shape). The snapshot is those blocks of the 0.4.0 contract it pins; new facts go
    into new keys (provenance_extensions, request_types, ocio, alpha, ...), never into these.

    One deliberate exception (ADR-31): hdr_sdr.tone_mapping_selection said "this skill never picks one automatically",
    which 0.5.1's default curve (ADR-28) contradicted. It is reworded, so a consumer re-pins once; every other key of
    every pinned block is still compared with the 0.4.0 snapshot."""
    import json
    from pathlib import Path
    pinned = json.loads((Path(__file__).parent / "fixtures" / "contract_0.4.0_pinned_blocks.json").read_text(encoding="utf-8"))
    live = skill_contract()
    reworded = {("hdr_sdr", "tone_mapping_selection")}
    for key, value in pinned.items():
        if key == "hdr_sdr":
            assert set(live[key]) == set(value), key
            for sub in value:
                if (key, sub) not in reworded:
                    assert live[key][sub] == value[sub], (key, sub)
            continue
        assert live[key] == value, key
    assert pinned["hdr_sdr"]["tone_mapping_selection"].endswith("this skill never picks one automatically")
    assert live["hdr_sdr"]["tone_mapping_selection"] != pinned["hdr_sdr"]["tone_mapping_selection"]


def test_every_parameter_has_one_of_the_four_scalar_types():
    """The types every consumer of the contract validates (video-production-agent's check_contract)."""
    for op in skill_contract()["operations"]:
        for name, ps in op["parameters"].items():
            assert ps["type"] in ("number", "integer", "string", "boolean"), (op["type"], name, ps["type"])
    assert set(skill_contract()["request_types"]["operation_types"]) == set(OPERATION_TYPES)
