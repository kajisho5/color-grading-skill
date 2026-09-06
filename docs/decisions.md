# Decisions

- **ADR-1 Execute through ffmpeg-skill only.** No direct ffmpeg. Missing capabilities (gamma, lift, gain, levels,
  curves, and white balance as its own single operation type) are declared gaps (docs/ffmpeg-skill.md), never
  worked around with a private ffmpeg call or a raw filter string.
- **ADR-2 Envelope carries both `ok` and `status`.** Same shape as audio-production-skill: the ecosystem's error
  contract asks for `{ok, error{code, message, retryable}}`; media-analysis-skill uses `status: ok|partial|error`.
  Both are present and consistent; exit codes are per error code, starting at 2.
- **ADR-3 No production defaults for creative parameters.** `lut_path` and `retag.target` have no default: the
  caller decides which LUT and which colour tags. `HDR_TO_SDR`'s `tonemap`/`peak_nits`/`desat` do have defaults
  (matching ffmpeg-skill/color's own: `hable` / 1000 nits / 0) because they are encode-quality parameters, not a
  creative decision about *which* colour treatment to apply — the decision to tone-map at all is still the
  caller's (there is no implicit HDR→SDR anywhere in this skill).
- **ADR-4 One operation, one file, named by identity.** Unlike audio-production-skill's PCM WAV intermediates
  (always the same container/codec), a colour intermediate keeps the source's own container extension: ffmpeg-skill
  /color re-encodes with x264 (HDR_TO_SDR, LUT_APPLY) or stream-copies (RETAG, STRIP_DOVI, unless a fallback
  re-encode is needed), and this skill does not add a second, unnecessary transcode on top. Enables reuse, hashing
  and validation with a single probe per artifact; costs disk space (no eviction, same as audio-production-skill).
- **ADR-5 LUT identity is content, not path (ADR-004 of media-analysis-skill, restated for LUTs).** `LUT_APPLY`'s
  operation identity substitutes the LUT's sha256 for its path before hashing. A LUT is resolved and hashed once,
  read-only, even under `--dry-run` / `plan`, the same way audio-production-skill probes sources under dry run.
- **ADR-6 A LUT is data, never a filter string.** The request carries a path; this skill resolves it through its own
  PathPolicy (a *separate* allowed-roots policy from the video input, ADR-7), checks the `.cube` extension and a
  size ceiling, and passes the resolved absolute path to ffmpeg-skill's `--lut` flag. Nothing about a LUT's content
  is parsed or interpreted by this skill; `ffmpeg-skill/color --lut` (ffmpeg's `lut3d` filter) is the only consumer
  of its bytes.
- **ADR-7 LUT path policy is separate from the input path policy.** A caller allowed to read footage under
  `/footage` should not thereby be able to name any file on the machine as a LUT; `--allowed-lut` defaults to
  `--allowed-input` only when not given explicitly, so an embedder who cares can confine LUTs to a `luts/` directory
  distinct from raw footage.
- **ADR-8 No container conversion.** An output's `format` extension must equal the source's. ffmpeg-skill/color
  never re-containers on its own (it writes to whatever extension `-o` is given, same family as the input in every
  documented example); adding "convert to X while grading" here would duplicate ffmpeg-skill/export's job and widen
  this skill's surface past "execute the named colour operation". A caller who wants both should call
  color-grading-skill, then ffmpeg-skill/export (or an export-production-skill, if the ecosystem gains one) on the
  result.
- **ADR-9 Refuse rather than approximate.** A non-HDR source into `HDR_TO_SDR` without `force: true`, a non-`.cube`
  LUT, an output container that does not match the source, a request for a still-unimplemented correction
  (`WHITE_BALANCE` as its own type, `GAMMA`, `LIFT`, `GAIN`, `LEVELS`, `CURVES`), or a `PRIMARY_CORRECTION` parameter
  outside its declared safe range — each is an explicit `UNSUPPORTED_*` / `INVALID_*` error, never a best-effort
  guess or a silently clamped value.
- **ADR-10 Unknown is a capability status.** `filter:zscale` / `filter:tonemap` / `filter:lut3d` are core ffmpeg
  filters that ffmpeg-skill's own doctor *does* attempt to detect (unlike the audio filters audio-production-skill
  relies on), but the same FFmpeg ≥ 8.0 detection defect applies (docs/ffmpeg-skill.md): when it fires, every filter
  capability is reported `unknown`, not `unsupported`, and execution proceeds; output validation decides.
- **ADR-11 Generic core.** No production-house or camera-brand vocabulary in code, contract or schemas; `RETAG`'s
  four targets and `HDR_TO_SDR`'s seven tonemap curves are exactly ffmpeg-skill/color's own enums, nothing added.
- **ADR-12 Verified against ffmpeg-skill 0.9.1, not "the latest" (superseded by ADR-15's 0.9.2 window).** All flags
  this skill emitted at the time
  (`--to-sdr/--lut/--retag/--strip-dovi/--tonemap/--peak/--desat/--lut-strength/--force/--crf/--preset`) were read
  from `scripts/color.py` and confirmed present in `scripts/_contract.py --json --static`'s generated `input_schema`
  at commit-pinned 0.9.1. The underlying principle stands (`doctor` / `run` refuse anything outside the verified
  window rather than assume an untested version behaves the same); only the window itself moved when
  `PRIMARY_CORRECTION` was added (ADR-15).
- **ADR-13 Declared but not implemented operations are a fixed, honest list.** `WHITE_BALANCE` (as its own single
  operation type — see ADR-15), `GAMMA`, `LIFT`, `GAIN`, `LEVELS`, `CURVES` are declared in
  `UNSUPPORTED_OPERATIONS` with the specific ffmpeg-skill gap that blocks each one; they raise
  `UNSUPPORTED_OPERATION` at validation and never appear in `contract --json`'s `operations[]` (STEP 1 of the design
  brief: only implemented operations are exposed as supported). Implementing any of them requires a corresponding
  typed capability in ffmpeg-skill's public contract first (or a decision to add a second execution backend); this
  skill will not grow a private `eq` / `colorbalance` / `curves` filter string to work around the gap. (`EXPOSURE`,
  `CONTRAST`, `SATURATION`, `TEMPERATURE` and `TINT` were removed from this list when `PRIMARY_CORRECTION` absorbed
  them, ADR-15.)
- **ADR-14 A Windows-only ffmpeg-skill defect is worked around from this skill's own side, never by editing
  ffmpeg-skill.** Measured on Windows CI (docs/ffmpeg-skill.md): an absolute Windows LUT path's drive-letter colon,
  once escaped by ffmpeg-skill's own `escape_filter_path` for the `-vf lut3d=file=...` value, is rejected by at
  least one Windows ffmpeg build's filter-option parser, making `LUT_APPLY` fail outright. A first attempt made the
  `--lut` value relative to the ffmpeg-skill subprocess's own working directory; measured as not enough, because
  GitHub Actions Windows runners put the repository checkout and the OS temp directory (where a workspace under a
  caller's own temp directory typically lives) on different drives, and no relative path can cross a Windows drive.
  The adapter now accepts an per-call `cwd` override (`adapter.run_tool`/`_popen`), and `executor._execute_node`
  runs `LUT_APPLY` with `cwd` at the LUT's own directory, so `executor._lut_arg` can pass just the bare file name —
  no drive letter, nothing to escape, on any drive layout. This changes only what this skill hands to `--lut` and
  which directory the subprocess starts in, not anything in ffmpeg-skill, and is inert on POSIX (a bare name in
  the process's own directory was already unambiguous there).
- **ADR-15 `PRIMARY_CORRECTION`: technical correction, not creative grading, added via ffmpeg-skill 0.9.2.**
  ffmpeg-skill 0.9.2 added a typed `--correct` mode to `color.py`: five typed, range-checked flags
  (`exposure`/`contrast`/`saturation`/`temperature`/`tint`) mapping 1:1 onto real ffmpeg filters (the dedicated
  `exposure` filter, `colortemperature`, `colorbalance`, `eq`), never a raw filter string. `PRIMARY_CORRECTION` is a
  thin typed front end for that one mode, following the same one-operation-per-node pattern as every other
  operation; it adds no filter of its own, and this skill's own range checks in `model.py` duplicate ffmpeg-skill's
  `die()` checks as an independent, fail-fast guard at the request boundary rather than relying on ffmpeg-skill's
  checks exclusively.
  - *Version window moves.* The adapter's compatibility window becomes `[0.9.2, 1.0.0)`, superseding ADR-12's
    `[0.9.1, 1.0.0)`: `--correct` and its five flags do not exist in 0.9.1. This skill does not support ffmpeg-skill
    versions per-operation — one located checkout is either compatible with everything this skill emits, or `doctor`
    / `run` refuse it wholesale with `ffmpeg_skill_incompatible`, never a silent per-operation degrade.
  - *Measurement, not judgement.* Every prior operation had a fixed target state to re-probe against (`HDR_TO_SDR`'s
    `hdr` flag, `RETAG`'s tag triple, `STRIP_DOVI`'s Dolby Vision flag, `LUT_APPLY`'s `pix_fmt`).
    `PRIMARY_CORRECTION` is a continuous transform with no such fixed target — "did `contrast=1.1` apply correctly"
    has no discrete pass/fail the way "is this file still tagged HDR" does. Rather than inventing a subjective
    "looks corrected" check, this skill carries ffmpeg-skill's own existing signalstats-based `analyze_levels`
    measurement (already used by `probe.py --analyze` for Log-footage detection) through unchanged as
    `measurements.input` / `measurements.output` in `NodeState`, the manifest and provenance — real, technical
    numbers (`y_avg`, `saturation_avg`, …) for the caller's own judgement, never evaluated by this skill as a
    pass/fail.
  - *Ordering is guidance, not enforcement.* The natural pipeline order for combining a technical correction with a
    creative look is `PRIMARY_CORRECTION` before `LUT_APPLY` (correct the plate, then apply the look) — expressed by
    the caller as an ordinary two-node chain in the existing operation graph, exactly as `HDR_TO_SDR` → `LUT_APPLY`
    already works; no new pipeline concept is introduced. This skill does not enforce, validate or default to that
    order: the operation graph has no notion of "technical" vs. "creative" node kinds, and a caller building the
    opposite order gets no error. Deciding operation order is video-production-agent's job, not something this
    execution layer would be right to second-guess.
  - *White balance stays a declared gap as its own operation type.* No single "white balance" flag exists in
    ffmpeg-skill's contract — only the two independent `temperature`/`tint` parameters of `PRIMARY_CORRECTION` that
    together achieve it (ADR-13). `GAMMA`/`LIFT`/`GAIN`/`LEVELS`/`CURVES` remain unimplemented for the same reason
    as before: no corresponding typed ffmpeg-skill capability yet (docs/ffmpeg-skill.md).
- **ADR-16 `provides`: publish this Skill's five operations as cross-repository Capability ids.** Added for
  `kajisho5/AI-video-production-OS`'s `CapabilityContract.provides` (`docs/SPEC.md` there), so a registry can
  resolve "who provides `color.hdr_to_sdr`" without hardcoding this repository. `model.OPERATION_TYPES` has no
  native capability-shaped id of its own, so the id per operation type is a new naming decision, not a mechanical
  derivation — it matches the ids already assigned in that project's own `docs/CAPABILITY_MATRIX.md`, kept here in
  `contract.CAPABILITY_IDS` as the single source of truth going forward: `HDR_TO_SDR` → `color.hdr_to_sdr`,
  `LUT_APPLY` → `color.lut_apply`, `RETAG` → `color.retag`, `STRIP_DOVI` → `color.strip_dovi`. `PRIMARY_CORRECTION`
  (ADR-15) postdates that matrix's own audit, so its id (`color.primary_correction`) is this repository's own
  provisional one, following the same convention (same situation as motion-graphics-skill's `bug`/`chapter` ids).
  Every operation type gets an id: this Skill has a single tool (`{SKILL_ID}/run`) and every operation always
  writes a validated video artifact through it. Additive: a new top-level `provides` key derived from
  `OPERATION_TYPES`, saying nothing `operations[]` doesn't already say, only indexed by Capability id instead of
  operation type.
