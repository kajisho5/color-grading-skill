"""Structured error model. Every failure that crosses the Skill boundary is a ColorError with a code from
ERROR_TABLE; the CLI turns it into {"ok": false, "error": {"code", "message", "retryable", "details"}}."""
from __future__ import annotations

from typing import Any, Dict, Optional

# code -> (exit code, retryable)
ERROR_TABLE: Dict[str, Any] = {
    "INVALID_REQUEST": (2, False),         # document shape / unknown fields / bad types
    "INVALID_INPUT": (3, False),           # an input file is missing, unreadable, not video, or not a regular file
    "PATH_NOT_ALLOWED": (4, False),        # input/LUT outside allowed roots, output outside workspace, traversal, symlink escape
    "UNSUPPORTED_OPERATION": (5, False),   # operation type not implemented by this skill
    "UNSUPPORTED_FORMAT": (6, False),      # output container not in the contract, extension mismatch, LUT format unsupported
    "INVALID_TIME_RANGE": (7, False),      # reserved for a future time-bounded operation; not raised by 0.1.0's operations
    "DEPENDENCY_ERROR": (8, False),        # operation graph cycle, duplicate id, self reference, unreachable output
    "MISSING_INPUT": (9, False),           # an operation references a source / operation that does not exist
    "OUTPUT_ERROR": (10, False),           # output could not be written, is empty, collides with an input, or exists
    "VALIDATION_ERROR": (11, False),       # output written but failed post-validation (stream, duration, colour tags)
    "TOOL_ERROR": (12, True),              # ffmpeg-skill / ffmpeg failed, timed out, or is unavailable
    "CANCELLED": (13, True),               # interrupted by signal
    "INTERNAL_ERROR": (14, False),         # a bug in this skill
}
ERROR_CODES = tuple(ERROR_TABLE)
EXIT_CODES = {code: ERROR_TABLE[code][0] for code in ERROR_CODES}


class ColorError(Exception):
    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None, retryable: Optional[bool] = None):
        if code not in ERROR_TABLE:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retryable = ERROR_TABLE[code][1] if retryable is None else bool(retryable)

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.code]
