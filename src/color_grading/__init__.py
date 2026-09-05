"""color-grading-skill: deterministic colour grading / colour correction execution Skill (not an AI agent).

It executes typed, validated colour operations (LUT application, HDR-to-SDR tone mapping, colour-tag
retagging, Dolby Vision RPU removal, typed primary colour correction) through ffmpeg-skill and reports
provenance. It does not decide which colour treatment to apply, does not look at a frame and judge
"cinematic", does not pick a LUT, a look or a correction value, and never runs a shell or an arbitrary
command or filter string. Those decisions belong to video-production-agent."""

SKILL_ID = "color-grading"
PACKAGE_NAME = "color-grading-skill"
VERSION = "0.2.0"

CONTRACT_SCHEMA_VERSION = 1
REQUEST_SCHEMA_VERSION = 1
RESPONSE_SCHEMA_VERSION = 1
DOCTOR_SCHEMA_VERSION = 1

__all__ = ["SKILL_ID", "PACKAGE_NAME", "VERSION"]
