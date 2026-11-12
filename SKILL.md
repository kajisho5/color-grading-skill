---
name: color-grading
description: Deterministic colour grading / colour correction execution Skill for the AI Video Production Ecosystem. Use it when a caller (normally video-production-agent) has already decided which colour treatment to apply to a video and needs it executed safely - HDR (PQ/HLG, BT.2020) to SDR BT.709 tone mapping with an explicit curve, application of a 3D .cube LUT, rewriting colour tags (BT.709 / BT.2020 PQ / BT.2020 HLG / BT.601) without a re-encode, removing a Dolby Vision RPU, or typed primary colour correction (exposure, contrast, saturation, white balance via temperature + tint, gamma, three-way shadows/highlights via lift + gain, levels, or a curves preset), soft-clipping into broadcast-legal levels (LEGALIZE, BT.709 or EBU R 103; the shares of samples out of range are measured before and after only when ffmpeg-skill has check --levels and the picture is Y'CbCr, otherwise the report sets measured to false), matching one camera to a reference camera from a frame pair the caller names (MATCH_CAMERA), or an OpenColorIO / ACES colour-space transform named by a config (camera log -> ACEScct -> Rec.709 or Rec.2100 PQ; optional PyOpenColorIO extra) - as a typed operation graph with validated outputs and provenance. Do NOT use it to decide which colour treatment, look, LUT or correction values to apply (video-production-agent), to measure or analyse media (media-analysis-skill), to edit video (video-editing-skill), or to run arbitrary ffmpeg commands or filters (it refuses them). WHITE_BALANCE is accepted as an alias of PRIMARY_CORRECTION restricted to temperature/tint (resolved at validation; results report PRIMARY_CORRECTION).
---

# color-grading

Machine interface: `color-grading run - --json` with a `color-grading/request@1` document on stdin; exactly one
`color-grading/response@1` document on stdout. `skill --json` prints the contract, `doctor --json` the environment,
`plan - --json` a dry run, `validate - --json` a schema check.

Rules for a calling agent:
1. Decide first (video-production-agent): which operation, which LUT, which tonemap curve, which retag target.
   This skill has no defaults for creative parameters that matter (LUT path, retag target) and never guesses which
   colour treatment to apply.
2. Give explicit, typed parameters: `HDR_TO_SDR` needs nothing but has typed `tonemap`/`peak_nits`/`desat`/`force`
   (without `tonemap` the curve follows the input's transfer: `mobius` for HLG, `hable` for PQ and anything else, ADR-28);
   `LUT_APPLY` needs `lut_path`; `RETAG` needs `target`; `PRIMARY_CORRECTION` needs nothing but has typed
   `exposure`/`contrast`/`saturation`/`temperature`/`tint`/`gamma`/`lift`/`gain`/`levels_in_black`/
   `levels_in_white`/`levels_out_black`/`levels_out_white`/`curves`, each independently optional and defaulting to
   unchanged (`curves` defaults to no preset); `OCIO_TRANSFORM` needs `source_space` and either `destination_space`
   or `display` + `view` (names from the config), optional `config`/`look`/`lut_size`/`output_tags`; `MATCH_CAMERA`
   needs `reference_path` (camera A), with `at`/`reference_at`, `model`, optional `patches`, `lut_size`. Tone-mapping curve and correction values are always the caller's,
   never automatic.
3. Never send commands, argv, filter strings or executable paths: the request is rejected. A LUT path is data,
   resolved through its own path policy and hashed into provenance; it is never a filter string.
4. Keep outputs inside the workspace, never at the input path; set `overwrite: true` deliberately. An output's
   container must match the source's (this skill does not convert containers; that is ffmpeg-skill/export).
5. Read `results[].status`, `outputs[].provenance` and `error` from the JSON; the exit code is 0 only when `ok`
   is true.
6. An alpha source (ProRes 4444, qtrle, PNG) keeps its alpha through `LUT_APPLY` / `PRIMARY_CORRECTION` when ffmpeg-skill
   has `--keep-alpha`; other re-encoding operations refuse it (`alpha_not_preserved`). Say how its colour is stored with
   `project.source.alpha_mode: straight | premultiplied` (unsaid: PNG is straight, anything else is taken as straight
   with a warning). A premultiplied source is refused for those grades (`alpha_premultiplied`: ffmpeg-skill cannot
   un-premultiply it). `project.source.drop_alpha: true` asks for an opaque result instead (ADR-33).

See README.md for the schemas, operation table, error codes and limitations.
