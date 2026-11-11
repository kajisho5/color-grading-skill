# Testing

`python -m pytest -q` runs everything; nothing is skipped. The integration tests need FFmpeg on PATH (to synthesise
fixtures and for ffmpeg-skill) and an ffmpeg-skill checkout (`COLOR_GRADING_FFMPEG_SKILL_DIR`, or `../ffmpeg-skill`,
`./vendor/ffmpeg-skill`, `~/.claude/skills/ffmpeg-skill`); a missing one fails the session (not a skip).

| file | covers |
|---|---|
| `tests/test_unit.py` | request schema (every operation's parameters, arity, references, forbidden fields), graph (order, cycles, unreachable, LUT content-hash identity override, determinism), canonical JSON, error table, file-name rules, path policy (input / LUT roots independent), symlink escapes, Windows names |
| `tests/test_security.py` | no shell in source, script allow-list (`probe.py`/`color.py` only), argv formatting (`fmt_num`), parameter injection through every operation's parameters, unsafe output paths / roots / symlinks through the CLI, LUT path policy separate from input path policy, input overwrite, existing output, malformed JSON, command/argv fields anywhere in the document, ffmpeg-skill directory handling, minimal environment, argv builder audit (every value is a fixed flag / formatted number / validated enum / absolute path) |
| `tests/test_contract.py` | contract ⇔ implementation, `skill` = `contract`, doctor statuses (`supported` / `unsupported` / `unknown`), doctor without ffmpeg-skill, capability table |
| `tests/test_compat.py` | ffmpeg-skill compatibility by capability detection (ADR-19): `--quality`/`--crf` selection, missing-flag refusal naming the flag, `--help` fallback, `_common.py`/`_common/` layouts, probe `hdr`/`bt2020_or_hdr`, fake checkouts at 0.12.3 / 1.0.0 / 2.10.3 / a far-future version, doctor's `detected` report, and the real checkout's detection |
| `tests/test_ocio.py` | `OCIO_TRANSFORM` (ADR-22): parameter rules, output tags derived or explicit, the baked 129³ LUT read back from its `.cube` text within 1/1023 of OpenColorIO's CPU processor on the test chart (S-Log3 → Rec.709 SDR, LogC4 → Rec.2100 PQ), a 17³ LUT's larger error reported, deterministic bake, names checked against the config, doctor and runs without PyOpenColorIO (only `OCIO_TRANSFORM` unavailable), real runs (Rec.709 colour against the processor within 2.5/255, PQ retag + reuse, `.ocio` file config, dry run writes nothing) |
| `tests/test_tonemap_default.py` | `HDR_TO_SDR`'s default curve (ADR-28): mobius for an HLG input, hable for PQ / anything else / an unknown input transfer, an explicit curve always wins, `tonemap_selection` in plan steps and results, identities (HLG default = explicit mobius ≠ hable), input transfer through `STRIP_DOVI` / `RETAG`, the contract's `hdr_sdr_defaults` (dry run, probe stubbed); the real run on the synthetic HLG fixture is `tests/test_integration.py::test_hdr_to_sdr_default_curve_follows_the_source_transfer` |
| `tests/test_alpha.py` | alpha sources (ADR-25, ADR-30): alpha pixel formats, the alpha line per input codec and container; with `--keep-alpha` detected, LUT_APPLY / PRIMARY_CORRECTION on the ProRes 4444 fixture keep the alpha (within one 8-bit code, read back by the test) under `run` and are planned under `plan`, the qtrle fixture's alpha is bit-identical after a LUT with one graded colour from transparent to opaque (no fringe), a LUT → PRIMARY_CORRECTION chain keeps it, an `.mp4` alpha source is refused; every other re-encoding operation (all of them without `--keep-alpha`) refused with nothing written; a RETAG stream copy keeping the alpha (or failing with `alpha_lost`); color's `alpha` step read as a validation (failed, missing, passing) |
| `tests/test_reuse.py` | reuse of intermediates (ADR-31): the identity includes this skill's version, so a manifest another version wrote is not reused; LUT_APPLY / PRIMARY_CORRECTION entries without a kept and verified `alpha` record (or with an opaque `pix_fmt`) are run again where this run keeps the alpha, one with it is reused, an opaque source's entry is reused as before (no ffmpeg; manifests written by the test) |
| `tests/test_optional_features.py` | ffmpeg-skill's optional `check --levels`, `color --legalize`, `color --keep-alpha` (ADR-29, ADR-30): flag-by-flag detection, capability statuses, contract and doctor entries, `check --levels` argv and its document read even when check fails, the executor's gates (used only when `supported`; `unknown` falls back), LEGALIZE's planned clamp / measurement and identity, its argv, color's `legal_levels` step read as a validation (no ffmpeg; stubbed) |
| `tests/test_legalize.py` | `LEGALIZE` (ADR-24, ADR-29): the soft clip (identity below the knee, monotone, inside the limits), deterministic LUT with its domain, profile limits in code values, parameter rules, the share report, and real runs measured by the test from the written file (rawvideo): the super-white fixture ends with 0 luma / chroma samples out of BT.709's range, `ebu-r103` inside its limits (and BT.709's, pinning the leg's 0..1 clamp); where the checkout has them, the clamp ran in the same encode with a passing `legal_levels` step and the reported before / after shares equal the test's own count (else `measured: false` and no `--legalize`); a reused run keeps its report; HDR refused; refused (and asserted so) before ffmpeg-skill 3.1.6 |
| `tests/test_match.py` | `MATCH_CAMERA` (ADR-23): CIEDE2000 against Sharma et al.'s published pairs, PNG filters and ffmpeg's 8/16-bit PNGs, both models inverting a known gain + tint, monotone curves from noisy samples, singular matrix, parameter rules, a checkout without look losing only MATCH_CAMERA, contract/doctor, and real runs: camera B → camera A within ΔE 2 on average (whole frame, curves) and with the matrix on patches, re-run reuse, framing mismatch, HDR refusal, reference path policy; refused (and asserted so) on an ffmpeg-skill before 3.1.6 |
| `tests/test_integration.py` | per operation positive + negative (`HDR_TO_SDR` incl. non-HDR source refusal and `force`, `LUT_APPLY` incl. wrong extension / oversized / outside allowed roots, `RETAG` for all four targets, `STRIP_DOVI` on HEVC and its refusal on H.264, `PRIMARY_CORRECTION` incl. defaults-are-near-identity, deterministic desaturation, out-of-range rejection and observed `measurements`, plus `gamma`/`lift`/`gain`/`levels_*`/`curves` measurable effects, invalid `curves` choice, and `levels_in_black`/`levels_out_black` ordering rejection), chained pipelines `HDR_TO_SDR → LUT_APPLY` and `PRIMARY_CORRECTION → LUT_APPLY`, dry run writes nothing, re-run reuse + `--no-reuse` + tampered intermediate, invalid inputs (no video stream, corrupt file), output container mismatch, missing output, expectation failure removes the output, timeout, SIGINT cancellation, CLI validate / exit codes / human output, deterministic pixel-level checks for `LUT_APPLY` and `PRIMARY_CORRECTION` |

Fixtures (`tests/fixtures/generate.py`) are synthesised with ffmpeg at test time: a 3 s 160×90 SDR H.264+AAC clip
(solid colour), a 2 s SDR H.264 clip with no audio, a 2 s SDR HEVC clip (no Dolby Vision side data), a 2 s HEVC
Main10 clip tagged BT.2020/PQ (HDR10), a 2 s mono PCM WAV (no video stream), a text file, and a size-2 "invert"
`.cube` LUT (a genuinely affine map: trilinear/tetrahedral interpolation of its 8 corners reproduces `output = 1 -
input` exactly at every point in the cube, so the resulting colour shift is deterministic and numerically checkable
— never a subjective "looks graded" judgement). Expected values in the tests follow from this construction.

## What real-media verification does and does not prove

- **`HDR_TO_SDR`**: run against the HEVC10/PQ/BT.2020 fixture; the output is probed and must report `hdr: false`
  (ffmpeg-skill/probe's own HDR detection — `bt2020_or_hdr`, or `hdr` before 2.0 — not this skill's guess) — a real, deterministic check that tone mapping
  actually ran, not merely that ffmpeg exited 0.
- **`LUT_APPLY`**: run with the invert LUT against the solid-colour fixture; the output's average colour (sampled
  by scaling the frame to 1×1, an area/box-filter average) must equal `255 - average colour of the input` within a
  documented tolerance (chroma subsampling and lossy H.264 encoding introduce a few counts of error). This is the
  "pixel statistics" check STEP 19 of the design brief asks for, applied to a mathematically known transform —
  never a subjective aesthetic judgement.
- **`RETAG`**: run for all four targets against the SDR fixture; the output's `(color_space, color_primaries,
  color_transfer)` triple, as reported by `ffmpeg-skill/probe`, must equal ffmpeg-skill's own documented mapping
  (`model.RETAG_TAGS`) exactly.
- **`STRIP_DOVI`**: run against the plain HEVC fixture; the operation succeeds (HEVC is accepted) and the output
  reports no Dolby Vision side data. **Limitation, stated plainly**: this repository cannot synthesise a genuine
  Dolby Vision RPU (profile 8.4) with plain `ffmpeg`/`lavfi` sources, so the test does not prove that an *existing*
  RPU is stripped — only that the operation runs end-to-end on an HEVC input and the output is validated the same
  way a real Dolby Vision source's output would be. `STRIP_DOVI` on a non-HEVC (H.264) source is tested as the
  negative case ffmpeg-skill itself enforces (`TOOL_ERROR`).
- **`PRIMARY_CORRECTION`**: run against the solid-colour fixture. All-default parameters (an intended no-op) leaves
  the sampled average colour within a documented tolerance of the input (chained-filter rounding plus the x264
  re-encode itself cost a few levels even at identity, which is real, measured drift, not a colour shift);
  `saturation=0.0` is checked as a deterministic, measurable transform (near-equal sampled R/G/B — grayscale is a
  mathematical fact about the output, not a "look"); an out-of-range parameter (e.g. `exposure=10.0`) is rejected
  with `INVALID_REQUEST` before any tool runs. ffmpeg-skill's own before/after `measurements`
  (signalstats-based, real numbers) are asserted present on both `results[].measurements` and the matching
  provenance chain entry, and are asserted to differ between input and output for a parameter combination that
  measurably changes the image — this is the "observed, not judged" requirement verified end to end, not merely
  unit-tested against a mocked tool response.

Static checks used in development: `python -m pyflakes src tests`, `python -m compileall src tests`.
