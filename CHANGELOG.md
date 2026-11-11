## v1.0.0 - 2026-11-11

- chore: Bump release-drafter/release-drafter from 6.4.0 to 7.10.0 (#35) (51f3410)

## v0.6.1 - 2026-11-07

- fix: never reuse an intermediate from another skill version, or an alpha grade without a kept-alpha record (#40) (537bc75)

## v0.6.0 - 2026-11-07

- feat: measured legal levels for LEGALIZE and alpha-aware grading (roadmap steps 7 and 8) (#39) (c78da86)

## v0.5.1 - 2026-11-07

- fix: default HDR_TO_SDR to mobius for an HLG input (ADR-28) (#38) (e155caf)

## v0.5.0 - 2026-11-06

- fix(release): resolve the version with release-drafter v7 dry-run (#37) (9120d76)
- fix: refuse re-encoding an alpha source instead of dropping its alpha (roadmap step 8, guard only) (#36) (5f1cb66)
- fix: keep the contract blocks video-production-agent pins unchanged; patches as a typed string (#34) (8a8f1e9)
- feat: LEGALIZE, broadcast-legal soft clip (roadmap step 7, partly: shares not measurable) (#33) (019780f)
- feat: MATCH_CAMERA, match a camera to a reference from a frame pair (roadmap step 6) (#32) (d0ae201)
- feat: OCIO_TRANSFORM, OpenColorIO / ACES transforms baked to a 3D LUT (roadmap step 5) (#31) (1267804)
- docs(roadmap): no live-streaming context (out of scope) (#29) (dd9c54d)
- fix: a signal no longer interrupts the executor between an intermediate and its manifest (#30) (76fe83a)
- docs(roadmap): field review steps 6-8 (2026-11-05) (#28) (98a9f53)
- docs(roadmap): step 5 (OCIO / ACES transforms) and review (#27) (0003c4d)
- fix: accept ffmpeg-skill 3.0.0 (contract_version 2.0); Python 3.9 CI on the last 2.x (#26) (2b5b1a5)
- docs: tick roadmap step 3 (agent side: gamma / lift / gain / levels / curves) [skip release] (#25) (54dad4d)
- ci: pin third-party actions by commit SHA (#24) (c7faa40)
- fix: contract_version, ffmpeg-skill dependency, and WHITE_BALANCE as a temperature/tint alias (#23) (fd43c01)
- Add ECOSYSTEM_ROADMAP.md: gaps, wiring and PR-sized steps from the 2026-10-30 ecosystem audit (#22) (a73fdf3)
- Add .github/FUNDING.yml (#21) (9f9d0d4)
- ci: bump GitHub Actions (checkout v7, setup-python v7, codeql v4 and others) (#20) (e6fa9ce)
- fix: work with any ffmpeg-skill version (capability detection, no version window) (#19) (6c45c9f)
- docs(readme): fresnel-style header with a Liquid Glass app icon (#18) (c7fae4d)
- Require major/breaking label to be applied by hand, not autodetected (#16) (17eb704)

## v0.4.0 - 2026-09-11

- Fix pyproject.toml/__init__.py version desync; harden release.yml (#15) (054b54e)

# Changelog

## v0.3.0 - 2026-09-11

- Add GitHub automation: release/labeling/CodeQL/dependabot/templates (#10) (8e0aaaf)
- CI: repoint the ffmpeg-skill pin to the commit verified for 0.12.3 (#11) (32b7cf6)
- Wire ffmpeg-skill 0.12.3's gamma/lift/gain/levels/curves into PRIMARY_CORRECTION (#9) (6a6d001)
- Surface reencoded/dropped_non_av_streams; add --audio-stream support (#7) (4d4de2f)
- Add PRIMARY_CORRECTION: typed primary colour correction via ffmpeg-skill 0.9.2 (#5) (3eb3606)
- Add provides: publish Capability ids for cross-repository discovery (2b7bfe9)
- README: add ecosystem-branded hero logo (#3) (8e8b9f8)
- README: redesign as an OSS landing page, matching ffmpeg-skill's structure (#2) (c3a340e)
- Implement color-grading-skill: deterministic colour grading execution on ffmpeg-skill (#1) (a1da243)
- Initial commit (1e3d4b6)
