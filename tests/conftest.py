import json
import os
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

from fixtures.generate import available, build_all, has_encoder  # noqa: E402
from color_grading.adapter import FfmpegSkill  # noqa: E402
from color_grading.errors import ColorError  # noqa: E402


def ffmpeg_skill_dir() -> Path:
    """The ffmpeg-skill checkout the integration tests run against. Nothing is skipped: a missing checkout fails."""
    try:
        return FfmpegSkill.locate(os.environ.get("COLOR_GRADING_FFMPEG_SKILL_DIR")).directory
    except ColorError as e:
        pytest.fail(f"ffmpeg-skill checkout is required for the integration tests (set COLOR_GRADING_FFMPEG_SKILL_DIR or clone ../ffmpeg-skill): {e.message} tried={e.details.get('tried')}")


@pytest.fixture(scope="session")
def skill_dir() -> Path:
    return ffmpeg_skill_dir()


@pytest.fixture(scope="session")
def hevc_available() -> bool:
    return has_encoder("libx265")


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    if not available():
        pytest.fail("ffmpeg / ffprobe are required for the integration tests (install FFmpeg); they are not skipped")
    return build_all(tmp_path_factory.mktemp("fixtures"))


@pytest.fixture
def workspace(tmp_path, media):
    """A fresh workspace with copies of the fixtures; the process cwd is moved there for the test."""
    ws = tmp_path / "ws"
    ws.mkdir()
    for p in media.values():
        shutil.copy(p, ws / p.name)
    old = os.getcwd()
    os.chdir(ws)
    try:
        yield ws
    finally:
        os.chdir(old)


def request_doc(ops, outputs=None, source=None, project_id="p1", options=None):
    source = source or {"source_id": "a", "path": "sdr.mp4"}
    last = ops[-1]["op_id"] if ops else None
    outputs = outputs or [{"output_id": "main", "operation": f"op:{last}" if last else "source", "path": "out/main.mp4", "format": "mp4"}]
    doc = {"schema": "color-grading/request@1", "project": {"project_id": project_id, "source": source, "operations": ops, "outputs": outputs}}
    if options:
        doc["options"] = options
    return doc


def run_cli(args, stdin_text=None, cwd=None):
    """Run the CLI in a subprocess (the real process boundary) and return (exit, stdout, stderr)."""
    import subprocess
    env = dict(os.environ)
    env["PYTHONPATH"] = str(HERE.parent / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("COLOR_GRADING_FFMPEG_SKILL_DIR", str(ffmpeg_skill_dir()))
    proc = subprocess.run([sys.executable, "-m", "color_grading.cli", *args], input=stdin_text, capture_output=True, text=True, env=env, cwd=cwd)
    return proc.returncode, proc.stdout, proc.stderr


def one_json(text: str):
    """stdout must be exactly one JSON document."""
    doc = json.loads(text)
    assert isinstance(doc, dict)
    return doc


def sample_avg_color(path):
    """Average RGB colour of the whole first frame, as a deterministic numeric fixture check (not a subjective
    'looks right' judgement): scale to 1x1 (box-filter average) and read the single output pixel."""
    import subprocess
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf", "scale=1:1:flags=area",
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    return tuple(out[:3])
