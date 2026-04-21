"""Multi-track NLE timeline widget.

Layout (top to bottom):
    Ruler            — time ticks + labels, scrubbable playhead
    Video track      — clip blocks (thumbnails), trim handles, drag-to-move
    Audio track      — waveform strip (drawn from each clip's ``waveform`` pixmap)

Interaction:
    - Click on ruler → move playhead
    - Click-drag on ruler (or empty area) → select a time region
    - Click on a clip → select it; drag → move it along the timeline
    - Drag the left/right edge of the selected clip → trim that side
    - Right-click on a selection → context menu (Delete / Crop-to-selection /
      Export)
    - Mouse wheel on the ruler → zoom in/out around cursor
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtWidgets import QMenu, QWidget

from .clip import AddedAudio, Clip, sequence_length, sort_clips
from .clip_bin import ASSET_MIME


RULER_H = 24
TRACK_GAP = 10
VIDEO_TRACK_H = 70
AUDIO_TRACK_H = 56
AUDIO2_TRACK_H = 48
VIDEO_TRACK_H_MIN = 40
VIDEO_TRACK_H_MAX = 260
AUDIO_TRACK_H_MIN = 36
AUDIO_TRACK_H_MAX = 220
LEFT_PAD = 8
RIGHT_PAD = 8
HANDLE_W = 8
MIN_PPS = 5.0
MAX_PPS = 800.0
CLICK_SLOP_PX = 5
CHAIN_BTN_W = 22
CHAIN_BTN_H = 18
CHAIN_BTN_MARGIN = 4


@dataclass
class _Drag:
    mode: str = ""                    # "seek", "select", "move_clip", "trim_l", "trim_r", "move_audio", "resize_tracks"
    clip_id: str = ""
    grab_offset_s: float = 0.0        # clip drag: t_clip_start at grab time
    original_clip_start: float = 0.0
    anchor_t: float = 0.0             # region-select anchor (seconds)
    press_x: int = 0                  # px position of the mouse press
    press_y: int = 0
    start_video_h: int = 0
    start_audio_h: int = 0
    moved: bool = False               # flips true once we exceed CLICK_SLOP_PX


class TimelineWidget(QWidget):
    rangeChanged = Signal(str, float, float)            # clip id, src_start, src_end
    clipMoved = Signal(str, float)                      # clip id, new timeline_start
    clipSelected = Signal(str)                          # clip id ("" = none)
    playheadMoved = Signal(float)                       # timeline seconds
    selectionChanged = Signal(float, float)             # selection start/end (seconds)
    regionDeleteRequested = Signal(float, float)
    regionCropRequested = Signal(float, float)
    regionExportRequested = Signal(float, float)
    splitAtPlayheadRequested = Signal()
    addedAudioDropped = Signal(str, float, int)           # path, drop_t, lane
    videoFileDropped = Signal(str, float)                 # OS video file, drop_t
    assetDroppedOnTimeline = Signal(str, float, int)      # asset id, drop_t, lane
    addedAudioReplaceToggled = Signal(bool)
    addedAudioOffsetChanged = Signal(str, float)          # audio id, offset
    addedAudioDeleteRequested = Signal(str)               # audio id ("" → clear all)
    clipDoubleClicked = Signal(str)               # clip id
    audioLinkToggled = Signal(str)                # clip id
    clipDeleteRequested = Signal(str)             # clip id
    audioOffsetChanged = Signal(str, float)       # clip id, new audio_offset (seconds)
    clipAudioRemoveRequested = Signal(str)        # clip id — delete clip's audio only
    scrollRangeChanged = Signal(int, int)         # scroll_max, page (in px)
    scrollValueChanged = Signal(int)              # current scroll_x (in px)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self._video_h: int = VIDEO_TRACK_H
        self._audio_h: int = AUDIO_TRACK_H
        self._audio2_h: int = AUDIO2_TRACK_H
        self._refresh_min_height()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAcceptDrops(True)

        self._clips: list[Clip] = []
        self._selected_id: str = ""
        self._selected_audio_clip_id: str = ""
        self._playhead: float = 0.0
        self._sel_start: float = 0.0
        self._sel_end: float = 0.0
        self._pps: float = 40.0           # pixels per second
        self._scroll_x: int = 0           # horizontal scroll offset in px
        self._drag = _Drag()
        self._added_audios: list[AddedAudio] = []
        self._added_audio_replace: bool = False
        self._added_audio_selected_id: str = ""

    def _refresh_min_height(self) -> None:
        total = (
            RULER_H + TRACK_GAP + self._video_h + TRACK_GAP + self._audio_h
            + TRACK_GAP + self._audio2_h + 4
        )
        self.setMinimumHeight(total)

    # ---- public API ---------------------------------------------------

    def set_clips(self, clips: list[Clip]) -> None:
        self._clips = sort_clips(clips)
        if self._selected_id and not any(c.id == self._selected_id for c in self._clips):
            self._selected_id = ""
        self._publish_scroll_range()
        self.update()

    def selected_id(self) -> str:
        return self._selected_id

    def selection(self) -> tuple[float, float]:
        if self._sel_end <= self._sel_start:
            return (0.0, 0.0)
        return (self._sel_start, self._sel_end)

    def clear_selection(self) -> None:
        if self._sel_end > self._sel_start:
            self._sel_start = 0.0
            self._sel_end = 0.0
            self.selectionChanged.emit(0.0, 0.0)
            self.update()

    def playhead(self) -> float:
        return self._playhead

    def set_playhead(self, t: float, emit: bool = True) -> None:
        self._playhead = max(0.0, min(self._total_length(), t))
        if emit:
            self.playheadMoved.emit(self._playhead)
        self._ensure_visible(self._playhead)
        self.update()

    def select_clip(self, clip_id: str) -> None:
        if clip_id != self._selected_id:
            self._selected_id = clip_id
            if clip_id:
                self._added_audio_selected_id = ""
                self._selected_audio_clip_id = ""
            self.clipSelected.emit(clip_id)
            self.update()

    def set_pixels_per_second(self, pps: float) -> None:
        self._pps = max(MIN_PPS, min(MAX_PPS, pps))
        self.update()

    def pixels_per_second(self) -> float:
        return self._pps

    def set_added_audios(self, audios: list[AddedAudio]) -> None:
        """Replace the list of added-audio entries. The timeline clones each
        entry so later edits on the app side don't silently affect painting."""
        self._added_audios = [a.clone() for a in audios]
        if self._added_audio_selected_id and not any(
            a.id == self._added_audio_selected_id for a in self._added_audios
        ):
            self._added_audio_selected_id = ""
        self.update()

    def added_audios(self) -> list[AddedAudio]:
        return list(self._added_audios)

    def set_added_audio_replace(self, replace: bool) -> None:
        self._added_audio_replace = bool(replace)

    def set_selection_range(self, start: float, end: float) -> None:
        self._sel_start = max(0.0, min(start, end))
        self._sel_end = max(self._sel_start, end)
        self.selectionChanged.emit(self._sel_start, self._sel_end)
        self.update()

    def set_scroll_x(self, x: int) -> None:
        self._scroll_x = max(0, int(x))
        self.update()

    def scroll_max_px(self) -> int:
        total = max(self._total_length() + 2.0, 0.1)
        track_w = max(1, self._track_rect().width())
        max_x = max(0, int(total * self._pps) - track_w)
        return max_x

    # ---- geometry -----------------------------------------------------

    def _total_length(self) -> float:
        added_end = max(
            (a.offset + a.duration for a in self._added_audios),
            default=0.0,
        )
        return max(
            sequence_length(self._clips), added_end,
            self._playhead, self._sel_end,
        )

    def _track_rect(self) -> QRect:
        return QRect(LEFT_PAD, 0, max(1, self.width() - LEFT_PAD - RIGHT_PAD),
                     self.height())

    def _ruler_rect(self) -> QRect:
        tr = self._track_rect()
        return QRect(tr.left(), 0, tr.width(), RULER_H)

    def _video_rect(self) -> QRect:
        tr = self._track_rect()
        return QRect(tr.left(), RULER_H + TRACK_GAP, tr.width(), self._video_h)

    def _audio_rect(self) -> QRect:
        vr = self._video_rect()
        return QRect(vr.left(), vr.bottom() + TRACK_GAP, vr.width(), self._audio_h)

    def _audio2_rect(self) -> QRect:
        ar = self._audio_rect()
        return QRect(ar.left(), ar.bottom() + TRACK_GAP, ar.width(), self._audio2_h)

    def _va_divider_rect(self) -> QRect:
        """Drag handle between the video and audio tracks."""
        vr = self._video_rect()
        return QRect(vr.left(), vr.bottom() + 1, vr.width(), TRACK_GAP - 2)

    def _time_to_x(self, t: float) -> int:
        return int(LEFT_PAD + t * self._pps - self._scroll_x)

    def _x_to_time(self, x: int) -> float:
        return max(0.0, (x - LEFT_PAD + self._scroll_x) / max(0.01, self._pps))

    def _clip_rect(self, c: Clip, track: QRect) -> QRect:
        x0 = self._time_to_x(c.timeline_start)
        x1 = self._time_to_x(c.timeline_end)
        return QRect(x0, track.top(), max(2, x1 - x0), track.height())

    def _ensure_visible(self, t: float) -> None:
        x = self._time_to_x(t)
        tr = self._track_rect()
        if x < tr.left() + 16:
            self._scroll_x = max(0, int((t * self._pps) - 16))
        elif x > tr.right() - 16:
            self._scroll_x = int((t * self._pps) - tr.width() + 16)
        self._publish_scroll_range()

    def _publish_scroll_range(self) -> None:
        self.scrollRangeChanged.emit(self.scroll_max_px(), self._track_rect().width())
        self.scrollValueChanged.emit(self._scroll_x)

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._publish_scroll_range()

    # ---- painting -----------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#1a1b1f"))

        rr = self._ruler_rect()
        vr = self._video_rect()
        ar = self._audio_rect()
        ar2 = self._audio2_rect()

        self._paint_ruler(p, rr)
        self._paint_video_track(p, vr)
        self._paint_audio_track(p, ar)
        self._paint_added_audio_track(p, ar2)
        self._paint_chain_chips(p, vr)
        self._paint_divider(p)

        # Selection band drawn ON TOP so it tints the track content (VideoPad-
        # style). The underlying thumbnails and waveform are still visible.
        if self._sel_end > self._sel_start:
            sx = self._time_to_x(self._sel_start)
            ex = self._time_to_x(self._sel_end)
            band = QRect(sx, rr.top(), max(1, ex - sx), ar2.bottom() - rr.top())
            p.fillRect(band, QColor(63, 120, 200, 95))
            pen = QPen(QColor(95, 180, 255, 230))
            pen.setWidth(2)
            p.setPen(pen)
            p.drawLine(sx, rr.top(), sx, ar2.bottom())
            p.drawLine(ex, rr.top(), ex, ar2.bottom())

        # playhead on top
        ph_x = self._time_to_x(self._playhead)
        if rr.left() - 4 <= ph_x <= rr.right() + 4:
            p.setPen(QPen(QColor("#ff5050"), 1))
            p.drawLine(ph_x, rr.top(), ph_x, ar2.bottom())
            p.setBrush(QColor("#ff5050"))
            p.setPen(Qt.NoPen)
            p.drawPolygon([
                QPoint(ph_x - 5, rr.top() + 2),
                QPoint(ph_x + 5, rr.top() + 2),
                QPoint(ph_x,     rr.top() + 10),
            ])

        p.end()

    def _paint_ruler(self, p: QPainter, rr: QRect) -> None:
        p.fillRect(rr, QColor("#24262c"))
        p.setPen(QColor("#3a3f4a"))
        p.drawLine(rr.bottomLeft(), rr.bottomRight())

        step = _choose_tick_step(self._pps)
        total = max(1.0, self._total_length() + 2.0)
        p.setPen(QColor("#cfd0d4"))
        f = p.font(); f.setPointSize(8); p.setFont(f)
        t = 0.0
        while t < total + step:
            x = self._time_to_x(t)
            if rr.left() - 2 <= x <= rr.right() + 2:
                p.setPen(QColor("#5a616f"))
                p.drawLine(x, rr.bottom() - 6, x, rr.bottom())
                p.setPen(QColor("#cfd0d4"))
                p.drawText(QPoint(x + 3, rr.bottom() - 6), _fmt_tick(t))
            t += step

    def _paint_video_track(self, p: QPainter, vr: QRect) -> None:
        p.fillRect(vr, QColor("#0f1116"))
        for c in self._clips:
            r = self._clip_rect(c, vr)
            if r.right() < vr.left() or r.left() > vr.right():
                continue
            self._paint_clip_video_body(p, c, r)

    def _paint_clip_video_body(self, p: QPainter, c: Clip, r: QRect) -> None:
        clip_rect = r.intersected(self._video_rect())
        if clip_rect.isEmpty():
            return

        # Background
        p.fillRect(clip_rect, QColor("#23252b"))

        # Aspect-correct thumb tiling. The slot fractions are mapped into
        # [src_start/dur, src_end/dur] so trimmed clips show the frames that
        # are actually in the clip, not frames from outside the trim window.
        if c.thumb_pixmaps:
            aspect = c.asset.width / max(1, c.asset.height)
            thumb_w = max(30, int(round(r.height() * aspect)))
            slots = max(1, r.width() // thumb_w)
            tile_w = r.width() / slots
            dur = max(0.001, c.asset.duration)
            start_frac = max(0.0, min(1.0, c.src_start / dur))
            end_frac = max(start_frac, min(1.0, c.src_end / dur))
            n = len(c.thumb_pixmaps)
            for i in range(slots):
                tgt = QRect(
                    int(round(r.left() + i * tile_w)), r.top(),
                    int(round(tile_w)) + 1, r.height(),
                )
                vis = tgt.intersected(clip_rect)
                if vis.isEmpty():
                    continue
                local_frac = (i + 0.5) / slots
                src_frac = start_frac + local_frac * (end_frac - start_frac)
                idx = min(n - 1, max(0, int(src_frac * n)))
                thumb = c.thumb_pixmaps[idx]
                scaled = thumb.scaled(
                    tgt.width(), tgt.height(),
                    Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
                )
                sx = max(0, (scaled.width() - tgt.width()) // 2) + (vis.left() - tgt.left())
                sy = max(0, (scaled.height() - tgt.height()) // 2)
                p.drawPixmap(
                    vis, scaled,
                    QRect(sx, sy, vis.width(), vis.height()),
                )
        elif c.asset.thumb is not None and not c.asset.thumb.isNull():
            # Thumb worker hasn't finished yet — show the single asset thumb
            # stretched across the clip so it's obvious the clip is there.
            pm = QPixmap.fromImage(c.asset.thumb)
            scaled = pm.scaled(
                clip_rect.width(), clip_rect.height(),
                Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
            )
            sx = max(0, (scaled.width() - clip_rect.width()) // 2)
            sy = max(0, (scaled.height() - clip_rect.height()) // 2)
            p.drawPixmap(
                clip_rect, scaled,
                QRect(sx, sy, clip_rect.width(), clip_rect.height()),
            )

        # Selection → border only (no clip body tint, so region-select tint
        # stays clearly distinguishable from "this clip is selected").
        is_selected = c.id == self._selected_id
        color = QColor("#5fb4ff") if is_selected else QColor("#39404d")
        pen = QPen(color)
        pen.setWidth(3 if is_selected else 1)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(clip_rect.adjusted(0, 0, -1, -1))

        if is_selected:
            left = QRect(r.left(), r.top(), HANDLE_W, r.height()).intersected(self._video_rect())
            right = QRect(r.right() - HANDLE_W, r.top(), HANDLE_W, r.height()).intersected(self._video_rect())
            p.fillRect(left, QColor(95, 180, 255, 200))
            p.fillRect(right, QColor(95, 180, 255, 200))

    def _chain_button_rect(self, clip_rect: QRect) -> QRect:
        """Chain chip sits in the gap between the video and audio tracks,
        anchored to the clip's left edge (so it visually joins the two)."""
        vr = self._video_rect()
        ar = self._audio_rect()
        mid = (vr.bottom() + ar.top()) // 2
        return QRect(
            clip_rect.left() + CHAIN_BTN_MARGIN,
            mid - CHAIN_BTN_H // 2,
            CHAIN_BTN_W, CHAIN_BTN_H,
        )

    def _paint_chain_icon(self, p: QPainter, rect: QRect, linked: bool) -> None:
        # Rounded chip background
        p.setPen(QPen(QColor("#0a0c11"), 1))
        p.setBrush(QColor(20, 22, 28, 220))
        p.drawRoundedRect(rect.adjusted(0, 0, -1, -1), 4, 4)

        color = QColor("#8fd47a") if linked else QColor("#ff6b6b")
        pen = QPen(color, 1.6)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)

        # Two rounded link rectangles overlapping slightly in the middle.
        cx = rect.center().x()
        cy = rect.center().y()
        half_w = 6
        half_h = 4
        left_link = QRect(cx - half_w - 2, cy - half_h, half_w * 2, half_h * 2)
        right_link = QRect(cx - half_w + 4, cy - half_h, half_w * 2, half_h * 2)
        p.drawRoundedRect(left_link, 3, 3)
        p.drawRoundedRect(right_link, 3, 3)

        if not linked:
            # Diagonal break stroke
            p.setPen(QPen(QColor("#ff6b6b"), 2))
            p.drawLine(rect.left() + 3, rect.bottom() - 3,
                       rect.right() - 3, rect.top() + 3)

    def _paint_divider(self, p: QPainter) -> None:
        gap = self._va_divider_rect()
        if gap.height() <= 0:
            return
        # Two faint horizontal grip lines centered in the gap.
        p.setPen(QPen(QColor("#3a414f"), 1))
        cy = gap.center().y()
        left = gap.left() + 30
        right = gap.right() - 30
        p.drawLine(left, cy - 1, right, cy - 1)
        p.drawLine(left, cy + 1, right, cy + 1)

    def _paint_chain_chips(self, p: QPainter, vr: QRect) -> None:
        """Draw a chain chip in the gap for every clip that has audio."""
        for c in self._clips:
            if not c.asset.has_audio:
                continue
            r = self._clip_rect(c, vr)
            if r.right() < vr.left() or r.left() > vr.right():
                continue
            btn = self._chain_button_rect(r)
            self._paint_chain_icon(p, btn, c.linked_audio)

    def _paint_audio_track(self, p: QPainter, ar: QRect) -> None:
        p.fillRect(ar, QColor("#0f1116"))
        for c in self._clips:
            r_vid = self._clip_rect(c, self._video_rect())
            # Audio shifts horizontally by audio_offset (in seconds) when the
            # user has unlinked and dragged it — linked clips always have
            # audio_offset=0.
            offset_px = int(round(c.audio_offset * self._pps))
            r = QRect(r_vid.left() + offset_px, ar.top(), r_vid.width(), ar.height())
            r_vis = r.intersected(ar)
            if r_vis.isEmpty():
                continue

            if not c.asset.has_audio or c.audio_removed:
                p.fillRect(r_vis, QColor("#16181d"))
                p.setPen(QColor("#5a616f"))
                label = (
                    "(audio deleted — chain chip to restore)"
                    if c.audio_removed else "no audio"
                )
                p.drawText(r_vis, Qt.AlignCenter, label)
                continue

            # Background tint — linked vs. unlinked.
            p.fillRect(r_vis, QColor("#16263a") if not c.linked_audio else QColor("#0d1622"))

            if c.waveform_peaks and c.waveform_rate > 0:
                wave_color = QColor("#5fb4ff") if c.linked_audio else QColor("#ffb067")
                self._draw_clip_waveform(p, c, r, r_vis, wave_color)

            audio_selected = c.id == self._selected_audio_clip_id
            if audio_selected:
                color = "#ffd38a"
                width = 2
            elif c.id == self._selected_id:
                color = "#5fb4ff"
                width = 2
            elif not c.linked_audio:
                color = "#ffb067"
                width = 2
            else:
                color = "#2a3a52"
                width = 1
            pen = QPen(QColor(color))
            pen.setWidth(width)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(r_vis.adjusted(0, 0, -1, -1))

        # Lane-0 added-audio entries live on Track 1 alongside clip audio.
        self._paint_added_audio_entries(p, ar, lane=0)

    def _paint_added_audio_track(self, p: QPainter, ar: QRect) -> None:
        p.fillRect(ar, QColor("#0f1116"))
        if not any(a.lane == 1 for a in self._added_audios):
            p.setPen(QColor("#5a616f"))
            f = p.font(); f.setItalic(True); p.setFont(f)
            p.drawText(
                ar, Qt.AlignCenter,
                "Drag audio here for Audio Track 2 (sound effects, music, etc.)",
            )
            f.setItalic(False); p.setFont(f)
            return
        self._paint_added_audio_entries(p, ar, lane=1)

    def _paint_added_audio_entries(self, p: QPainter, track_rect: QRect, lane: int) -> None:
        for audio in self._added_audios:
            if audio.lane != lane or audio.duration <= 0:
                continue
            tile = self._added_audio_tile_rect(audio)
            tile_vis = tile.intersected(track_rect)
            if tile_vis.isEmpty():
                continue
            p.fillRect(tile_vis, QColor("#2a1f10"))
            if audio.peaks and audio.rate > 0:
                self._draw_added_audio_chunk(
                    p, audio.peaks, audio.rate,
                    tile, tile_vis, audio.duration, QColor("#ffb067"),
                )
            selected = audio.id == self._added_audio_selected_id
            if selected:
                pen = QPen(QColor("#ffd38a"))
                pen.setWidth(2)
            else:
                pen = QPen(QColor("#5a3a1a"))
                pen.setWidth(1)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(tile_vis.adjusted(0, 0, -1, -1))

    def _draw_clip_waveform(
        self, p: QPainter, c: Clip, r: QRect, r_vis: QRect, color: QColor,
    ) -> None:
        """Build a filled polygon from `c.waveform_peaks` for the pixel columns
        of `r_vis`. The waveform stays crisp at any zoom because we resample
        from peak data every paint instead of scaling a bitmap."""
        peaks = c.waveform_peaks
        rate = c.waveform_rate
        if not peaks or rate <= 0:
            return

        cy = r.center().y()
        half_h = max(1, r.height() // 2 - 2)

        # seconds of source audio covered by one screen pixel
        src_per_px = max(c.speed / max(0.01, self._pps), 1.0 / rate)
        samples_per_px = src_per_px * rate
        half_window_samples = max(1, int(round(samples_per_px / 2)))
        interp = samples_per_px < 2.0
        n_peaks = len(peaks)

        # For each visible column, look up the peak from the clip's source
        # time, bounded to the clip's trimmed source window.
        src_start = c.src_start
        src_end = c.src_end
        left_px = r.left()
        pps = self._pps
        speed = c.speed

        top_pts: list[QPointF] = []
        bot_pts: list[QPointF] = []
        for x in range(r_vis.left(), r_vis.right() + 1):
            # column x → source time (relative to the video start, then shifted
            # back by audio_offset since the rect is already offset)
            px_into_rect = x - left_px
            src_t = src_start + (px_into_rect / pps) * speed
            if src_t < src_start or src_t > src_end:
                continue
            if interp:
                raw_idx = src_t * rate
                i0 = int(raw_idx)
                if i0 < 0:
                    continue
                if i0 >= n_peaks - 1:
                    pk = peaks[n_peaks - 1]
                else:
                    f = raw_idx - i0
                    pk = peaks[i0] * (1.0 - f) + peaks[i0 + 1] * f
            else:
                idx = int(src_t * rate)
                a = max(0, idx - half_window_samples)
                b = min(n_peaks, idx + half_window_samples + 1)
                if b <= a:
                    continue
                pk = max(peaks[a:b])
            y = pk * half_h
            top_pts.append(QPointF(x + 0.5, cy - y))
            bot_pts.append(QPointF(x + 0.5, cy + y))

        if not top_pts:
            return
        polygon = QPolygonF(top_pts + list(reversed(bot_pts)))
        path = QPainterPath()
        path.addPolygon(polygon)
        path.closeSubpath()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.fillPath(path, color)
        # centerline for a polished look
        p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 90), 1))
        p.drawLine(r_vis.left(), cy, r_vis.right(), cy)

    def _draw_added_audio_chunk(
        self, p: QPainter, peaks: list[float], rate: int,
        chunk: QRect, chunk_vis: QRect, chunk_seconds: float,
        color: QColor,
    ) -> None:
        """Draw one loop iteration of the added audio. The chunk's left edge
        maps to source second 0; its right edge to `chunk_seconds`."""
        n = len(peaks)
        if n == 0 or rate <= 0 or chunk.width() <= 0 or chunk_seconds <= 0:
            return
        cy = chunk.center().y()
        half_h = max(1, chunk.height() // 2 - 2)
        pps = max(0.01, self._pps)
        src_per_px = 1.0 / pps
        samples_per_px = src_per_px * rate
        # Interpolate between adjacent peaks when zoomed in enough that each
        # pixel covers less than ~2 samples — otherwise use max-in-window.
        interp = samples_per_px < 2.0
        half_window = max(1, int(round(samples_per_px / 2)))
        top_pts: list[QPointF] = []
        bot_pts: list[QPointF] = []
        for x in range(chunk_vis.left(), chunk_vis.right() + 1):
            audio_t = (x - chunk.left()) / pps
            if audio_t < 0 or audio_t > chunk_seconds:
                continue
            if interp:
                raw_idx = audio_t * rate
                i0 = int(raw_idx)
                if i0 < 0:
                    continue
                if i0 >= n - 1:
                    pk = peaks[n - 1]
                else:
                    f = raw_idx - i0
                    pk = peaks[i0] * (1.0 - f) + peaks[i0 + 1] * f
            else:
                idx = int(audio_t * rate)
                a = max(0, idx - half_window)
                b = min(n, idx + half_window + 1)
                if b <= a:
                    continue
                pk = max(peaks[a:b])
            y = pk * half_h
            top_pts.append(QPointF(x + 0.5, cy - y))
            bot_pts.append(QPointF(x + 0.5, cy + y))
        if not top_pts:
            return
        polygon = QPolygonF(top_pts + list(reversed(bot_pts)))
        path = QPainterPath()
        path.addPolygon(polygon)
        path.closeSubpath()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.fillPath(path, color)
        p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 90), 1))
        p.drawLine(chunk_vis.left(), cy, chunk_vis.right(), cy)

    # ---- mouse --------------------------------------------------------

    _PLAYHEAD_SLOP_PX = 4

    def _is_over_playhead(self, pos: QPoint) -> bool:
        return abs(pos.x() - self._time_to_x(self._playhead)) <= self._PLAYHEAD_SLOP_PX

    def _audio_clip_rect(self, c: Clip) -> QRect:
        """Rect for a clip's audio block on the audio track (honors offset)."""
        vr = self._video_rect()
        ar = self._audio_rect()
        r_vid = self._clip_rect(c, vr)
        offset_px = int(round(c.audio_offset * self._pps))
        return QRect(r_vid.left() + offset_px, ar.top(), r_vid.width(), ar.height())

    def _added_audio_tile_rect(self, audio: AddedAudio) -> QRect:
        """Rect for a single added-audio entry on its lane (Track 1 or 2)."""
        lane_rect = self._audio_rect() if audio.lane == 0 else self._audio2_rect()
        x0 = self._time_to_x(audio.offset)
        x1 = self._time_to_x(audio.offset + max(0.01, audio.duration))
        return QRect(x0, lane_rect.top(), max(2, x1 - x0), lane_rect.height())

    def _hit_added_audio(self, pos: QPoint, lane: int) -> AddedAudio | None:
        """Topmost added-audio entry on `lane` under `pos`, iterating
        newest-first so later additions win when two entries overlap."""
        for audio in reversed(self._added_audios):
            if audio.lane != lane:
                continue
            if audio.duration <= 0 or not audio.peaks:
                continue
            if self._added_audio_tile_rect(audio).contains(pos):
                return audio
        return None

    def _lane_for_y(self, y: int) -> int:
        """Return 0 for Audio Track 1 area, 1 for Audio Track 2 (default)."""
        ar = self._audio_rect()
        if ar.top() <= y <= ar.bottom():
            return 0
        return 1

    def _hit_test(self, pos: QPoint) -> _Drag:
        rr = self._ruler_rect()
        vr = self._video_rect()
        ar = self._audio_rect()
        ar2 = self._audio2_rect()
        gap = self._va_divider_rect()

        # Chain chip lives in the gap — check it before the generic gap drag.
        for c in self._clips:
            if not c.asset.has_audio:
                continue
            r = self._clip_rect(c, vr)
            if self._chain_button_rect(r).contains(pos):
                return _Drag(mode="chain", clip_id=c.id)

        # Video/audio gap → drag to resize tracks
        if gap.contains(pos):
            return _Drag(mode="resize_tracks")

        if rr.contains(pos):
            # Clicking anywhere on the ruler — including the red playhead
            # handle itself — starts a seek drag.
            return _Drag(mode="seek")

        if vr.contains(pos):
            sel = next((c for c in self._clips if c.id == self._selected_id), None)
            if sel is not None:
                r = self._clip_rect(sel, vr)
                if abs(pos.x() - r.left()) <= HANDLE_W:
                    return _Drag(mode="trim_l", clip_id=sel.id)
                if abs(pos.x() - r.right()) <= HANDLE_W:
                    return _Drag(mode="trim_r", clip_id=sel.id)
            if self._is_over_playhead(pos):
                return _Drag(mode="playhead_region")
            for c in self._clips:
                r = self._clip_rect(c, vr)
                if r.contains(pos):
                    return _Drag(mode="move_clip", clip_id=c.id)
            return _Drag(mode="select")

        if ar.contains(pos):
            if self._is_over_playhead(pos):
                return _Drag(mode="playhead_region")
            # Lane-0 added audio sits on top of clip audio — check it first.
            hit_audio = self._hit_added_audio(pos, lane=0)
            if hit_audio is not None:
                return _Drag(mode="select_added", clip_id=hit_audio.id)
            # Unlinked clip audio block under cursor → drag it independently.
            for c in self._clips:
                if not c.asset.has_audio or c.audio_removed:
                    continue
                rect = self._audio_clip_rect(c)
                if rect.contains(pos):
                    if not c.linked_audio:
                        return _Drag(mode="move_audio", clip_id=c.id)
                    return _Drag(mode="move_clip", clip_id=c.id)
            return _Drag(mode="select")

        if ar2.contains(pos):
            if self._is_over_playhead(pos):
                return _Drag(mode="playhead_region")
            hit_audio = self._hit_added_audio(pos, lane=1)
            if hit_audio is not None:
                return _Drag(mode="select_added", clip_id=hit_audio.id)
            return _Drag(mode="select")
        return _Drag(mode="select")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        if event.button() == Qt.RightButton:
            self._show_context_menu(event.globalPosition().toPoint(), pos)
            return
        if event.button() != Qt.LeftButton:
            return

        shift_held = bool(event.modifiers() & Qt.ShiftModifier)
        hit = self._hit_test(pos)
        hit.press_x = pos.x()

        # Any press that isn't a click on the added-audio tile clears the
        # added-audio selection highlight.
        if hit.mode != "select_added" and self._added_audio_selected_id:
            self._added_audio_selected_id = ""
            self.update()
        # Clicking anywhere other than an unlinked clip's audio block clears
        # the audio-only selection.
        if hit.mode != "move_audio" and self._selected_audio_clip_id:
            self._selected_audio_clip_id = ""
            self.update()

        # Any left-click that isn't starting a new region selection (shift-
        # drag or playhead-region drag) clears the existing blue highlight.
        region_modes = ("playhead_region",)
        if (
            not shift_held
            and hit.mode not in region_modes
            and self._sel_end > self._sel_start
        ):
            self.clear_selection()

        # Shift anywhere → region-select regardless of what's under the cursor.
        if shift_held and hit.mode != "seek":
            t = self._x_to_time(pos.x())
            self._drag = _Drag(mode="select", anchor_t=t, press_x=pos.x())
            self._sel_start = t
            self._sel_end = t
            self.update()
            return

        if hit.mode == "seek":
            self.clear_selection()
            self.set_playhead(self._x_to_time(pos.x()))
            self._drag = hit
            return
        if hit.mode == "playhead_region":
            t = self._playhead
            self._drag = _Drag(mode="select", anchor_t=t, press_x=pos.x())
            self._sel_start = t
            self._sel_end = t
            self.update()
            return
        if hit.mode == "chain":
            self.audioLinkToggled.emit(hit.clip_id)
            self._drag = _Drag()
            return
        if hit.mode == "select_added":
            # Click an added-audio tile → select it and arm a drag so the
            # user can reposition it along the timeline.
            audio = next(
                (a for a in self._added_audios if a.id == hit.clip_id), None,
            )
            if audio is None:
                self._drag = _Drag()
                return
            self._added_audio_selected_id = audio.id
            if self._selected_id:
                self._selected_id = ""
                self.clipSelected.emit("")
            self.clear_selection()
            self._drag = _Drag(
                mode="move_added",
                clip_id=audio.id,
                grab_offset_s=self._x_to_time(pos.x()) - audio.offset,
                press_x=pos.x(),
            )
            self.update()
            return
        if hit.mode == "resize_tracks":
            self._drag = _Drag(
                mode="resize_tracks", press_x=pos.x(), press_y=pos.y(),
                start_video_h=self._video_h, start_audio_h=self._audio_h,
            )
            return
        if hit.mode in ("trim_l", "trim_r"):
            self._drag = hit
            return
        if hit.mode == "move_audio":
            c = self._find(hit.clip_id)
            if c is not None:
                # Select just the audio block, not the whole clip — Delete
                # then removes the audio without touching the video.
                self._selected_audio_clip_id = c.id
                if self._selected_id:
                    self._selected_id = ""
                    self.clipSelected.emit("")
                self.clear_selection()
                self._drag = _Drag(
                    mode="move_audio", clip_id=c.id,
                    grab_offset_s=self._x_to_time(pos.x()) - (c.timeline_start + c.audio_offset),
                    press_x=pos.x(),
                )
                self.update()
                return
        if hit.mode == "move_clip":
            c = self._find(hit.clip_id)
            if c is not None:
                if c.id != self._selected_id:
                    self.select_clip(c.id)
                self._drag = _Drag(
                    mode="move_clip", clip_id=c.id,
                    original_clip_start=c.timeline_start,
                    grab_offset_s=self._x_to_time(pos.x()) - c.timeline_start,
                    press_x=pos.x(),
                )
                return

        # empty area drag → region select
        t = self._x_to_time(pos.x())
        self._drag = _Drag(mode="select", anchor_t=t, press_x=pos.x())
        self._sel_start = t
        self._sel_end = t
        self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton:
            return
        pos = event.position().toPoint()
        # Double click on a clip → emit for properties dialog.
        for c in self._clips:
            r = self._clip_rect(c, self._video_rect())
            if r.contains(pos):
                self.select_clip(c.id)
                self.clipDoubleClicked.emit(c.id)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        if not self._drag.mode:
            hit = self._hit_test(pos)
            self.setCursor({
                "seek": Qt.PointingHandCursor,
                "select": Qt.IBeamCursor,
                "trim_l": Qt.SplitHCursor, "trim_r": Qt.SplitHCursor,
                "move_clip": Qt.SizeAllCursor,
                "move_audio": Qt.SizeHorCursor,
                "playhead_region": Qt.SizeHorCursor,
                "resize_tracks": Qt.SplitVCursor,
                "chain": Qt.PointingHandCursor,
                "select_added": Qt.SizeAllCursor,
            }.get(hit.mode, Qt.ArrowCursor))
            return

        if (abs(pos.x() - self._drag.press_x) > CLICK_SLOP_PX
                or abs(pos.y() - self._drag.press_y) > CLICK_SLOP_PX):
            self._drag.moved = True

        t = self._x_to_time(pos.x())
        if self._drag.mode == "seek":
            self.set_playhead(t)
        elif self._drag.mode == "select":
            a = self._drag.anchor_t
            self._sel_start = min(a, t)
            self._sel_end = max(a, t)
            self.update()
        elif self._drag.mode in ("trim_l", "trim_r"):
            self._apply_trim(self._drag.mode, t)
        elif self._drag.mode == "move_clip":
            if self._drag.moved:
                new_start = max(0.0, t - self._drag.grab_offset_s)
                c = self._find(self._drag.clip_id)
                if c is not None:
                    new_start = self._snap_clip_start(c, new_start)
                    # When the audio is unlinked, keep its absolute timeline
                    # position fixed by compensating audio_offset against the
                    # new video start. Linked audio rides along as before.
                    if not c.linked_audio:
                        old_audio_abs = c.timeline_start + c.audio_offset
                        c.audio_offset = old_audio_abs - new_start
                    c.timeline_start = new_start
                    self.update()
        elif self._drag.mode == "move_audio":
            c = self._find(self._drag.clip_id)
            if c is not None and self._drag.moved:
                new_audio_start = max(0.0, t - self._drag.grab_offset_s)
                c.audio_offset = new_audio_start - c.timeline_start
                self.update()
        elif self._drag.mode == "move_added":
            if self._drag.moved:
                audio = next(
                    (a for a in self._added_audios if a.id == self._drag.clip_id),
                    None,
                )
                if audio is not None:
                    audio.offset = max(0.0, t - self._drag.grab_offset_s)
                    self.update()
        elif self._drag.mode == "resize_tracks":
            dy = pos.y() - self._drag.press_y
            new_vh = max(VIDEO_TRACK_H_MIN,
                         min(VIDEO_TRACK_H_MAX, self._drag.start_video_h + dy))
            new_ah = max(AUDIO_TRACK_H_MIN,
                         min(AUDIO_TRACK_H_MAX, self._drag.start_audio_h - dy))
            if new_vh != self._video_h or new_ah != self._audio_h:
                self._video_h = new_vh
                self._audio_h = new_ah
                self._refresh_min_height()
                self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        mode = self._drag.mode
        if mode in ("trim_l", "trim_r"):
            c = self._find(self._drag.clip_id)
            if c is not None:
                self.rangeChanged.emit(c.id, c.src_start, c.src_end)
        elif mode == "move_clip":
            c = self._find(self._drag.clip_id)
            if c is not None and self._drag.moved:
                self._resolve_overlaps(c)
                self.clipMoved.emit(c.id, c.timeline_start)
                self._clips = sort_clips(self._clips)
                self.update()
            # A bare click on a clip just leaves it selected — no region span.
        elif mode == "move_audio":
            c = self._find(self._drag.clip_id)
            if c is not None and self._drag.moved:
                self.audioOffsetChanged.emit(c.id, c.audio_offset)
        elif mode == "move_added":
            if self._drag.moved:
                audio = next(
                    (a for a in self._added_audios if a.id == self._drag.clip_id),
                    None,
                )
                if audio is not None:
                    self.addedAudioOffsetChanged.emit(audio.id, audio.offset)
        elif mode == "select":
            if self._sel_end > self._sel_start + 0.01:
                self.selectionChanged.emit(self._sel_start, self._sel_end)
            else:
                # bare click on empty area → clear any old selection
                self.clear_selection()
        self._drag = _Drag()

    def wheelEvent(self, event: QWheelEvent) -> None:
        # Plain wheel zooms horizontally around the cursor; shift+wheel scrolls.
        # Track resizing is drag-only via the video/audio divider.
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.ShiftModifier:
            step_px = 80 if delta > 0 else -80
            self._scroll_x = max(0, min(self.scroll_max_px(), self._scroll_x - step_px))
            self._publish_scroll_range()
            self.update()
            return

        factor = 1.2 if delta > 0 else (1 / 1.2)
        pivot_t = self._x_to_time(int(event.position().x()))
        new_pps = max(MIN_PPS, min(MAX_PPS, self._pps * factor))
        if new_pps != self._pps:
            old_screen_x = int(event.position().x())
            self._pps = new_pps
            self._scroll_x = max(0, int(pivot_t * self._pps - (old_screen_x - LEFT_PAD)))
            self._publish_scroll_range()
            self.update()

    def keyPressEvent(self, event) -> None:  # noqa: ANN001
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            if self._sel_end > self._sel_start:
                self.regionDeleteRequested.emit(self._sel_start, self._sel_end)
                event.accept()
                return
            if self._added_audio_selected_id:
                audio_id = self._added_audio_selected_id
                self._added_audio_selected_id = ""
                self.addedAudioDeleteRequested.emit(audio_id)
                event.accept()
                return
            if self._selected_audio_clip_id:
                cid = self._selected_audio_clip_id
                self._selected_audio_clip_id = ""
                self.clipAudioRemoveRequested.emit(cid)
                event.accept()
                return
            if self._selected_id:
                self.clipDeleteRequested.emit(self._selected_id)
                event.accept()
                return
        if event.key() == Qt.Key_S and event.modifiers() == Qt.NoModifier:
            self.splitAtPlayheadRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    # ---- helpers ------------------------------------------------------

    def _apply_trim(self, mode: str, t_timeline: float) -> None:
        c = self._find(self._drag.clip_id)
        if c is None:
            return
        min_gap = max(0.05, c.asset.duration * 0.005)
        if mode == "trim_l":
            # trim the source start; keep the right edge anchored on the timeline
            anchor_end_t = c.timeline_end
            new_src_start = max(0.0, c.src_for_timeline(max(c.timeline_start, t_timeline)))
            # don't let the start cross the end minus a gap
            new_src_start = min(new_src_start, c.src_end - min_gap)
            c.src_start = new_src_start
            c.timeline_start = anchor_end_t - c.timeline_length
            c.timeline_start = max(0.0, c.timeline_start)
        else:
            new_src_end = c.src_for_timeline(min(c.timeline_end, t_timeline))
            c.src_end = max(c.src_start + min_gap, min(c.asset.duration, new_src_end))
        self.update()

    def _snap_clip_start(self, moved: Clip, new_start: float) -> float:
        """Snap moved clip's start/end to other clip edges, t=0, and the
        playhead within 10px."""
        tolerance_s = 10.0 / max(1.0, self._pps)
        candidates: list[float] = [0.0, self._playhead]
        for c in self._clips:
            if c.id == moved.id:
                continue
            candidates.append(c.timeline_start)
            candidates.append(c.timeline_end)
        length = moved.timeline_length
        # consider snapping the moved clip's start AND its end
        best_start = new_start
        best_d = tolerance_s
        for cand in candidates:
            for anchor in (cand, cand - length):
                d = abs(anchor - new_start)
                if d < best_d:
                    best_d = d
                    best_start = anchor
        return max(0.0, best_start)

    def _resolve_overlaps(self, moved: Clip) -> None:
        # Simple policy: push other clips aside. If `moved` overlaps with any
        # existing clip, shift `moved` to the nearest non-overlapping spot
        # (prefer the right side of the overlap).
        others = [c for c in self._clips if c.id != moved.id]
        others.sort(key=lambda c: c.timeline_start)
        # find any colliding clip
        for oc in others:
            if moved.timeline_end <= oc.timeline_start or moved.timeline_start >= oc.timeline_end:
                continue
            # collision: snap to the right of this clip
            moved.timeline_start = oc.timeline_end
            # re-check by restarting the loop
            return self._resolve_overlaps(moved)

    def _show_context_menu(self, global_pos: QPoint, local_pos: QPoint) -> None:
        has_selection = self._sel_end > self._sel_start
        clicked_audio = (
            self._hit_added_audio(local_pos)
            if self._audio2_rect().contains(local_pos) else None
        )
        menu = QMenu(self)
        if has_selection:
            menu.addAction("Delete Selected Region")
            menu.addAction("Crop to Selected Region")
            menu.addAction("Export Selected Region…")
        else:
            menu.addAction("(no region selected)").setEnabled(False)
        menu.addSeparator()
        menu.addAction("Split at Playhead")
        replace_action = None
        remove_this_action = None
        remove_all_action = None
        if clicked_audio is not None:
            menu.addSeparator()
            replace_action = menu.addAction("Replace original audio")
            replace_action.setCheckable(True)
            replace_action.setChecked(self._added_audio_replace)
            remove_this_action = menu.addAction("Remove This Audio Clip")
            if len(self._added_audios) > 1:
                remove_all_action = menu.addAction("Remove All Added Audio")
        chosen = menu.exec(global_pos)
        if not chosen:
            return
        text = chosen.text()
        if text == "Split at Playhead":
            self.splitAtPlayheadRequested.emit()
            return
        if remove_this_action is not None and chosen is remove_this_action:
            self.addedAudioDeleteRequested.emit(clicked_audio.id)
            return
        if remove_all_action is not None and chosen is remove_all_action:
            self.addedAudioDeleteRequested.emit("")
            return
        if replace_action is not None and chosen is replace_action:
            self._added_audio_replace = replace_action.isChecked()
            self.addedAudioReplaceToggled.emit(self._added_audio_replace)
            return
        if not has_selection:
            return
        if text.startswith("Delete"):
            self.regionDeleteRequested.emit(self._sel_start, self._sel_end)
        elif text.startswith("Crop"):
            self.regionCropRequested.emit(self._sel_start, self._sel_end)
        elif text.startswith("Export"):
            self.regionExportRequested.emit(self._sel_start, self._sel_end)

    # ---- drag & drop of media -----------------------------------------

    _AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}
    _VIDEO_EXTS = {
        ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
        ".mpg", ".mpeg", ".wmv",
    }

    def _accepts_drop(self, event) -> bool:  # noqa: ANN001
        md = event.mimeData()
        if md.hasFormat(ASSET_MIME):
            return True
        if self._drop_audio_path(event) is not None:
            return True
        if self._drop_video_path(event) is not None:
            return True
        return False

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._accepts_drop(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._accepts_drop(event):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        md = event.mimeData()
        drop_x = int(event.position().x())
        drop_y = int(event.position().y())
        drop_t = max(0.0, self._x_to_time(drop_x))
        lane = self._lane_for_y(drop_y)
        if md.hasFormat(ASSET_MIME):
            asset_id = bytes(md.data(ASSET_MIME)).decode()
            if asset_id:
                event.acceptProposedAction()
                self.assetDroppedOnTimeline.emit(asset_id, drop_t, lane)
                return
        audio_path = self._drop_audio_path(event)
        if audio_path is not None:
            event.acceptProposedAction()
            self.addedAudioDropped.emit(audio_path, drop_t, lane)
            return
        video_path = self._drop_video_path(event)
        if video_path is not None:
            event.acceptProposedAction()
            self.videoFileDropped.emit(video_path, drop_t)

    def _drop_audio_path(self, event) -> str | None:  # noqa: ANN001
        return self._first_path_with_ext(event, self._AUDIO_EXTS)

    def _drop_video_path(self, event) -> str | None:  # noqa: ANN001
        return self._first_path_with_ext(event, self._VIDEO_EXTS)

    def _first_path_with_ext(self, event, exts: set[str]) -> str | None:  # noqa: ANN001
        md = event.mimeData()
        if not md.hasUrls():
            return None
        for u in md.urls():
            p = u.toLocalFile()
            if p and any(p.lower().endswith(ext) for ext in exts):
                return p
        return None

    def _find(self, clip_id: str) -> Clip | None:
        return next((c for c in self._clips if c.id == clip_id), None)

    def sizeHint(self) -> QSize:
        return QSize(
            900,
            RULER_H + TRACK_GAP + VIDEO_TRACK_H + TRACK_GAP + AUDIO_TRACK_H
            + TRACK_GAP + AUDIO2_TRACK_H + 4,
        )


def _choose_tick_step(pps: float) -> float:
    """Return a reasonable tick step (seconds) based on the current zoom."""
    for step in (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600):
        if step * pps >= 70:
            return step
    return 600


def _fmt_tick(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m = int(seconds // 60)
    s = seconds - m * 60
    if seconds < 60:
        return f"{s:.2f}s" if s < 10 else f"{s:.1f}s"
    return f"{m}:{int(s):02d}"
