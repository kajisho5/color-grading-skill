# Security boundary

## Enforced

| rule | where | tested by |
|---|---|---|
| No shell, no `eval` / `exec`, no `os.system`; exactly one `subprocess.Popen` (argv list) in the package | `adapter._popen` | `test_security.test_no_shell_or_eval_in_source` |
| Only `scripts/{probe,color}.py` of the located ffmpeg-skill can be started | `adapter.FfmpegSkill.script` | `test_security.test_only_allowlisted_ffmpeg_skill_scripts_can_run` |
| The request never names a command, argv, executable, script, filter, environment, working directory, API key or "workspace": those field names are rejected anywhere in the document; unknown fields are rejected everywhere; parameter types, enums and ranges are validated per operation type | `model.FORBIDDEN_KEYS`, `model.validate_parameters` | `test_security.test_request_rejects_command_like_fields_anywhere`, `test_security.test_parameter_injection_is_rejected` |
| Every argv element is a fixed flag, a number formatted by this skill, an enum member already checked against the model schema, or a resolved absolute path (never a raw request string) | `adapter.fmt_num`, `executor._argv` | `test_security.test_argv_builder_uses_only_numbers_enums_and_resolved_paths` |
| A LUT path is resolved through its own `PathPolicy.resolve_lut` (separate allowed roots from the video input), its extension checked (`.cube` only) and its bytes hashed; it is handed to ffmpeg-skill as a resolved absolute path after `--lut`, never interpreted, parsed or executed by this skill | `security.PathPolicy.resolve_lut`, `executor._run` | `test_security.test_lut_path_policy_is_separate_from_input`, `test_security.test_lut_must_be_cube_extension` |
| The ffmpeg-skill directory comes from the CLI / environment, never from the request; an explicit directory is validated (contract version, flags) and never silently replaced by a fallback | `adapter.locate`, `adapter.info` | `test_security.test_ffmpeg_skill_dir_is_not_taken_from_the_request`, `test_security.test_bogus_ffmpeg_skill_dir_is_rejected` |
| Inputs and LUTs: regular files, symlinks resolved before every check, optional `--allowed-input` / `--allowed-lut` roots (`PATH_NOT_ALLOWED`) | `security.PathPolicy.resolve_input`, `.resolve_lut` | `test_security.test_path_policy_inputs_and_outputs`, `test_security.test_symlink_escape_is_refused` |
| Outputs and the work directory resolve inside `--workspace`; `..`, absolute paths outside and symlinked directories pointing outside are refused; containment uses path components, not string prefixes | `security.PathPolicy.resolve_write_path` | same tests, `test_security.test_unsafe_output_paths_through_cli` |
| File names: no control / invalid characters, no reserved Windows device names (`CON`, `NUL`, `COM1`…), no trailing dot / space, no leading `-`, length limits | `security.check_filename` | `test_unit.test_filename_rules` |
| An output is never an input (after symlink resolution) and never an existing file unless `overwrite: true`; an output's container must match the source's; inputs are never modified | `executor._run` | `test_security.test_output_may_not_overwrite_input`, `test_integration.test_output_format_must_match_source_container` |
| Child processes: own process group (`start_new_session` / `CREATE_NEW_PROCESS_GROUP`), killed as a tree on timeout or signal; minimal environment (`PATH`, home / temp / locale, Windows system variables, `PYTHONUTF8`) | `adapter` | `test_security.test_child_environment_is_minimal`, `test_integration.test_timeout_is_a_retryable_tool_error`, `test_integration.test_signal_cancellation_leaves_no_partial_output` |
| Failure hygiene: on any error the node's intermediate is removed; a failed output is removed; a manifest is written only after validation, so an intermediate without a manifest is never reused | `executor._execute_node`, `_materialize`, `_reusable` | `test_integration.test_loudness... (n/a)`, `test_integration.test_output_expectation_failure_removes_output`, `test_integration.test_rerun_reuses_intermediates_and_is_deterministic` |
| stdout is exactly one JSON document under `--json`, on success and failure alike, including malformed input; diagnostics go to stderr; no secrets are printed (`doctor.secrets_shown = false`) | `cli` | `test_security.test_malformed_documents_yield_one_json_error` |
| Request size limit 16 MiB; at most 200 operations; a LUT larger than 200 MiB is rejected before use | `cli._read_document`, `model`, `executor._run` | unit tests |

## Not enforced

- Without `--allowed-input` / `--allowed-lut`, any regular file the process can read is accepted (same posture as
  media-analysis-skill, audio-production-skill and video-production-agent ADR-010). Embedders should set the roots,
  and should set `--allowed-lut` to a directory distinct from raw footage if that separation matters to them
  (docs/decisions.md ADR-7).
- Resource use: long or high-resolution videos mean long ffmpeg runs; `--timeout` (per tool call) and
  `options.timeout` bound wall time, not memory or disk.
- The work directory is trusted: manifests contain absolute paths and hashes; tampering with an intermediate is
  detected (sha256) and causes re-processing, but the directory should not be world-writable.
- ffmpeg-skill itself is trusted code: this skill validates its contract and its outputs, not its source.
- A `.cube` LUT's *content* is not parsed or sanity-checked by this skill (only its extension, size and hash); a
  malformed LUT is ffmpeg's own error at execution (`TOOL_ERROR`), not a validation failure caught earlier. It is
  never treated as anything other than data handed to ffmpeg's `lut3d` filter.
