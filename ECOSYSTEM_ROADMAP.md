# Roadmap — color-grading-skill

Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **映像 (Visual)**, status `active`.
Written from a read-only audit of this repository on 2026-10-30. Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

v0.4.0, Python. HDR_TO_SDR, LUT_APPLY, RETAG, STRIP_DOVI, PRIMARY_CORRECTION (exposure ... curves), OCIO_TRANSFORM (step 5), MATCH_CAMERA (step 6), LEGALIZE (step 7, without the shares report). 170 tests, CI on 3 OSes, release workflow.

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| 色補正 | done |  |
| LUT | done |  |
| HDR→SDR | done |  |

## Wiring

Agent wires 5 color_* skills through color-grading/run, every PRIMARY_CORRECTION parameter included (exposure ... curves, read from its pinned 0.4.0 contract; step 3). Delegates to ffmpeg-skill probe, color; ffmpeg-skill 3.x accepted (step 4).

## Steps

- [x] **1. contract_version + dependencies in the contract** — dependency on ffmpeg-skill with a range
  - Done when: OS registry conformance shows the dependency
- [x] **2. WHITE_BALANCE decision** — remove from `unsupported` or alias to temperature/tint (ADR)
  - Done when: no ambiguous entry
- [x] **3. (agent side) expose gamma/lift/gain/levels/curves** — tracked in video-production-agent ([video-production-agent#80](https://github.com/kajisho5/video-production-agent/pull/80), [#91](https://github.com/kajisho5/video-production-agent/pull/91))
  - Done when: plan carries them to PRIMARY_CORRECTION
  - Done (video-production-agent ADR-062 / ADR-076): `color.gamma` / `.lift` / `.gain` / `.levels_in_*` / `.levels_out_*` / `.curves` reach the PRIMARY_CORRECTION request by contract name; every `color.*` key, type, range and enum is read from the agent's pinned copy of this contract (0.4.0) and checked at plan time
- [x] **4. ffmpeg-skill 3.0.0** — [#26](https://github.com/kajisho5/color-grading-skill/pull/26) — accept its contract_version 2.0; Python 3.9 CI against the last 2.x (ADR-21)
  - Done when: CI passes against 3.0.0 and the range includes it
- [x] **5. OCIO / ACES transforms** — new op `OCIO_TRANSFORM`: a colour-space conversion named by an OpenColorIO config (the ACES studio / CG configs, or the user's own `.ocio`), for example camera log → ACEScct → Rec.709 or Rec.2100 PQ, with an optional look and display / view. PyOpenColorIO (BSD-3-Clause, optional extra `ocio`) only bakes the transform to a 3D LUT of a stated size; the LUT is applied through ffmpeg-skill as LUT_APPLY already does, so the engine stays the only thing touching media. Provenance records the config's name, version and hash, the source / destination spaces and the LUT size. The ACES configs' own licence is checked in the ADR
  - Done when: ADR; a baked LUT matches OCIO's own CPU processor within 1/1023 on a test chart; the doctor reports `ocio` missing without breaking the other ops; the output's colour tags match the destination space
  - Done (ADR-22): `OCIO_TRANSFORM` bakes with PyOpenColorIO (extra `ocio`) and applies through ffmpeg-skill `--lut` (+ `--retag` for PQ / HLG); the 129³ LUT read back from its `.cube` is within 1/1023 of OCIO's CPU processor on the ColorChecker + grey-ramp chart (S-Log3 → Rec.709 SDR, LogC4 → Rec.2100 PQ), every bake records its own measured error (65³, the default, measures up to about 2.7/1023 on that chart); doctor without PyOpenColorIO: `ocio` missing, only `OCIO_TRANSFORM` unavailable; output tags measured equal to the destination's. Limits: needs an ffmpeg-skill whose `--retag` declares `bsf:h264_metadata` (1.x on); on 0.12.3 it is unsupported in doctor and refused before encoding, since that version cannot guarantee any destination's tags; ffmpeg-skill before 3.1.6 shifts every LUT's colours (its #410, doctor warns)
- [x] **6. Match cameras to a reference** — MATCH_CAMERA: from a frame pair of the same subject (camera B against camera A), fit a correction (per-channel curves, or a small 3D LUT) and write it as a LUT per camera; the residual colour difference (ΔE2000 on stated patches or the whole frame) is reported. Field review 2026-11-05 (Velisa ROADMAP, third round)
  - なぜ要るか：会場の複数カメラの色の違いを、マルチカムの切り替えで目立たなくできる
  - Done when: a fixture whose camera B is camera A with a known tint and gain matches within ΔE 2 on average
  - Done (ADR-23): `MATCH_CAMERA` (frames by ffmpeg-skill/look, curves or 3×3 matrix fit, LUT applied by `--lut`); on the fixture (B = A with R ×0.86, G ×0.97 + 10, B ×0.90 + 6) ΔE2000 mean 5.19 → 0.50 measured on the written file (whole frame, curves; 9 patches with the matrix 4.81 → 0.25). Needs ffmpeg-skill ≥ 3.1.6 (its `--lut` matrix fix): on older checkouts it is unsupported in doctor and refused
- [ ] **7. Broadcast legal range and gamut** — LEGALIZE: soft-clip to BT.709 legal levels (8-bit 16-235 / 10-bit 64-940, chroma limits) and report the share of pixels out of range before and after; the limits follow EBU R103 when that profile is asked for
  - なぜ要るか：放送局への納品で、レベル外の信号で差し戻されない
  - Done when: a fixture with super-whites ends within range and the report gives both shares
  - Partly done (ADR-24), left unchecked: `LEGALIZE` (profiles `bt709` / `ebu-r103`, a soft-clip LUT over ffmpeg-skill's float RGB) brings the fixture's super-whites / sub-blacks from 12.2 % of luma samples out of 16–235 to 0, chroma 0 → 0 (measured by the test from the written file). **Not met: the report cannot give the shares**: ffmpeg-skill exposes no per-pixel range measurement and this skill runs no ffmpeg itself; provenance says `measured: false`. Needs an ffmpeg-skill legal-range measurement (proposal in ADR-24). Also measured: ffmpeg-skill's float-RGB leg clamps to 0..1 on the way back, so `ebu-r103`'s headroom is never written, and hard saturated edges overshoot chroma after 4:2:0 (testsrc2 ×1.3 chroma: 1.26 % left)
- [ ] **8. Alpha-aware grading** — LUT_APPLY / PRIMARY_CORRECTION on a source with alpha keep the alpha unchanged, and a premultiplied source is un-premultiplied before the correction and premultiplied again after
  - なぜ要るか：テロップや CG の色を変えても、縁が黒ずんだり白く浮いたりしない
  - Done when: a premultiplied fixture's alpha is bit-identical after a LUT, and edge pixels have no dark fringe

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (3.x, 42 tools, contract_version 2.0; treated as complete, proposals go to Velisa's ROADMAP). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).
