"""Unit tests: request schema, graph, identities, canonical JSON, error table, path policy, filename rules.
No ffmpeg / ffmpeg-skill needed."""
import pytest

from color_grading.canonical import canonical_json, sha256_text, stable_hash
from color_grading.errors import ERROR_CODES, EXIT_CODES, ColorError
from color_grading.graph import OperationGraph
from color_grading.model import (MAX_OPERATIONS, OPERATION_TYPES, OUTPUT_FORMATS, RETAG_TAGS, UNSUPPORTED_OPERATIONS,
                                 ColorOperation, ColorOutput, ColorProject, ColorSource, parse_ref, parse_request, validate_parameters)
from color_grading.security import PathPolicy, check_filename


def base_doc(ops=None, outputs=None):
    ops = ops if ops is not None else []
    last = ops[-1]["op_id"] if ops else None
    outputs = outputs or [{"output_id": "o1", "operation": f"op:{last}" if last else "source", "path": "out.mp4", "format": "mp4"}]
    return {"schema": "color-grading/request@1", "project": {"project_id": "p1", "source": {"source_id": "s1", "path": "in.mp4"}, "operations": ops, "outputs": outputs}}


# ---- request schema
def test_valid_request_parses():
    doc = base_doc([{"op_id": "a", "type": "RETAG", "input": "source", "parameters": {"target": "bt709"}}])
    req = parse_request(doc)
    assert req.project.project_id == "p1"
    assert req.project.operations[0].type == "RETAG"
    assert req.project.operations[0].parameters == {"target": "bt709", "audio_stream": 0}


def test_unknown_schema_rejected():
    doc = base_doc()
    doc["schema"] = "color-grading/request@2"
    with pytest.raises(ColorError) as e:
        parse_request(doc)
    assert e.value.code == "INVALID_REQUEST"


def test_unknown_top_level_field_rejected():
    doc = base_doc()
    doc["bogus"] = 1
    with pytest.raises(ColorError):
        parse_request(doc)


@pytest.mark.parametrize("field", ["command", "argv", "cmd", "shell", "exec", "executable", "script", "filter", "filters", "filter_complex", "vf", "af", "env", "cwd", "api_key", "workspace"])
def test_forbidden_fields_rejected_anywhere(field):
    doc = base_doc([{"op_id": "a", "type": "RETAG", "input": "source", "parameters": {"target": "bt709", field: "x"}}])
    with pytest.raises(ColorError) as e:
        parse_request(doc)
    assert e.value.code == "INVALID_REQUEST"
    assert e.value.details.get("reason") == "forbidden_field"


def test_forbidden_field_nested_in_output_rejected():
    doc = base_doc(outputs=[{"output_id": "o1", "operation": "source", "path": "out.mp4", "format": "mp4", "expect": {"argv": ["x"]}}])
    with pytest.raises(ColorError):
        parse_request(doc)


def test_unknown_operation_type_rejected():
    doc = base_doc([{"op_id": "a", "type": "BOGUS", "input": "source", "parameters": {}}])
    with pytest.raises(ColorError) as e:
        parse_request(doc)
    assert e.value.code == "UNSUPPORTED_OPERATION"


@pytest.mark.parametrize("typ", list(UNSUPPORTED_OPERATIONS))
def test_declared_unsupported_operations_rejected_with_reason(typ):
    doc = base_doc([{"op_id": "a", "type": typ, "input": "source", "parameters": {}}])
    with pytest.raises(ColorError) as e:
        parse_request(doc)
    assert e.value.code == "UNSUPPORTED_OPERATION"
    assert UNSUPPORTED_OPERATIONS[typ] in e.value.message
    assert typ not in OPERATION_TYPES


def test_duplicate_op_id_rejected():
    ops = [{"op_id": "a", "type": "RETAG", "input": "source", "parameters": {"target": "bt709"}},
           {"op_id": "a", "type": "STRIP_DOVI", "input": "source", "parameters": {}}]
    with pytest.raises(ColorError) as e:
        parse_request(base_doc(ops))
    assert e.value.code == "DEPENDENCY_ERROR"


def test_self_reference_rejected():
    ops = [{"op_id": "a", "type": "RETAG", "input": "op:a", "parameters": {"target": "bt709"}}]
    with pytest.raises(ColorError) as e:
        parse_request(base_doc(ops, outputs=[{"output_id": "o", "operation": "op:a", "path": "x.mp4", "format": "mp4"}]))
    assert e.value.code == "DEPENDENCY_ERROR"


def test_missing_operation_reference_rejected():
    ops = [{"op_id": "a", "type": "RETAG", "input": "op:nope", "parameters": {"target": "bt709"}}]
    with pytest.raises(ColorError) as e:
        parse_request(base_doc(ops))
    assert e.value.code == "MISSING_INPUT"


def test_bad_ref_syntax_rejected():
    ops = [{"op_id": "a", "type": "RETAG", "input": "track:x", "parameters": {"target": "bt709"}}]
    with pytest.raises(ColorError):
        parse_request(base_doc(ops))


def test_output_missing_output_operation_reference_rejected():
    doc = base_doc(outputs=[{"output_id": "o1", "operation": "op:missing", "path": "out.mp4", "format": "mp4"}])
    with pytest.raises(ColorError) as e:
        parse_request(doc)
    assert e.value.code == "MISSING_INPUT"


def test_duplicate_output_id_rejected():
    doc = base_doc(outputs=[{"output_id": "o", "operation": "source", "path": "a.mp4", "format": "mp4"},
                            {"output_id": "o", "operation": "source", "path": "b.mp4", "format": "mp4"}])
    with pytest.raises(ColorError) as e:
        parse_request(doc)
    assert e.value.code == "DEPENDENCY_ERROR"


def test_duplicate_output_path_rejected():
    doc = base_doc(outputs=[{"output_id": "o1", "operation": "source", "path": "a.mp4", "format": "mp4"},
                            {"output_id": "o2", "operation": "source", "path": "a.mp4", "format": "mp4"}])
    with pytest.raises(ColorError) as e:
        parse_request(doc)
    assert e.value.code == "OUTPUT_ERROR"


@pytest.mark.parametrize("fmt,path", [("mov", "out.mp4"), ("mp4", "out.mov"), ("bogus", "out.bogus")])
def test_output_format_extension_mismatch_rejected(fmt, path):
    doc = base_doc(outputs=[{"output_id": "o1", "operation": "source", "path": path, "format": fmt}])
    with pytest.raises(ColorError) as e:
        parse_request(doc)
    assert e.value.code == "UNSUPPORTED_FORMAT"


def test_too_many_operations_rejected():
    ops = [{"op_id": f"a{i}", "type": "RETAG", "input": "source" if i == 0 else f"op:a{i - 1}", "parameters": {"target": "bt709"}} for i in range(MAX_OPERATIONS + 1)]
    with pytest.raises(ColorError) as e:
        parse_request(base_doc(ops))
    assert e.value.code == "INVALID_REQUEST"


# ---- parameter validation per operation type
def test_hdr_to_sdr_defaults():
    p = validate_parameters("HDR_TO_SDR", {}, "x")
    assert p == {"tonemap": "hable", "peak_nits": 1000.0, "desat": 0.0, "force": False, "crf": 18, "preset": "medium", "audio_stream": 0}


def test_hdr_to_sdr_unknown_tonemap_rejected():
    with pytest.raises(ColorError):
        validate_parameters("HDR_TO_SDR", {"tonemap": "bogus"}, "x")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_rejected(value):
    with pytest.raises(ColorError) as e:
        validate_parameters("HDR_TO_SDR", {"peak_nits": value}, "x")
    assert e.value.code == "INVALID_REQUEST"


def test_out_of_range_rejected():
    with pytest.raises(ColorError):
        validate_parameters("HDR_TO_SDR", {"desat": -1.0}, "x")
    with pytest.raises(ColorError):
        validate_parameters("LUT_APPLY", {"lut_path": "x.cube", "lut_strength": 1.5}, "x")
    with pytest.raises(ColorError):
        validate_parameters("HDR_TO_SDR", {"crf": 52}, "x")


def test_lut_apply_requires_lut_path():
    with pytest.raises(ColorError) as e:
        validate_parameters("LUT_APPLY", {}, "x")
    assert e.value.code == "INVALID_REQUEST"


def test_retag_requires_target_enum():
    p = validate_parameters("RETAG", {"target": "bt2020-hlg"}, "x")
    assert p == {"target": "bt2020-hlg", "audio_stream": 0}
    with pytest.raises(ColorError):
        validate_parameters("RETAG", {"target": "bogus"}, "x")
    with pytest.raises(ColorError):
        validate_parameters("RETAG", {}, "x")


def test_strip_dovi_takes_no_parameters():
    assert validate_parameters("STRIP_DOVI", {}, "x") == {}
    with pytest.raises(ColorError):
        validate_parameters("STRIP_DOVI", {"crf": 18}, "x")
    # STRIP_DOVI is always a full stream copy (-map 0): audio_stream has nothing to select, so it is not offered
    with pytest.raises(ColorError):
        validate_parameters("STRIP_DOVI", {"audio_stream": 0}, "x")


# ---- audio_stream (ffmpeg-skill >= 0.12.0 --audio-stream): HDR_TO_SDR/LUT_APPLY/RETAG/PRIMARY_CORRECTION only
@pytest.mark.parametrize("typ,extra", [("HDR_TO_SDR", {}), ("LUT_APPLY", {"lut_path": "x.cube"}), ("RETAG", {"target": "bt709"}), ("PRIMARY_CORRECTION", {})])
def test_audio_stream_defaults_to_zero_on_every_applicable_operation(typ, extra):
    assert validate_parameters(typ, extra, "x")["audio_stream"] == 0


def test_audio_stream_accepts_a_positive_index():
    assert validate_parameters("RETAG", {"target": "bt709", "audio_stream": 3}, "x")["audio_stream"] == 3


def test_audio_stream_negative_rejected():
    with pytest.raises(ColorError) as e:
        validate_parameters("RETAG", {"target": "bt709", "audio_stream": -1}, "x")
    assert e.value.code == "INVALID_REQUEST"


def test_audio_stream_non_integer_rejected():
    with pytest.raises(ColorError):
        validate_parameters("RETAG", {"target": "bt709", "audio_stream": 1.5}, "x")


def test_preset_enum_rejects_unknown():
    with pytest.raises(ColorError):
        validate_parameters("HDR_TO_SDR", {"preset": "turbo"}, "x")


def test_primary_correction_defaults_are_identity():
    p = validate_parameters("PRIMARY_CORRECTION", {}, "x")
    assert p == {"exposure": 0.0, "contrast": 1.0, "saturation": 1.0, "temperature": 6500.0, "tint": 0.0, "crf": 18, "preset": "medium", "audio_stream": 0}


@pytest.mark.parametrize("name,bad", [("exposure", -3.01), ("exposure", 3.01), ("contrast", -0.01), ("contrast", 2.01),
                                       ("saturation", -0.01), ("saturation", 2.01), ("temperature", 1999.0), ("temperature", 12001.0),
                                       ("tint", -1.01), ("tint", 1.01)])
def test_primary_correction_out_of_safe_range_rejected(name, bad):
    with pytest.raises(ColorError) as e:
        validate_parameters("PRIMARY_CORRECTION", {name: bad}, "x")
    assert e.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("name,edge", [("exposure", -3.0), ("exposure", 3.0), ("contrast", 0.0), ("contrast", 2.0),
                                        ("saturation", 0.0), ("saturation", 2.0), ("temperature", 2000.0), ("temperature", 12000.0),
                                        ("tint", -1.0), ("tint", 1.0)])
def test_primary_correction_range_boundaries_accepted(name, edge):
    p = validate_parameters("PRIMARY_CORRECTION", {name: edge}, "x")
    assert p[name] == edge


def test_primary_correction_is_not_in_unsupported_operations():
    assert "PRIMARY_CORRECTION" not in UNSUPPORTED_OPERATIONS
    assert "PRIMARY_CORRECTION" in OPERATION_TYPES


def test_retag_tags_have_four_entries_matching_targets():
    from color_grading.model import RETAG_TARGETS
    assert set(RETAG_TAGS) == set(RETAG_TARGETS)
    for v in RETAG_TAGS.values():
        assert len(v) == 3


def test_parse_ref():
    assert parse_ref("source", "x") == ("source", "")
    assert parse_ref("op:abc", "x") == ("op", "abc")
    with pytest.raises(ColorError):
        parse_ref("track:abc", "x")
    with pytest.raises(ColorError):
        parse_ref("bogus", "x")


# ---- graph
def _project(ops, outputs=None):
    outputs = outputs or [ColorOutput("o1", f"op:{ops[-1].op_id}" if ops else "source", "out.mp4", "mp4")]
    return ColorProject("p1", ColorSource("s1", "in.mp4"), ops, outputs)


def test_graph_simple_chain_order():
    ops = [ColorOperation("a", "RETAG", "source", {"target": "bt709"}), ColorOperation("b", "STRIP_DOVI", "op:a", {})]
    g = OperationGraph(_project(ops))
    assert g.order == ["source", "op:a", "op:b"]
    assert g.output_nodes == {"o1": "op:b"}


def test_graph_cycle_detected():
    from color_grading.errors import ColorError as CE
    ops = [ColorOperation("a", "RETAG", "op:b", {"target": "bt709"}), ColorOperation("b", "STRIP_DOVI", "op:a", {})]
    with pytest.raises(CE) as e:
        OperationGraph(_project(ops, outputs=[ColorOutput("o1", "op:a", "out.mp4", "mp4")]))
    assert e.value.code == "DEPENDENCY_ERROR"


def test_graph_unreachable_operation_detected():
    ops = [ColorOperation("a", "RETAG", "source", {"target": "bt709"}), ColorOperation("b", "STRIP_DOVI", "source", {})]
    with pytest.raises(ColorError) as e:
        OperationGraph(_project(ops, outputs=[ColorOutput("o1", "op:a", "out.mp4", "mp4")]))
    assert e.value.code == "DEPENDENCY_ERROR"
    assert "op:b" in str(e.value.details.get("unreachable"))


def test_graph_identities_deterministic_and_depend_on_parameters():
    ops = [ColorOperation("a", "RETAG", "source", {"target": "bt709"})]
    g1 = OperationGraph(_project(ops))
    ids1 = g1.identities("deadbeef", {"ffmpeg-skill": "0.9.1"})
    g2 = OperationGraph(_project(ops))
    ids2 = g2.identities("deadbeef", {"ffmpeg-skill": "0.9.1"})
    assert ids1 == ids2
    ops3 = [ColorOperation("a", "RETAG", "source", {"target": "bt601"})]
    ids3 = OperationGraph(_project(ops3)).identities("deadbeef", {"ffmpeg-skill": "0.9.1"})
    assert ids3["op:a"] != ids1["op:a"]
    # op_id is a label, not part of the identity
    ops4 = [ColorOperation("different-label", "RETAG", "source", {"target": "bt709"})]
    ids4 = OperationGraph(_project(ops4, outputs=[ColorOutput("o1", "op:different-label", "out.mp4", "mp4")])).identities("deadbeef", {"ffmpeg-skill": "0.9.1"})
    assert ids4["op:different-label"] == ids1["op:a"]


def test_graph_identities_depend_on_primary_correction_parameters():
    params = {"exposure": 0.5, "contrast": 1.0, "saturation": 1.0, "temperature": 6500.0, "tint": 0.0, "crf": 18, "preset": "medium"}
    ops = [ColorOperation("a", "PRIMARY_CORRECTION", "source", dict(params))]
    ids1 = OperationGraph(_project(ops)).identities("deadbeef", {"ffmpeg-skill": "0.9.2"})
    ops2 = [ColorOperation("a", "PRIMARY_CORRECTION", "source", {**params, "exposure": 0.6})]
    ids2 = OperationGraph(_project(ops2)).identities("deadbeef", {"ffmpeg-skill": "0.9.2"})
    assert ids1["op:a"] != ids2["op:a"]
    ops3 = [ColorOperation("a", "PRIMARY_CORRECTION", "source", dict(params))]
    ids3 = OperationGraph(_project(ops3)).identities("deadbeef", {"ffmpeg-skill": "0.9.2"})
    assert ids1["op:a"] == ids3["op:a"]


def test_graph_identity_source_depends_on_fingerprint_only():
    g = OperationGraph(_project([]))
    ids_a = g.identities("aaaa", {})
    ids_b = g.identities("bbbb", {})
    assert ids_a["source"] != ids_b["source"]


def test_graph_lut_identity_uses_content_override_not_path():
    ops = [ColorOperation("a", "LUT_APPLY", "source", {"lut_path": "/tmp/one.cube", "lut_strength": 1.0, "crf": 18, "preset": "medium"})]
    g = OperationGraph(_project(ops))
    ids_path1 = g.identities("fp", {}, {"op:a": {"lut_path": "sha-of-content"}})
    ops2 = [ColorOperation("a", "LUT_APPLY", "source", {"lut_path": "/somewhere/else/two.cube", "lut_strength": 1.0, "crf": 18, "preset": "medium"})]
    g2 = OperationGraph(_project(ops2))
    ids_path2 = g2.identities("fp", {}, {"op:a": {"lut_path": "sha-of-content"}})
    assert ids_path1["op:a"] == ids_path2["op:a"]     # same content hash, different path -> same identity
    ids_path3 = g2.identities("fp", {}, {"op:a": {"lut_path": "sha-of-different-content"}})
    assert ids_path3["op:a"] != ids_path2["op:a"]      # different content hash -> different identity


# ---- executor._argv: --audio-stream threaded to every applicable ffmpeg-skill/color invocation, no ffmpeg needed
def _argv_for(tmp_path, typ, params, lut=None):
    from color_grading.executor import Executor, LutInfo, NodeState
    from color_grading.graph import Node
    ex = Executor(PathPolicy(str(tmp_path)), skill=None)
    st = NodeState(Node("op:x", typ, ["source"], params), "identity16")
    if lut is not None:
        st.lut = LutInfo(lut, 4, "deadbeef")
    return ex._argv(st, "/in.mp4", tmp_path / "out.mp4")


@pytest.mark.parametrize("typ,params", [
    ("HDR_TO_SDR", {"tonemap": "hable", "peak_nits": 1000.0, "desat": 0.0, "force": False, "crf": 18, "preset": "medium", "audio_stream": 2}),
    ("RETAG", {"target": "bt709", "audio_stream": 2}),
    ("PRIMARY_CORRECTION", {"exposure": 0.0, "contrast": 1.0, "saturation": 1.0, "temperature": 6500.0, "tint": 0.0, "crf": 18, "preset": "medium", "audio_stream": 2}),
])
def test_argv_includes_audio_stream_flag_with_its_value(tmp_path, typ, params):
    argv = _argv_for(tmp_path, typ, params)
    assert "--audio-stream" in argv
    assert argv[argv.index("--audio-stream") + 1] == "2"


def test_argv_lut_apply_includes_audio_stream_flag(tmp_path):
    lut = tmp_path / "x.cube"
    lut.write_text("x")
    argv = _argv_for(tmp_path, "LUT_APPLY", {"lut_path": str(lut), "lut_strength": 1.0, "crf": 18, "preset": "medium", "audio_stream": 1}, lut=lut)
    assert "--audio-stream" in argv and argv[argv.index("--audio-stream") + 1] == "1"


def test_argv_strip_dovi_has_no_audio_stream_flag(tmp_path):
    """STRIP_DOVI takes no audio_stream parameter (model.py) and _argv must not invent one: its stream copy keeps
    every audio track untouched regardless."""
    argv = _argv_for(tmp_path, "STRIP_DOVI", {})
    assert "--audio-stream" not in argv


# ---- executor._validate_artifact: independent subtitle/data-stream-survival re-probe (no ffmpeg needed, ffmpeg-skill/
# probe is stubbed so the check under test -- comparing before/after counts -- is isolated from ffmpeg-skill itself)
class _StubSkill:
    def __init__(self, probe_result):
        self._probe_result = probe_result

    def probe(self, path, timeout=None):
        return self._probe_result


def _validate(tmp_path, probe_result, source_subs=1, source_data=0, dropped_non_av_streams=None, node_type="STRIP_DOVI", params=None):
    from color_grading.executor import Executor, NodeState
    from color_grading.graph import Node
    out = tmp_path / "out.mp4"
    out.write_bytes(b"x" * 100)
    ex = Executor(PathPolicy(str(tmp_path)), _StubSkill(probe_result))
    st = NodeState(Node("op:x", node_type, ["source"], params or {}), "identity16")
    st.dropped_non_av_streams = dropped_non_av_streams
    source = {"duration": 0.0, "width": 0, "height": 0, "subtitle_streams": source_subs, "data_streams": source_data}
    return ex._validate_artifact(out, st, source)


def _probe(subtitle_streams=0, data_streams=0, **video_extra):
    video = {"width": 160, "height": 90, "pix_fmt": "yuv420p", "codec": "hevc", "color_space": None, "color_primaries": None,
             "color_transfer": None, "color_range": None, "hdr": False, "dolby_vision": False}
    video.update(video_extra)
    return {"duration": 0.0, "video": video, "subtitle_streams": subtitle_streams, "data_streams": data_streams}


def test_validate_artifact_flags_unreported_subtitle_loss(tmp_path):
    with pytest.raises(ColorError) as e:
        _validate(tmp_path, _probe(subtitle_streams=0), source_subs=1, dropped_non_av_streams=None)
    assert e.value.code == "VALIDATION_ERROR" and e.value.details["reason"] == "stream_loss_unreported"


def test_validate_artifact_flags_unreported_subtitle_loss_even_when_dropped_flag_is_explicitly_false(tmp_path):
    with pytest.raises(ColorError) as e:
        _validate(tmp_path, _probe(subtitle_streams=0), source_subs=1, dropped_non_av_streams=False)
    assert e.value.code == "VALIDATION_ERROR" and e.value.details["reason"] == "stream_loss_unreported"


def test_validate_artifact_allows_a_loss_ffmpeg_skill_itself_reported(tmp_path):
    art = _validate(tmp_path, _probe(subtitle_streams=0), source_subs=1, dropped_non_av_streams=True)
    assert art.sha256


def test_validate_artifact_allows_survived_streams(tmp_path):
    art = _validate(tmp_path, _probe(subtitle_streams=1), source_subs=1, dropped_non_av_streams=False)
    assert art.sha256


def test_validate_artifact_skips_the_check_when_source_had_no_subtitle_or_data_streams(tmp_path):
    art = _validate(tmp_path, _probe(subtitle_streams=0, data_streams=0), source_subs=0, source_data=0, dropped_non_av_streams=None)
    assert art.sha256


def test_validate_artifact_flags_unreported_data_stream_loss(tmp_path):
    with pytest.raises(ColorError) as e:
        _validate(tmp_path, _probe(data_streams=0), source_subs=0, source_data=1, dropped_non_av_streams=None)
    assert e.value.code == "VALIDATION_ERROR" and e.value.details["reason"] == "stream_loss_unreported"


# ---- canonical JSON
def test_canonical_json_key_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_canonical_json_rejects_nan():
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


def test_stable_hash_matches_sha256_of_canonical_json():
    obj = {"a": 1, "b": [1, 2, 3]}
    assert stable_hash(obj) == sha256_text(canonical_json(obj))


# ---- errors
def test_error_table_shape():
    assert set(EXIT_CODES) == set(ERROR_CODES)
    assert len(set(EXIT_CODES.values())) == len(EXIT_CODES)   # unique exit codes
    for code in ERROR_CODES:
        assert EXIT_CODES[code] >= 2
    with pytest.raises(ValueError):
        ColorError("NOT_A_CODE", "x")


def test_error_to_dict_and_retryable():
    e = ColorError("TOOL_ERROR", "boom")
    d = e.to_dict()
    assert d["code"] == "TOOL_ERROR" and d["retryable"] is True and d["message"] == "boom"
    e2 = ColorError("TOOL_ERROR", "boom", retryable=False)
    assert e2.to_dict()["retryable"] is False


# ---- path policy / filenames (no filesystem I/O beyond tmp_path)
def test_path_policy_resolves_input_inside_workspace(tmp_path):
    f = tmp_path / "in.mp4"
    f.write_bytes(b"x")
    policy = PathPolicy(str(tmp_path))
    resolved = policy.resolve_input("in.mp4")
    assert resolved == f.resolve()


def test_path_policy_missing_input_raises(tmp_path):
    policy = PathPolicy(str(tmp_path))
    with pytest.raises(ColorError) as e:
        policy.resolve_input("nope.mp4")
    assert e.value.code == "INVALID_INPUT"


def test_path_policy_allowed_input_roots(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    inside = root / "in.mp4"
    inside.write_bytes(b"x")
    outside = other / "in.mp4"
    outside.write_bytes(b"x")
    policy = PathPolicy(str(tmp_path), allowed_input_roots=[str(root)])
    policy.resolve_input(str(inside))
    with pytest.raises(ColorError) as e:
        policy.resolve_input(str(outside))
    assert e.value.code == "PATH_NOT_ALLOWED"


def test_lut_roots_default_to_input_roots_when_not_set(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    policy = PathPolicy(str(tmp_path), allowed_input_roots=[str(root)])
    assert policy.allowed_lut_roots == policy.allowed_input_roots


def test_lut_roots_independent_when_set(tmp_path):
    in_root = tmp_path / "footage"
    in_root.mkdir()
    lut_root = tmp_path / "luts"
    lut_root.mkdir()
    lut = lut_root / "x.cube"
    lut.write_text("x")
    policy = PathPolicy(str(tmp_path), allowed_input_roots=[str(in_root)], allowed_lut_roots=[str(lut_root)])
    policy.resolve_lut(str(lut))
    with pytest.raises(ColorError):
        policy.resolve_input(str(lut))   # a LUT is not allowed as a video input under this policy


def test_traversal_in_output_rejected(tmp_path):
    policy = PathPolicy(str(tmp_path))
    with pytest.raises(ColorError) as e:
        policy.resolve_write_path("../escape.mp4")
    assert e.value.code == "PATH_NOT_ALLOWED"


def test_absolute_output_outside_workspace_rejected(tmp_path):
    policy = PathPolicy(str(tmp_path))
    with pytest.raises(ColorError) as e:
        policy.resolve_write_path("/etc/escape.mp4")
    assert e.value.code == "PATH_NOT_ALLOWED"


def test_symlink_escape_is_refused(tmp_path):
    outside = tmp_path.parent / "outside_target"
    outside.mkdir(exist_ok=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    link = workspace / "escape"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not permitted in this environment")
    policy = PathPolicy(str(workspace))
    with pytest.raises(ColorError):
        policy.resolve_write_path("escape/out.mp4")


@pytest.mark.parametrize("name", ["CON", "con.mp4", "NUL", "COM1", "a" * 300, "-flag.mp4", "trailing.", "trailing "])
def test_filename_rules_reject_unsafe_names(name):
    with pytest.raises(ColorError):
        check_filename(name)


def test_filename_rules_accept_normal_names():
    check_filename("clip_01.mp4")
    check_filename("graded-output.mov")


def test_output_formats_have_dot_extensions():
    for fmt, spec in OUTPUT_FORMATS.items():
        assert spec["extension"].startswith(".")
