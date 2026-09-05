# color-grading-skill

Deterministic colour grading / colour correction **execution** Skill for the AI Video Production Ecosystem:
HDR (PQ/HLG, BT.2020) to SDR BT.709 tone mapping with an explicit curve, application of a 3D `.cube` LUT, colour-tag
retagging (BT.709 / BT.2020 PQ / BT.2020 HLG / BT.601) without a re-encode where possible, and Dolby Vision RPU
removal, executed as a typed **operation graph** through [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill),
with validated artifacts and provenance out.

**color-grading-skill is NOT an AI agent.** It contains no LLM, no prompt, no reasoning, no decision, no automatic
look or LUT selection, no "does this look cinematic" judgement. It executes what a typed request says, refuses
everything else, and reports what it observed.

```text
color-grading skill --json              # Skill / Capability / Tool contract (alias: contract --json)
color-grading doctor --json             # environment vs. contract: ffmpeg-skill, ffmpeg, capabilities per operation
color-grading validate - --json         # validate a request document, run nothing, read no media
color-grading plan - --json             # dry run: graph, tool selection, expected geometry; writes no media
color-grading run - --json              # execute (stdin: request document; stdout: exactly one response document)
```

Requirements: Python 3.9+, standard library only; an **ffmpeg-skill** checkout (0.9.1 ≤ version < 1.0, contract 1.0)
and FFmpeg (`ffmpeg` + `ffprobe`) on PATH for ffmpeg-skill. Install: `pip install -e .`

## What it is, and what it is not

| | [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) | [media-analysis-skill](https://github.com/kajisho5/media-analysis-skill) | **color-grading-skill** | [video-production-agent](https://github.com/kajisho5/video-production-agent) |
|---|---|---|---|---|
| Role | low-level media execution engine (hands) | measurement / observation (meters) | **colour grading execution** (typed colour operations) | reasoning / decision / planning / orchestration (brain) |
| Does | runs ffmpeg for colour (`color.py --to-sdr/--lut/--retag/--strip-dovi`), cut, audio, export, … | measures loudness, silence, streams, integrity | executes HDR_TO_SDR / LUT_APPLY / RETAG / STRIP_DOVI as a dependency graph, validates every artifact (measured colour tags, not assumed), records provenance | decides *whether* and *which* colour treatment, LUT or tonemap curve to apply, builds the request |
| Never | holds a project model | edits or writes media | decides which LUT/look/curve to use, looks at a frame and judges it, performs primary colour correction (exposure/contrast/saturation/white balance/gamma/lift/gain/levels/curves — ffmpeg-skill has no typed filter for these yet), converts containers, runs ffmpeg directly, accepts commands / filters | runs ffmpeg |

- **color-grading-skill ≠ media-analysis-skill.** Neither package measures *for a decision* here: colour metadata
  (`color_space`, `color_primaries`, `color_transfer`, `hdr`, `dolby_vision`) is read only to validate that an
  operation had its intended, measurable effect (STEP 4/19 of the design brief keep measurement and transformation
  separate: "input is BT.709" is a fact, "convert to BT.709" is an operation).
- **color-grading-skill ≠ ffmpeg-skill.** ffmpeg-skill runs FFmpeg per script call. color-grading-skill owns the
  *colour project model*: one source, an operation graph with deterministic identities, LUT resolution and hashing,
  intermediate management, output validation and provenance. It never calls `ffmpeg` itself: every process it
  starts is `python3 <ffmpeg-skill>/scripts/{probe,color}.py` with a typed argv.
- **color-grading-skill ≠ video-production-agent.** There is no Observation → Inference → Decision → ProductionPlan
  here. The agent decides which LUT, which tonemap curve, which retag target; this skill executes one typed
  request and says exactly what happened.

## Architecture

```text
external caller (video-production-agent adapter)
   │  JSON request  color-grading/request@1   (stdin)
   ▼
color-grading run - --json
   ├─ model.parse_request        typed validation: schema, ids, references, parameter schemas per operation type,
   │                             forbidden fields (command / argv / filter / api_key / workspace / …), enums, ranges
   ├─ security.PathPolicy        source is a regular file (symlinks resolved, optional allowed roots); LUTs resolved
   │                             through a *separate* allowed-roots policy; outputs inside the workspace, never an
   │                             input, never an existing file unless overwrite, container must match the source
   ├─ graph.OperationGraph       nodes (source, operations), deterministic topological order, cycle / unreachable
   │                             detection
   ├─ adapter.FfmpegSkill.probe  the source probed (read-only) and sha256-fingerprinted; every LUT resolved and
   │                             sha256-hashed (read-only, also under dry run)
   ├─ graph.identities           operation_id = sha256(type, parameters, input identity, tool versions);
   │                             LUT_APPLY's identity uses the LUT's sha256, never its path
   ├─ plan                       tool per node (always ffmpeg-skill/color), argv template, expected geometry
   ├─ execute (in order)         ffmpeg-skill/color → one intermediate per node, reused when the identity and input
   │                             hash match a manifest
   ├─ validate                   exists, size > 0, readable, video stream, duration/resolution unchanged, sha256,
   │                             and the operation's own measurable effect (HDR_TO_SDR: no longer HDR; RETAG: exact
   │                             tag triple; STRIP_DOVI: no Dolby Vision side data; LUT_APPLY: pix_fmt yuv420p)
   ├─ materialise outputs        copy the validated artifact's bytes to the requested path (no reformatting needed),
   │                             re-validated against `expect`
   ▼
JSON response  color-grading/response@1   (stdout, exactly one document; stderr = diagnostics)
```

Full description: [docs/architecture.md](docs/architecture.md).

## Skill / Capability / Tool

- **Skill**: `color-grading` (this package), kind `execution`, one tool `color-grading/run`.
- **Capabilities** (vocabulary of video-production-agent's CapabilityResolver): `ffmpeg-skill`, `ffmpeg`, `ffprobe`,
  `filter:<name>`, `encoder:<name>`, `bsf:<name>`. `doctor` reports each as `supported`, `unsupported` or `unknown`.
- **Tools used** (all through ffmpeg-skill's public contract 1.0): `ffmpeg-skill/probe`, `ffmpeg-skill/color`. Which
  flags are used is listed in `skill --json` → `ffmpeg_skill.flags_used` and checked against the live ffmpeg-skill
  contract by `doctor`.

## Contract

`color-grading skill --json` prints `color-grading/contract@1`: stable identifiers `skill_id`, `version`,
`tools[].tool_id`, `operations[].type`, `operations[].parameters`, `operations[].required_capabilities`,
`unsupported_operations`, `output_formats`, `lut`, `color_space`, `hdr_sdr`, `errors.codes`, `errors.exit_codes`,
`schema_versions`. Everything is derived from the tables the code runs on; there are no placeholder operations.

### Input schema (`color-grading/request@1`)

```json
{
  "schema": "color-grading/request@1",
  "project": {
    "project_id": "grade-42",
    "source": {"source_id": "raw", "path": "footage/iphone_hdr.mov"},
    "operations": [
      {"op_id": "sdr", "type": "HDR_TO_SDR", "input": "source",
       "parameters": {"tonemap": "hable", "peak_nits": 1000, "desat": 0.1}},
      {"op_id": "grade", "type": "LUT_APPLY", "input": "op:sdr",
       "parameters": {"lut_path": "luts/look_a.cube", "lut_strength": 0.8}}
    ],
    "outputs": [
      {"output_id": "graded", "operation": "op:grade", "path": "deliver/grade-42.mov", "format": "mov",
       "expect": {"width": 3840, "height": 2160}}
    ]
  },
  "options": {"reuse_intermediates": true, "timeout": 900}
}
```

- `source`: one file (a colour operation processes one clip; batch a shoot by sending one request per clip, or use
  ffmpeg-skill/batch for that orchestration). `operations`: the graph; `input` is a reference `source` / `op:<id>`,
  always exactly one per operation (no colour operation here merges two clips). `outputs`: terminal artifacts;
  `format`'s extension must equal the source's (no container conversion, ADR-8).
- Every id matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`; a reference matches `^(source|op:[A-Za-z0-9][A-Za-z0-9._-]{0,63})$`.
- Fields named `command`, `argv`, `cmd`, `shell`, `exec`, `executable`, `script`, `filter`, `filters`,
  `filter_complex`, `vf`, `af`, `ffmpeg`, `env`, `cwd`, `api_key`, `workspace` are rejected anywhere in the
  document; unknown fields are rejected everywhere.
- **LUT**: `lut_path` is required, no default: the caller decides which LUT. It is data (never a filter string),
  resolved through its own PathPolicy, checked for a `.cube` extension and a size ceiling, and sha256-hashed into
  provenance and into the operation's identity (docs/decisions.md ADR-5/6/7).
- **Tone mapping**: `tonemap` / `peak_nits` / `desat` / `force` are always explicit, typed parameters; this skill
  never picks a tonemap curve automatically, and `HDR_TO_SDR` refuses a non-HDR-tagged source unless `force: true`.
- **Retag**: `target` is one of `bt709` / `bt2020-pq` / `bt2020-hlg` / `bt601`, required, no default.

### Output schema (`color-grading/response@1`)

```json
{
  "schema": "color-grading/response@1", "skill": {"id": "color-grading", "version": "0.1.0"},
  "ok": true, "status": "ok", "dry_run": false,
  "plan": {"plan_id": "<sha256>", "graph": {"order": ["source", "op:sdr", "op:grade"]}, "steps": ["..."], "required_capabilities": ["..."], "tool_versions": {"ffmpeg-skill": "0.9.1", "ffmpeg": "6.1.1"}},
  "results": [{
    "node_id": "op:grade", "operation_id": "<sha256>", "type": "LUT_APPLY", "tool": "ffmpeg-skill/color", "status": "completed",
    "parameters": {"lut_path": "luts/look_a.cube", "lut_strength": 0.8, "crf": 18, "preset": "medium"},
    "input": "op:sdr", "input_hash": "<sha256>", "lut": {"path": ".../luts/look_a.cube", "size": 4096, "sha256": "<sha256>"},
    "artifact": {"path": ".../.color-grading/grade-42/<id16>.mov", "duration": 12.5, "width": 3840, "height": 2160, "pix_fmt": "yuv420p", "codec": "h264", "hdr": false, "dolby_vision": false, "sha256": "<sha256>"},
    "tool_commands_observed": ["ffmpeg ..."], "seconds": 8.1
  }],
  "outputs": [{"output_id": "graded", "status": "completed", "path": ".../deliver/grade-42.mov", "format": "mov", "artifact": {"sha256": "<sha256>"},
               "provenance": {"skill": "color-grading", "skill_version": "0.1.0", "tool_versions": {}, "output_hash": "<sha256>",
                              "operation_id": "<sha256>", "operations": ["output -> operation -> ... -> source, with hashes and status"], "source": {"sha256": "<sha256>"}}}],
  "tool_runs": [{"tool": "ffmpeg-skill/probe", "exit_code": 0, "seconds": 0.1, "commands_observed": []}],
  "warnings": []
}
```

Failure (any stage) is the same document shape with `"ok": false`, `"status": "error" | "cancelled"` and
`"error": {"code", "message", "retryable", "details"}`; the per-node `results` show which node failed and which
were skipped. `ok` mirrors the process exit code (0 ⇔ `ok`); `status` follows the media-analysis-skill convention.

## Supported operations

| type | parameters | ffmpeg-skill tool / flags | notes |
|---|---|---|---|
| `HDR_TO_SDR` | `tonemap` (`hable`\|`mobius`\|`reinhard`\|`bt2390`\|`clip`\|`linear`\|`gamma`, default `hable`), `peak_nits` [1, 10000] (default 1000), `desat` [0, 5] (default 0), `force` (default false), `crf` [0, 51] (default 18), `preset` (x264 preset, default `medium`) | `color --to-sdr --tonemap … --peak … --desat … [--force] --crf … --preset …` | refuses a non-HDR source unless `force: true`; output verified to no longer report `hdr: true` |
| `LUT_APPLY` | `lut_path` (required), `lut_strength` [0, 1] (default 1.0), `crf`, `preset` | `color --lut <resolved path> --lut-strength … --crf … --preset …` | a strength of exactly `0.0` behaves like `1.0` (full LUT) — ffmpeg-skill's own contract, see docs/ffmpeg-skill.md; output verified `pix_fmt == yuv420p` |
| `RETAG` | `target` (`bt709`\|`bt2020-pq`\|`bt2020-hlg`\|`bt601`, required) | `color --retag <target>` | stream copy when the container allows it; output verified against the exact `(color_space, color_primaries, color_transfer)` triple |
| `STRIP_DOVI` | *(none)* | `color --strip-dovi` | HEVC only; stream copy; output verified to no longer report `dolby_vision` |

Every operation takes exactly one input (`source` or another `op:<id>`); none of them change duration or frame
geometry (verified: duration and resolution must match the source within `duration_tolerance`, default 0.2 s).

Output formats (`outputs[].format`): `mp4`, `mov`, `m4v`, `mkv` — the extension must match the source's; this skill
does not convert containers (ADR-8; use ffmpeg-skill/export for that).

**Declared but not implemented** (`unsupported_operations` in the contract, `UNSUPPORTED_OPERATION` at validation):
`EXPOSURE`, `CONTRAST`, `SATURATION`, `TEMPERATURE`, `TINT`, `WHITE_BALANCE`, `GAMMA`, `LIFT`, `GAIN`, `LEVELS`,
`CURVES` — ffmpeg-skill's public contract has no typed primary colour-correction filter for any of them. Details
and the exact gaps: [docs/ffmpeg-skill.md](docs/ffmpeg-skill.md).

## CLI

| command | reads media | writes media | exit |
|---|---|---|---|
| `skill --json` / `contract --json` | no | no | 0 |
| `doctor --json [--ffmpeg-skill DIR] [--workspace DIR] [--allowed-input ROOT] [--allowed-lut ROOT]` | no (runs ffmpeg-skill doctor) | no | 0, 1 on `fail` |
| `validate REQUEST\|- --json` | no | no | 0 / error exit code |
| `plan REQUEST\|- --json` | probe source + hash any LUTs (read-only) | no | 0 / error exit code |
| `run REQUEST\|- --json [--dry-run] [--workspace DIR] [--allowed-input ROOT] [--allowed-lut ROOT] [--ffmpeg-skill DIR] [--timeout S] [--no-reuse]` | yes | yes | 0 / error exit code |

The ffmpeg-skill checkout is found from `--ffmpeg-skill`, else `COLOR_GRADING_FFMPEG_SKILL_DIR`,
`VIDEO_AGENT_FFMPEG_SKILL_DIR`, `~/.claude/skills/ffmpeg-skill`, `./vendor/ffmpeg-skill`, `../ffmpeg-skill`. It is
never taken from the request document.

## Doctor

`doctor --json` (`color-grading/doctor@1`) reports: Python; the located ffmpeg-skill (directory, version, contract
version, the flags this skill needs, problems); ffmpeg / ffprobe versions as detected by ffmpeg-skill's doctor;
every capability as `supported` / `unsupported` / `unknown`; every operation type with its status and the missing
or unknown capabilities; the not-implemented operations; output formats; the path policy (workspace, allowed input
roots, allowed LUT roots). Status: `ok`, `degraded` (some operation unsupported), `fail` (ffmpeg-skill missing /
incompatible or path policy broken). `secrets_shown` is always `false`.

## Process boundary

`caller → JSON (stdin) → typed validation → operation graph → ffmpeg-skill adapter (argv) → media execution →
output validation → JSON (stdout)`. With `--json`, stdout carries exactly one document, on success and failure
alike; stderr carries diagnostics (including ffmpeg-skill's own log lines). Exit codes are stable (`errors.exit_codes`).

## Security

- No shell, no `eval`, no user-supplied command, argv, executable, script or filter string; the request schema
  rejects such fields by name and rejects unknown fields. One `subprocess.Popen` in the package (adapter), argv
  only, allow-listed scripts only (`probe.py`, `color.py`), minimal child environment.
- Every argv value is a fixed flag, a number formatted by this skill, an enum member already validated against the
  model schema, or a resolved absolute path.
- A LUT is data: resolved through its own path policy (separate allowed roots from the video input), checked for a
  `.cube` extension and a size ceiling, hashed; never parsed, interpreted or treated as a filter string.
- Inputs and LUTs: regular files, symlinks resolved, optional `--allowed-input` / `--allowed-lut` roots; outputs:
  inside `--workspace`, no `..`, no symlinked escape, no reserved Windows names / invalid characters / option-like
  names, never an input, never an existing file unless `overwrite: true`, container must match the source. Inputs
  are never modified.
- Failed or cancelled runs leave no partial output and no intermediate without a manifest.

Details: [docs/security.md](docs/security.md).

## Determinism

Operation identity = sha256 over canonical JSON of `{type, effective parameters, input identity, tool versions}`;
the source's identity is its file sha256; a `LUT_APPLY` identity uses the LUT's sha256, never its path (a LUT moved
without changing content keeps its identity). No timestamps, no UUIDs, stable topological order (smallest node id
first among ready nodes), stable key order. Identical request + identical inputs + identical ffmpeg-skill / ffmpeg
version → identical `plan_id` and operation ids, reused intermediates, and content-equivalent outputs.

## Provenance

Per operation: `operation_id`, `type`, `tool`, `tool_versions`, `parameters`, `input_hash`, `output_hash` (in
`artifact.sha256`), `lut` (LUT_APPLY only: path, size, sha256), `status`, `measurements`, `tool_commands_observed`.
Per output: `skill`, `skill_version`, `tool_versions`, `output_hash`, the whole operation chain back to the source
with its sha256. The caller's *instruction* (the request) and the skill's *observation* (`results`,
`tool_runs`) are separate parts of the response; measured colour metadata (`color_space`, `color_primaries`,
`color_transfer`, `color_range`, `pix_fmt`, `hdr`, `dolby_vision`) is always OBSERVED (from `ffmpeg-skill/probe`),
never inferred or assumed from the request.

## Error handling

| code | exit | retryable | when |
|---|---|---|---|
| `INVALID_REQUEST` | 2 | no | document shape, unknown / forbidden field, bad type, enum or range |
| `INVALID_INPUT` | 3 | no | source missing, not a regular file, unreadable, no video stream, LUT missing/empty/oversized |
| `PATH_NOT_ALLOWED` | 4 | no | outside allowed roots / workspace, traversal, symlink escape, unsafe name |
| `UNSUPPORTED_OPERATION` | 5 | no | not implemented (declared or unknown type) |
| `UNSUPPORTED_FORMAT` | 6 | no | output format unknown, extension mismatch, output container ≠ source container, LUT not `.cube` |
| `INVALID_TIME_RANGE` | 7 | no | reserved for a future time-bounded operation; not raised by 0.1.0's operations |
| `DEPENDENCY_ERROR` | 8 | no | duplicate id, cycle, self-reference, unreachable node |
| `MISSING_INPUT` | 9 | no | reference to an undeclared operation |
| `OUTPUT_ERROR` | 10 | no | output exists, collides with the input, empty, could not be written |
| `VALIDATION_ERROR` | 11 | no | artifact failed post-validation (stream, duration, resolution, colour tags, pix_fmt) |
| `TOOL_ERROR` | 12 | yes | ffmpeg-skill missing / incompatible (not retryable), tool failure, timeout |
| `CANCELLED` | 13 | yes | SIGINT / SIGTERM |
| `INTERNAL_ERROR` | 14 | no | a bug in this skill (still one JSON document) |

## Testing

```text
pip install -e . pytest
export COLOR_GRADING_FFMPEG_SKILL_DIR=/path/to/ffmpeg-skill     # or clone it as ../ffmpeg-skill
python -m pytest -q          # unit + security + contract + integration with real video; nothing is skipped
```

See [docs/testing.md](docs/testing.md) for the file-by-file coverage. CI runs Linux (3.9, 3.11), Windows and macOS
with a real FFmpeg and a fresh ffmpeg-skill clone: [.github/workflows/tests.yml](.github/workflows/tests.yml).

## Real media verification

`tests/test_integration.py` runs every implemented operation (`HDR_TO_SDR`, `LUT_APPLY`, `RETAG`, `STRIP_DOVI`) and
a chained pipeline against real video through the real ffmpeg-skill and FFmpeg, and asserts, from `ffmpeg-skill/probe`
of the output, exactly what STEP 18/19 of the design brief ask for: the artifact exists, its size and sha256, its
duration and resolution, its video stream, and its measured colour metadata against the requested operation — plus
one deterministic pixel-level check (`LUT_APPLY` with a size-2 "invert" `.cube` LUT: the average colour of the
output must be `255 - average colour of the input`, within a documented tolerance for chroma subsampling and lossy
encoding, never a subjective "looks graded" judgement). See docs/testing.md for exact fixture construction and the
one real limitation (no genuine Dolby Vision RPU can be synthesised with plain ffmpeg, so `STRIP_DOVI`'s real-media
test exercises the operation and its "no Dolby Vision side data present" check, not actual RPU removal).

## Relationship to the other skills

- **ffmpeg-skill** – the execution engine; see [docs/ffmpeg-skill.md](docs/ffmpeg-skill.md) for the exact tools,
  flags and the compatibility gaps that define what is *not* offered here.
- **media-analysis-skill** – measures; not colour-specific in its 0.1.0 (no colour analyzer), so this skill's own
  `ffmpeg-skill/probe` calls are currently the only source of colour facts it consumes.
- **video-production-agent** – the only intended caller: decides which colour treatment, LUT or tonemap curve to
  apply, builds the request, runs `color-grading run - --json`, reads `results` / `outputs` into its provenance and
  Artifact model.
- **audio-production-skill, video-editing-skill, transcription-skill, subtitle-skill, QC** – not touched; this
  skill has no audio, cut/trim, speech or final-QC role.

## Current limitations

- One source per request (a colour operation processes one clip; there is no MIX/CONCAT analogue for colour).
- No container/format conversion (ADR-8): output must keep the source's container.
- A colour operation chain of N operations costs N re-encodes (or stream copies for RETAG/STRIP_DOVI); there is no
  way to fold `HDR_TO_SDR` and `LUT_APPLY` into one ffmpeg process, because ffmpeg-skill/color's mode flags are
  mutually exclusive (docs/ffmpeg-skill.md).
- `LUT_APPLY.lut_strength` of exactly `0.0` behaves like `1.0` (full LUT applied) — this is ffmpeg-skill's own
  documented behaviour (`0 < lut_strength < 1` blends; the endpoints do not), not a bug this skill introduces or
  can fix without changing ffmpeg-skill.
- `doctor` reports `filter:zscale` / `filter:tonemap` / `filter:lut3d` as `unknown` when ffmpeg-skill's own filter
  detection fails (FFmpeg ≥ 8.0's `-filters` output has a different flag width than ffmpeg-skill 0.9's parser
  expects); execution proceeds and output validation decides.
- No cache eviction: the work directory grows until the caller removes `<workspace>/.color-grading/<project_id>/`.
- Primary colour correction (exposure, contrast, saturation, white balance, temperature, tint, gamma, lift, gain,
  levels, curves) is not implemented: ffmpeg-skill has no typed filter for it yet (docs/ffmpeg-skill.md).
- `LUT_APPLY` on Windows, when the LUT and the ffmpeg-skill checkout are on *different* drives: ffmpeg-skill's own
  LUT-path escaping is rejected by at least one Windows ffmpeg build's filter-option parser (measured; docs/
  ffmpeg-skill.md), and a cross-drive path cannot be made relative to sidestep it the way this skill does for the
  common (same-drive) case — that narrow case keeps ffmpeg-skill's limitation.

## Future extensions (not in this release)

Primary colour correction, once ffmpeg-skill (or a second execution backend) gains a typed filter for it; multiple
sources / a colour-consistent look applied across a batch of clips in one request; audio-stream selection; each
requires a corresponding capability in ffmpeg-skill's public contract, and will be declared only once implemented
and tested.

## Support

If this skill saves you time, you can help keep it maintained through [GitHub Sponsors](https://github.com/sponsors/kajisho5).
Issues and pull requests are just as welcome.

## License

[MIT](LICENSE)
