from __future__ import annotations

import copy
from pathlib import Path

import time

from PySide6.QtCore import (
    QEvent,
    QRectF,
    QSizeF,
    QStandardPaths,
    QThread,
    QTimer,
    QUrl,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QImage,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPixmap,
    QShortcut,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollBar,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import ffmpeg_utils as ff
from .clip import (
    AddedAudio,
    Clip,
    MediaAsset,
    clip_at_timeline,
    delete_region,
    keep_only_region,
    sequence_length,
    sort_clips,
    split_clip,
)
from .clip_bin import ASSET_MIME, ClipBin
from .crop_overlay import CropOverlay
from .exporter import AudioTrack, ExportJob, start_export
from .thumbnails import start_thumbnails, start_waveform
from .timeline_widget import TimelineWidget


VIDEO_FILTERS = (
    "Videos (*.mp4 *.mkv *.webm *.mov *.avi *.m4v *.mpg *.mpeg *.wmv);;All files (*)"
)
AUDIO_FILTERS = "Audio (*.mp3 *.m4a *.aac *.wav *.ogg *.opus *.flac);;All files (*)"
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac"}

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ICON_PATH = ASSETS_DIR / "cove_icon.png"


class VideoView(QGraphicsView):
    clicked = Signal()
    contextMenuRequestedAt = Signal(object)   # QPoint (global)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("background:#000; border:none;")
        self.setAlignment(Qt.AlignCenter)

        self.video_item = QGraphicsVideoItem()
        self.video_item.setSize(QSizeF(640, 360))
        self._scene.addItem(self.video_item)
        self._scene.setSceneRect(QRectF(0, 0, 640, 360))

    def video_output(self) -> QGraphicsVideoItem:
        return self.video_item

    def set_video_visible(self, visible: bool) -> None:
        self.video_item.setVisible(bool(visible))

    def set_native_size(self, width: int, height: int) -> None:
        self.video_item.setSize(QSizeF(width, height))
        self._scene.setSceneRect(QRectF(0, 0, width, height))
        self._fit()

    def _fit(self) -> None:
        if not self._scene.sceneRect().isEmpty():
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self._fit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: ANN001
        self.contextMenuRequestedAt.emit(event.globalPos())
        event.accept()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Cove Video Editor")
        self.resize(1400, 880)
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))

        self._assets: dict[str, MediaAsset] = {}
        self._clips: list[Clip] = []
        self._thumb_threads: dict[str, QThread] = {}
        self._thumb_workers: dict[str, object] = {}
        self._wave_threads: dict[str, QThread] = {}
        self._wave_workers: dict[str, object] = {}
        # One waveform worker per added-audio entry (keyed by entry id).
        self._added_wave_threads: dict[str, QThread] = {}
        self._added_wave_workers: dict[str, object] = {}
        self._export_thread: QThread | None = None
        self._export_worker = None
        # List of added-audio clips on the mix track; each has its own player.
        self._added_audios: list[AddedAudio] = []
        self._added_players: dict[str, QMediaPlayer] = {}
        self._added_outputs: dict[str, QAudioOutput] = {}
        self._preview_clip_id: str = ""
        self._region_export_range: tuple[float, float] | None = None

        # Undo stack — each entry is a full state snapshot (see _snapshot).
        self._undo_stack: list[dict] = []
        self._undo_limit: int = 80

        self._build_ui()
        self._install_shortcuts()
        self._update_controls_enabled()
        self._check_ffmpeg()
        self.setAcceptDrops(True)

    # --- shortcuts -----------------------------------------------------

    def _install_shortcuts(self) -> None:
        undo = QShortcut(QKeySequence.Undo, self)
        undo.activated.connect(self._undo)
        space = QShortcut(QKeySequence(Qt.Key_Space), self)
        space.setContext(Qt.ApplicationShortcut)
        space.activated.connect(self._toggle_play)

    # --- scrollbar glue ------------------------------------------------

    def _on_timeline_scroll_range(self, max_px: int, page_px: int) -> None:
        sb = self.timeline_scrollbar
        sb.blockSignals(True)
        sb.setRange(0, max(0, max_px))
        sb.setPageStep(max(1, page_px))
        sb.setSingleStep(40)
        sb.blockSignals(False)
        sb.setVisible(max_px > 0)

    def _on_timeline_scroll_value(self, v: int) -> None:
        sb = self.timeline_scrollbar
        if sb.value() != v:
            sb.blockSignals(True)
            sb.setValue(v)
            sb.blockSignals(False)

    # --- undo ---------------------------------------------------------

    def _snapshot(self) -> None:
        """Push the current editable state onto the undo stack."""
        snap = {
            "clips": [c.clone() for c in self._clips],
            "selected_id": self.timeline.selected_id() if hasattr(self, "timeline") else "",
            "playhead": self.timeline.playhead() if hasattr(self, "timeline") else 0.0,
            "added_audios": [a.clone() for a in self._added_audios],
            "replace_audio": self.audio_replace_cb.isChecked() if hasattr(self, "audio_replace_cb") else False,
            "added_gain": self.audio_gain.value() if hasattr(self, "audio_gain") else 1.0,
            "orig_gain": self.orig_gain.value() if hasattr(self, "orig_gain") else 1.0,
        }
        self._undo_stack.append(snap)
        if len(self._undo_stack) > self._undo_limit:
            self._undo_stack.pop(0)

    def _undo(self) -> None:
        if not self._undo_stack:
            self.status.showMessage("Nothing to undo.", 2000)
            return
        snap = self._undo_stack.pop()
        self._clips = [c.clone() for c in snap["clips"]]
        self._restore_added_audios(snap.get("added_audios", []))
        # restore UI-bound settings without retriggering _snapshot
        for w, val in (
            (self.audio_replace_cb, snap["replace_audio"]),
        ):
            w.blockSignals(True); w.setChecked(bool(val)); w.blockSignals(False)
        for w, val in (
            (self.audio_gain, snap["added_gain"]),
            (self.orig_gain, snap["orig_gain"]),
        ):
            w.blockSignals(True); w.setValue(float(val)); w.blockSignals(False)

        self.timeline.set_clips(self._clips)
        self._refresh_added_audio_display()

        sid = snap["selected_id"] or (self._clips[0].id if self._clips else "")
        if sid:
            self.timeline.select_clip(sid)
        self.timeline.set_playhead(snap["playhead"], emit=False)

        # pick a sensible preview clip (first surviving one, or selected)
        preview = next((c for c in self._clips if c.id == sid), None)
        if preview is None and self._clips:
            preview = self._clips[0]
        if preview is not None:
            self._set_preview_clip(preview)
        else:
            self._preview_clip_id = ""
            self.player.setSource(QUrl())

        self._sync_selected_clip_ui()
        self._update_range_label()
        self._update_controls_enabled()
        self.status.showMessage("Undone.", 1500)

    # --- UI construction ----------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # top splitter: bin | preview
        top_split = QSplitter(Qt.Horizontal)
        top_split.setChildrenCollapsible(False)

        self.clip_bin = ClipBin()
        self.clip_bin.addClicked.connect(self._on_bin_add_clicked)
        self.clip_bin.assetActivated.connect(self._on_asset_activated)
        self.clip_bin.assetDeleteRequested.connect(self._on_asset_delete_requested)
        self.clip_bin.filesDropped.connect(self._on_bin_files_dropped)
        top_split.addWidget(self.clip_bin)

        preview_box = QFrame()
        preview_box.setFrameShape(QFrame.StyledPanel)
        preview_box.setStyleSheet("QFrame { background:#0f1116; border:1px solid #2a2f3a; }")
        pv_lay = QVBoxLayout(preview_box)
        pv_lay.setContentsMargins(0, 0, 0, 0)
        pv_lay.setSpacing(0)

        self.video_container = QWidget()
        self.video_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_view = VideoView(self.video_container)
        self.video_view.clicked.connect(self._toggle_play)
        self.video_view.contextMenuRequestedAt.connect(self._show_preview_menu)
        self.crop_overlay = CropOverlay(self.video_container)
        self.crop_overlay.setVisible(False)
        self.video_container.installEventFilter(self)
        pv_lay.addWidget(self.video_container, stretch=1)

        top_split.addWidget(preview_box)
        top_split.setStretchFactor(0, 2)
        top_split.setStretchFactor(1, 7)
        top_split.setSizes([260, 980])
        root.addWidget(top_split, stretch=1)

        # Added-audio mode controls — no UI; we default to "mix with equal
        # gains" and flip to replace mode via the audio-track context menu.
        self.audio_replace_cb = QCheckBox(); self.audio_replace_cb.setVisible(False)
        self.audio_gain = QDoubleSpinBox(); self.audio_gain.setRange(0.0, 3.0); self.audio_gain.setValue(1.0); self.audio_gain.setVisible(False)
        self.orig_gain = QDoubleSpinBox(); self.orig_gain.setRange(0.0, 3.0); self.orig_gain.setValue(1.0); self.orig_gain.setVisible(False)

        # --- player
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.7)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_view.video_output())
        self.player.mediaStatusChanged.connect(self._on_media_status)

        # Timer-driven playhead — makes playback work through gaps between
        # clips and over audio-only timelines. Wall-clock advances the
        # playhead every tick; the main player is a passive video renderer
        # whose position is slaved to the timeline.
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(30)
        self._play_timer.timeout.connect(self._on_play_tick)
        self._play_last_wall: float = 0.0

        # One QMediaPlayer per added-audio entry (populated lazily in
        # `_append_added_audio`). Each plays once for its natural duration
        # inside its placement range and then pauses (no looping).

        # Dedicated player for the CURRENT clip's audio when it's been unlinked
        # from its video. We mute the main player's embedded audio for the
        # unlinked clip and push its audio through this player, offset by
        # `audio_offset`, so the waveform on the timeline matches what the
        # user hears.
        self.clip_audio_player = QMediaPlayer(self)
        self.clip_audio_output = QAudioOutput(self)
        self.clip_audio_output.setVolume(0.0)
        self.clip_audio_player.setAudioOutput(self.clip_audio_output)

        # --- transport row
        transport = QHBoxLayout()
        self.play_btn = QToolButton()
        self.play_btn.setText("Play")
        self.play_btn.clicked.connect(self._toggle_play)
        self.split_btn = QPushButton("Split")
        self.split_btn.setToolTip("Split the clip under the playhead")
        self.split_btn.clicked.connect(self._split_at_playhead)
        self.delete_clip_btn = QPushButton("Delete clip")
        self.delete_clip_btn.clicked.connect(self._delete_selected_clip)
        self.crop_btn = QPushButton("Crop")
        self.crop_btn.setCheckable(True)
        self.crop_btn.toggled.connect(self._on_crop_toggled)
        self.crop_reset_btn = QPushButton("Reset crop")
        self.crop_reset_btn.setVisible(False)
        self.crop_reset_btn.clicked.connect(self._on_crop_reset)
        self.range_label = QLabel("—")
        self.range_label.setStyleSheet("color:#cfd0d4;")

        transport.addWidget(self.play_btn)
        transport.addSpacing(8)
        transport.addWidget(self.split_btn)
        transport.addWidget(self.delete_clip_btn)
        transport.addSpacing(8)
        transport.addWidget(self.crop_btn)
        transport.addWidget(self.crop_reset_btn)
        transport.addStretch(1)
        transport.addWidget(self.range_label)
        root.addLayout(transport)

        # --- timeline
        self.timeline = TimelineWidget()
        self.timeline.setMinimumHeight(200)
        self.timeline.playheadMoved.connect(self._on_timeline_playhead)
        self.timeline.clipSelected.connect(self._on_clip_selected)
        self.timeline.rangeChanged.connect(self._on_clip_range_changed)
        self.timeline.clipMoved.connect(self._on_clip_moved)
        self.timeline.selectionChanged.connect(self._on_selection_changed)
        self.timeline.regionDeleteRequested.connect(self._on_region_delete)
        self.timeline.regionCropRequested.connect(self._on_region_crop)
        self.timeline.regionExportRequested.connect(self._on_region_export)
        self.timeline.splitAtPlayheadRequested.connect(self._split_at_playhead)
        self.timeline.addedAudioDropped.connect(self._on_added_audio_dropped)
        self.timeline.videoFileDropped.connect(self._on_video_file_dropped)
        self.timeline.assetDroppedOnTimeline.connect(self._on_asset_dropped_on_timeline)
        self.timeline.addedAudioDeleteRequested.connect(self._on_added_audio_delete_requested)
        self.timeline.addedAudioReplaceToggled.connect(self._on_added_audio_replace_toggled)
        self.timeline.addedAudioOffsetChanged.connect(self._on_added_audio_offset_changed)
        self.timeline.clipDoubleClicked.connect(self._open_clip_properties)
        self.timeline.audioLinkToggled.connect(self._on_audio_link_toggled)
        self.timeline.clipDeleteRequested.connect(self._on_clip_delete_requested)
        self.timeline.audioOffsetChanged.connect(self._on_audio_offset_changed)
        self.timeline.clipAudioRemoveRequested.connect(self._on_clip_audio_remove_requested)
        self.timeline.scrollRangeChanged.connect(self._on_timeline_scroll_range)
        self.timeline.scrollValueChanged.connect(self._on_timeline_scroll_value)
        root.addWidget(self.timeline, stretch=0)

        self.timeline_scrollbar = QScrollBar(Qt.Horizontal)
        self.timeline_scrollbar.setRange(0, 0)
        self.timeline_scrollbar.valueChanged.connect(self.timeline.set_scroll_x)
        root.addWidget(self.timeline_scrollbar)

        # --- export row
        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        self.format_combo = QComboBox()
        for key in ff.EXPORT_FORMATS:
            self.format_combo.addItem(key)
        self.format_combo.setCurrentText("MP4 (H.264 + AAC)")
        bottom.addWidget(QLabel("Export as"))
        bottom.addWidget(self.format_combo, stretch=1)

        self.export_btn = QPushButton("Export")
        self.export_btn.setMinimumHeight(34)
        self.export_btn.setStyleSheet(
            "QPushButton { background:#2563eb; color:white; font-weight:600;"
            " border:none; border-radius:6px; padding:6px 20px; }"
            "QPushButton:hover { background:#1d4ed8; }"
            "QPushButton:disabled { background:#3a4150; color:#9aa0ad; }"
        )
        self.export_btn.clicked.connect(self._on_export_clicked)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        bottom.addWidget(self.export_btn)
        bottom.addWidget(self.cancel_btn)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        self._last_progress = 0
        self._last_eta: float | None = None
        bottom.addWidget(self.progress, stretch=2)

        root.addLayout(bottom)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Drop videos into the Media panel, or click + Video.", 10000)

    # --- drag & drop into the whole window ---------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() or event.mimeData().hasFormat(ASSET_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: ANN001
        if event.mimeData().hasUrls() or event.mimeData().hasFormat(ASSET_MIME):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        md = event.mimeData()
        if md.hasFormat(ASSET_MIME):
            asset_id = bytes(md.data(ASSET_MIME)).decode()
            asset = self._assets.get(asset_id)
            if asset and asset.kind == "audio":
                self._set_added_audio(asset.path)
            elif asset:
                self._append_clip_for_asset(asset_id)
            event.acceptProposedAction()
            return
        if md.hasUrls():
            paths = [Path(u.toLocalFile()) for u in md.urls() if u.toLocalFile()]
            audio_paths = [p for p in paths if p.suffix.lower() in AUDIO_EXTS]
            video_paths = [p for p in paths if p.suffix.lower() not in AUDIO_EXTS]
            if video_paths:
                self._import_paths(video_paths)
            for p in audio_paths:
                self._append_added_audio(p)
            event.acceptProposedAction()

    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001
        et = event.type()
        if getattr(self, "video_container", None) is obj and et == QEvent.Resize:
            r = self.video_container.rect()
            self.video_view.setGeometry(r)
            self.crop_overlay.setGeometry(r)
            self.crop_overlay.raise_()
        return super().eventFilter(obj, event)

    # --- importing ----------------------------------------------------

    def _on_bin_add_clicked(self, kind: str) -> None:
        if kind == "audio":
            paths, _ = QFileDialog.getOpenFileNames(self, "Add audio", "", AUDIO_FILTERS)
        else:
            videos_dir = QStandardPaths.writableLocation(QStandardPaths.MoviesLocation)
            paths, _ = QFileDialog.getOpenFileNames(self, "Add videos", videos_dir, VIDEO_FILTERS)
        if paths:
            self._import_paths([Path(p) for p in paths])

    def _import_paths(self, paths: list[Path], append_to_timeline: bool = True) -> None:
        new_assets: list[MediaAsset] = []
        for p in paths:
            if not p.exists():
                continue
            if p.suffix.lower() in AUDIO_EXTS:
                try:
                    dur = ff.probe_audio_duration(p)
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, f"Could not open {p.name}", str(exc))
                    continue
                asset = MediaAsset(
                    path=p, duration=dur, width=0, height=0, fps=0.0,
                    has_audio=True, kind="audio",
                )
            else:
                try:
                    info = ff.probe(p)
                except ff.FFmpegMissingError as exc:
                    QMessageBox.critical(self, "Missing dependency", str(exc))
                    return
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, f"Could not open {p.name}", str(exc))
                    continue
                asset = MediaAsset(
                    path=p, duration=info.duration,
                    width=info.width, height=info.height, fps=info.fps,
                    has_audio=info.has_audio, kind="video",
                )
            self._assets[asset.id] = asset
            self.clip_bin.add_asset(asset)
            new_assets.append(asset)
            if append_to_timeline and asset.kind == "video":
                self._append_clip_for_asset(asset.id)

        self._update_controls_enabled()

    def _on_asset_activated(self, asset_id: str) -> None:
        asset = self._assets.get(asset_id)
        if asset is None:
            return
        if asset.kind == "audio":
            self._append_added_audio(asset.path)
        else:
            self._append_clip_for_asset(asset_id)

    def _on_bin_files_dropped(self, paths: list) -> None:
        # Import into the library only — don't auto-append videos to the
        # timeline. User drags to timeline explicitly when they want them.
        self._import_paths([Path(p) for p in paths if p], append_to_timeline=False)

    def _on_asset_delete_requested(self, asset_id: str) -> None:
        asset = self._assets.get(asset_id)
        if asset is None:
            return
        affected = [c for c in self._clips if c.asset.id == asset_id]
        # Snapshot before deleting so Ctrl+Z can bring it back (state only;
        # the bin tiles themselves aren't in the snapshot).
        self._snapshot()
        # If any timeline clips use this asset, drop them.
        if affected:
            self._clips = [c for c in self._clips if c.asset.id != asset_id]
            self.timeline.set_clips(self._clips)
            if self._preview_clip_id in {c.id for c in affected}:
                self._preview_clip_id = ""
                self.player.setSource(QUrl())
        # Drop any added-audio entries that referenced this asset's path.
        doomed = [a.id for a in self._added_audios if a.path == asset.path]
        if doomed:
            for aid in doomed:
                self._destroy_added_player(aid)
            self._added_audios = [
                a for a in self._added_audios if a.id not in doomed
            ]
            self._refresh_added_audio_display()
            self._update_audio_volumes()
        self._assets.pop(asset_id, None)
        self.clip_bin.remove_asset(asset_id)
        if not self._clips:
            self._halt_playback_no_clips()
        self._sync_selected_clip_ui()
        self._update_range_label()
        self._update_controls_enabled()
        self.status.showMessage(f"Removed {asset.path.name}.", 3000)

    def _append_clip_for_asset(self, asset_id: str) -> None:
        self._insert_clip_at(asset_id, sequence_length(self._clips))

    def _insert_clip_at(self, asset_id: str, drop_t: float) -> None:
        """Add a video clip to the timeline at `drop_t`. If the position
        falls inside an existing clip, advance to the end of that clip so
        the new clip lands cleanly to its right."""
        asset = self._assets.get(asset_id)
        if asset is None or asset.kind != "video":
            return
        self._snapshot()
        start_t = max(0.0, drop_t)
        for c in sort_clips(self._clips):
            if c.timeline_start <= start_t < c.timeline_end:
                start_t = c.timeline_end
        clip = Clip(asset=asset, timeline_start=start_t)
        self._clips.append(clip)
        self._clips = sort_clips(self._clips)
        self.timeline.set_clips(self._clips)
        self.timeline.select_clip(clip.id)
        self._kick_off_thumbs(clip)
        if asset.has_audio:
            self._kick_off_waveform(clip)
        if not self._preview_clip_id:
            self._set_preview_clip(clip)
        self._sync_selected_clip_ui()
        self._update_range_label()
        self._update_controls_enabled()

    def _on_asset_dropped_on_timeline(
        self, asset_id: str, drop_t: float, lane: int = 1,
    ) -> None:
        asset = self._assets.get(asset_id)
        if asset is None:
            return
        if asset.kind == "audio":
            self._append_added_audio(
                asset.path, initial_offset=drop_t, lane=int(lane),
            )
        else:
            self._insert_clip_at(asset_id, drop_t)

    def _on_video_file_dropped(self, path: str, drop_t: float) -> None:
        p = Path(path)
        if not p.exists():
            return
        # Reuse an already-imported asset for the same file.
        existing = next(
            (a for a in self._assets.values() if a.path == p), None,
        )
        if existing is not None:
            self._insert_clip_at(existing.id, drop_t)
            return
        try:
            info = ff.probe(p)
        except ff.FFmpegMissingError as exc:
            QMessageBox.critical(self, "Missing dependency", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, f"Could not open {p.name}", str(exc))
            return
        asset = MediaAsset(
            path=p, duration=info.duration,
            width=info.width, height=info.height, fps=info.fps,
            has_audio=info.has_audio, kind="video",
        )
        self._assets[asset.id] = asset
        self.clip_bin.add_asset(asset)
        self._insert_clip_at(asset.id, drop_t)

    def _set_preview_clip(self, clip: Clip) -> None:
        self._preview_clip_id = clip.id
        self.video_view.set_native_size(clip.asset.width, clip.asset.height)
        self.crop_overlay.set_video_aspect(clip.asset.width / max(1, clip.asset.height))
        self.player.setSource(QUrl.fromLocalFile(str(clip.path)))
        self.player.setPosition(int(clip.src_start * 1000))
        # Swap the unlinked-audio player's source so it's ready if this
        # clip is unlinked (otherwise the first tick picks it up late).
        self.clip_audio_player.pause()
        self.clip_audio_player.setSource(QUrl.fromLocalFile(str(clip.path)))
        self._update_audio_volumes()

    def _kick_off_thumbs(self, clip: Clip) -> None:
        thread, worker = start_thumbnails(clip.id, clip.path, clip.asset.duration, count=24)
        worker.finished.connect(self._on_thumbs_ready, Qt.QueuedConnection)
        worker.failed.connect(self._on_thumb_error, Qt.QueuedConnection)
        thread.finished.connect(
            lambda cid=clip.id: self._thumb_done(cid), Qt.QueuedConnection,
        )
        self._thumb_threads[clip.id] = thread
        self._thumb_workers[clip.id] = worker
        thread.start()

    def _kick_off_waveform(self, clip: Clip) -> None:
        thread, worker = start_waveform(clip.id, clip.path)
        worker.finished.connect(self._on_waveform_ready, Qt.QueuedConnection)
        worker.failed.connect(self._on_waveform_error, Qt.QueuedConnection)
        thread.finished.connect(
            lambda cid=clip.id: self._wave_done(cid), Qt.QueuedConnection,
        )
        self._wave_threads[clip.id] = thread
        self._wave_workers[clip.id] = worker
        thread.start()

    def _thumb_done(self, clip_id: str) -> None:
        self._thumb_threads.pop(clip_id, None)
        self._thumb_workers.pop(clip_id, None)

    def _wave_done(self, clip_id: str) -> None:
        self._wave_threads.pop(clip_id, None)
        self._wave_workers.pop(clip_id, None)

    def _on_thumbs_ready(self, clip_id: str, images: list) -> None:
        clip = next((c for c in self._clips if c.id == clip_id), None)
        if not clip:
            return
        clip.thumbs = images
        clip.thumb_pixmaps = [QPixmap.fromImage(img) for img in images]
        if images and clip.asset.thumb is None:
            clip.asset.thumb = images[len(images) // 2]
            self.clip_bin.set_asset_thumb(clip.asset.id, clip.asset.thumb)
        self.timeline.update()

    def _on_thumb_error(self, _clip_id: str, msg: str) -> None:
        self.status.showMessage(f"Thumbnail error: {msg}", 4000)

    def _on_waveform_ready(self, clip_id: str, peaks: list, rate: int) -> None:
        clip = next((c for c in self._clips if c.id == clip_id), None)
        if clip and peaks:
            clip.waveform_peaks = list(peaks)
            clip.waveform_rate = int(rate)
            self.timeline.update()

    def _on_waveform_error(self, _clip_id: str, _msg: str) -> None:
        pass

    # --- selection / editing -----------------------------------------

    def _selected_clip(self) -> Clip | None:
        sid = self.timeline.selected_id()
        return next((c for c in self._clips if c.id == sid), None)

    def _sync_selected_clip_ui(self) -> None:
        # The settings panel is gone; the transport just shows sequence info.
        pass

    def _on_clip_selected(self, clip_id: str) -> None:
        c = next((c for c in self._clips if c.id == clip_id), None)
        if c and self._preview_clip_id != c.id:
            self._set_preview_clip(c)
        self._sync_selected_clip_ui()
        self._update_audio_volumes()

    def _on_clip_range_changed(self, _id: str, _s: float, _e: float) -> None:
        self._snapshot()
        self._clips = sort_clips(self._clips)
        self.timeline.set_clips(self._clips)
        self._sync_selected_clip_ui()
        self._update_range_label()

    def _on_clip_moved(self, _id: str, _start: float) -> None:
        self._snapshot()
        self._clips = sort_clips(self._clips)
        self.timeline.set_clips(self._clips)
        self._update_range_label()

    def _open_clip_properties(self, clip_id: str) -> None:
        c = next((c for c in self._clips if c.id == clip_id), None)
        if c is None:
            return
        dlg = ClipPropertiesDialog(c, self)
        if dlg.exec() == QDialog.Accepted:
            vals = dlg.result_values() or {}
            changed = (
                abs(c.speed - vals["speed"]) > 1e-6
                or abs(c.src_start - vals["src_start"]) > 1e-4
                or abs(c.src_end - vals["src_end"]) > 1e-4
                or c.muted != vals["muted"]
            )
            if not changed:
                return
            self._snapshot()
            c.speed = vals["speed"]
            c.src_start = vals["src_start"]
            c.src_end = vals["src_end"]
            c.muted = vals["muted"]
            self.timeline.set_clips(self._clips)
            self._update_range_label()
            self._update_audio_volumes()

    def _split_at_playhead(self) -> None:
        t = self.timeline.playhead()
        c = clip_at_timeline(self._clips, t)
        if c is None:
            self.status.showMessage("Playhead isn't over a clip.", 3000)
            return
        self._snapshot()
        new = split_clip(c, t)
        if new is None:
            # Roll back the snapshot because we didn't actually mutate.
            self._undo_stack.pop()
            self.status.showMessage("Playhead is too close to a clip edge.", 3000)
            return
        self._clips.append(new)
        self._clips = sort_clips(self._clips)
        self.timeline.set_clips(self._clips)
        self.timeline.select_clip(new.id)
        self._sync_selected_clip_ui()

    def _delete_selected_clip(self) -> None:
        c = self._selected_clip()
        if not c:
            return
        self._delete_clip_by_id(c.id)

    def _on_clip_delete_requested(self, clip_id: str) -> None:
        self._delete_clip_by_id(clip_id)

    def _delete_clip_by_id(self, clip_id: str) -> None:
        if not any(c.id == clip_id for c in self._clips):
            return
        self._snapshot()
        self._clips = [cc for cc in self._clips if cc.id != clip_id]
        self.timeline.set_clips(self._clips)
        self._sync_selected_clip_ui()
        self._update_range_label()
        if not self._clips:
            self._halt_playback_no_clips()
        elif self._preview_clip_id == clip_id:
            self._preview_clip_id = ""
            first = self._clips[0]
            self._set_preview_clip(first)
            self.timeline.select_clip(first.id)
        self.status.showMessage("Clip deleted.", 2500)

    def _halt_playback_no_clips(self) -> None:
        """Called when the timeline's clip list emptied out. Stops the
        video player and the unlinked-clip player; added audio keeps
        playing if there are still entries."""
        self.player.setSource(QUrl())
        self.clip_audio_player.pause()
        self.clip_audio_player.setSource(QUrl())
        self._preview_clip_id = ""
        if not self._added_audios:
            # Nothing at all to play — fully stop playback.
            if self._play_timer.isActive():
                self._play_timer.stop()
            self._pause_all_added_players()
            self.play_btn.setText("Play")
        self._update_controls_enabled()

    def _on_audio_offset_changed(self, _clip_id: str, _offset: float) -> None:
        self._snapshot()
        self._sync_clip_audio_playback()

    def _on_clip_audio_remove_requested(self, clip_id: str) -> None:
        c = next((cc for cc in self._clips if cc.id == clip_id), None)
        if c is None or not c.asset.has_audio:
            return
        self._snapshot()
        c.audio_removed = True
        c.linked_audio = True
        c.audio_offset = 0.0
        self.timeline.set_clips(self._clips)
        self._update_audio_volumes()
        self._sync_clip_audio_playback()
        self.status.showMessage(
            "Audio removed — chain chip restores it.", 3500,
        )

    # --- timeline region actions -------------------------------------

    def _on_selection_changed(self, start: float, end: float) -> None:
        if end <= start:
            self.status.clearMessage()
        else:
            self.status.showMessage(f"Selected region: {_fmt(start)} → {_fmt(end)}  ({_fmt(end - start)})", 0)

    def _on_region_delete(self, start: float, end: float) -> None:
        self._snapshot()
        self._clips = delete_region(self._clips, start, end)
        self.timeline.set_clips(self._clips)
        self.timeline.clear_selection()
        self._sync_selected_clip_ui()
        self._update_range_label()
        if not self._clips:
            self._halt_playback_no_clips()

    def _on_region_crop(self, start: float, end: float) -> None:
        self._snapshot()
        self._clips = keep_only_region(self._clips, start, end)
        self.timeline.set_clips(self._clips)
        self.timeline.clear_selection()
        self._sync_selected_clip_ui()
        self._update_range_label()
        self.timeline.set_playhead(0.0)

    def _on_region_export(self, start: float, end: float) -> None:
        self._region_export_range = (start, end)
        try:
            self._on_export_clicked()
        finally:
            self._region_export_range = None

    # --- player -------------------------------------------------------

    def _toggle_play(self) -> None:
        if self._play_timer.isActive():
            self._play_timer.stop()
            self.player.pause()
            self._pause_all_added_players()
            self.clip_audio_player.pause()
            self.play_btn.setText("Play")
            return
        # Need at least one clip OR one added-audio entry to play.
        if not self._clips and not self._added_audios:
            return
        # If we're at the very end, rewind to 0 so hitting play again replays.
        total = self._total_playback_length()
        if self.timeline.playhead() >= total - 1e-3:
            self.timeline.set_playhead(0.0, emit=False)
        self._update_audio_volumes()
        self._play_last_wall = time.monotonic()
        self._play_timer.start()
        # Align main + aux players to the current playhead before the first tick.
        self._drive_main_player_from_playhead()
        self._sync_clip_audio_playback()
        self._sync_added_audio_playback()
        self.play_btn.setText("Pause")

    def _total_playback_length(self) -> float:
        clip_end = sequence_length(self._clips)
        audio_end = max(
            (a.offset + a.duration for a in self._added_audios), default=0.0,
        )
        return max(clip_end, audio_end)

    def _on_play_tick(self) -> None:
        now = time.monotonic()
        dt = now - self._play_last_wall
        self._play_last_wall = now
        new_t = self.timeline.playhead() + dt
        total = self._total_playback_length()
        if total > 0 and new_t >= total:
            new_t = total
            self.timeline.set_playhead(new_t, emit=False)
            self._toggle_play()
            return
        self.timeline.set_playhead(new_t, emit=False)
        self._drive_main_player_from_playhead()
        self._sync_clip_audio_playback()
        self._sync_added_audio_playback()

    def _drive_main_player_from_playhead(self) -> None:
        """Make the main player mirror where the playhead is on the timeline:
        if it's inside a clip, show that clip's frame; if it's in a gap or
        past every clip, hide the video so the preview goes black while the
        timer keeps advancing so any trailing added audio can play."""
        t = self.timeline.playhead()
        clip = clip_at_timeline(self._clips, t) if self._clips else None
        if clip is None:
            if self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()
            self.video_view.set_video_visible(False)
            return
        if clip.id != self._preview_clip_id:
            self._set_preview_clip(clip)
        self.video_view.set_video_visible(True)
        src_t = clip.src_for_timeline(t)
        # Don't try to re-seek/play a clip whose trim has already elapsed —
        # that's what produced the "loops the end" behaviour when added
        # audio still had content past the last clip. Let it stay paused.
        if src_t >= clip.src_end - 0.03:
            if self.player.playbackState() == QMediaPlayer.PlayingState:
                self.player.pause()
            return
        target_ms = int(src_t * 1000)
        if self.player.playbackState() != QMediaPlayer.PlayingState:
            self.player.setPosition(target_ms)
            self.player.play()
        elif abs(self.player.position() - target_ms) > self._SYNC_DRIFT_MS:
            self.player.setPosition(target_ms)

    def _pause_all_added_players(self) -> None:
        for player in self._added_players.values():
            if player.playbackState() == QMediaPlayer.PlayingState:
                player.pause()

    def _current_preview_clip(self) -> Clip | None:
        return next((c for c in self._clips if c.id == self._preview_clip_id), None)

    def _update_audio_volumes(self) -> None:
        clip = self._current_preview_clip()
        clip_muted = clip is None or clip.muted
        # An unlinked clip's embedded audio is silenced on the main player;
        # the detached audio is routed through `clip_audio_player` instead
        # so it obeys `audio_offset`.
        unlinked = (
            clip is not None
            and clip.asset.has_audio
            and not clip.linked_audio
        )
        has_added = bool(self._added_audios)
        default_clip_vol = 0.0 if clip_muted else 0.7
        added_gain = max(0.0, min(1.0, self.audio_gain.value()))
        orig_vol = max(0.0, min(1.0, self.orig_gain.value()))

        if not has_added:
            if unlinked:
                self.audio.setVolume(0.0)
                self.clip_audio_output.setVolume(default_clip_vol)
            else:
                self.audio.setVolume(default_clip_vol)
                self.clip_audio_output.setVolume(0.0)
            for out in self._added_outputs.values():
                out.setVolume(0.0)
            return

        if self.audio_replace_cb.isChecked() or clip_muted:
            self.audio.setVolume(0.0)
            self.clip_audio_output.setVolume(0.0)
        elif unlinked:
            self.audio.setVolume(0.0)
            self.clip_audio_output.setVolume(orig_vol)
        else:
            self.audio.setVolume(orig_vol)
            self.clip_audio_output.setVolume(0.0)
        for out in self._added_outputs.values():
            out.setVolume(added_gain)

    _SYNC_DRIFT_MS = 200

    def _sync_clip_audio_playback(self) -> None:
        """Position the unlinked-clip audio player relative to the clip's
        shifted range. Silent outside [start+offset, end+offset]."""
        clip = self._current_preview_clip()
        if (
            clip is None or not clip.asset.has_audio or clip.linked_audio
            or getattr(clip, "audio_removed", False)
        ):
            if self.clip_audio_player.playbackState() == QMediaPlayer.PlayingState:
                self.clip_audio_player.pause()
            return
        # Load the clip's file into the dedicated player if needed.
        src = QUrl.fromLocalFile(str(clip.path))
        if self.clip_audio_player.source() != src:
            self.clip_audio_player.setSource(src)
        if self.player.playbackState() != QMediaPlayer.PlayingState and not self._play_timer.isActive():
            if self.clip_audio_player.playbackState() == QMediaPlayer.PlayingState:
                self.clip_audio_player.pause()
            return
        t = self.timeline.playhead()
        audio_start = clip.timeline_start + clip.audio_offset
        audio_end = clip.timeline_end + clip.audio_offset
        in_range = audio_start <= t < audio_end
        if in_range:
            src_t = clip.src_start + max(0.0, (t - audio_start)) * clip.speed
            target_ms = int(src_t * 1000)
            playing = (
                self.clip_audio_player.playbackState() == QMediaPlayer.PlayingState
            )
            if not playing:
                self.clip_audio_player.setPosition(target_ms)
                self.clip_audio_player.play()
            elif abs(self.clip_audio_player.position() - target_ms) > self._SYNC_DRIFT_MS:
                # Scrub landed us somewhere else — resync.
                self.clip_audio_player.setPosition(target_ms)
        else:
            if self.clip_audio_player.playbackState() == QMediaPlayer.PlayingState:
                self.clip_audio_player.pause()

    def _sync_added_audio_playback(self) -> None:
        """Walk every added-audio entry and start/pause its player based on
        whether the playhead is inside that entry's [offset, offset+duration]
        range."""
        if not self._is_playing():
            self._pause_all_added_players()
            return
        t = self.timeline.playhead()
        for audio in self._added_audios:
            player = self._added_players.get(audio.id)
            if player is None or audio.duration <= 0:
                continue
            start = audio.offset
            end = audio.offset + audio.duration
            in_range = start <= t < end
            if in_range:
                target_ms = int(max(0.0, t - start) * 1000)
                playing = (
                    player.playbackState() == QMediaPlayer.PlayingState
                )
                if not playing:
                    player.setPosition(target_ms)
                    player.play()
                elif abs(player.position() - target_ms) > self._SYNC_DRIFT_MS:
                    player.setPosition(target_ms)
            else:
                if player.playbackState() == QMediaPlayer.PlayingState:
                    player.pause()

    def _is_playing(self) -> bool:
        return (
            self.player.playbackState() == QMediaPlayer.PlayingState
            or self._play_timer.isActive()
        )

    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        c = next((c for c in self._clips if c.id == self._preview_clip_id), None)
        if c and status == QMediaPlayer.LoadedMedia:
            self.player.setPosition(int(c.src_start * 1000))

    def _on_timeline_playhead(self, t: float) -> None:
        c = clip_at_timeline(self._clips, t)
        if c is None:
            return
        if c.id != self._preview_clip_id:
            self._set_preview_clip(c)
            self.timeline.select_clip(c.id)
        src_t = c.src_for_timeline(t)
        self.player.setPosition(int(src_t * 1000))
        self._sync_added_audio_playback()
        self._sync_clip_audio_playback()

    def _update_range_label(self) -> None:
        total = sequence_length(self._clips)
        self.range_label.setText(f"Sequence: {_fmt(total)}  •  {len(self._clips)} clip(s)")

    # --- crop ---------------------------------------------------------

    def _on_crop_toggled(self, checked: bool) -> None:
        c = self._selected_clip()
        if checked and not c:
            self.crop_btn.setChecked(False)
            return
        if checked and c:
            self.crop_overlay.set_video_aspect(c.asset.width / max(1, c.asset.height))
            if self.crop_overlay.normalized_rect() == QRectF(0, 0, 1, 1):
                self.crop_overlay.set_normalized_rect(QRectF(0.1, 0.1, 0.8, 0.8))
        self.crop_overlay.setVisible(checked)
        self.crop_overlay.raise_()
        self.crop_reset_btn.setVisible(checked)

    def _on_crop_reset(self) -> None:
        self.crop_overlay.reset()

    # --- preview context menu ----------------------------------------

    def _show_preview_menu(self, global_pos) -> None:  # noqa: ANN001
        menu = QMenu(self)
        extract_act = menu.addAction("Extract frame as JPG…")
        has_clip = self._current_preview_clip() is not None
        extract_act.setEnabled(has_clip)
        chosen = menu.exec(global_pos)
        if chosen is extract_act and has_clip:
            self._extract_current_frame()

    def _extract_current_frame(self) -> None:
        c = self._current_preview_clip()
        if c is None:
            return
        src_t = self.player.position() / 1000.0
        # Default filename: <clip>-frame-<hh_mm_ss>.jpg
        stamp = int(round(src_t))
        minutes, seconds = divmod(stamp, 60)
        hours, minutes = divmod(minutes, 60)
        suggested = str(
            c.path.with_name(f"{c.path.stem}-frame-{hours:02d}_{minutes:02d}_{seconds:02d}.jpg")
        )
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Save frame as…", suggested, "JPEG (*.jpg *.jpeg);;PNG (*.png);;All files (*)",
        )
        if not out_path:
            return
        try:
            ff.extract_frame_full(c.path, src_t, Path(out_path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Extract failed", str(exc))
            return
        self.status.showMessage(f"Saved frame → {Path(out_path).name}", 5000)

    def _crop_pixels(self) -> tuple[int, int, int, int] | None:
        c = self._selected_clip()
        if not self.crop_btn.isChecked() or c is None:
            return None
        r = self.crop_overlay.normalized_rect()
        if r == QRectF(0, 0, 1, 1):
            return None
        sw, sh = c.asset.width, c.asset.height
        x = int(round(r.x() * sw)); y = int(round(r.y() * sh))
        w = int(round(r.width() * sw)); h = int(round(r.height() * sh))
        w -= w % 2; h -= h % 2
        x = max(0, min(sw - w, x - x % 2))
        y = max(0, min(sh - h, y - y % 2))
        if w < 2 or h < 2:
            return None
        return (x, y, w, h)

    # --- added audio ---------------------------------------------------

    def _on_added_audio_dropped(
        self, path: str, drop_t: float = -1.0, lane: int = 1,
    ) -> None:
        self._append_added_audio(
            Path(path), initial_offset=float(drop_t), lane=int(lane),
        )

    def _set_added_audio(self, path: Path) -> None:
        """Historical alias kept for callers that treat dropping audio as
        picking a single mix track. Appends to Audio Track 2 by default."""
        self._append_added_audio(path)

    def _on_added_audio_offset_changed(self, audio_id: str, offset: float) -> None:
        audio = next((a for a in self._added_audios if a.id == audio_id), None)
        if audio is None or abs(audio.offset - offset) < 1e-4:
            return
        self._snapshot()
        audio.offset = max(0.0, float(offset))
        self._sync_added_audio_playback()

    def _on_added_audio_delete_requested(self, audio_id: str) -> None:
        if audio_id:
            self._remove_added_audio(audio_id)
        else:
            self._clear_all_added_audio()

    def _append_added_audio(
        self, path: Path, initial_offset: float = -1.0, lane: int = 1,
    ) -> None:
        if not path.exists():
            return
        try:
            dur = ff.probe_audio_duration(path)
        except Exception:  # noqa: BLE001
            dur = 0.0
        self._snapshot()
        # Drop-at-cursor when an offset is passed; otherwise append after the
        # last entry on the same lane for a natural back-to-back layout.
        if initial_offset >= 0.0:
            offset = initial_offset
        else:
            offset = max(
                (a.offset + a.duration for a in self._added_audios if a.lane == lane),
                default=0.0,
            )
        audio = AddedAudio(path=path, duration=dur, offset=offset, lane=int(lane))
        self._added_audios.append(audio)
        self._create_added_player(audio)
        self._kick_off_added_waveform(audio.id, path)
        self._refresh_added_audio_display()
        self._update_audio_volumes()
        self._update_controls_enabled()
        if self._play_timer.isActive():
            self._sync_added_audio_playback()
        self.status.showMessage(f"Added audio: {path.name}", 5000)

    def _remove_added_audio(self, audio_id: str) -> None:
        if not any(a.id == audio_id for a in self._added_audios):
            return
        self._snapshot()
        self._destroy_added_player(audio_id)
        self._added_audios = [a for a in self._added_audios if a.id != audio_id]
        self._refresh_added_audio_display()
        self._update_audio_volumes()
        self._update_controls_enabled()
        if not self._clips and not self._added_audios and self._play_timer.isActive():
            self._toggle_play()
        self.status.showMessage("Audio clip removed.", 2500)

    def _clear_all_added_audio(self) -> None:
        if not self._added_audios:
            return
        self._snapshot()
        for aid in list(self._added_players.keys()):
            self._destroy_added_player(aid)
        self._added_audios = []
        self._refresh_added_audio_display()
        self._update_audio_volumes()
        self._update_controls_enabled()
        if not self._clips and self._play_timer.isActive():
            self._toggle_play()
        self.status.showMessage("All added audio removed.", 3000)

    def _create_added_player(self, audio: AddedAudio) -> None:
        player = QMediaPlayer(self)
        output = QAudioOutput(self)
        output.setVolume(0.0)
        player.setAudioOutput(output)
        player.setSource(QUrl.fromLocalFile(str(audio.path)))
        self._added_players[audio.id] = player
        self._added_outputs[audio.id] = output

    def _destroy_added_player(self, audio_id: str) -> None:
        player = self._added_players.pop(audio_id, None)
        output = self._added_outputs.pop(audio_id, None)
        if player is not None:
            player.stop()
            player.setSource(QUrl())
            player.deleteLater()
        if output is not None:
            output.deleteLater()
        worker = self._added_wave_workers.pop(audio_id, None)
        if worker is not None:
            try:
                worker.cancel()
            except Exception:  # noqa: BLE001
                pass
        thread = self._added_wave_threads.pop(audio_id, None)
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(500)

    def _restore_added_audios(self, audios: list) -> None:
        """Rebuild the added-audio state from an undo snapshot. Tears down
        players for entries no longer present and creates players for new
        ones. Existing entries have their offsets/metadata refreshed in
        place."""
        new_ids = {a.id for a in audios}
        for aid in list(self._added_players.keys()):
            if aid not in new_ids:
                self._destroy_added_player(aid)
        cloned = [a.clone() for a in audios]
        existing_ids = set(self._added_players.keys())
        self._added_audios = cloned
        for a in cloned:
            if a.id not in existing_ids:
                self._create_added_player(a)

    def _on_audio_link_toggled(self, clip_id: str) -> None:
        c = next((cc for cc in self._clips if cc.id == clip_id), None)
        if c is None:
            return
        self._snapshot()
        if c.audio_removed:
            # Clicking the chip on a removed-audio clip restores it.
            c.audio_removed = False
            c.linked_audio = True
            c.audio_offset = 0.0
            self.timeline.set_clips(self._clips)
            self._update_audio_volumes()
            self._sync_clip_audio_playback()
            self.status.showMessage("Audio restored.", 3000)
            return
        c.linked_audio = not c.linked_audio
        self.timeline.set_clips(self._clips)
        self._update_audio_volumes()
        self._sync_clip_audio_playback()
        if c.linked_audio:
            c.audio_offset = 0.0
            self.timeline.set_clips(self._clips)
            self.status.showMessage("Audio re-linked to clip.", 3500)
        else:
            self.status.showMessage(
                "Audio unlinked — drag along the audio track, or press Delete to remove.",
                5000,
            )

    def _on_added_audio_replace_toggled(self, replace: bool) -> None:
        if self.audio_replace_cb.isChecked() != replace:
            self._snapshot()
            self.audio_replace_cb.setChecked(replace)
            self._update_audio_volumes()
            self.status.showMessage(
                "Added audio will replace the clip audio." if replace
                else "Added audio will mix with the clip audio.",
                3500,
            )

    def _refresh_added_audio_display(self) -> None:
        self.timeline.set_added_audios(self._added_audios)

    def _kick_off_added_waveform(self, audio_id: str, path: Path) -> None:
        # Cancel any stale worker for this id first (shouldn't usually exist).
        prev = self._added_wave_workers.pop(audio_id, None)
        if prev is not None:
            try:
                prev.cancel()
            except Exception:  # noqa: BLE001
                pass
        thread, worker = start_waveform(audio_id, path)
        worker.finished.connect(self._on_added_waveform_ready, Qt.QueuedConnection)
        worker.failed.connect(self._on_added_waveform_error, Qt.QueuedConnection)
        thread.finished.connect(
            lambda aid=audio_id: self._added_wave_done(aid), Qt.QueuedConnection,
        )
        self._added_wave_threads[audio_id] = thread
        self._added_wave_workers[audio_id] = worker
        thread.start()

    def _on_added_waveform_ready(self, audio_id: str, peaks: list, rate: int) -> None:
        audio = next((a for a in self._added_audios if a.id == audio_id), None)
        if audio is None or not peaks:
            return
        audio.peaks = list(peaks)
        audio.rate = int(rate)
        self._refresh_added_audio_display()

    def _on_added_waveform_error(self, _audio_id: str, _msg: str) -> None:
        pass

    def _added_wave_done(self, audio_id: str) -> None:
        self._added_wave_threads.pop(audio_id, None)
        self._added_wave_workers.pop(audio_id, None)

    # --- export -------------------------------------------------------

    def _on_export_clicked(self) -> None:
        if not self._clips:
            return
        fmt_key = self.format_combo.currentText()
        spec = ff.EXPORT_FORMATS[fmt_key]
        ext = spec["ext"]
        first = self._clips[0]
        suggested = str(first.path.with_suffix(f".edited.{ext}"))
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Export to…", suggested, f"{ext.upper()} (*.{ext});;All files (*)",
        )
        if not out_path:
            return

        audio_tracks: list[AudioTrack] = []
        replace = self.audio_replace_cb.isChecked()
        vol = self.audio_gain.value()
        orig_vol = self.orig_gain.value()
        for audio in self._added_audios:
            audio_tracks.append(
                AudioTrack(
                    path=audio.path,
                    replace=replace,
                    volume=vol,
                    original_volume=orig_vol,
                    offset=audio.offset,
                    duration=audio.duration,
                )
            )

        region_start = region_end = None
        if self._region_export_range is not None:
            region_start, region_end = self._region_export_range

        job = ExportJob(
            clips=[c.clone() for c in self._clips],
            output=Path(out_path),
            fmt_key=fmt_key,
            crop=self._crop_pixels(),
            audio_tracks=audio_tracks,
            region_start=region_start,
            region_end=region_end,
        )

        self._last_progress = 0
        self._last_eta = None
        self.progress.setValue(0)
        self.progress.setFormat("starting…")
        self.export_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.status.showMessage("Exporting…")

        thread, worker = start_export(job)
        worker.progress.connect(self._on_progress, Qt.QueuedConnection)
        worker.eta.connect(self._on_eta, Qt.QueuedConnection)
        worker.log.connect(self._on_worker_log, Qt.QueuedConnection)
        worker.finished.connect(self._on_export_done, Qt.QueuedConnection)
        worker.failed.connect(self._on_export_failed, Qt.QueuedConnection)
        thread.finished.connect(self._reset_after_export)
        self._export_thread = thread
        self._export_worker = worker
        thread.start()

    def _on_cancel_clicked(self) -> None:
        if self._export_worker:
            self._export_worker.cancel()
            self.status.showMessage("Cancelling…")

    def _on_worker_log(self, msg: str) -> None:
        self.status.showMessage(msg, 4000)

    def _on_progress(self, pct: int) -> None:
        self._last_progress = max(self._last_progress, pct)
        self.progress.setValue(self._last_progress)
        self._refresh_progress_text()

    def _on_eta(self, seconds: float) -> None:
        self._last_eta = seconds
        self._refresh_progress_text()

    def _refresh_progress_text(self) -> None:
        if self._last_progress >= 99 or self._last_eta is None:
            self.progress.setFormat("%p%")
        else:
            secs = max(0, int(round(self._last_eta)))
            m, s = divmod(secs, 60)
            self.progress.setFormat(f"%p%  •  ETA {m}:{s:02d}")

    def _on_export_done(self, out: Path) -> None:
        size_b = out.stat().st_size
        size, unit = (size_b / 1024, "KB")
        if size >= 1024:
            size, unit = (size / 1024, "MB")
        self.status.showMessage(f"Saved {out.name} ({size:.1f} {unit})", 8000)
        self._last_progress = 100
        self._last_eta = None
        self.progress.setValue(100)
        self.progress.setFormat("%p%")

    def _on_export_failed(self, msg: str) -> None:
        self.status.showMessage(f"Failed: {msg}", 8000)
        QMessageBox.warning(self, "Export failed", msg)

    def _reset_after_export(self) -> None:
        self._export_thread = None
        self._export_worker = None
        self.export_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    # --- misc ---------------------------------------------------------

    def _update_controls_enabled(self) -> None:
        loaded = bool(self._clips)
        # Play button is enabled whenever there's anything on the timeline —
        # audio-only sequences are valid too.
        self.play_btn.setEnabled(loaded or bool(self._added_audios))
        for w in (
            self.split_btn, self.delete_clip_btn,
            self.crop_btn, self.format_combo, self.export_btn,
        ):
            w.setEnabled(loaded)

    def closeEvent(self, event) -> None:  # noqa: ANN001
        for d in (self._thumb_workers, self._wave_workers, self._added_wave_workers):
            for worker in list(d.values()):
                try:
                    worker.cancel()
                except Exception:  # noqa: BLE001
                    pass
        if self._export_worker is not None:
            try:
                self._export_worker.cancel()
            except Exception:  # noqa: BLE001
                pass
        for d in (self._thumb_threads, self._wave_threads, self._added_wave_threads):
            for thread in list(d.values()):
                if thread.isRunning():
                    thread.quit(); thread.wait(1500)
        if self._export_thread and self._export_thread.isRunning():
            self._export_thread.quit(); self._export_thread.wait(2000)
        super().closeEvent(event)

    def _check_ffmpeg(self) -> None:
        try:
            ff.require_ffmpeg()
            ff.require_ffprobe()
        except ff.FFmpegMissingError as exc:
            QMessageBox.critical(
                self, "Missing dependency",
                f"{exc}\n\nffmpeg and ffprobe should ship next to this application. "
                f"If you are running from source, install ffmpeg with your package "
                f"manager or drop the binaries next to the app.",
            )


def _fmt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m:02d}:{s:05.2f}"


class ClipPropertiesDialog(QDialog):
    """Small modal shown on double-click / right-click → Properties.

    Edits speed, source trim bounds, and per-clip mute. Values are applied to
    the live Clip object only after the user accepts; cancel discards.
    """

    def __init__(self, clip: Clip, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Clip properties")
        self._clip = clip
        self._accepted = False
        self._result: dict | None = None

        lay = QVBoxLayout(self)
        header = QLabel(
            f"<b>{clip.path.name}</b><br>"
            f"<span style='color:#9aa0ad'>{clip.asset.width}×{clip.asset.height}"
            f"  •  source {_fmt(clip.asset.duration)}</span>"
        )
        header.setTextFormat(Qt.RichText)
        lay.addWidget(header)

        form = QFormLayout()
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.25, 4.0); self.speed.setSingleStep(0.25)
        self.speed.setDecimals(2); self.speed.setSuffix("x")
        self.speed.setValue(clip.speed)
        form.addRow("Speed", self.speed)

        self.trim_start = QDoubleSpinBox()
        self.trim_start.setRange(0.0, clip.asset.duration)
        self.trim_start.setDecimals(3); self.trim_start.setSuffix(" s")
        self.trim_start.setValue(clip.src_start)
        form.addRow("Trim start", self.trim_start)

        self.trim_end = QDoubleSpinBox()
        self.trim_end.setRange(0.0, clip.asset.duration)
        self.trim_end.setDecimals(3); self.trim_end.setSuffix(" s")
        self.trim_end.setValue(clip.src_end)
        form.addRow("Trim end", self.trim_end)

        self.muted = QCheckBox("Mute this clip's audio")
        self.muted.setChecked(clip.muted)
        form.addRow("", self.muted)

        lay.addLayout(form)

        row = QHBoxLayout()
        self.reset_btn = QPushButton("Reset trim")
        self.reset_btn.clicked.connect(self._reset_trim)
        row.addWidget(self.reset_btn)
        row.addStretch(1)
        lay.addLayout(row)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _reset_trim(self) -> None:
        self.trim_start.setValue(0.0)
        self.trim_end.setValue(self._clip.asset.duration)

    def accept(self) -> None:
        s, e = self.trim_start.value(), self.trim_end.value()
        if e <= s + 0.01:
            QMessageBox.information(self, "Invalid trim", "End must be after start.")
            return
        self._result = {
            "speed": self.speed.value(),
            "src_start": s,
            "src_end": e,
            "muted": self.muted.isChecked(),
        }
        super().accept()

    def result_values(self) -> dict | None:
        return self._result
