"""Path policy: what may be read (input video, LUT files), where may be written, what may never be named.

- Inputs and LUTs must be existing regular files; symlinks are resolved before every check so a link cannot escape
  a root.
- With allowed_input_roots set, the resolved input must live under one of them (PATH_NOT_ALLOWED otherwise).
- LUTs are resolved through a *separate* allowed-roots policy (allowed_lut_roots): a caller that may read an input
  under /footage should not thereby be able to name any file on the machine as a ".cube" LUT. When
  allowed_lut_roots is not set it falls back to allowed_input_roots (never "any file" by accident once inputs are
  confined; both unset means any readable regular file, like a CLI user would expect).
- Every write (outputs, work directory) must resolve inside `workspace`; ".." segments, absolute paths outside and
  symlinked directories pointing outside are refused.
- An output may never be an input (no in-place processing), may not exist unless overwrite is requested, and its
  file name must be safe on every platform (no control characters, no reserved Windows names, no trailing dot/space).
- Nothing in a request ever becomes an executable, a command, an argv fragment or a filter string; executables are
  found by the ffmpeg-skill adapter only (see adapter.py). A LUT is data (a lookup table read by ffmpeg's lut3d
  filter), never code: this policy validates its path and extension only; content is not parsed here."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

from .errors import ColorError

WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
INVALID_NAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
MAX_PATH_LENGTH = 4096
MAX_NAME_LENGTH = 255


def check_filename(name: str) -> None:
    """Refuse file names that are unsafe or non-portable. Applies to every path component we create."""
    if not name or name in (".", ".."):
        raise ColorError("PATH_NOT_ALLOWED", f"unsafe file name: {name!r}", {"reason": "empty_or_dot"})
    if len(name.encode("utf-8", "replace")) > MAX_NAME_LENGTH:
        raise ColorError("PATH_NOT_ALLOWED", "file name is too long", {"reason": "name_too_long"})
    if INVALID_NAME_CHARS.search(name):
        raise ColorError("PATH_NOT_ALLOWED", f"file name contains an invalid character: {name!r}", {"reason": "invalid_character"})
    if name.endswith(" ") or name.endswith("."):
        raise ColorError("PATH_NOT_ALLOWED", f"file name may not end with a space or a dot: {name!r}", {"reason": "trailing_space_or_dot"})
    stem = name.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED:
        raise ColorError("PATH_NOT_ALLOWED", f"file name is a reserved device name: {name!r}", {"reason": "reserved_name"})
    if name.startswith("-"):
        raise ColorError("PATH_NOT_ALLOWED", f"file name may not start with '-': {name!r}", {"reason": "option_like_name"})


def _check_path_string(path: object, what: str) -> str:
    if not isinstance(path, str) or not path:
        raise ColorError("INVALID_REQUEST", f"{what} must be a non-empty path string", {"field": what})
    if "\x00" in path:
        raise ColorError("PATH_NOT_ALLOWED", f"{what} contains a NUL byte", {"reason": "nul_byte"})
    if len(path) > MAX_PATH_LENGTH:
        raise ColorError("PATH_NOT_ALLOWED", f"{what} is too long", {"reason": "path_too_long"})
    return path


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class PathPolicy:
    def __init__(self, workspace: Optional[str] = None, allowed_input_roots: Optional[List[str]] = None,
                 allowed_lut_roots: Optional[List[str]] = None):
        self.workspace = Path(workspace or os.getcwd()).resolve()
        if not self.workspace.is_dir():
            raise ColorError("PATH_NOT_ALLOWED", f"workspace is not a directory: {self.workspace}", {"reason": "workspace_missing"})
        self.allowed_input_roots = [Path(r).resolve() for r in allowed_input_roots] if allowed_input_roots else None
        for r in self.allowed_input_roots or []:
            if not r.is_dir():
                raise ColorError("PATH_NOT_ALLOWED", f"allowed input root is not a directory: {r}", {"reason": "root_not_directory"})
        if allowed_lut_roots:
            self.allowed_lut_roots: Optional[List[Path]] = [Path(r).resolve() for r in allowed_lut_roots]
            for r in self.allowed_lut_roots:
                if not r.is_dir():
                    raise ColorError("PATH_NOT_ALLOWED", f"allowed LUT root is not a directory: {r}", {"reason": "root_not_directory"})
        else:
            self.allowed_lut_roots = self.allowed_input_roots

    # ---- inputs (video)
    def resolve_input(self, path: object, what: str = "input") -> Path:
        text = _check_path_string(path, what)
        p = Path(text)
        if not p.is_absolute():
            p = self.workspace / p
        try:
            resolved = p.resolve(strict=True)
        except FileNotFoundError:
            raise ColorError("INVALID_INPUT", f"{what} not found: {text}", {"reason": "not_found", "path": text})
        except (OSError, RuntimeError) as e:
            raise ColorError("INVALID_INPUT", f"cannot resolve {what} path: {e}", {"reason": "unresolvable", "path": text})
        if not resolved.is_file():
            raise ColorError("INVALID_INPUT", f"{what} is not a regular file: {text}", {"reason": "not_regular_file", "path": text})
        if self.allowed_input_roots is not None and not any(_under(resolved, r) for r in self.allowed_input_roots):
            raise ColorError("PATH_NOT_ALLOWED", f"{what} is outside the allowed input roots: {text}",
                             {"reason": "outside_allowed_roots", "allowed_input_roots": [str(r) for r in self.allowed_input_roots]})
        if not os.access(str(resolved), os.R_OK):
            raise ColorError("INVALID_INPUT", f"{what} is not readable: {text}", {"reason": "not_readable", "path": text})
        return resolved

    # ---- LUTs (data, never code; a separate allowed-roots policy from video inputs)
    def resolve_lut(self, path: object, what: str = "lut") -> Path:
        text = _check_path_string(path, what)
        p = Path(text)
        if not p.is_absolute():
            p = self.workspace / p
        try:
            resolved = p.resolve(strict=True)
        except FileNotFoundError:
            raise ColorError("INVALID_INPUT", f"{what} not found: {text}", {"reason": "not_found", "path": text})
        except (OSError, RuntimeError) as e:
            raise ColorError("INVALID_INPUT", f"cannot resolve {what} path: {e}", {"reason": "unresolvable", "path": text})
        if not resolved.is_file():
            raise ColorError("INVALID_INPUT", f"{what} is not a regular file: {text}", {"reason": "not_regular_file", "path": text})
        if self.allowed_lut_roots is not None and not any(_under(resolved, r) for r in self.allowed_lut_roots):
            raise ColorError("PATH_NOT_ALLOWED", f"{what} is outside the allowed LUT roots: {text}",
                             {"reason": "outside_allowed_roots", "allowed_lut_roots": [str(r) for r in self.allowed_lut_roots]})
        if not os.access(str(resolved), os.R_OK):
            raise ColorError("INVALID_INPUT", f"{what} is not readable: {text}", {"reason": "not_readable", "path": text})
        return resolved

    # ---- writes
    def resolve_write_path(self, path: object, what: str = "output", allow_dir: bool = False) -> Path:
        """A file this skill may create. Must resolve inside the workspace (deepest existing ancestor resolved so a
        symlinked directory cannot escape), with a safe file name in every component that does not exist yet."""
        text = _check_path_string(path, what)
        target = Path(text)
        if not target.is_absolute():
            target = self.workspace / target
        if any(part == ".." for part in Path(text).parts):
            raise ColorError("PATH_NOT_ALLOWED", f"{what} path may not contain '..': {text}", {"reason": "traversal"})
        probe = target
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            base = probe.resolve()
        except (OSError, RuntimeError) as e:
            raise ColorError("PATH_NOT_ALLOWED", f"cannot resolve {what} path: {e}", {"reason": "unresolvable"})
        resolved = base / target.relative_to(probe) if probe != target else base
        if not _under(resolved, self.workspace):
            raise ColorError("PATH_NOT_ALLOWED", f"{what} is outside the workspace: {text}",
                             {"reason": "outside_workspace", "workspace": str(self.workspace)})
        for part in target.relative_to(probe).parts if probe != target else (target.name,):
            check_filename(part)
        check_filename(resolved.name)
        if resolved.exists() and not (resolved.is_dir() if allow_dir else resolved.is_file()):
            raise ColorError("OUTPUT_ERROR", f"{what} exists and is not a regular {'directory' if allow_dir else 'file'}: {text}", {"reason": "wrong_kind"})
        return resolved

    def resolve_work_dir(self, name: str) -> Path:
        return self.resolve_write_path(name, "work directory", allow_dir=True)
