# ffmpeg-skill relationship

color-grading-skill is a client of ffmpeg-skill's **public contract** (`ffmpeg-skill contract --json`,
`contract_version 1.0`, verified against ffmpeg-skill 0.9.1; 0.9.0 is not verified and is refused by the adapter's
version window, the same posture audio-production-skill takes). It never calls `ffmpeg` or `ffprobe` itself.

## Tools and flags used

| ffmpeg-skill tool | used for | flags emitted |
|---|---|---|
| `ffmpeg-skill/probe` | source facts, every artifact's validation | positional `inputs` (one path) |
| `ffmpeg-skill/color` | `HDR_TO_SDR` (`--to-sdr`), `LUT_APPLY` (`--lut`), `RETAG` (`--retag`), `STRIP_DOVI` (`--strip-dovi`) | `--to-sdr`, `--lut`, `--retag`, `--strip-dovi` (mutually exclusive, exactly one required — matches this skill's one-operation-per-node model exactly), `--tonemap`, `--peak`, `--desat`, `--force`, `--lut-strength`, `--crf`, `--preset`, `-o`, `--json` |

`doctor` checks that the located ffmpeg-skill declares `color` with `video_required: true` and that every flag
listed above exists in the tool's generated `input_schema`; a mismatch is a `fail` and `run` refuses with
`TOOL_ERROR` (`ffmpeg_skill_incompatible`).

## Why 0.9.1

Every flag this skill emits was read directly from `scripts/color.py`'s argparse definition and confirmed present in
`scripts/_contract.py --json --static`'s generated `input_schema` at ffmpeg-skill 0.9.1. Earlier versions were not
inspected and are not claimed to work; the adapter's version window is therefore `[0.9.1, 1.0.0)`, the same
convention audio-production-skill uses for its own dependency.

## Observed behaviour this skill relies on (measured by reading ffmpeg-skill 0.9.1's `scripts/color.py`)

- `color.py` requires a video stream (`die("input has no video stream")` when `probe` reports none) and refuses to
  run without exactly one of `--to-sdr` / `--lut` / `--retag` / `--strip-dovi` (an argparse mutually-exclusive,
  required group) — this is exactly this skill's one-operation-per-node model; there is no request shape in which
  color-grading-skill could ask ffmpeg-skill/color to do more than one of these in a single call.
- `--to-sdr` refuses a source that is not tagged HDR (`color_transfer` / `color_primaries`) unless `--force` is
  given; it builds a `zscale → format=gbrpf32le → zscale → tonemap → zscale → format=yuv420p` chain from the
  *measured* input tags (falls back to `smpte2084`/`bt2020`/`bt2020nc`/`tv` when a tag is missing) and always
  re-encodes with libx264 (`x264_args`), carrying audio through AAC when present. **Measured**: `--force` on a
  source that genuinely never was PQ/HLG-encoded (no HDR tags, ordinary SDR content) can make the zscale/tonemap
  filter chain itself fail ("Nothing was written into output file..."), since the chain assumes a PQ-range input.
  This is a real ffmpeg failure surfaced as `TOOL_ERROR`, not a defect in this skill's validation; `force: true` is
  the caller's explicit request to treat unlabelled content as HDR anyway, and that can legitimately fail.
- `--retag`'s stream-copy path (`-c copy -colorspace/-color_primaries/-color_trc`) only reliably changes what
  `ffprobe` reports when the source's own bitstream (the H.264/HEVC SPS VUI) does not already carry an explicit,
  conflicting colour description. **Measured** with ffmpeg 6.1.1: retagging a file whose H.264 stream was encoded
  with explicit `-colorspace/-color_primaries/-color_trc` (baked into the SPS) leaves `ffprobe`'s reported tags
  unchanged after `--retag`, even though `color.py` reports `wrote ... (tags -> ...)` and exits 0 — the container-
  level override does not rewrite already-encoded VUI bits, and `color.py`'s fallback to a re-encode only triggers
  on a non-zero ffmpeg exit code, which this case does not produce. Retagging a source with *no* explicit tags (the
  common case `--retag` is documented for: "the colours are tagged wrong", i.e. missing or incorrect metadata on
  otherwise untagged footage) works as documented. This skill's own output validation (`executor._validate_artifact`,
  the `RETAG` branch) re-probes the output and compares the exact tag triple against `model.RETAG_TAGS[target]`, so
  a retag that silently did not take effect is reported as `VALIDATION_ERROR` (`reason: retag_mismatch`), never as a
  false success — the gap is ffmpeg-skill's, the honesty about it is this skill's.
- `--lut` applies `lut3d=file=<path>:interp=tetrahedral`; **`--lut-strength` blends the graded and original streams
  only for a value strictly between 0 and 1** (`0 < args.lut_strength < 1` in the source) — a strength of exactly
  `0.0` is *not* "no LUT": ffmpeg-skill applies the LUT at full strength for both `0.0` and `1.0`. This skill
  accepts the documented `[0.0, 1.0]` range unchanged (it is ffmpeg-skill's contract, not a bug this skill works
  around) and states the caveat in the parameter's own description (`model.OPERATION_TYPES["LUT_APPLY"]`).
  Always re-encodes with libx264; an HDR source keeps whatever tags the input had (the LUT path does not retag).
- `--retag` attempts a stream copy first (`-c copy -colorspace … -color_primaries … -color_trc …`); ffmpeg-skill
  itself falls back to a re-encode (`x264_args(crf, preset, keep_bt709=False)`) only when the stream copy fails for
  a given codec/container combination. This skill's `RETAG` parameters carry no `crf`/`preset` (unlike
  `HDR_TO_SDR`/`LUT_APPLY`): the common case is a stream copy where they have no effect, and ffmpeg-skill's own
  defaults (18 / medium) apply on the rare fallback. The four targets and their exact tag triples
  (`color_space, color_primaries, color_transfer`) are ffmpeg-skill's own mapping, copied verbatim into
  `model.RETAG_TAGS` and used again to verify the output.
- `--strip-dovi` only applies to HEVC video (`die` otherwise) and is always a stream copy
  (`-bsf:v filter_units=remove_types=62 -tag:v hvc1`), removing the Dolby Vision RPU NAL unit; it does not require
  the input to already carry detected Dolby Vision side data (it warns and proceeds), but this skill still verifies
  the *output* no longer reports `dolby_vision` after the call.
- Failure document: `{"status": "failed", "error": {"kind": "input|ffmpeg|missing_tool", "message"}}` with a
  non-zero exit; parsed into `TOOL_ERROR` with `details.error_kind`.
- `ffmpeg-skill/probe`'s `video.hdr` is `true` when `color_transfer` is `smpte2084` or `arib-std-b67`, or
  `color_primaries` is `bt2020`, or Dolby Vision side data is present; `video.dolby_vision` is `null` or a dict with
  `profile`/`level`/`bl_compatibility_id`. This skill's `HDR_TO_SDR` / `STRIP_DOVI` validation reads exactly these
  two fields, never re-derives HDR-ness from raw tags itself.
- **`--lut` and a Windows drive-letter path.** `color.py` builds `lut3d=file=<escape_filter_path(args.lut)>:interp=
  tetrahedral` for the `-vf` filter graph. `escape_filter_path` backslash-escapes a colon (`C:\...` → `C\:/...`) so
  the graph-level parser does not treat it as a filter separator. **Measured** on a Windows CI runner (gyan.dev
  ffmpeg 9.0.1 essentials, Windows Server 2025): that escaping is rejected by this build's filter-*option* value
  parser (`No option name near '...invert.cube:interp=tetrahedral'`, `Error parsing filterchain ...`), so an
  absolute Windows LUT path made `--lut` fail outright — not a corrupted result, a hard failure ffmpeg-skill itself
  reports (`kind: ffmpeg`), which this skill's adapter turns into `TOOL_ERROR`. Since only the drive letter's colon
  triggers the bug, this skill sidesteps it entirely from its own side, without touching ffmpeg-skill:
  `executor._execute_node` runs the `ffmpeg-skill/color` subprocess for `LUT_APPLY` with `cwd` set to the LUT
  file's own directory (`adapter.run_tool`/`_popen` take an optional `cwd` override for this, defaulting to the
  ffmpeg-skill checkout as before for every other call) and `executor._lut_arg` passes just the LUT's bare file
  name as `--lut` — no drive letter, no colon, nothing for `escape_filter_path` to (mis)escape, regardless of what
  drive the LUT, the caller's workspace and the ffmpeg-skill checkout each happen to be on. (A relative-path
  attempt was tried first and was not enough: GitHub Actions Windows runners put the repository checkout and the
  OS temp directory — where a caller's own workspace typically lives — on different drives, and no relative path
  can cross a Windows drive at all.)

## Compatibility gaps (required capability → not implemented here)

| wanted operation | missing in ffmpeg-skill 0.9 public contract | consequence |
|---|---|---|
| Exposure / contrast / saturation (`eq` wrapper) | `color.py` has no gain/brightness/contrast/saturation flags | declared in `UNSUPPORTED_OPERATIONS` (`EXPOSURE`, `CONTRAST`, `SATURATION`); not implemented |
| Colour temperature / tint / white balance | no `colortemperature` / `colorbalance` wrapper in any script | declared (`TEMPERATURE`, `TINT`, `WHITE_BALANCE`); not implemented |
| Gamma / lift / gain (shadows-mids-highlights) | no typed lift-gamma-gain filter in any script | declared (`GAMMA`, `LIFT`, `GAIN`); not implemented |
| Levels / curves | no typed `levels` / `curves` wrapper in any script | declared (`LEVELS`, `CURVES`); not implemented |
| Container / format conversion alongside grading | `color.py` writes to whatever extension `-o` names, no explicit "convert to X"; that is `export.py`'s job | outputs must keep the source's container (ADR-8); use ffmpeg-skill/export separately |
| Multiple LUTs / multiple colour ops in one ffmpeg process | `color.py`'s mode flags are mutually exclusive | each operation is its own `ffmpeg-skill/color` call and its own intermediate; a chain of N operations costs N re-encodes (documented in README's Current limitations) |
| Filter/encoder capability detection on FFmpeg ≥ 8.0 | ffmpeg-skill 0.9's `_ff_list` matches the pre-8.0 three-flag format; FFmpeg 8 prints two flags, so every filter is reported "missing" | when the doctor reports ffmpeg but zero filters, `filter:*` capabilities are `unknown`, not `unsupported`; execution proceeds and output validation decides (docs/decisions.md ADR-10) |

Each gap is a request to ffmpeg-skill (or a future second backend), not something this skill works around with its
own ffmpeg invocation or a raw filter string.
