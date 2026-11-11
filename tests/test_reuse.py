"""ADR-31: an intermediate is reused only when this version of the skill wrote it with what this run needs recorded.

The cache identity includes this skill's version, so a manifest another version wrote (with that version's checks and
recorded provenance) never matches. LUT_APPLY / PRIMARY_CORRECTION on a source whose alpha this run keeps is reused only
from an entry whose `alpha` record says the alpha was kept and verified; one without it (an opaque grade) is run again. An entry for a
source that was opaque is reused as before. No ffmpeg needed: the manifests and intermediates are written by the test."""
import json

import pytest

import color_grading.graph as graph_mod
from color_grading import VERSION
from color_grading.canonical import sha256_file
from color_grading.executor import ALPHA_GRADING_OPERATIONS, MANIFEST_SCHEMA, Executor, NodeState
from color_grading.graph import Node, OperationGraph
from color_grading.model import ColorOperation, ColorOutput, ColorProject, ColorSource
from color_grading.security import PathPolicy

KEPT = {"kept": True, "precision_bits": 8, "alpha_codec": "prores4444", "alpha_mode": "straight",
        "source_colour": "taken as straight (not un-premultiplied)", "tool": "ffmpeg-skill/color --keep-alpha",
        "verification": {"step": "alpha", "ok": True}}


def _graph(op_type="LUT_APPLY", params=None):
    ops = [ColorOperation("a", op_type, "source", params or {"lut_path": "look.cube"})]
    return OperationGraph(ColorProject("p1", ColorSource("s1", "in.mov"), ops, [ColorOutput("o1", "op:a", "out.mov", "mov")]))


def _identity(version, monkeypatch):
    monkeypatch.setattr(graph_mod, "VERSION", version)
    return _graph().identities("source-sha", {"ffmpeg-skill": "3.2.3"}, {"op:a": {"lut_path": "lut-sha"}})["op:a"]


def _entry(tmp_path, identity, op_type="LUT_APPLY", pix_fmt="yuva444p10le", alpha=None, skill_version=VERSION):
    """An intermediate and its manifest, shaped as _execute_node writes them."""
    out = tmp_path / f"{identity[:16]}.mov"
    out.write_bytes(b"intermediate bytes")
    art = {"path": str(out), "duration": 2.0, "width": 160, "height": 90, "pix_fmt": pix_fmt, "codec": "prores" if pix_fmt.startswith("yuva") else "h264",
           "color_space": "bt709", "color_primaries": "bt709", "color_transfer": "bt709", "color_range": "tv", "hdr": False,
           "dolby_vision": False, "size": out.stat().st_size, "sha256": sha256_file(str(out))}
    m = {"schema": MANIFEST_SCHEMA, "operation_id": identity, "type": op_type, "parameters": {}, "input_hash": "in", "lut": None,
         "artifact": art, "measurements": {}, "reencoded": None, "dropped_non_av_streams": None, "retagged": None,
         "tool": "ffmpeg-skill/color", "tool_versions": {"ffmpeg-skill": "3.2.3"}, "skill": "color-grading", "skill_version": skill_version}
    if alpha is not None:
        m["alpha"] = alpha
    manifest = out.with_suffix(out.suffix + ".json")
    manifest.write_text(json.dumps(m), encoding="utf-8")
    return out, manifest


def _state(identity, op_type="LUT_APPLY", line="prores4444"):
    st = NodeState(Node("op:a", op_type, ["source"], {}), identity)
    st.alpha_line = line
    return st


def _reusable(tmp_path, st, out, manifest):
    return Executor(PathPolicy(str(tmp_path)), skill=None)._reusable(st, out, manifest)


# ---- the skill's version is part of the identity
def test_identity_depends_on_the_skill_version(monkeypatch):
    assert _identity(VERSION, monkeypatch) == _identity(VERSION, monkeypatch)
    assert _identity("0.5.1", monkeypatch) != _identity(VERSION, monkeypatch)
    assert _identity("0.6.0", monkeypatch) != _identity("0.6.1", monkeypatch)


def test_a_manifest_another_skill_version_wrote_is_not_reused(tmp_path, monkeypatch):
    """The 0.5.x case: an opaque grade of a source not seen as alpha, written under that version's identity."""
    old = _identity("0.5.1", monkeypatch)
    current = _identity(VERSION, monkeypatch)
    out, manifest = _entry(tmp_path, old, pix_fmt="yuv420p", skill_version="0.5.1")
    # the current run looks under its own identity: the old file is not where it looks, and its manifest does not match
    assert not (tmp_path / f"{current[:16]}.mov").exists()
    assert _reusable(tmp_path, _state(current, line=None), out, manifest) is False
    # the same entry under the identity that wrote it would have been reused: only the version tells them apart
    assert _reusable(tmp_path, _state(old, line=None), out, manifest) is True


# ---- an alpha grade is reused only from an entry that recorded the alpha as kept
@pytest.mark.parametrize("op_type", ALPHA_GRADING_OPERATIONS)
@pytest.mark.parametrize("alpha,pix_fmt", [(None, "yuv420p"), (None, "yuva444p10le"), ({**KEPT, "kept": False}, "yuva444p10le"),
                                           ("kept", "yuva444p10le"), (KEPT, "yuv420p"),
                                           ({**KEPT, "verification": None}, "yuva444p10le"),
                                           ({**KEPT, "verification": {"step": "alpha", "ok": False}}, "yuva444p10le")])
def test_an_entry_without_a_kept_alpha_record_is_not_reused_where_alpha_is_kept(tmp_path, op_type, alpha, pix_fmt):
    out, manifest = _entry(tmp_path, "i" * 64, op_type, pix_fmt=pix_fmt, alpha=alpha)
    st = _state("i" * 64, op_type)
    assert _reusable(tmp_path, st, out, manifest) is False
    assert st.artifact is None and st.alpha is None


@pytest.mark.parametrize("op_type", ALPHA_GRADING_OPERATIONS)
def test_an_entry_with_a_kept_alpha_record_is_reused(tmp_path, op_type):
    out, manifest = _entry(tmp_path, "k" * 64, op_type, alpha=KEPT)
    st = _state("k" * 64, op_type)
    assert _reusable(tmp_path, st, out, manifest) is True
    assert st.alpha == KEPT and st.artifact.pix_fmt == "yuva444p10le"


@pytest.mark.parametrize("op_type", ALPHA_GRADING_OPERATIONS)
def test_an_opaque_sources_entry_is_still_reused(tmp_path, op_type):
    """A source that was opaque has no alpha line and no alpha record: nothing is required of it."""
    out, manifest = _entry(tmp_path, "o" * 64, op_type, pix_fmt="yuv420p")
    st = _state("o" * 64, op_type, line=None)
    assert _reusable(tmp_path, st, out, manifest) is True
    assert st.alpha is None
