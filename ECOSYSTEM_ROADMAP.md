# Roadmap — color-grading-skill

Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **映像 (Visual)**, status `active`.
Written from a read-only audit of this repository on 2026-10-30. Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

v0.4.0, Python. HDR_TO_SDR, LUT_APPLY, RETAG, STRIP_DOVI, PRIMARY_CORRECTION (exposure ... curves). 170 tests, CI on 3 OSes, release workflow.

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| 色補正 | done |  |
| LUT | done |  |
| HDR→SDR | done |  |

## Wiring

Agent wires 5 color_* skills through color-grading/run, but only exposure, contrast, saturation, temperature, tint; gamma, lift, gain, levels, curves are not reachable. Delegates to ffmpeg-skill probe, color.

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

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (42 tools, treated as complete). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).
