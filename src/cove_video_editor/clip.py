from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtGui import QImage, QPixmap


@dataclass
class AddedAudio:
    """One audio file placed on one of the audio tracks.

    Multiple entries can coexist on either track; each has its own timeline
    `offset` and plays for its natural `duration` (no looping). `lane`
    controls which audio row it renders on: 0 = Audio Track 1 (sits next to
    clip audio), 1 = Audio Track 2 (dedicated overlay lane).
    """
    path: Path
    duration: float = 0.0
    rate: int = 0
    peaks: list[float] = field(default_factory=list)
    offset: float = 0.0
    lane: int = 1
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    @property
    def timeline_end(self) -> float:
        return self.offset + self.duration

    def clone(self) -> AddedAudio:
        a = AddedAudio(
            path=self.path, duration=self.duration, rate=self.rate,
            peaks=list(self.peaks), offset=self.offset, lane=self.lane,
        )
        a.id = self.id
        return a


@dataclass
class MediaAsset:
    """A file in the clip bin. Independent of any timeline placement."""
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    kind: str = "video"        # "video" or "audio"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    thumb: QImage | None = None


@dataclass
class Clip:
    """One clip placed on the timeline.

    `src_start` / `src_end` are in the asset's time domain (seconds).
    `timeline_start` is the absolute position on the sequence timeline.
    `speed` multiplies playback speed — 0.5 = half, 2.0 = double.
    """

    asset: MediaAsset
    timeline_start: float = 0.0
    src_start: float = 0.0
    src_end: float = 0.0
    speed: float = 1.0
    muted: bool = False
    linked_audio: bool = True
    audio_offset: float = 0.0
    # True when the user deleted the clip's audio track; the video stays on
    # the timeline but no waveform is shown and the audio is silent. The
    # chain chip toggles this back to False when the user wants to restore.
    audio_removed: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    thumbs: list[QImage] = field(default_factory=list)
    thumb_pixmaps: list[QPixmap] = field(default_factory=list)
    waveform_peaks: list[float] = field(default_factory=list)
    waveform_rate: int = 0

    def __post_init__(self) -> None:
        if self.src_end <= 0:
            self.src_end = self.asset.duration

    @property
    def path(self) -> Path:
        return self.asset.path

    @property
    def src_span(self) -> float:
        return max(0.001, self.src_end - self.src_start)

    @property
    def timeline_length(self) -> float:
        return self.src_span / max(0.01, self.speed)

    @property
    def timeline_end(self) -> float:
        return self.timeline_start + self.timeline_length

    def clone(self) -> Clip:
        c = Clip(
            asset=self.asset, timeline_start=self.timeline_start,
            src_start=self.src_start, src_end=self.src_end, speed=self.speed,
            muted=self.muted, linked_audio=self.linked_audio,
            audio_offset=self.audio_offset, audio_removed=self.audio_removed,
        )
        c.thumbs = list(self.thumbs)
        c.thumb_pixmaps = list(self.thumb_pixmaps)
        c.waveform_peaks = self.waveform_peaks
        c.waveform_rate = self.waveform_rate
        return c

    def src_for_timeline(self, t_timeline: float) -> float:
        """Given a timeline second that falls within this clip, return the
        corresponding source second."""
        t = max(self.timeline_start, min(self.timeline_end, t_timeline))
        return self.src_start + (t - self.timeline_start) * self.speed


# ---- Region operations on the list of clips -------------------------------

def sort_clips(clips: list[Clip]) -> list[Clip]:
    return sorted(clips, key=lambda c: (c.timeline_start, c.id))


def sequence_length(clips: list[Clip]) -> float:
    if not clips:
        return 0.0
    return max(c.timeline_end for c in clips)


def clip_at_timeline(clips: list[Clip], t: float) -> Clip | None:
    for c in sort_clips(clips):
        if c.timeline_start <= t < c.timeline_end:
            return c
    # fall back to last clip if t is exactly at the end
    if clips:
        last = max(clips, key=lambda c: c.timeline_end)
        if abs(t - last.timeline_end) < 1e-3:
            return last
    return None


def split_clip(clip: Clip, t_timeline: float) -> Clip | None:
    """Split `clip` at timeline time `t`. Returns the new right-hand piece
    (the original is trimmed in place), or None if `t` is outside the clip."""
    if t_timeline <= clip.timeline_start + 0.01 or t_timeline >= clip.timeline_end - 0.01:
        return None
    src_t = clip.src_for_timeline(t_timeline)
    right = clip.clone()
    right.id = uuid.uuid4().hex[:8]
    right.src_start = src_t
    right.src_end = clip.src_end
    right.timeline_start = t_timeline
    clip.src_end = src_t
    return right


def delete_region(clips: list[Clip], start: float, end: float) -> list[Clip]:
    """Ripple-delete the [start, end) slice from the timeline.

    - Clips fully inside the region → dropped.
    - Clips partially overlapping → trimmed to the surviving side(s); clips
      spanning the whole region are split into two.
    - Clips after the region → shifted left by (end-start).
    """
    if end <= start:
        return clips
    dur = end - start
    out: list[Clip] = []
    for c in sort_clips(clips):
        cs, ce = c.timeline_start, c.timeline_end
        if ce <= start:
            out.append(c)
        elif cs >= end:
            c.timeline_start -= dur
            out.append(c)
        else:
            # some overlap
            if cs < start and ce > end:
                # split: keep left part, then right part shifted
                left = c
                right = left.clone()
                right.id = uuid.uuid4().hex[:8]
                # left keeps its timeline_start, trim its src_end to start
                left_src_end = left.src_for_timeline(start)
                # right starts at (end) on the timeline, but after shift by dur
                # we want it continuing from the original right segment
                right_src_start = left.src_for_timeline(end)
                left.src_end = left_src_end
                right.src_start = right_src_start
                right.timeline_start = start  # because everything after is shifted -dur; original right started at `end` → end-dur = start
                out.append(left)
                out.append(right)
            elif cs < start <= ce:
                # trim right side off
                c.src_end = c.src_for_timeline(start)
                out.append(c)
            elif cs < end <= ce:
                # trim left side off and shift
                c.src_start = c.src_for_timeline(end)
                c.timeline_start = start  # end - dur == start
                out.append(c)
            # fully inside → drop
    return sort_clips(out)


def keep_only_region(clips: list[Clip], start: float, end: float) -> list[Clip]:
    """Trim the sequence to only what's inside [start, end), shifting the
    kept content so it starts at t=0 on the timeline."""
    if end <= start:
        return clips
    out: list[Clip] = []
    for c in sort_clips(clips):
        cs, ce = c.timeline_start, c.timeline_end
        if ce <= start or cs >= end:
            continue
        new_src_start = c.src_start
        new_src_end = c.src_end
        new_ts = c.timeline_start
        if cs < start:
            new_src_start = c.src_for_timeline(start)
            new_ts = start
        if ce > end:
            new_src_end = c.src_for_timeline(end)
        c.src_start = new_src_start
        c.src_end = new_src_end
        c.timeline_start = new_ts - start
        out.append(c)
    return sort_clips(out)


def snap_to_next_boundary(clips: list[Clip], t: float, tolerance: float = 0.15) -> float:
    """Snap timeline time `t` to a nearby clip edge if within tolerance."""
    candidates = [0.0]
    for c in clips:
        candidates.append(c.timeline_start)
        candidates.append(c.timeline_end)
    best = t
    best_d = tolerance
    for cand in candidates:
        d = abs(cand - t)
        if d < best_d:
            best_d = d
            best = cand
    return best
