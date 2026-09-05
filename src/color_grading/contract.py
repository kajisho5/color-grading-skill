"""Machine-readable Skill / Capability / Tool contract (`color-grading skill --json`, alias `contract --json`).
Derived from the same tables the code runs on (model.OPERATION_TYPES, executor.TOOL_FOR, errors.ERROR_TABLE);
nothing is hand-maintained beside the implementation. Only operations this package actually executes appear here:
there are no placeholder operations (STEP 1 of the design brief: contract/doctor expose implemented operations only)."""
from __future__ import annotations

from typing import Any, Dict, List

from . import CONTRACT_SCHEMA_VERSION, DOCTOR_SCHEMA_VERSION, PACKAGE_NAME, REQUEST_SCHEMA_VERSION, RESPONSE_SCHEMA_VERSION, SKILL_ID, VERSION
from .adapter import FLAGS_USED, SUPPORTED_CONTRACT_VERSION, SUPPORTED_MAX_EXCLUSIVE, SUPPORTED_MIN, TOOLS_USED
from .errors import ERROR_CODES, ERROR_TABLE, EXIT_CODES
from .executor import DURATION_TOLERANCE, TOOL_FOR, WORK_DIR_NAME
from .model import (CUBE_EXTENSIONS, FORBIDDEN_KEYS, ID_RE, MAX_LUT_BYTES, MAX_OPERATIONS, OPERATION_TYPES, OUTPUT_FORMATS, REF_RE,
                    REQUEST_SCHEMA_ID, RETAG_TAGS, RETAG_TARGETS, TONEMAPS, UNSUPPORTED_OPERATIONS, X264_PRESETS)

CONTRACT_SCHEMA_ID = f"{SKILL_ID}/contract@{CONTRACT_SCHEMA_VERSION}"


def _param_schema(ps: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in ps.items() if k in ("type", "required", "min", "max", "enum", "default", "description")}


def operation_specs() -> List[Dict[str, Any]]:
    out = []
    for typ, spec in OPERATION_TYPES.items():
        tool, extra = TOOL_FOR[typ]
        out.append({"type": typ, "description": spec["description"], "inputs": 1,
                    "parameters": {k: _param_schema(v) for k, v in spec["parameters"].items()},
                    "tool": f"ffmpeg-skill/{tool}", "required_capabilities": ["ffmpeg-skill", "ffmpeg", "ffprobe", *extra],
                    "changes_duration": False, "changes_resolution": False, "deterministic": "content_equivalent"})
    return out


def skill_contract() -> Dict[str, Any]:
    tools = [{"tool_id": f"{SKILL_ID}/run", "skill_id": SKILL_ID, "version": VERSION, "role": "execution",
              "description": "Execute a typed ColorProject (operation graph) and write validated video artifacts with provenance",
              "inputs": ["request document (stdin)"], "input_type": REQUEST_SCHEMA_ID, "produces_output": True, "writes_media": True, "deterministic": True,
              "idempotency_hint": "content_equivalent; intermediates reused by deterministic operation id",
              "operations": sorted(OPERATION_TYPES), "supports": {"dry_run": True, "timeout": True, "cancel": True, "validate": True},
              "verification": "every artifact is probed (video stream, duration, resolution, colour tags measured and checked against the requested operation, sha256); "
                              "HDR_TO_SDR checks the output is no longer tagged HDR, RETAG checks the exact tag triple, STRIP_DOVI checks the Dolby Vision side data is gone",
              "provenance": "OBSERVED", "mutates_input": False, "delegates_to": [f"ffmpeg-skill/{t}" for t in TOOLS_USED]}]
    return {
        "schema": CONTRACT_SCHEMA_ID, "skill_id": SKILL_ID, "id": SKILL_ID, "name": PACKAGE_NAME, "package": PACKAGE_NAME, "version": VERSION,
        "kind": "execution", "role": "colour grading / colour correction execution (processing); not decision, not measurement, not automatic look/LUT selection",
        "description": "Deterministic colour grading execution: HDR-to-SDR tone mapping, 3D .cube LUT application, colour-tag retagging and Dolby Vision RPU "
                       "removal, executed as a typed operation graph through ffmpeg-skill's public contract, with output validation and provenance. Not an AI agent: "
                       "it never decides which colour treatment, LUT or look to apply.",
        "repository": "https://github.com/kajisho5/color-grading-skill",
        "not_provided": ["AI reasoning", "decisions", "production plans", "automatic colour grading", "automatic look selection", "automatic LUT selection",
                         "image understanding", "scene/face detection", "primary colour correction (exposure/contrast/saturation/white balance/gamma/lift/gain/levels/curves)",
                         "container/format conversion (ffmpeg-skill/export)", "arbitrary ffmpeg filters", "shell execution", "network access"],
        "tools": tools,
        "operations": operation_specs(),
        "unsupported_operations": [{"type": t, "status": "not_implemented", "reason": r} for t, r in UNSUPPORTED_OPERATIONS.items()],
        "output_formats": {f: {"extension": s["extension"], "note": "must match the source container; this skill does not convert containers"} for f, s in OUTPUT_FORMATS.items()},
        "lut": {"format": "cube", "extensions": list(CUBE_EXTENSIONS), "max_bytes": MAX_LUT_BYTES, "hashed": "sha256, recorded in provenance",
                "strength_range": [0.0, 1.0], "note": "a LUT is data read by ffmpeg's lut3d filter, never a filter string or executable; path policy applies (allowed_lut_roots)"},
        "color_space": {"measured_fields": ["color_space", "color_primaries", "color_transfer", "color_range", "pix_fmt", "hdr", "dolby_vision"],
                        "source": "ffmpeg-skill/probe (ffprobe); OBSERVED, never inferred", "retag_targets": list(RETAG_TARGETS), "retag_tags": {k: list(v) for k, v in RETAG_TAGS.items()},
                        "note": "measurement of input/output colour information is kept separate from the RETAG / HDR_TO_SDR operations that change it (STEP 4 of the design brief)"},
        "hdr_sdr": {"tonemap_curves": list(TONEMAPS), "peak_nits_range": [1.0, 10000.0], "desat_range": [0.0, 5.0],
                    "tone_mapping_selection": "always an explicit typed parameter (tonemap curve, peak, desat) from the caller; this skill never picks one automatically",
                    "hdr_detection": "measured via ffmpeg-skill/probe's hdr flag (color_transfer/color_primaries); HDR_TO_SDR refuses a non-HDR source unless force is set"},
        "x264": {"crf_range": [0, 51], "presets": list(X264_PRESETS)},
        "geometry": {"changes": "no implemented operation changes duration or frame geometry", "duration_tolerance": DURATION_TOLERANCE,
                    "work_dir": f"<workspace>/{WORK_DIR_NAME}/<project_id>/"},
        "execution": {"mode": "local", "canonical_invocation": ["color-grading", "run", "-", "--json"], "stdin": REQUEST_SCHEMA_ID,
                      "stdout": f"exactly one {SKILL_ID}/response@{RESPONSE_SCHEMA_VERSION} document", "stderr": "diagnostics only",
                      "executables": ["python3 <ffmpeg-skill>/scripts/{probe,color}.py (argv lists)"],
                      "executable_resolution": "ffmpeg-skill directory: --ffmpeg-skill, COLOR_GRADING_FFMPEG_SKILL_DIR, VIDEO_AGENT_FFMPEG_SKILL_DIR, "
                                               "~/.claude/skills/ffmpeg-skill, ./vendor/ffmpeg-skill, ../ffmpeg-skill; ffmpeg/ffprobe: PATH lookup by ffmpeg-skill",
                      "shell": False, "arbitrary_executables": False, "arbitrary_filters": False, "network": False, "input_mutation": False, "ai": False},
        "ffmpeg_skill": {"contract_version": SUPPORTED_CONTRACT_VERSION, "version_window": {"min": ".".join(map(str, SUPPORTED_MIN)), "max_exclusive": ".".join(map(str, SUPPORTED_MAX_EXCLUSIVE))},
                         "tools_used": list(TOOLS_USED), "flags_used": {k: list(v) for k, v in FLAGS_USED.items()}},
        "request": {"schema": REQUEST_SCHEMA_ID, "id_pattern": ID_RE.pattern, "reference_pattern": REF_RE.pattern, "forbidden_fields": sorted(FORBIDDEN_KEYS),
                    "max_operations": MAX_OPERATIONS,
                    "shape": {"schema": REQUEST_SCHEMA_ID, "project": {"project_id": "id", "source": {"source_id": "id", "path": "file"},
                              "operations": [{"op_id": "id", "type": "HDR_TO_SDR|LUT_APPLY|RETAG|STRIP_DOVI", "input": "source|op:<id>", "parameters": {}}],
                              "outputs": [{"output_id": "id", "operation": "source|op:<id>", "path": "file", "format": "mp4|mov|m4v|mkv", "overwrite?": False,
                                           "expect?": {"width?": 1920, "height?": 1080, "duration?": 0, "duration_tolerance?": 0.2, "pix_fmt?": "yuv420p"}}]},
                              "options?": {"reuse_intermediates?": True, "timeout?": 600}}},
        "response": {"schema": f"{SKILL_ID}/response@{RESPONSE_SCHEMA_VERSION}", "success": {"ok": True, "status": "ok", "dry_run": "bool", "plan": "...", "results": "[OperationResult]", "outputs": "[output artifact + provenance]"},
                     "failure": {"ok": False, "status": "error|cancelled", "error": {"code": "one of errors.codes", "message": "str", "retryable": "bool", "details": {}}}},
        "provenance": {"per_operation": ["operation_id", "type", "tool", "tool_versions", "parameters", "input_hash", "output_hash", "status", "measurements", "tool_commands_observed", "lut (LUT_APPLY only)"],
                       "per_output": ["skill", "skill_version", "tool_versions", "output_hash", "operations (chain)", "source (sha256)"],
                       "identity": "sha256 over canonical JSON of {type, parameters, input identity, tool versions}; a LUT_APPLY identity uses the LUT's sha256, not its path; "
                                   "the source's identity is its file sha256; no timestamps, no UUIDs"},
        "schema_versions": {"contract": str(CONTRACT_SCHEMA_VERSION), "request": str(REQUEST_SCHEMA_VERSION), "response": str(RESPONSE_SCHEMA_VERSION), "doctor": str(DOCTOR_SCHEMA_VERSION)},
        "errors": {"codes": list(ERROR_CODES), "exit_codes": dict(EXIT_CODES), "retryable": {c: ERROR_TABLE[c][1] for c in ERROR_CODES}, "success_exit_code": 0},
        "deterministic": True,
    }
