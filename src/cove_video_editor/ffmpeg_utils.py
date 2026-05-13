from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class FFmpegMissingError(RuntimeError):
    pass


if os.name == "nt":
    _CREATE_NO_WINDOW = 0x08000000
    _SUBPROCESS_KWARGS: dict = {"creationflags": _CREATE_NO_WINDOW}
else:
    _SUBPROCESS_KWARGS = {}


def _bundle_dirs() -> list[Path]:
    dirs: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass))
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    dirs.append(Path(__file__).resolve().parent.parent.parent / "assets" / "bin")
    return dirs


def _find_binary(name: str) -> str | None:
    exe = f"{name}.exe" if os.name == "nt" else name
    for d in _bundle_dirs():
        candidate = d / exe
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def require_ffmpeg() -> str:
    path = _find_binary("ffmpeg")
    if not path:
        raise FFmpegMissingError("ffmpeg not found")
    return path


def require_ffprobe() -> str:
    path = _find_binary("ffprobe")
    if not path:
        raise FFmpegMissingError("ffprobe not found")
    return path


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def probe(video: Path) -> VideoInfo:
    cmd = [
        require_ffprobe(),
        "-v", "error",
        "-show_entries", "stream=codec_type,width,height,r_frame_rate:format=duration",
        "-of", "json",
        str(video),
    ]
    out = subprocess.check_output(cmd, text=True, **_SUBPROCESS_KWARGS)
    data = json.loads(out)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise RuntimeError("no video stream")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = float(data["format"]["duration"])
    num, den = v["r_frame_rate"].split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    return VideoInfo(
        duration=duration,
        width=int(v["width"]),
        height=int(v["height"]),
        fps=fps,
        has_audio=has_audio,
    )


def probe_audio_duration(audio: Path) -> float:
    cmd = [
        require_ffprobe(),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio),
    ]
    out = subprocess.check_output(cmd, text=True, **_SUBPROCESS_KWARGS).strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def extract_thumbnail(video: Path, time: float, out: Path, height: int = 80) -> None:
    cmd = [
        require_ffmpeg(),
        "-y",
        "-ss", f"{time:.3f}",
        "-i", str(video),
        "-frames:v", "1",
        "-vf", f"scale=-2:{height}",
        "-q:v", "5",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, **_SUBPROCESS_KWARGS)


def extract_frame_full(video: Path, time: float, out: Path, quality: int = 2) -> None:
    """Full-resolution single-frame extract (`quality` 2 is near-lossless JPEG)."""
    cmd = [
        require_ffmpeg(),
        "-y",
        "-ss", f"{time:.3f}",
        "-i", str(video),
        "-frames:v", "1",
        "-q:v", str(quality),
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, **_SUBPROCESS_KWARGS)


# ---- Export pipeline ------------------------------------------------------

# Output containers + codec choices. Each entry is:
#   (display name, file extension, video codec, audio codec, extra args)
# "copy" is used only when no filters touch the stream (fast path).
EXPORT_FORMATS: dict[str, dict] = {
    "MP4 (H.264 + AAC)":   {"ext": "mp4",  "vcodec": "libx264",  "acodec": "aac",         "extra": ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]},
    "MP4 (H.265 + AAC)":   {"ext": "mp4",  "vcodec": "libx265",  "acodec": "aac",         "extra": ["-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-movflags", "+faststart"]},
    "MKV (H.264 + AAC)":   {"ext": "mkv",  "vcodec": "libx264",  "acodec": "aac",         "extra": ["-pix_fmt", "yuv420p"]},
    "WebM (VP9 + Opus)":   {"ext": "webm", "vcodec": "libvpx-vp9", "acodec": "libopus",   "extra": ["-b:v", "0", "-crf", "32", "-row-mt", "1"]},
    "MOV (H.264 + AAC)":   {"ext": "mov",  "vcodec": "libx264",  "acodec": "aac",         "extra": ["-pix_fmt", "yuv420p"]},
    "AVI (MPEG-4 + MP3)":  {"ext": "avi",  "vcodec": "mpeg4",    "acodec": "libmp3lame",  "extra": ["-qscale:v", "4", "-ar", "44100", "-ac", "2"]},
    "GIF (animation)":     {"ext": "gif",  "vcodec": "gif",      "acodec": None,           "extra": []},
    "MP3 (audio only)":    {"ext": "mp3",  "vcodec": None,       "acodec": "libmp3lame",   "extra": ["-q:a", "2"]},
    "WAV (audio only)":    {"ext": "wav",  "vcodec": None,       "acodec": "pcm_s16le",    "extra": []},
}


def escape_filter_arg(value: str) -> str:
    # Escape characters special to ffmpeg's filtergraph parser for filenames
    # passed as filter options (e.g. the subtitles= source path).
    return (
        value.replace("\\", "\\\\")
             .replace(":", "\\:")
             .replace("'", "\\\\'")
             .replace(",", "\\,")
    )


