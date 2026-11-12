# Decisions

- **ADR-1 Execute through ffmpeg-skill only.** No direct ffmpeg. A missing capability (white balance as its own
  single operation type; gamma/lift/gain/levels/curves were declared gaps here until ffmpeg-skill 0.12.3 added
  typed `--correct` flags for them, ADR-18) is a declared gap (docs/ffmpeg-skill.md), never worked around with a
  private ffmpeg call or a raw filter string.
- **ADR-2 Envelope carries both `ok` and `status`.** Same shape as audio-production-skill: the ecosystem's error
  contract asks for `{ok, error{code, message, retryable}}`; media-analysis-skill uses `status: ok|partial|error`.
  Both are present and consistent; exit codes are per error code, starting at 2.
- **ADR-3 No production defaults for creative parameters.** `lut_path` and `retag.target` have no default: the
  caller decides which LUT and which colour tags. `HDR_TO_SDR`'s `tonemap`/`peak_nits`/`desat` do have defaults
  (matching ffmpeg-skill/color's own: `hable` / 1000 nits / 0; since ADR-28 an omitted `tonemap` is `mobius` for an
  HLG input) because they are encode-quality parameters, not a
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
  LUT, an output container that does not match the source, a request for an operation declared in
  `UNSUPPORTED_OPERATIONS`, a parameter an alias does not take (`exposure` under `WHITE_BALANCE`, ADR-20), a `PRIMARY_CORRECTION` parameter outside its declared safe range, or a
  `PRIMARY_CORRECTION.levels_in_black`/`levels_out_black` not strictly less than its matching
  `levels_in_white`/`levels_out_white` (ADR-18) — each is an explicit `UNSUPPORTED_*` / `INVALID_*` error, never a
  best-effort guess or a silently clamped value.
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
  at commit-pinned 0.9.1. The window moved with ADR-15/17/18, and ADR-19 replaced it with capability detection:
  the principle that survives is "refuse what cannot be shown to work", now shown by the flags themselves rather
  than by a version number.
- **ADR-13 Declared but not implemented operations are a fixed, honest list.** `WHITE_BALANCE` (as its own single
  operation type — see ADR-15) is declared in `UNSUPPORTED_OPERATIONS` with the specific ffmpeg-skill gap that
  blocks it; it raises `UNSUPPORTED_OPERATION` at validation and never appears in `contract --json`'s
  `operations[]` (STEP 1 of the design brief: only implemented operations are exposed as supported). Implementing
  it requires a single typed "white balance" capability in ffmpeg-skill's public contract first (or a decision to
  add a second execution backend); this skill will not grow a private filter string to work around the gap.
  (`EXPOSURE`, `CONTRAST`, `SATURATION`, `TEMPERATURE` and `TINT` were removed from this list when
  `PRIMARY_CORRECTION` absorbed them, ADR-15; `GAMMA`, `LIFT`, `GAIN`, `LEVELS` and `CURVES` were removed the same
  way when `PRIMARY_CORRECTION` absorbed their matching parameters, ADR-18. **Superseded for `WHITE_BALANCE`
  2026-10-30, ADR-20:** it was never blocked by a gap in ffmpeg-skill, so it left this list and became an alias; the
  list is empty and stays as the place for an operation ffmpeg-skill really cannot execute.)
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
    together achieve it (ADR-13). (`GAMMA`/`LIFT`/`GAIN`/`LEVELS`/`CURVES` remained unimplemented for the same
    reason at the time this ADR was written; ffmpeg-skill 0.12.3 later added typed `--correct` flags for all five,
    and `PRIMARY_CORRECTION` absorbed them the same way it absorbed exposure/contrast/saturation/temperature/tint
    here — see ADR-18.)
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
- **ADR-17 Surface ffmpeg-skill's `reencoded`/`dropped_non_av_streams` signal; add `audio_stream` (ffmpeg-skill
  0.12.0/0.12.1); version window raised to `[0.12.1, 1.0.0)`.** ffmpeg-skill 0.12.0 gave `color.py --retag`'s
  re-encode fallback an honest `reencoded`/`dropped_non_av_streams` pair in its `--json` output instead of a bare
  `"completed"`, and 0.12.1 extended `dropped_non_av_streams` reporting to `--to-sdr`/`--lut`/`--correct` as well
  (a source's subtitle/data streams are now kept through every re-encoding colour operation on a best-effort basis,
  and the tool says so when it could not). This skill was discarding both fields (`executor._execute_node` only
  ever read `measurements`); they are now carried onto `NodeState` the same way `measurements` already is, written
  into every manifest and the output provenance chain, and reported (when not `None`) on every operation result.
  - *An independent re-probe, not blind trust.* Matching this skill's existing posture (`RETAG`'s tag-triple
    re-probe, ADR before this one), `executor._validate_artifact` re-probes the output's own `subtitle_streams`/
    `data_streams` counts (via `ffmpeg-skill/probe`, the same call already made for every other check) and compares
    them against the source's. A drop ffmpeg-skill itself reported (`dropped_non_av_streams: true`) is not an
    error — the caller already sees it on the result — but a stream count that fell with no such report is a
    `VALIDATION_ERROR` (`reason: stream_loss_unreported`): the scenario this skill's provenance philosophy exists to
    catch, a tool reporting "completed" while something silently vanished.
  - **`audio_stream` (ffmpeg-skill 0.12.0's `--audio-stream N`, default 0).** Added to `HDR_TO_SDR`/`LUT_APPLY`/
    `RETAG`/`PRIMARY_CORRECTION`'s parameters (not `STRIP_DOVI`: its stream copy, like a successful `RETAG` stream
    copy, keeps every audio track untouched, so the flag has nothing to select there) and threaded through
    `executor._argv` to `--audio-stream` on every `ffmpeg-skill/color` invocation those four operations make,
    including `RETAG`'s own re-encode fallback (`color.py` forwards it there too, unlike `crf`/`preset`). Default
    `0` reproduces every prior release's behaviour exactly (always the first audio track); a multi-track source
    (dubbed languages, M&E stems) can now name which track to keep instead of silently always getting track 0.
  - *Version window moves again.* `[0.9.2, 1.0.0)` (ADR-15) becomes `[0.12.1, 1.0.0)`: `--audio-stream` does not
    exist in `color.py` before 0.12.0, and the stream-survival check above is only meaningful for `HDR_TO_SDR`/
    `LUT_APPLY`/`PRIMARY_CORRECTION` once `dropped_non_av_streams` is real there too, which is 0.12.1. As with every
    prior window move, this is not per-operation: one located checkout is either compatible with everything this
    skill now emits, or `doctor`/`run` refuse it wholesale.
- **ADR-18 `PRIMARY_CORRECTION` gains gamma, three-way lift/gain, levels and a curves preset, added via
  ffmpeg-skill 0.12.3.** ffmpeg-skill 0.12.3 added five more typed `--correct` flags to `color.py`, on top of the
  five `PRIMARY_CORRECTION` already covered (ADR-15): `--gamma` (`eq`'s own gamma option, 0.1..10, default 1),
  `--lift`/`--gain` (three-way shadows/highlights via `colorbalance`'s shadow/highlight channels, -1..1 each,
  default 0), `--levels-in-black`/`-white`/`--levels-out-black`/`-white` (`colorlevels`, 0..255 each, defaults
  0/255/0/255 — ffmpeg-skill's own 8-bit-unit convention over the filter's native 0..1 range), and `--curves`
  (the `curves` filter's ten built-in presets, `model.CURVES_PRESETS`, taken verbatim from ffmpeg-skill's own
  `CURVES_PRESETS`; no default, curves is the one PRIMARY_CORRECTION field that stays `None` rather than an
  identity number). All eight became additional optional parameters of the existing `PRIMARY_CORRECTION` operation
  type — not five new operation types — because ffmpeg-skill itself expresses every one of them as one more option
  on the same `--correct` invocation, in the same fixed filter chain (`exposure` → `colortemperature` →
  `colorbalance` → `eq`, plus `colorlevels`/`curves` appended only when asked for); splitting them into separate
  operation types would misrepresent that they are all one ffmpeg-skill call, not five independent tool
  invocations.
  - *Levels ordering is validated at the request boundary, not left to ffmpeg-skill alone.* `color.py --correct`
    itself `die()`s when `levels_in_black >= levels_in_white` or `levels_out_black >= levels_out_white`;
    `model._validate_levels_ordering` duplicates that exact check in `validate_parameters` so a bad request is an
    `INVALID_REQUEST` before any ffmpeg-skill process starts, the same fail-fast posture as every other
    `PRIMARY_CORRECTION` range check (ADR-15) and every other cross-field check in this skill (ADR-9).
  - *`curves` is optional with no numeric default; every other new field always emits its flag.* `gamma`/`lift`/
    `gain`/`levels_in_black`/`levels_in_white`/`levels_out_black`/`levels_out_white` each have an identity default
    (1 / 0 / 0 / 0 / 255 / 0 / 255) and are always passed to `--correct`, matching how `exposure`/`contrast`/
    `saturation`/`temperature`/`tint` are already always passed (ADR-15's "every stage in this dict is always
    emitted" convention, restated in `color.py`'s own `CORRECTION` comment) — passing an identity value is
    harmless because ffmpeg-skill's own filter chain treats it as a no-op. `curves` has no such identity value
    (there is no "no curve" preset name); `executor._argv` only appends `--curves <preset>` when the caller set
    one, mirroring `HDR_TO_SDR`'s `--force` (a flag only added when true) and ffmpeg-skill's own `color.py`
    behaviour of only adding the `curves` filter term when `--curves` was actually given.
  - *Measurement, not judgement, again.* Same posture as ADR-15: none of the eight new parameters gets a
    discrete pass/fail check (there is no fixed "is gamma=1.2 correctly applied" target state); `measurements`
    (ffmpeg-skill's existing `analyze_levels`) is the only before/after signal, unchanged by this addition.
  - *Version window moves again.* `[0.12.1, 1.0.0)` (ADR-17) becomes `[0.12.3, 1.0.0)`: `--gamma`/`--lift`/
    `--gain`/`--levels-*`/`--curves` do not exist in `color.py` before 0.12.3. As with every prior window move,
    this is not per-operation: one located checkout is either compatible with everything `PRIMARY_CORRECTION` now
    emits, or `doctor`/`run` refuse it wholesale with `ffmpeg_skill_incompatible`.
- **ADR-19 ffmpeg-skill compatibility is detected, not a version window.** Supersedes the `[min, 1.0.0)` windows of
  ADR-12/15/17/18. The window refused every ffmpeg-skill release from 1.0.0 on (`ffmpeg-skill 2.10.3 is outside
  the supported window [0.12.3, 1.0.0)`) although most of them worked, and CI pinned one old commit, so a release
  that really broke this skill would not have been noticed either. The owner's requirement is that this skill
  works with whichever ffmpeg-skill is installed.
  - *What is checked.* `adapter.check_tools`: every flag in `REQUIRED_FLAGS` is present in the located checkout's
    `probe`/`color` input_schema (read from `_contract.py --json --static`, else `_contract.py --json`, else
    `contract.py --json`, else each tool's `--help`), plus one of each `FLAG_ALTERNATIVES` group. A missing flag
    is a `fail` that names the flag. The version and `contract_version` are reported, never compared.
  - *Why no lower bound remains.* Every minimum the window carried was raised for a flag (`--correct`'s five
    flags at 0.9.2, `--audio-stream` at 0.12.0, gamma/lift/gain/levels/curves at 0.12.3); the flag check refuses
    exactly those checkouts. The one guarantee that was not a flag, honest `dropped_non_av_streams` from
    `--to-sdr`/`--lut`/`--correct` (0.12.1), predates 0.12.3, so the flag check implies it.
  - *Renamed flag.* ffmpeg-skill 1.10 added `--quality` (same CRF scale and default) and 2.0 removed `--crf` from
    `color.py`. The request keeps its `crf` parameter; `executor._argv` emits it under whichever flag the
    checkout offers, `--quality` first (so a 1.10 – 1.x checkout prints no deprecation warning).
  - *Changed JSON meaning.* 2.0 narrowed probe's `hdr` to PQ/HLG/Dolby Vision and put the 1.x meaning in
    `bt2020_or_hdr`. `adapter.probe_hdr` reads `bt2020_or_hdr` when present, else `hdr`, so `HDR_TO_SDR`'s
    "still HDR" check and every artifact's `hdr` field keep one meaning across versions.
  - *`--overwrite`.* Not passed. Every intermediate path is deleted right before its `color.py` call, so the tool
    never has an existing output to refuse, and giving blanket overwrite consent would only widen what a race
    could clobber. Whether the checkout has the flag is reported (`detected.overwrite_flag`).
  - *CI.* `tests.yml` runs every platform against ffmpeg-skill main (also on a daily schedule, so a new release is
    exercised without a push here) and one Linux job against the oldest verified commit (0.12.3,
    `c3a4a5f0ef6629018308a7eaeb5a1f592e5defc1`). `tests/test_compat.py` pins the detection against fake 0.12.3,
    1.0.0, 2.10.3 and far-future checkouts, a contract-less one, and the real checkout.
- **ADR-20 `WHITE_BALANCE` is an alias of `PRIMARY_CORRECTION`'s temperature/tint, not an unsupported operation.**
  ECOSYSTEM_ROADMAP.md step 2. `WHITE_BALANCE` sat in `unsupported_operations` with the status `not_implemented`,
  while `PRIMARY_CORRECTION` already executed white balance through ffmpeg-skill's typed `--temperature`/`--tint`
  (ADR-15). One capability was reported as missing and present at once.
  - *Decision.* `WHITE_BALANCE` moves to a new table, `model.OPERATION_ALIASES`, and is listed in the contract's new
    `operation_aliases` (`type`, `alias_of`, `parameters`). A request may use it as an operation `type`; validation
    accepts only `temperature`, `tint`, `crf`, `preset` and `audio_stream` and resolves it to `PRIMARY_CORRECTION`,
    every other correction parameter at its identity default. Any other parameter is `INVALID_REQUEST` naming it and
    pointing to `PRIMARY_CORRECTION`, not silently ignored (ADR-9). `UNSUPPORTED_OPERATIONS` becomes empty.
  - *Why an alias, not a sixth operation type.* A sixth type would run the same `color.py --correct` call under a
    second name, need its own Capability id, and give the same correction two identities, so neither could reuse
    the other's intermediate. Resolved at validation, a `WHITE_BALANCE` node and the equivalent `PRIMARY_CORRECTION`
    node have one identity, and the plan, results and provenance name `PRIMARY_CORRECTION`. `operations[]`, the
    tool's `operations`, `provides` and `request.shape` stay as they were, so a consumer pinned to them sees no change.
  - *Why it was never a real gap.* ADR-13 kept it out until ffmpeg-skill had "a single typed white balance
    capability". No such flag is needed: the two typed flags exist, and the alias emits exactly them. No raw filter
    string is involved (ADR-9, docs/architecture.md).
  - *Not automatic.* The alias takes explicit values. Estimating a neutral point from the image would be a decision
    and stays out of this skill (`not_provided`).
- **ADR-21 ffmpeg-skill 3.0.0: accept its `contract_version` 2.0; Python 3.9 is tested against the last 2.x.**
  ffmpeg-skill 3.0.0 (`1dc2f52`) declares `contract_version` 2.0 (1.x and 2.x declared 1.0), drops Python 3.9 and
  FFmpeg 5.x, and changes `cut.py`'s defaults. The registry range `>=1.0 <2.0` excluded it.
  - *What this skill uses did not change.* It calls only `probe.py` and `color.py`. The 2.0 bump is for `cut.py`'s
    `precision` / `keyframe_snapped` meaning (ffmpeg-skill `docs/contract.md` "What 3.0 changed"); no flag of
    `REQUIRED_FLAGS` / `FLAG_ALTERNATIVES` was removed, and probe only gained `timecode`. The full suite (306 tests,
    real media included) passes against 3.0.0 on Python 3.11 with ffmpeg 6.1.1.
  - *Decision.* `adapter.KNOWN_CONTRACT_VERSIONS = ("1.0", "2.0")`; the dependency range becomes `>=1.0 <3.0`, and
    the contract's `ffmpeg_skill` block lists `contract_versions_known`. `ffmpeg_skill.contract_version` stays the
    oldest (1.0), which the range starts from. Still reported, never gated on (ADR-19).
  - *Python 3.9 stays supported here* (`requires-python >=3.9`): this skill's own code needs nothing newer. ffmpeg-skill
    3.x no longer supports it (its doctor reports `ok: false`), so the CI job on Python 3.9 now runs against the
    last 2.x, 2.24.1 (`a2c8433`), pinned by commit. A new job runs 3.0.0 (`1dc2f52`, pinned) on Python 3.11. The
    main jobs (Linux, Windows, macOS) already ran Python 3.11 with FFmpeg ≥ 6.1.
  - *Re-pin.* `dependencies[].version_range` changes, so a consumer holding a copy of this contract
    (video-production-agent) sees the new range when it re-pins.
- **ADR-22 `OCIO_TRANSFORM`: an OpenColorIO transform baked to a 3D LUT by the optional PyOpenColorIO, applied by
  ffmpeg-skill.** ECOSYSTEM_ROADMAP.md step 5. Camera log → ACEScct → Rec.709 or Rec.2100 PQ is a colour-space
  conversion that a config defines exactly; asking the caller to find or bake a `.cube` for it moved the colour
  science out of any record.
  - *Request.* `config` (an `ocio://` built-in, default `ocio://studio-config-latest`, or a `.ocio` path resolved
    through the LUT path policy), `source_space`, then either `destination_space` or `display` + `view`, an optional
    `look`, `lut_size` (17 / 33 / 65 / 129, default 65), `output_tags` (a RETAG target) and the encode parameters.
    Validation needs no OpenColorIO: one destination, display with view, a config that is a built-in or `.ocio`.
    Names are checked against the loaded config at planning (`INVALID_REQUEST` listing the config's own names).
  - *Output tags are known, never guessed.* `ocio.KNOWN_DESTINATION_TAGS` maps the unambiguous destinations
    (`Rec.1886 Rec.709 - Display`, `Gamma 2.4 Encoded Rec.709`, `Camera Rec.709` → bt709; `Rec.2100-PQ - Display` →
    bt2020-pq; `Rec.2100-HLG - Display` → bt2020-hlg). Any other destination needs `output_tags`; one that
    contradicts the table is refused. sRGB, P3 and log destinations have no RETAG target with their transfer, so
    they are never tagged silently as BT.709.
  - *Who touches media.* PyOpenColorIO only evaluates the transform on the cube's nodes (red fastest, clamped to
    [0, 1], six decimals) at planning, also under dry-run, and nothing is written then. At execution the cube is
    written to the work directory (its bytes checked against the hash the identity used) and applied by
    ffmpeg-skill/color `--lut` (with `--force` on an HDR-tagged input, which `--lut` otherwise refuses); when the
    `--lut` output (BT.709-tagged H.264) does not carry the destination's tags, `--retag <output_tags>` rewrites
    them. The tags are then measured on the result (`destination_tags_mismatch` otherwise), as RETAG's are.
  - *Identity and provenance.* The identity replaces `config` with the config's serialized sha256 and adds the
    baked LUT's sha256, so the `latest` alias or a moved `.ocio` file does not change it and a library or config
    update that changes the numbers does. Provenance (`ocio`) records the library version, the config (built-in
    requested and resolved name, or path and file sha256; name, profile version, serialized sha256), the transform,
    `lut_size`, `lut_sha256`, how many LUT values were clamped, and `accuracy`. A `.ocio` file's external LUT files
    are not hashed one by one; their effect is in `lut_sha256`.
  - *Accuracy is measured on every bake.* The cube is interpolated tetrahedrally (lut3d's `interp=tetrahedral`,
    which ffmpeg-skill asks for) and compared with OpenColorIO's own CPU processor on a test chart: the 24
    ColorChecker patches at −2..+2 stops plus an 18 % grey ramp from −10 to +8 stops, scene-linear Rec.709 encoded
    into the source space by the config (points outside [0, 1] after encoding are left out). Measured with OCIO
    2.6.0's studio config (max error in 1/1023): S-Log3 S-Gamut3.Cine → Rec.709 ACES 2.0 SDR 6.98 at 33³, 2.73 at
    65³, 0.83 at 129³; ACEScct → same 16.8 / 5.09 / 0.84; ARRI LogC4 → Rec.2100 PQ 1000 nits 5.13 / 1.89 / 0.90;
    V-Log → ACEScct 2.73 / 0.74 / 0.17. So **within 1/1023 holds at `lut_size` 129** for the ACES display
    transforms, and is what the test asserts (the cube read back from its text). 65 stays the default (8 MB of
    text against 58 MB) and reports its own error; ffmpeg's `.cube` reader takes no 1D shaper, which is what would
    let a smaller cube reach it. A transform that clips (a colorimetric conversion to a smaller gamut) is not
    within 1/1023 at any size near the clip, because the cube interpolates between clamped nodes.
  - *Optional extra.* `pip install 'color-grading-skill[ocio]'` (`opencolorio>=2.3`, the first release with the
    built-in studio config). Without it, `doctor` reports `checks.ocio.status: missing` with the install hint,
    `python:PyOpenColorIO` unsupported, `OCIO_TRANSFORM` in `unavailable_operations` (status `degraded`, never
    `fail`), and every other operation unchanged; a run of `OCIO_TRANSFORM` is `TOOL_ERROR`
    (`reason: ocio_missing`), any other run is unaffected. CI installs the extra on every job, so its tests run.
  - *ffmpeg-skill behaviour that has no flag.* Two things OCIO_TRANSFORM depends on are behaviour, not flags
    (ADR-19 gates only on flags): (1) `--retag` rewrites an H.264 stream's own colour description only where the
    checkout declares `bsf:h264_metadata` (from 1.x; 0.12.3 only writes the container hint, which ffprobe does not
    report over the stream's VUI). The adapter reads that capability from the contract
    (`detected.retag_rewrites_h264_tags`, capability `ffmpeg-skill/color:retag-h264-vui`). On such a checkout no
    destination's tags can be guaranteed: its `--lut` output is tagged by the FFmpeg build (all three BT.709 with
    FFmpeg 6.1, only the matrix with 8.0, measured in CI) and its `--retag` cannot repair that. So `OCIO_TRANSFORM` is
    unsupported there: `doctor` lists the capability as missing, and a run (or a plan) is refused before anything is
    encoded (`TOOL_ERROR`, `retag_cannot_rewrite_tags`) instead of writing a file that fails validation or, worse,
    one tagged by chance. Every other operation is unaffected. (2) Before ffmpeg-skill
    3.1.6, `--lut` converts to RGB with BT.709 and back with BT.601 (its #410): every LUT, a baked one included,
    lands on shifted colours. `doctor` warns for such a version; CI's main jobs run ffmpeg-skill main.
  - *Licences.* PyOpenColorIO is BSD-3-Clause (the wheel's LICENSE, "Copyright Contributors to the OpenColorIO
    Project"). The built-in ACES configs are compiled into that library and ship under the same file; nothing of
    them is vendored here, and a baked LUT is derived data. The upstream OpenColorIO-Config-ACES repository (which
    publishes the configs, BSD-3-Clause as far as this repository knows) could not be read from the session that
    wrote this ADR, so its LICENSE file was not re-read here.
  - *Contract.* New operation and Capability id `color.ocio_transform` (EXPERIMENTAL), `optional_dependencies`
    (`ocio`), an `ocio` block; `dependencies` (ffmpeg-skill only, required) unchanged. Additive: a consumer pinned to
    the previous contract sees one operation more when it re-pins.
- **ADR-23 `MATCH_CAMERA`: fit camera B to camera A from a frame pair, write the fit as a 3D LUT, measure the
  residual ΔE2000 on the written file.** ECOSYSTEM_ROADMAP.md step 6. In a multicam edit the cameras' colour
  differences show at every cut; matching them by hand per camera is slow and unrecorded.
  - *Request.* The operation's input is camera B. `reference_path` (camera A, resolved through the *input* path
    policy and identified by its sha256), `at` / `reference_at` (the two frame times, seconds), `model` (`curves`,
    default, or `matrix`), optional `patches` (up to 64, a string `x,y,w,h[/x,y,w,h];...` in fractions of
    the frame, ADR-26), `lut_size` (17 / 33 / 65, default 33) and the encode parameters. The caller names the
    frames and, optionally, the patches; nothing here looks for a matching shot (`not_provided` says so).
  - *Who touches media.* ffmpeg-skill/look (`--at T --width 320 --no-timecode`) extracts the two frames as PNG; this
    skill decodes them (`match.read_png`: 8/16-bit RGB(A), the five PNG filters), fits, writes the cube, and
    ffmpeg-skill/color `--lut` applies it. After validation, look extracts the same frame of the written file and
    the residual is measured on it, so `after` is what the encoded output achieved, not what the fit predicted
    (`fit` is reported too). look is an optional tool of the adapter (`OPTIONAL_TOOL_FLAGS`): a checkout without
    it, or without one of its flags, loses only MATCH_CAMERA (capability `ffmpeg-skill/look`).
  - *Sampling.* Whole frame: the mean of every 8×8 block of the 320-px-wide analysis frame, both frames the same
    size (`frame_size_mismatch` otherwise: different framing needs stated patches). Patches: the mean of each
    rectangle, one in each frame, so the two cameras may frame the subject differently.
  - *Models.* `curves`: per channel, the samples are binned (64 bins over the sampled range) and fitted
    non-decreasing by pool-adjacent-violators; outside the sampled range the least-squares line continues the curve.
    It corrects gain, offset, gamma and tone-curve differences per channel. `matrix`: least squares of a 3×3 matrix
    plus offset (needs 4 distinct samples), which also corrects cross-channel mixing (a different camera gamut). A
    free-form 3D LUT fit was not added: from one frame pair it would fit noise wherever samples are sparse.
  - *Residual.* CIEDE2000 (Sharma, Wu & Dalal 2005, checked against their published test pairs) between
    index-aligned samples, RGB read as BT.709 primaries with the BT.1886 EOTF (γ 2.4) and D65 as the matrix's own
    white; mean, p95 and max, `before` (input vs reference), `fit` (predicted) and `after` (measured on the output).
    Reported, never judged: no threshold fails the run.
  - *Result on the fixture.* `camera_b` is `camera_a` (testsrc2) with R ×0.86, G ×0.97 + 10, B ×0.90 + 6 in 8-bit
    RGB. Whole frame, curves: ΔE2000 mean 5.19 before, 0.50 after (p95 1.75); 9 patches, matrix: 4.81 → 0.25. The
    test asserts before > 3 and after < 2 (the roadmap's "within ΔE 2 on average").
  - *Needs ffmpeg-skill ≥ 3.1.6.* Before it, `--lut` reads RGB with BT.709 and writes back with BT.601 (its
    #410), while look's frames show the BT.709 RGB, so the fit is not what `--lut` applies (measured on 0.12.3:
    after 4.96, worse than before). This is behaviour without a flag, so it is read from the version: capability
    `ffmpeg-skill/color:lut-bt709-legs` (`detected.lut_legs_bt709`; unknown when the version cannot be read).
    Where it is false, MATCH_CAMERA is unsupported in doctor and refused at planning. OCIO_TRANSFORM keeps the
    doctor warning only (ADR-22): its LUT is the exact transform of whatever RGB `--lut` sees, like LUT_APPLY's.
  - *HDR input* is refused (`hdr_input`): the frames are display-referred SDR; run HDR_TO_SDR or OCIO_TRANSFORM first.
  - *Identity and reuse.* The identity uses the reference's sha256 in place of its path; the cube, the frames and
    the residuals are written at execution, kept in the work directory, and restored from the manifest on reuse.
  - *Contract.* New operation and Capability id `color.match_camera` (EXPERIMENTAL); `dependencies[0].optional_tools`
    (`ffmpeg-skill/look`); `ffmpeg_skill.flags_used.look`; "shot matching" in `not_provided` becomes "automatic shot
    matching". Additive for a pinned consumer.
- **ADR-24 `LEGALIZE`: a broadcast-legal soft clip as a LUT over ffmpeg-skill's float RGB; the out-of-range shares
  are not measured, because nothing this skill may call can measure them.** ECOSYSTEM_ROADMAP.md step 7, left
  unchecked: the clip is implemented and verified, the report of both shares is not.
  - *Request.* `profile` `bt709` (default: RGB 0..1, i.e. 8-bit luma 16–235, chroma 16–240) or `ebu-r103` (EBU
    R 103: RGB −5..105 %, luma −1..103 %; RGB limited to −1..103 % so luma stays inside), `knee` (0.8–0.99, default
    0.95), `lut_size` (33 / 65) and the encode parameters. HDR input is refused (`hdr_input`).
  - *How.* ffmpeg-skill 3.1.6 on converts the frame to float RGB (`gbrpf32le`) for `--lut`, so super-whites arrive
    above 1 (neutral 8-bit Y′ 255 is 1.091). The LUT declares `DOMAIN_MAX 1.25` and maps each channel through a
    tanh shoulder from the knee that approaches the profile's upper limit without reaching it; below the knee it is
    unchanged. Measured constraints of FFmpeg's `.cube` reader and lut3d: `DOMAIN_MIN` is taken into the scale but
    no offset is applied (a [−0.25, 1.25] domain shifted every value), and input below 0 is clamped, so sub-blacks
    are clipped hard at RGB 0 (an illegal colour clamped in RGB can come out with higher luma: the fixture's Y′ 4
    pixels with strong chroma leave at Y′ ≥ 33). ffmpeg-skill's way back (`scale=out_color_matrix=bt709:out_range=tv`)
    clamps float RGB to 0..1 (neutral Y′ 250 leaves at 236 with no LUT at all), so `ebu-r103`'s headroom above
    100 % is never written: both profiles end inside BT.709's range; they differ only in the shoulder. Before 3.1.6
    the legs are 8-bit with mismatched matrices (ADR-23), so LEGALIZE needs `ffmpeg-skill/color:lut-bt709-legs`
    and is unsupported (refused) on older checkouts.
  - *Result on the fixture* (`super_white.mp4`: luma ramp 4..254, smooth chroma ±60): luma out of 16–235 on 12.2 %
    of samples before, 0 after; chroma 0 before and after. Measured by the test itself from the written file
    (rawvideo), every frame. A harsher picture, testsrc2 with chroma ×1.3: luma 17.6 % → 0, chroma out of 16–240
    43.6 % → 1.26 %: on hard saturated edges the 4:2:0 downsampling in ffmpeg-skill's leg (and the encode) overshoots
    an RGB-legal picture, which a LUT before that leg cannot prevent.
  - *Why the shares are not reported.* The roadmap asks for the share of pixels out of range before and after.
    That needs per-pixel Y′CbCr (or signalstats' BRNG). ffmpeg-skill exposes signalstats only as luma levels
    (`--correct`'s and `probe --analyze`'s min / max / percentiles, no share), look returns RGB PNGs (super-whites
    already clipped), and this skill runs no ffmpeg of its own (docs/architecture.md). Provenance says so
    (`legalize.out_of_range_share: {measured: false, reason}`) instead of a number.
  - *Proposal for ffmpeg-skill* (not changed here; for Velisa's ROADMAP): a legal-range measurement, e.g.
    `probe.py --analyze` (or `color.py --lut`'s `measurements`) reporting the share of luma samples outside
    16–235 / 64–940 and chroma outside 16–240 / 64–960 (signalstats `stat=brng` plus a plane histogram), and an
    optional legal-range clamp after the 4:2:0 conversion of `--lut` / `--correct`. With the first, LEGALIZE can
    report both shares and step 7 can be ticked.
  - *Contract.* New operation and Capability id `color.legalize` (EXPERIMENTAL). Additive.
- **ADR-25 Alpha: refused where it would be lost; alpha-aware grading is not possible through ffmpeg-skill/color
  today.** ECOSYSTEM_ROADMAP.md step 8, left unchecked.
  - *What was measured.* ffmpeg-skill/color `--lut` (main, 3.2.0) on a ProRes 4444 source with alpha
    (`yuva444p12le`) completes with `"verified": true` and writes H.264 `yuv420p`: the alpha plane is gone and
    nothing says so. Every re-encoding mode (`--to-sdr`, `--lut`, `--correct`) goes through `x264_args`, and the only
    other codec its `--codec` offers there is ProRes 422 HQ, also without alpha. This skill's own validation did not
    notice either (LUT_APPLY checks `pix_fmt == yuv420p`).
  - *Decision.* A source whose pixel format carries alpha (`yuva*`, `gbrap*`, `ya8`/`ya16`, `rgba`/`bgra`/`argb`/
    `abgr` and their 64-bit forms; `executor.has_alpha`) is refused at planning, also under `plan`, for every
    operation that re-encodes through color (`REENCODING_OPERATIONS`: HDR_TO_SDR, LUT_APPLY, PRIMARY_CORRECTION,
    OCIO_TRANSFORM, MATCH_CAMERA, LEGALIZE): `UNSUPPORTED_FORMAT`, `reason: alpha_not_preserved`, naming the ops.
    RETAG and STRIP_DOVI are stream copies and keep it (measured: RETAG on the ProRes 4444 fixture keeps
    `yuva444p10le`); any output of an alpha source that has lost the alpha fails validation (`alpha_lost`), the same
    rule as the existing subtitle-stream check. This is a behaviour change for an alpha source: such a run used to
    complete with a flattened H.264 file and now refuses; a caller that wants the fill without alpha says so by
    exporting it first (ffmpeg-skill/export).
  - *Why the Done when is not met.* "A premultiplied fixture's alpha is bit-identical after a LUT, and edge pixels
    have no dark fringe" needs a colour path that carries alpha: split the alpha, grade the colour (un-premultiplied
    first when the source is premultiplied), re-premultiply, and write a codec with alpha. Doing that here would mean
    writing ffmpeg filter graphs of this skill's own (alphaextract / unpremultiply / alphamerge / premultiply),
    which crosses the ffmpeg-skill boundary (docs/architecture.md). ffmpeg-skill/export already writes alpha
    masters (prores4444, qtrle, vp9-alpha, hap-alpha, `--alpha-mode straight|premultiplied`) but grades nothing.
  - *Proposal for ffmpeg-skill* (not changed here; for Velisa's ROADMAP): an alpha-preserving `color.py` path:
    `--codec prores4444` (or `--keep-alpha`) for `--lut` / `--correct`, grading the colour planes only, with
    `--alpha-mode straight|premultiplied` un-premultiplying before and premultiplying after, and an `alpha`
    verification step like export's (alpha compared frame by frame). With it, this skill would add the flag to
    REQUIRED/optional flags, lift the refusal for those operations, and test the bit-identical alpha and the
    fringe-free edge.
- **ADR-26 Contract compatibility with a pinned consumer: the pinned blocks stay byte-identical; parameters keep the
  four scalar types.** video-production-agent pins this contract (its `contract_0.4.0.json`) and, per its ADR-045,
  treats a change in `schema`, `skill_id`, `kind`, `request`, `response`, `provenance`, `execution`,
  `schema_versions`, `errors`, `lut`, `color_space` or `hdr_sdr` as a protocol change that makes this skill
  unavailable until it re-pins; its `check_contract` also refuses any operation parameter whose `type` is not
  `number` / `integer` / `string` / `boolean`. Steps 5 and 6 (#31, #32) had changed `request.shape`'s example type
  list, `provenance.per_operation` and `execution.executables`, and declared MATCH_CAMERA's `patches` as an array
  (`check_contract`: "parameter 'patches' has no typed schema"), so main stopped being usable by the agent.
  - *Decision.* Those blocks are restored to exactly what 0.4.0 published, and
    `tests/test_contract.py::test_blocks_a_consumer_pins_are_unchanged_since_0_4_0` compares them with a snapshot of
    the agent's pinned contract (`tests/fixtures/contract_0.4.0_pinned_blocks.json`). New facts go into new keys:
    `provenance_extensions`, `request_types` (every accepted operation type and alias), and the existing additive
    blocks (`ocio`, `optional_dependencies`, `ffmpeg_skill.flags_used.look`, `dependencies[0].optional_tools`).
  - *`patches` becomes a string* with a grammar: `;`-separated patches, each `x,y,w,h` (the same rectangle in both
    frames) or `x,y,w,h/x,y,w,h` (reference / input), fractions of the frame; normalised to a canonical spelling so
    two spellings have one identity. Declared as `type: string` with `format`. A test asserts every parameter of
    every operation has one of the four types.
  - *Effect on the agent.* With this, the agent's pinned 0.4.0 contract shows no breaking drift and passes
    `check_contract` against the live contract (checked with the agent's own functions); the new operations appear as
    capability it does not use until it re-pins.
- **ADR-27 Releases: the version is resolved by release-drafter v7 in dry-run mode.** After v0.4.0 every push to
  main (#26 to #36: OCIO_TRANSFORM, MATCH_CAMERA, LEGALIZE, the contract fix and the alpha guard among them) ran the
  release workflow to a green finish without a release: the log says "No version could be resolved: nothing to
  release." with `RESOLVED_VERSION` empty, although the merged PRs carry `feature` / `fix` / `chore` labels and the
  config has `default: patch`. The resolver step was release-drafter v6.4.0 with `disable-releaser: true`, which
  skips resolving a release at all and so never sets `resolved_version`. v0.3.0 and v0.4.0 were released only
  because their versions were bumped by hand (the "respecting the manual bump" path), which does not read the
  resolver.
  - *Decision.* The release workflow resolves with release-drafter v7.10.0 and `dry-run: true` (pinned by SHA, the
    same step the sibling video-editing-skill uses), which computes `resolved_version` and creates no release. The
    labeler workflow kept v6.4.0 at first (`disable-releaser: true` there only means "label, don't draft", which
    works). It moved to v7.10.0 with the pin bump in #35, as the separate `release-drafter/autolabeler` action: in v7
    the main action no longer applies labels and has no `disable-releaser` input, so it would try to draft a release
    from the labeler job.
  - *Guard.* On the auto-bump path an empty resolved version with commits after the latest tag now fails the job
    (`::error::`) instead of ending green, so a resolver that stops producing a version is seen at once.
  - *Effect.* The first push after this resolves the labels merged since v0.4.0; the `feature` PRs make it a minor
    release, 0.5.0.
- **ADR-28 `HDR_TO_SDR`: the default curve follows the input's transfer (mobius for HLG).** Until 0.5.0 an omitted
  `tonemap` was always `hable`. A real test on a phone's HLG clip (HEVC, HLG BT.2020, 1080p60) showed `hable` makes
  HLG camera footage clearly too dark; `mobius` looked natural; `hable` with a 400-nit peak was still worse than
  `mobius`. The owner approved `mobius` as the HLG default.
  - *Decision.* When the request gives no `tonemap`, the executor picks it from the transfer the operation's input
    carries: `arib-std-b67` (HLG) → `mobius`; `smpte2084` (PQ) and anything else → `hable`, unchanged. An explicit
    `tonemap` always wins, including `hable` on HLG. The input transfer is the source's measured `color_transfer`,
    followed through `STRIP_DOVI` (stream copy, tags kept) or set by a `RETAG`; after any other operation it is not
    known before running, so the general default (`hable`) applies and `input_transfer` is recorded as `null`.
    `force` on an untagged source ("treat as PQ") therefore keeps `hable`.
  - *A fixed rule, not a judgement.* It reads one measured tag, never the picture, so ADR-3 still holds: the caller
    decides whether to tone-map, and can always name the curve.
  - *Record.* Plan steps, results and each provenance operation carry `tonemap_selection {tonemap, rule, input_transfer}`
    with `rule` = `request` | `hlg_default` | `default`; `parameters.tonemap` holds the curve that ran. The curve is
    resolved before identities are computed, so an HLG default and an explicit `mobius` share an identity and an
    intermediate written by 0.5.0 with `hable` is not reused.
  - *Contract.* `operations[HDR_TO_SDR].parameters.tonemap.default` becomes `null` with the rule in its description,
    and a new key `hdr_sdr_defaults` states the rule. The pinned `hdr_sdr` block (ADR-26) is left byte-identical:
    its `tone_mapping_selection` still holds for every explicit curve, and video-production-agent treats a change there
    as breaking. A parameter's `default` is outside its breaking set, and its `check_contract` still passes (the
    parameter stays a `string`). The agent sends no `tonemap` for HDR_TO_SDR, so its HLG sources get `mobius`.
  - *Release.* A patch release (0.5.1), inside the agent's accepted 0.5.x range.
- **ADR-29 `LEGALIZE`: the out-of-range shares are measured by ffmpeg-skill/check `--levels`, and the clamp after
  4:2:0 runs in the same encode through color `--legalize`, where the located ffmpeg-skill has them.** Completes
  ECOSYSTEM_ROADMAP.md step 7; supersedes ADR-24's "the shares are not reported" and its proposal, which ffmpeg-skill
  3.2.3 implemented.
  - *Detected, not assumed.* `check` is an optional tool (`input`, `--platform`, `--no-loudness`, `--levels`,
    `--levels-window`, `--levels-tolerance`, `--json`; capability `ffmpeg-skill/check:levels`) and `--legalize` an
    optional flag of `color` (`ffmpeg-skill/color:legalize`), both read from the checkout's contract like every other
    flag (ADR-19). Neither is in any operation's required capabilities: on a checkout without them LEGALIZE runs as
    before (the soft clip alone; `out_of_range_share: {measured: false, reason}`), and doctor lists both under
    `optional_capabilities`. Only a detected `supported` turns one on; `unknown` keeps the fallback.
  - *One encode.* `color --lut <soft clip> --legalize <profile>`: the soft clip keeps highlight detail, and the clamp
    after the 8-bit 4:2:0 conversion catches what the chroma downsampling and the RGB legs put back out. color measures
    its own file (`legal_levels` step, at most 1 % of one frame's luma or chroma samples out); a failed or missing step
    is a `VALIDATION_ERROR` (`legal_levels_failed`), never a pass. The step and color's residual shares are recorded
    as `legalize.clamp {applied, tool, standard, verification, residual}`. The profile names are the same in both
    skills (`bt709`, `ebu-r103`). The clamp changes the pixels, so its profile (or `null`) joins the identity.
  - *Before and after.* The operation's input and the written file are each measured with `check --levels <profile>
    --levels-tolerance 100 --platform custom --no-loudness`: the row never fails the call, other rows are the cheap
    ones, and the `levels` document is read even when check exits non-zero for another row. Provenance keeps per file
    `frames`, `bit_depth`, `legal`, `out_of_range_pct {y, u, v, chroma}`, `brng_pct`, `worst_frame`, `min`, `max`,
    `window_seconds`, `windows` (10 s); `out_of_range_share.measured` is true only when both files were measured (an
    RGB picture is reported unmeasured with check's reason). The shares are reported, not judged: the clamp's step
    judges the written file.
  - *Result on the fixture* (`super_white.mp4`): `bt709` luma 12.1597 % → 0 %, chroma 0 → 0; `ebu-r103` (luma 14–241,
    chroma 5–251) luma 8.7776 % → 0 %. The test counts every sample of both files itself (rawvideo) and checks the
    reported shares against its own count.
  - *Reuse.* A manifest written before the shares and the clamp were recorded is not reused: the step runs again
    rather than report less. A reused report that is unmeasured (no check then, or a file it could not measure) is
    measured again when check is available now, and the manifest keeps the new report.
  - *A measurement that cannot finish* (check times out or fails to start) leaves the shares `measured: false` with
    the reason; the already validated file is still delivered. A cancellation stays a cancellation.
  - *Contract.* Additive: `legalize` (what the clamp and the measurement need and record), `optional_capabilities`,
    `ffmpeg_skill.optional_flags`, `ffmpeg_skill.flags_used.check`, `dependencies[0].optional_tools` gains
    `ffmpeg-skill/check`, and `provenance_extensions` describes the new `legalize` keys. The pinned blocks (ADR-26) are
    unchanged.
- **ADR-30 Alpha-aware grading: LUT_APPLY and PRIMARY_CORRECTION keep an alpha source's alpha through ffmpeg-skill/color
  `--keep-alpha`, where the located ffmpeg-skill has it.** ECOSYSTEM_ROADMAP.md step 8; replaces ADR-25's refusal for
  these two operations on such a checkout, and keeps it everywhere else.
  - *What color does.* `--keep-alpha` (with `--lut` or `--correct`) splits the alpha off, grades the colour at 4:4:4,
    merges the untouched alpha back and writes an alpha line (`--alpha-codec`) with the source's colour tags; its
    `alpha` step compares the written alpha with the source's frame by frame. Capability
    `ffmpeg-skill/color:keep-alpha` (flags `keep_alpha`, `alpha_codec`, `alpha_mode`), optional as in ADR-29.
  - *Line.* Chosen from the codec of the operation's input, so an editing master stays in its family: ProRes → ProRes
    4444, qtrle → qtrle, PNG → PNG, VP9 → VP9 (only `.mkv`, `.webm` is not an output format here), anything else →
    ProRes 4444. The container must hold the line (this skill does not convert containers): an alpha source in `.mp4`
    is refused (`alpha_not_preserved`, `keep_alpha.alpha_codec: null`).
  - *Still refused.* HDR_TO_SDR (color's `--keep-alpha` does not take `--to-sdr`), LEGALIZE (color refuses `--legalize`
    with `--keep-alpha`: the alpha line converts the clamped picture again; a broadcast delivery is opaque),
    OCIO_TRANSFORM and MATCH_CAMERA (their `--retag` / frame-fit steps assume the H.264 line). The refusal now carries
    `keep_alpha {available, operations, alpha_codec}`, and its message says whether to update ffmpeg-skill, rewrap
    the container, or export the fill first.
  - *Verified.* color's `alpha` step must be present and pass, else `VALIDATION_ERROR` (`alpha_unverified`,
    `alpha_lost` for `alpha_dropped`, `alpha_mismatch`); a failed `alpha` step is also read on a source this skill does
    not see as alpha. A VP9 stream with an alpha layer probes as `yuv420p` through ffmpeg-skill/probe, which reports no
    alpha field, and this skill runs no ffprobe of its own: such a source is graded without `--keep-alpha`, color's
    `alpha` step fails, and the run is a `VALIDATION_ERROR` (`alpha_lost`) whose message says to export it to ProRes
    4444 / qtrle / PNG first. Detecting it up front needs an alpha field in ffmpeg-skill/probe (proposal).
  - *Encoder.* An alpha grade requires its line's encoder (`encoder:prores_ks`, `qtrle`, `png`, `libvpx-vp9`) in place
    of `encoder:libx264`; doctor reports those encoders.
  - *Precision.* color splits the alpha at 8 bits. A deeper source alpha (ProRes 4444's 10 / 12 / 16-bit, `gbrap16`,
    `rgba64`) comes back within one 8-bit code, which color's own step accepts (2 of 255). It is not refused: ProRes
    4444 is the usual alpha master and one 8-bit code is below what a composite shows; it is recorded instead
    (`alpha.precision_bits: 8`) and named in a warning. Bit-identical alpha holds for 8-bit alpha (qtrle, PNG, rgba). The output must store an alpha plane, and the
    `yuv420p` check of LUT_APPLY does not apply to an alpha line. Results and provenance carry `alpha {kept,
    precision_bits, alpha_codec, alpha_mode, source_colour, tool, verification}`; plan steps carry `alpha {keep_alpha, alpha_codec,
    alpha_mode}`.
  - *Measured.* qtrle fixture (one straight colour under a 0..255 alpha ramp), LUT: the alpha is bit-identical in every
    frame and the graded colour is one colour from transparent to opaque pixels (no fringe). ProRes 4444 fixture
    (10-bit alpha): within one 8-bit code in every sample (color's split runs at 8 bits). A chain LUT_APPLY →
    PRIMARY_CORRECTION keeps it bit-identical.
  - *Not done.* A premultiplied source is not un-premultiplied before the grade: color takes every source as straight
    (`--alpha-mode` only describes the written colour), so this skill writes `straight` and records
    `source_colour: taken as straight`. The roadmap's premultiplied case needs an un-premultiply step in ffmpeg-skill
    (proposal: `--alpha-mode premultiplied` on the input side, un-premultiply before the grade and premultiply after).
    Also measured: on an RGB alpha source (qtrle / PNG) color's `--lut` legs write BT.709 limited-range Y'CbCr that the
    RGB line reads back with another matrix on FFmpeg 6.1 (an identity LUT moved 0x7f3ebf to 0x7a2fba; `--correct` is
    exact there). The alpha and the absence of a fringe are unaffected; LUT_APPLY on an RGB alpha source adds a warning
    naming it (proposal for ffmpeg-skill: convert back with the matrix the legs wrote).
  - *Contract.* `alpha` keeps `preserved_by`, `refused_for` (the list for a checkout without `--keep-alpha`),
    `pix_fmt_pattern`, and gains `keep_alpha`; `provenance_extensions` lists `alpha`. Not a pinned block (ADR-26). For
    a caller this is a behaviour change on a capable checkout: an alpha source that exited 6 now completes, in the
    alpha line.
- **ADR-31 Reuse: the cache identity includes this skill's version, and an alpha grade is reused only from an entry
  that recorded the alpha as kept; `hdr_sdr.tone_mapping_selection` says what the executor does.** An addendum to
  ADR-28 and ADR-30 found after 0.6.0.
  - *Problem.* An operation's identity was its type, effective parameters, input identity, the tool versions and the
    source's sha256, not this skill's version, so a 0.6.0 run looked up intermediates a 0.5.x run had written under the
    same identity. `_reusable` asked a reused LEGALIZE entry for its clamp record (ADR-29) but asked a LUT_APPLY /
    PRIMARY_CORRECTION entry for nothing about the alpha. A VP9 stream with an alpha layer probes as `yuv420p`
    (ADR-30), so 0.5.x graded it without `--keep-alpha` and without color's `alpha` step being read, and wrote an
    opaque intermediate; 0.6.0 reused that file as if it had passed 0.6.0's checks.
  - *Decision: version in the identity.* `OperationGraph.identities` adds `skill_version` (the package `VERSION`) to
    every operation's hashed record. An intermediate written by any other version, older or newer, never matches, so
    a release always re-runs what an earlier one wrote with other checks or less provenance. The manifest already
    carried `skill_version`; nothing else in its shape changes. The cost is one re-run per operation after an upgrade.
  - *Decision: alpha record required where the alpha is kept.* When this run grades with the alpha kept (an
    `alpha_line` is set: LUT_APPLY / PRIMARY_CORRECTION on a source seen as alpha, with `--keep-alpha` detected),
    an entry is reused only if its manifest has an `alpha` record with `kept: true` and a passing `verification`
    (color's `alpha` step, `ok: true`), and its artifact's `pix_fmt` has an alpha plane; otherwise it is run again (and then verified by color's `alpha` step as on a first run). A source that
    was opaque has no alpha line, so nothing about alpha is asked of its entries and they are reused as before. Every
    other operation on an alpha source is refused before planning (ADR-25, ADR-30) and never reaches reuse.
  - *Not changed.* A source with an alpha layer this skill cannot see (VP9 probed as `yuv420p`) is still graded
    opaque and caught by color's failed `alpha` step on the run that writes it (ADR-30); within one version such an
    entry only exists if that step passed or was not reported. Detecting it up front still needs an alpha field from
    ffmpeg-skill/probe.
  - *Contract wording.* `hdr_sdr.tone_mapping_selection` said "always an explicit typed parameter ... from the caller;
    this skill never picks one automatically", which ADR-28 made untrue: with no `tonemap`, HLG selects `mobius` and
    PQ or any other or unknown transfer selects `hable`. The line now says so (an explicit curve always wins; else the
    fixed rule on the input's measured transfer, detailed in `hdr_sdr_defaults`; never chosen from the picture).
    `hdr_sdr` is a block a consumer pins (ADR-26), so this is a deliberate one-line change to it: a consumer holding
    the 0.4.0 copy sees drift there until it re-pins. `tests/test_contract.py` still compares every other key of every
    pinned block with the 0.4.0 snapshot and pins the new wording against the constants that implement the rule.
  - *Tests.* `tests/test_reuse.py`: the identity changes with the skill version and a manifest another version wrote
    is not reused; LUT_APPLY / PRIMARY_CORRECTION entries without a kept alpha record (missing, `kept: false`,
    malformed, without a passing verification, or an opaque `pix_fmt`) are not reused where the alpha is kept, one with it is, and an opaque source's
    entry still is.
  - *Release.* A patch release (0.6.1).
- **ADR-32 Releases: a Dependabot pull request cannot raise the release level; v1.0.0 stays.** #35 (a
  Dependabot bump of release-drafter from 6.4.0 to 7.10.0) was merged carrying the label `major`. Dependabot puts
  that label on a pull request whose *dependency* moved a major version, as soon as a label named `major` exists
  in the repository; `.github/release-drafter.yml` maps the same label to a major bump of this product, so the
  release workflow resolved 1.0.0 from 0.6.1 and tagged it, although the only change was a CI pin.
  - *Decision.* Before release-drafter resolves the version, `release.yml` runs
    `.github/scripts/dependabot_release_labels.py`: on every pull request authored by `dependabot[bot]` it removes
    `major`, `breaking`, `minor`, `feature` and `enhancement` unless the newest `labeled` event for that label was
    made by a person. Pull requests by anyone else are not touched, so the rule in `release-drafter.yml` stands: a
    major release happens only when a person applies the label, on a Dependabot pull request too. The job's token
    has `pull-requests: write` for this; a failed API call fails the release instead of resolving from labels that
    were not checked.
  - *Rejected.* A `labels:` list in `dependabot.yml`: it sets Dependabot's default labels, it is not documented to
    stop the semver label added on top, and a later edit of that file would reopen the hole unnoticed. Removing the
    label when it is applied (in the labeler workflow): a missed or failed run would go unnoticed until the next
    accidental release.
  - *v1.0.0 stays.* It was not meant as a major release (no breaking change went into it), but its tag and GitHub
    Release are public, so it is kept and the 1.x line continues from it; the version files and the v1.0.0
    CHANGELOG entry are unchanged. From here on a major release happens only when a person applies the label.
  - *Tests.* `tests/test_release_labels.py`.
  - *Release.* A patch release (1.0.1).
- **ADR-33 Alpha mode and an opaque result on request: `project.source.alpha_mode` and `project.source.drop_alpha`; a
  premultiplied source is refused for a grade that keeps the alpha, because ffmpeg-skill/color cannot un-premultiply it.**
  ECOSYSTEM_ROADMAP.md step 8 (left unchecked); extends ADR-25 and ADR-30.
  - *Problem.* ADR-30 took every alpha source's colour as straight and said so only in provenance (`source_colour`). The
    request had no way to say how the colour is stored, a premultiplied source graded as straight gets wrong edges, and
    the only answer to "I want an opaque result" was to export the fill first.
  - *Detection.* An alpha plane is read from ffprobe's `pix_fmt` through ffmpeg-skill/probe (`has_alpha`, ADR-25).
    Whether the colour is premultiplied is not in anything ffmpeg-skill/probe reports, for any format. So the request
    says it: `project.source.alpha_mode`, `straight` or `premultiplied` (optional; any other value is
    `INVALID_REQUEST`).
  - *Default, and every assumption.* (1) The request always wins (`rule: request`). (2) PNG (codec `png`, also inside
    `.mov` / `.mkv`): the PNG specification defines its alpha as not premultiplied, so `straight` (`rule: format`). (3)
    Every other format (ProRes 4444, QuickTime Animation, VP9 with alpha, FFV1, ...): the stream carries no
    premultiplication flag that ffmpeg-skill/probe reports, and the colour can be stored either way (the application
    that renders the file chooses), so the format does not state it. (4) Not stated and not in the request: taken as
    `straight` (`rule: assumed`), the only colour ffmpeg-skill/color grades, with a warning naming the source, its pixel
    format and codec, the operations and the field to send. Chosen over a refusal because an alpha source sent without
    the field completed in 1.0.x (ADR-30): refusing it now would break every caller that does not know the new field (a
    major change); the warning keeps such a run working and says what was assumed. Where the mode changes nothing that
    is written (stream copies, `drop_alpha`) there is no warning. (5) `alpha_mode` on a source that shows no alpha plane
    is ignored with a warning (a VP9 alpha layer probes as `yuv420p`, ADR-30).
  - *Premultiplied: refused where the alpha is kept.* LUT_APPLY / PRIMARY_CORRECTION on a premultiplied source without
    `drop_alpha`: `UNSUPPORTED_FORMAT`, reason `alpha_premultiplied`, before anything runs (also under `plan`), on every
    ffmpeg-skill checkout; the message names the ways out (a straight-alpha source with `alpha_mode: straight`,
    `drop_alpha: true`, or a stream copy). Why it is not un-premultiplied here: color `--keep-alpha` splits the alpha
    off, grades the colour planes as stored and merges the alpha back; every grade there is a function of the colour
    alone and its only alpha-dependent step, `--alpha-mode premultiplied`, multiplies the written colour by the alpha.
    Nothing in ffmpeg-skill 3.5.0 divides by it, so un-premultiplying cannot be expressed through ffmpeg-skill, and
    running a filter graph of this skill's own would break ADR-1 and change the `execution` block that
    video-production-agent pins (ADR-26). Grading premultiplied colour as if straight is not an approximation to keep:
    it is exact only where the alpha is 0 or 1; at partial alpha an offset (lift, a LUT's black point) lifts
    transparent edges and a non-linear grade (gamma, curves, most LUTs) darkens or brightens them. Order of the
    refusals: an operation that can never keep the alpha is named first (`alpha_not_preserved`, as before), then
    `alpha_premultiplied`, then a missing `--keep-alpha` or a container without an alpha line (`alpha_not_preserved`):
    updating ffmpeg-skill would not make a premultiplied grade right. RETAG / STRIP_DOVI copy a premultiplied source
    unchanged (measured: alpha and colour bit-identical after RETAG on the premultiplied ProRes 4444 fixture).
  - *`drop_alpha`: the alpha given up only when asked.* `project.source.drop_alpha: true` (boolean, default `false`):
    every re-encoding operation on an alpha input writes the opaque H.264 line on purpose and none is refused for the
    alpha; RETAG / STRIP_DOVI still copy the stream and must still store the alpha (a
    fallback re-encode that loses it is `alpha_lost`, as without `drop_alpha`); what follows an opaque intermediate is
    opaque and gets no flag. A re-encode under `drop_alpha` whose output still stores an alpha plane is a
    `VALIDATION_ERROR` (`alpha_not_dropped`), and a failed `alpha` step for any reason other than `alpha_dropped` stays
    one.
    color `--drop-alpha` is passed where detected (`ffmpeg-skill/color:drop-alpha`, optional flag group
    `color_drop_alpha`, ADR-29's pattern); without it the same opaque line is written and color's `alpha` step, on a
    checkout that has one, reports `alpha_dropped`, which is then the expected outcome, not a `VALIDATION_ERROR`. The
    colour is kept as stored: a premultiplied source reads as composited over black, a straight one shows the colour its
    transparent pixels hold. Without `drop_alpha` an output setting that would drop the alpha is refused before anything
    runs: an output that keeps the alpha with an `expect.pix_fmt` that has no alpha plane (`alpha_not_preserved`,
    `output_id`), next to the existing refusals (an operation that cannot keep it, a container without an alpha line),
    whose messages now name `drop_alpha`.
  - *Record.* `alpha_mode_selection {alpha_mode, rule, format_states, codec}` in `plan.alpha` (with `pix_fmt`,
    `drop_alpha`, `alpha_codec`), in each re-encoding step on an alpha input, and in the `alpha` record of results and
    provenance (kept-alpha grades as before plus this key; a dropped one is `{kept: false, dropped: true, requested_by,
    tool, reported_dropped, verification, alpha_mode_selection}`; plan step `alpha {keep_alpha: false, drop_alpha: true,
    drop_alpha_flag}`). A reused intermediate gets this request's selection.
  - *Identity.* A re-encode of an alpha input adds `alpha_handling {alpha_line, drop_alpha}`: a kept and a dropped
    alpha are different files. The mode and its rule are not in it: a stated and an assumed `straight` write the same
    file (measured: the second run reuses the first's intermediate). An opaque source's identities do not change.
  - *Measured* (`tests/test_alpha_mode.py`, FFmpeg 6.1, ffmpeg-skill 3.5.0; sources made with lavfi: one fill colour
    0x4a7fb5 under three bands of alpha 0 | 128 | 255, straight and premultiplied, as ProRes 4444 tagged BT.709 and as
    qtrle, each with an opaque twin of the same colour). Straight, alpha kept: the qtrle alpha is bit-identical in every
    sample, the ProRes 4444 alpha within one 8-bit code (color's 8-bit split, ADR-30), the bands still read 0, 128 and
    255, and the graded colour is one colour under every band (within 1.5 codes: no fringe). Opaque pixels against the
    same grade through the non-alpha path on the opaque twin (H.264 4:2:0, CRF 18; tolerance 3 codes): ProRes 4444
    LUT_APPLY within 1.0, PRIMARY_CORRECTION within 2.4; qtrle PRIMARY_CORRECTION within 2.3; qtrle LUT_APPLY shows
    ADR-30's RGB-line shift (176,127,78 against 180,128,74), equal to BT.709 written and BT.601 read within 2 codes, and
    warned. Premultiplied with `drop_alpha`, LUT_APPLY: opaque `yuv420p`; the alpha-0 band is white (premultiplied
    black, inverted), the partial band the inverse of half the fill within 3 codes (6 before ffmpeg-skill 3.1.6, whose
    `--lut` legs read BT.709 and write BT.601), the opaque band equal to the non-alpha path. CI runs the same tests with
    FFmpeg 8.0.1 (Ubuntu) and 9.0.2 (macOS, Windows) against ffmpeg-skill 0.12.3, 2.24.1, 3.0.0 and main. From FFmpeg 8.0
    on, colour tags given to the ProRes encoder only as output options did not all reach the file (the transfer was
    missing), which first showed as wrong colours and failed tone maps that were the fixture's, not this skill's; the
    fixtures now put the tags on the frames as well and the fixture check asserts them. FFmpeg's own premultiply also
    rounds one code differently from 8.0 on (181 under alpha 255 becomes 180); the fixture check allows that code.
  - *Also measured, not changed here.* An RGB alpha source tagged colour space `gbr`, which FFmpeg's PNG encoder writes
    in `.mov` and `.mkv`, makes color `--keep-alpha` fail: it passes the source's tag on as `-colorspace gbr`, which
    FFmpeg's option parser does not accept ("Undefined constant ... 'gbr'"). LUT_APPLY / PRIMARY_CORRECTION on such a
    PNG source end in `TOOL_ERROR` (reported, not silent); the PNG default is checked under `plan`.
  - *Proposals for ffmpeg-skill* (not changed here; for Velisa's ROADMAP): (1) an input-side alpha mode for color
    `--keep-alpha`, e.g. `--source-alpha-mode premultiplied`: un-premultiply the colour after the split, grade, and
    premultiply in RGB before the alpha line, named in the `alpha` step. With it this skill passes the request's
    `alpha_mode`, lifts `alpha_premultiplied`, and step 8 can be ticked. (2) For an RGB line, write the colour-space tag
    under the encoder option's name (`rgb`) or leave it out, so a PNG source tagged `gbr` can be graded. (3) Drop the
    alpha before `--to-sdr`'s zscale chain, so an HDR alpha master tone-maps without depending on how the build's zscale
    treats an alpha plane.
  - *Contract.* Additive: `alpha.alpha_mode`, `alpha.drop_alpha`, `alpha.output_settings`, the reworded
    `alpha.keep_alpha.premultiplied_source`, `request_extensions` (the new source fields; the pinned `request.shape`
    is unchanged), `optional_capabilities` and `ffmpeg_skill.optional_flags` gain the drop flag, `provenance_extensions`
    names the new keys. The pinned blocks (ADR-26) are byte-identical.
  - *For a caller.* A request without the new fields runs as in 1.0.1, with the warning when an alpha source's mode is
    assumed; a premultiplied source has to be stated to be refused (unstated, it is still graded as straight, with the
    warning). A minor release (1.1.0).
