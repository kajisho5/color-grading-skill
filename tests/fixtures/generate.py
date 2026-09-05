"""Synthetic video fixtures generated with ffmpeg at test time (nothing binary is committed). Only the tests call
ffmpeg directly; the skill under test never does. Every fixture has a known construction:

  sdr.mp4       3 s 160x90 H.264 (yuv420p) solid colour + mono AAC tone, SDR, no explicit colour tags baked into
                the bitstream (like most untagged camera footage: ffmpeg-skill's RETAG can then genuinely retag it,
                see the note below)
  sdr_noaudio.mp4  2 s 160x90 H.264 (yuv420p), no audio stream, no explicit colour tags
  hevc_sdr.mp4  2 s 160x90 HEVC (yuv420p), SDR, no explicit colour tags, no Dolby Vision side data
  hdr.mp4       2 s 160x90 HEVC Main10 (yuv420p10le), tagged BT.2020 / PQ (smpte2084) - HDR10
  audio.wav     2 s mono PCM tone, no video stream (audio-only)
  text.txt      not media
  invert.cube   a 3D LUT (size 2) that exactly inverts every channel (output = 1 - input); trilinear/tetrahedral
                interpolation of a size-2 cube reproduces this affine map exactly at every point, so it is a
                deterministic, verifiable transform, not a subjective "look".

Note on RETAG: ffmpeg-skill's `--retag` rewrites colour tags via `-c copy -colorspace/-color_primaries/-color_trc`.
Measured behaviour: this reliably changes what `ffprobe` reports only when the source's own bitstream (the H.264/
HEVC SPS VUI) does not already carry an explicit, conflicting colour description -- a stream copy does not rewrite
already-encoded VUI bits, only the container-level hint. sdr.mp4/hevc_sdr.mp4 are therefore encoded *without*
explicit colour tags (as most untagged camera or screen-recording footage is), matching the case ffmpeg-skill's
own `--retag` is documented for ("the colours are tagged wrong") and the case its own test suite exercises."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Dict

FF = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]
TONE = "0.1*sin(2*PI*1000*t)"
SOLID = "color=c=0x8040c0:s=160x90:rate=25"

INVERT_CUBE = """TITLE "invert"
LUT_3D_SIZE 2
1.0 1.0 1.0
0.0 1.0 1.0
1.0 0.0 1.0
0.0 0.0 1.0
1.0 1.0 0.0
0.0 1.0 0.0
1.0 0.0 0.0
0.0 0.0 0.0
"""


def available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def has_encoder(name: str) -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return False
    return any(name in line for line in out.splitlines())


def _run(args):
    subprocess.run(FF + args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def build_all(directory: Path) -> Dict[str, Path]:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    f = {k: d / v for k, v in {"sdr": "sdr.mp4", "sdr_noaudio": "sdr_noaudio.mp4", "hevc_sdr": "hevc_sdr.mp4", "hdr": "hdr.mp4",
                               "audio": "audio.wav", "text": "text.txt", "invert_cube": "invert.cube"}.items()}
    _run(["-f", "lavfi", "-i", SOLID, "-f", "lavfi", "-i", f"aevalsrc='{TONE}':s=48000", "-t", "3",
          "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(f["sdr"])])
    _run(["-f", "lavfi", "-i", SOLID, "-t", "2", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(f["sdr_noaudio"])])
    _run(["-f", "lavfi", "-i", SOLID, "-t", "2", "-c:v", "libx265", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-tag:v", "hvc1", str(f["hevc_sdr"])])
    _run(["-f", "lavfi", "-i", SOLID, "-t", "2", "-c:v", "libx265", "-preset", "veryfast", "-pix_fmt", "yuv420p10le",
          "-colorspace", "bt2020nc", "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-tag:v", "hvc1", str(f["hdr"])])
    _run(["-f", "lavfi", "-i", f"aevalsrc='{TONE}':s=48000:c=mono", "-t", "2", "-c:a", "pcm_s16le", str(f["audio"])])
    f["text"].write_text("not media\n", encoding="utf-8")
    f["invert_cube"].write_text(INVERT_CUBE, encoding="utf-8")
    return f
