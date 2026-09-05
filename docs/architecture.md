# Architecture

## Position in the ecosystem

```text
video-production-agent          Observation → Inference → Decision → ProductionPlan            (brain)
        │  builds a typed request (source, operations, outputs) naming an explicit LUT / tonemap curve / retag target
        ▼
color-grading-skill             request → validation → graph → plan → execute → validate       (this package)
        │  ffmpeg-skill/{probe,color} with typed argv
        ▼
ffmpeg-skill                    runs ffmpeg / ffprobe                                          (hands)

media-analysis-skill            measures inputs for the agent (not colour-specific in 0.1.0)     (meters)
```

Responsibilities that are deliberately **absent** here: deciding which colour treatment, LUT or tonemap curve to
apply; automatic "look" or LUT selection; looking at a frame and judging it "cinematic" or "washed out"; primary
colour correction (exposure, contrast, saturation, white balance, temperature, tint, gamma, lift, gain, levels,
curves) — ffmpeg-skill has no typed filter for these yet (docs/ffmpeg-skill.md); production planning; approvals;
transcription; subtitles; QC; container/format conversion (that is ffmpeg-skill/export).

## Typed Colour Project Model (`model.py`)

| concept | fields | notes |
|---|---|---|
| `ColorProject` | `project_id`, `source`, `operations[]`, `outputs[]` | one request = one project, one source video |
| `ColorSource` | `source_id`, `path` | fingerprinted (sha256) at execution; must have a video stream |
| `ColorOperation` | `op_id`, `type`, `input` (ref `"source"` or `"op:<id>"`), `parameters` | every implemented type takes exactly one input; parameters validated per type (`OPERATION_TYPES`) |
| `ColorOutput` | `output_id`, `operation` (ref), `path`, `format`, `overwrite?`, `expect?` | terminal artifact; `format`'s extension must match the source's |
| `OperationDependency` (`graph.py`) | node `inputs` (0 or 1), `consumers[]` | topological order = Kahn with smallest ready id first |
| `OperationResult` (`executor.NodeState`) | `operation_id`, `type`, `tool`, `status`, `artifact`, `input_hash`, `lut?`, `measurements`, `tool_commands_observed`, `error?` | one per node in the response |

Every operation maps 1:1 onto one mode of ffmpeg-skill/color (`--to-sdr`, `--lut`, `--retag`, `--strip-dovi`): this
skill is a typed front end for that tool's public contract, not a colour-correction engine of its own. There is no
timeline and no segment mapping (unlike audio-production-skill): none of the implemented operations change duration
or frame geometry, so the graph is a set of simple chains rooted at `"source"` (still validated as a general DAG for
duplicate ids, cycles and unreachable nodes, the same reasons audio-production-skill validates its richer graph).

## Operation graph and identity

Nodes: `"source"` and `"op:<id>"`. The graph rejects duplicate ids, self references, cycles, and operations not
connected to any output.

Identity (`graph.identities`): `sha256(canonical_json({kind: "source", source_sha256}))` for the source,
`sha256(canonical_json({kind: "operation", type, parameters, input: identity, tool_versions}))` for an operation.
`op_id` is a label, not part of the identity. For `LUT_APPLY`, the raw `lut_path` parameter is replaced by the LUT's
own sha256 before hashing (`content_overrides` in `graph.identities`), so identity depends on the LUT's bytes, not
the path it happened to be read from on this machine: a LUT moved without changing content keeps its identity, one
edited at the same path does not. `plan_id` hashes every node identity plus the outputs.

## Execution (`executor.py`)

1. Resolve and probe the source through `ffmpeg-skill/probe` (read-only, also under dry run), refuse a source
   without a video stream, fingerprint it (sha256), and record its colour metadata (`color_space`,
   `color_primaries`, `color_transfer`, `color_range`, `pix_fmt`, `hdr`, `dolby_vision`) as OBSERVED facts.
2. Resolve every `LUT_APPLY` operation's `lut_path` through the LUT PathPolicy, check the `.cube` extension and a
   size ceiling, and hash it (read-only, also under dry run).
3. Resolve outputs (workspace, collisions with the input, existence, container match with the source).
4. Compute deterministic identities; plan every node: tool selection (`TOOL_FOR`), capability check against the
   doctor's statuses. `plan` / `--dry-run` stops here and returns the plan and planned results.
5. Execute in topological order. Each node writes one intermediate next to
   `<workspace>/.color-grading/<project_id>/<identity[:16]><source-extension>` with a manifest
   (`<identity[:16]>.json`); if both exist, the manifest matches the identity and input hash and the file's sha256
   matches, the node is `reused`. Each node is exactly one `ffmpeg-skill/color` call built from typed parameters
   (`executor._argv`): `HDR_TO_SDR` → `--to-sdr --tonemap … --peak … --desat … [--force] --crf … --preset …`;
   `LUT_APPLY` → `--lut <resolved path> --lut-strength … --crf … --preset …`; `RETAG` → `--retag <target>`;
   `STRIP_DOVI` → `--strip-dovi`.
6. Validate every intermediate: exists, size > 0, readable, probed video stream, duration and resolution unchanged
   from the source (within `DURATION_TOLERANCE`), and the operation's own measurable effect: `HDR_TO_SDR` checks the
   output is no longer tagged HDR; `RETAG` checks the exact `(color_space, color_primaries, color_transfer)` triple;
   `STRIP_DOVI` checks the Dolby Vision side data is gone; `LUT_APPLY` checks `pix_fmt == yuv420p` (what
   ffmpeg-skill's LUT path always produces). None of this is "looks graded"; it is what `ffmpeg-skill/probe`
   measured, checked against what the operation is supposed to have done (STEP 19 of the design brief: deterministic
   verification, never a subjective "looks cinematic" pass).
7. Materialise every output: the final node's validated artifact is copied (`shutil.copy2`, not another ffmpeg-skill
   call — a colour operation never needs reformatting) to the requested path and re-validated (exists, size,
   sha256 match, video stream, duration/resolution/pix_fmt against `expect`).
8. Return one response document: `ok`, `status`, `plan`, `results`, `outputs` (with provenance), `tool_runs`,
   `error?`. On the first failure the remaining nodes are `skipped`; on SIGINT / SIGTERM the running tool's process
   group is killed and the status is `cancelled`.

## ffmpeg-skill adapter (`adapter.py`)

The only module that starts a process. `locate()` finds the checkout; `info()` reads its contract
(`_contract.py --json --static`) and checks `contract_version == 1.0`, the version window, and that every flag this
skill emits exists in `ffmpeg-skill/{probe,color}`'s generated `input_schema`; `run_tool()` runs
`[sys.executable, scripts/<tool>.py, *argv, --json]` in its own process group with a minimal environment and a
timeout, and parses the `{"status": "completed"|"failed"}` document; `probe()` wraps the read-only tool.

## Response envelope

`{"schema": "color-grading/response@1", "skill": {"id", "version"}, "ok": bool, "status": "ok|error|cancelled", ...}`.
`ok` is the field named by the ecosystem's typed error contract (`error: {code, message, retryable, details}`);
`status` mirrors media-analysis-skill's convention so an adapter written for it can read this document too. The
process exit code is `0` iff `ok`, else `errors.EXIT_CODES[code]`.

## Versioning

- Package / Skill version: `color_grading.VERSION` (`0.1.0`), carried in every document and in every intermediate manifest.
- Document schemas: `color-grading/{contract,request,response,doctor}@1`, versioned independently; within `@1`
  changes are additive only. Renaming an operation type, a parameter, or changing how an operation is realised bumps
  the minor package version (and therefore every operation identity, by design).
- ffmpeg-skill compatibility window: contract `1.0`, version `[0.9.1, 1.0.0)` (measured; see docs/ffmpeg-skill.md).
