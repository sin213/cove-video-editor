from __future__ import annotations

import array
import subprocess
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QImage

from . import ffmpeg_utils as ff


class ThumbnailWorker(QObject):
    finished = Signal(str, list)   # clip id, list[QImage]
    failed = Signal(str, str)

    def __init__(self, clip_id: str, video: Path, duration: float, count: int = 24, height: int = 80) -> None:
        super().__init__()
        self._id = clip_id
        self._video = video
        self._duration = duration
        self._count = max(1, count)
        self._height = height
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        images: list[QImage] = []
        try:
            with tempfile.TemporaryDirectory(prefix="cove-ve-thumbs-") as tmp:
                tmp_path = Path(tmp)
                step = self._duration / self._count
                for i in range(self._count):
                    if self._cancelled:
                        return
                    t = min(self._duration - 0.05, max(0.0, step * (i + 0.5)))
                    out = tmp_path / f"t_{i:03d}.jpg"
                    try:
                        ff.extract_thumbnail(self._video, t, out, height=self._height)
                    except Exception:  # noqa: BLE001
                        continue
                    img = QImage(str(out))
                    if not img.isNull():
                        images.append(img.copy())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(self._id, str(exc))
            return
        self.finished.emit(self._id, images)


class WaveformWorker(QObject):
    """Decodes the audio to mono float32 at PEAK_RATE Hz, then emits a
    normalized absolute-amplitude envelope. The timeline renders it as a
    filled polygon, so it stays crisp at any zoom."""

    finished = Signal(str, list, int)   # clip id, peaks (list[float] in 0..1), rate
    failed = Signal(str, str)

    PEAK_RATE = 400

    def __init__(self, clip_id: str, path: Path) -> None:
        super().__init__()
        self._id = clip_id
        self._path = path
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            cmd = [
                ff.require_ffmpeg(),
                "-hide_banner", "-loglevel", "error",
                "-i", str(self._path),
                "-vn", "-ac", "1",
                "-ar", str(self.PEAK_RATE),
                "-f", "f32le",
                "-",
            ]
            proc = subprocess.run(
                cmd, check=True, capture_output=True,
                **ff._SUBPROCESS_KWARGS,  # type: ignore[attr-defined]
            )
            if self._cancelled:
                return
            samples = array.array("f")
            samples.frombytes(proc.stdout)
            if not samples:
                raise RuntimeError("audio stream produced no samples")
            peaks = [abs(s) for s in samples]
            peak_max = max(peaks)
            if peak_max > 1e-4:
                peaks = [p / peak_max for p in peaks]
            self.finished.emit(self._id, peaks, self.PEAK_RATE)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(self._id, str(exc))


def start_thumbnails(clip_id: str, video: Path, duration: float, count: int = 24) -> tuple[QThread, ThumbnailWorker]:
    # Callers must keep Python refs to both returned objects until the thread
    # finishes; we deliberately avoid deleteLater here because double-deletion
    # via Python GC + C++ deleteLater triggers a Qt fatal in PySide6.
    thread = QThread()
    worker = ThumbnailWorker(clip_id, video, duration, count=count)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    return thread, worker


def start_waveform(clip_id: str, video: Path) -> tuple[QThread, WaveformWorker]:
    thread = QThread()
    worker = WaveformWorker(clip_id, video)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    return thread, worker
