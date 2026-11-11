"""ADR-28: HDR_TO_SDR's tone-mapping curve when the request names none follows the input's transfer (mobius for HLG,
hable for PQ and anything else); an explicit curve always wins; the choice and its rule are recorded. No ffmpeg needed:
ffmpeg-skill/probe is stubbed and the executor runs in dry-run mode (planning only)."""
import pytest

from color_grading.contract import skill_contract
from color_grading.errors import ColorError
from color_grading.executor import Executor
from color_grading.model import TONEMAPS, select_tonemap, validate_parameters
from color_grading.security import PathPolicy

HLG, PQ = "arib-std-b67", "smpte2084"


# ---- the rule itself
@pytest.mark.parametrize("transfer,curve,rule", [
    (HLG, "mobius", "hlg_default"),
    (PQ, "hable", "default"),
    ("bt709", "hable", "default"),
    (None, "hable", "default"),
])
def test_default_curve_by_input_transfer(transfer, curve, rule):
    assert select_tonemap(None, transfer) == {"tonemap": curve, "rule": rule, "input_transfer": transfer}


@pytest.mark.parametrize("requested", TONEMAPS)
@pytest.mark.parametrize("transfer", [HLG, PQ, None])
def test_explicit_curve_always_wins(requested, transfer):
    assert select_tonemap(requested, transfer) == {"tonemap": requested, "rule": "request", "input_transfer": transfer}


def test_omitted_tonemap_is_left_for_the_executor():
    assert validate_parameters("HDR_TO_SDR", {}, "x")["tonemap"] is None
    assert validate_parameters("HDR_TO_SDR", {"tonemap": "hable"}, "x")["tonemap"] == "hable"


def test_explicit_null_tonemap_rejected():
    with pytest.raises(ColorError) as e:
        validate_parameters("HDR_TO_SDR", {"tonemap": None}, "x")
    assert e.value.code == "INVALID_REQUEST"


# ---- the executor (dry run, stubbed probe)
class _StubSkill:
    def __init__(self, transfer):
        self.transfer = transfer
        self.runs = []

    def probe(self, path, timeout=None):
        video = {"width": 160, "height": 90, "pix_fmt": "yuv420p10le", "codec": "hevc", "color_space": "bt2020nc", "color_primaries": "bt2020",
                 "color_transfer": self.transfer, "color_range": "tv", "bt2020_or_hdr": True, "hdr": True, "dolby_vision": False}
        return {"duration": 2.0, "video": video, "audio": None, "subtitle_streams": 0, "data_streams": 0}


def _op(op_id, typ, ref, **params):
    return {"op_id": op_id, "type": typ, "input": ref, "parameters": params}


def _plan(tmp_path, monkeypatch, transfer, ops):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.mp4").write_bytes(b"not decoded in a dry run")
    doc = {"schema": "color-grading/request@1", "project": {"project_id": "p1", "source": {"source_id": "s1", "path": "in.mp4"}, "operations": ops,
           "outputs": [{"output_id": "o1", "operation": f"op:{ops[-1]['op_id']}", "path": "out.mp4", "format": "mp4"}]}}
    d = Executor(PathPolicy(str(tmp_path)), _StubSkill(transfer), dry_run=True, tool_versions={"ffmpeg-skill": "test"}).response(doc)
    assert d["ok"], d.get("error")
    return d


def _result(d, node_id):
    return next(r for r in d["results"] if r["node_id"] == node_id)


@pytest.mark.parametrize("transfer,curve,rule", [(HLG, "mobius", "hlg_default"), (PQ, "hable", "default"), ("bt709", "hable", "default")])
def test_executor_resolves_the_default_from_the_source_transfer(tmp_path, monkeypatch, transfer, curve, rule):
    d = _plan(tmp_path, monkeypatch, transfer, [_op("s", "HDR_TO_SDR", "source")])
    r = _result(d, "op:s")
    want = {"tonemap": curve, "rule": rule, "input_transfer": transfer}
    assert r["parameters"]["tonemap"] == curve and r["tonemap_selection"] == want
    step = next(s for s in d["plan"]["steps"] if s["node_id"] == "op:s")
    assert step["parameters"]["tonemap"] == curve and step["tonemap_selection"] == want


def test_executor_keeps_an_explicit_curve_on_hlg(tmp_path, monkeypatch):
    d = _plan(tmp_path, monkeypatch, HLG, [_op("s", "HDR_TO_SDR", "source", tonemap="hable", peak_nits=400)])
    r = _result(d, "op:s")
    assert r["parameters"]["tonemap"] == "hable" and r["tonemap_selection"] == {"tonemap": "hable", "rule": "request", "input_transfer": HLG}


def test_hlg_default_and_explicit_mobius_share_an_identity_but_not_hable(tmp_path, monkeypatch):
    default = _result(_plan(tmp_path, monkeypatch, HLG, [_op("s", "HDR_TO_SDR", "source")]), "op:s")["operation_id"]
    mobius = _result(_plan(tmp_path, monkeypatch, HLG, [_op("s", "HDR_TO_SDR", "source", tonemap="mobius")]), "op:s")["operation_id"]
    hable = _result(_plan(tmp_path, monkeypatch, HLG, [_op("s", "HDR_TO_SDR", "source", tonemap="hable")]), "op:s")["operation_id"]
    assert default == mobius != hable


def test_input_transfer_follows_strip_dovi_and_retag(tmp_path, monkeypatch):
    d = _plan(tmp_path, monkeypatch, HLG, [_op("d", "STRIP_DOVI", "source"), _op("s", "HDR_TO_SDR", "op:d")])
    assert _result(d, "op:s")["tonemap_selection"] == {"tonemap": "mobius", "rule": "hlg_default", "input_transfer": HLG}
    d = _plan(tmp_path, monkeypatch, PQ, [_op("t", "RETAG", "source", target="bt2020-hlg"), _op("s", "HDR_TO_SDR", "op:t")])
    assert _result(d, "op:s")["tonemap_selection"] == {"tonemap": "mobius", "rule": "hlg_default", "input_transfer": HLG}
    d = _plan(tmp_path, monkeypatch, HLG, [_op("t", "RETAG", "source", target="bt2020-pq"), _op("s", "HDR_TO_SDR", "op:t")])
    assert _result(d, "op:s")["tonemap_selection"] == {"tonemap": "hable", "rule": "default", "input_transfer": PQ}


def test_input_transfer_unknown_after_a_reencoding_operation(tmp_path, monkeypatch):
    d = _plan(tmp_path, monkeypatch, HLG, [_op("c", "PRIMARY_CORRECTION", "source", exposure=0.5), _op("s", "HDR_TO_SDR", "op:c")])
    assert _result(d, "op:s")["tonemap_selection"] == {"tonemap": "hable", "rule": "default", "input_transfer": None}


# ---- the contract says so, outside the blocks a consumer pins
def test_contract_declares_the_default_rule():
    c = skill_contract()
    (spec,) = [o for o in c["operations"] if o["type"] == "HDR_TO_SDR"]
    assert spec["parameters"]["tonemap"]["default"] is None and spec["parameters"]["tonemap"]["type"] == "string"
    rule = c["hdr_sdr_defaults"]["tonemap"]
    assert rule["by_input_transfer"] == {HLG: "mobius"} and rule["otherwise"] == "hable" and rule["explicit_wins"] is True
    assert set(rule["rules"]) == {"request", "hlg_default", "default"}
    assert any(e.startswith("tonemap_selection") for e in c["provenance_extensions"]["per_operation"])
