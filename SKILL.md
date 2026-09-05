---
name: color-grading
description: Deterministic colour grading / colour correction execution Skill for the AI Video Production Ecosystem. Use it when a caller (normally video-production-agent) has already decided which colour treatment to apply to a video and needs it executed safely - HDR (PQ/HLG, BT.2020) to SDR BT.709 tone mapping with an explicit curve, application of a 3D .cube LUT, rewriting colour tags (BT.709 / BT.2020 PQ / BT.2020 HLG / BT.601) without a re-encode, or removing a Dolby Vision RPU - as a typed operation graph with validated outputs and provenance. Do NOT use it to decide which colour treatment, look or LUT to apply (video-production-agent), to measure or analyse media (media-analysis-skill), to edit video (video-editing-skill), or to run arbitrary ffmpeg commands or filters (it refuses them). It has no primary colour correction (exposure, contrast, saturation, white balance, temperature, tint, gamma, lift, gain, levels, curves): ffmpeg-skill has no typed filter for those yet.
---

# color-grading

Machine interface: `color-grading run - --json` with a `color-grading/request@1` document on stdin; exactly one
`color-grading/response@1` document on stdout. `skill --json` prints the contract, `doctor --json` the environment,
`plan - --json` a dry run, `validate - --json` a schema check.

Rules for a calling agent:
1. Decide first (video-production-agent): which operation, which LUT, which tonemap curve, which retag target.
   This skill has no defaults for creative parameters that matter (LUT path, retag target) and never guesses which
   colour treatment to apply.
2. Give explicit, typed parameters: `HDR_TO_SDR` needs nothing but has typed `tonemap`/`peak_nits`/`desat`/`force`;
   `LUT_APPLY` needs `lut_path`; `RETAG` needs `target`. Tone-mapping curve selection is always the caller's,
   never automatic.
3. Never send commands, argv, filter strings or executable paths: the request is rejected. A LUT path is data,
   resolved through its own path policy and hashed into provenance; it is never a filter string.
4. Keep outputs inside the workspace, never at the input path; set `overwrite: true` deliberately. An output's
   container must match the source's (this skill does not convert containers; that is ffmpeg-skill/export).
5. Read `results[].status`, `outputs[].provenance` and `error` from the JSON; the exit code is 0 only when `ok`
   is true.

See README.md for the schemas, operation table, error codes and limitations.
