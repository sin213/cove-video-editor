# Cove Video Editor

PySide6 desktop video editor. Uses ffmpeg/ffprobe for probe, thumbnails, waveforms, and export.

## Run / dev loop

```bash
PYTHONPATH=src python3 -m cove_video_editor
```

`__main__.py` sets `QT_MEDIA_BACKEND=ffmpeg` before QApplication touches the plugins — the GStreamer default crashes on some codecs. Don't remove that.

## Module layout (`src/cove_video_editor/`)

- `app.py` — `MainWindow`, players, signal wiring, undo stack, export-job glue.
- `timeline_widget.py` — the timeline canvas: painting, hit-testing, drag state, keyboard.
- `clip.py` — `Clip`, `AddedAudio`, `MediaAsset` dataclasses and region ops (`delete_region`, `keep_only_region`, `split_clip`).
- `clip_bin.py` — left-side media library, drag source, `ASSET_MIME`.
- `exporter.py` — builds the ffmpeg filtergraph (video concat + per-track audio placement + mix).
- `thumbnails.py` — async QThread workers for thumbnails and peak waveforms.
- `ffmpeg_utils.py` — probing, extraction, format specs (`EXPORT_FORMATS`).
- `crop_overlay.py` — draggable crop box over the preview.

## Data model — read this before touching playback or export

`Clip` (video placement on the video track):
- `src_start / src_end` — trim in the asset's time domain.
- `timeline_start` — absolute position on the sequence.
- `timeline_length = src_span / speed`; `timeline_end` is derived.
- `linked_audio` — if False, the clip's audio is **detached and movable**, not muted. The orange waveform is draggable on the audio row via `audio_offset` (seconds, can be negative).
- `audio_offset` — only meaningful when `linked_audio` is False.
- `muted` — permanent silence regardless of link state (set via the properties dialog).
- `audio_removed` — the user deleted the clip's audio track; no waveform drawn, no audio plays. Chain chip on a removed-audio clip **restores** it (resets `audio_removed`, `linked_audio=True`, `audio_offset=0`).

`AddedAudio` (independent audio clip — sound effects, music, anything not tied to a video):
- `path`, `duration`, `offset`, `rate`, `peaks`.
- `lane` — **0 = Audio Track 1** (sits alongside clip audio), **1 = Audio Track 2** (dedicated overlay lane, the default for fresh drops).
- Each entry has its own `QMediaPlayer` in `self._added_players[audio.id]`.

## Playback is timer-driven — don't reconnect `positionChanged`

`self._play_timer` is a 30ms `QTimer`. The **timer** advances the playhead by wall-clock `dt` every tick; the main `QMediaPlayer` is a passive video renderer whose position is slaved to the playhead.

- `_on_play_tick` → advances playhead, stops at `_total_playback_length()`, calls `_drive_main_player_from_playhead` + `_sync_clip_audio_playback` + `_sync_added_audio_playback`.
- `_drive_main_player_from_playhead`:
  - No clip covers playhead → pause main player **and hide the `QGraphicsVideoItem`** so the preview goes black (not a frozen last frame).
  - `src_t >= clip.src_end - 0.03` → pause. Do NOT re-seek + replay here, that's the bug that produced the "loops the end" behavior when trailing audio was still live.
  - Otherwise: seek + play. Resync position only when drift > `_SYNC_DRIFT_MS` (200 ms) to avoid per-tick jitter.
- Aux players (`clip_audio_player`, each `_added_players[id]`) use the same drift-threshold resync pattern so scrubbing during playback doesn't leave them stuck on the old position.

`_is_playing()` returns True when the main player is playing **or** the timer is active — aux sync uses this, not `player.playbackState()` directly, otherwise audio-only playback (no clips) would be treated as stopped.

`_total_playback_length()` in `app.py` and `TimelineWidget._total_length()` both have to include added-audio end (`max(offset + duration)`) or the playhead clamps at the last clip's end. This was a real bug — keep both in sync.

## Hit-testing and drag modes in `timeline_widget.py`

`_hit_test` returns a `_Drag` with one of:
- `seek` — ruler click; drag moves playhead. No `playhead_region` in the ruler — grabbing the red bar is a seek.
- `select` — empty area drag = region-select band.
- `playhead_region` — clicking the playhead in a track (not ruler) spans a region from it.
- `move_clip` / `trim_l` / `trim_r` — video row.
- `move_audio` — clicking an unlinked clip's audio block; drags `audio_offset` independently.
- `select_added` — clicking an added-audio tile. Carries `clip_id = audio.id`. Becomes a `move_added` drag that updates `audio.offset` only within its lane.
- `chain` — the little chip in the gap between video and audio rows.
- `resize_tracks` — the divider between video and audio rows.

Selection state is tripartite and mutually exclusive:
- `_selected_id` — a clip.
- `_selected_audio_clip_id` — just the audio block of an unlinked clip.
- `_added_audio_selected_id` — an added-audio tile.

Delete key priority: region > added-audio > clip-audio > clip.

## Drops

Timeline accepts three kinds of drops, all cursor-aware:
- `ASSET_MIME` from the clip bin → `assetDroppedOnTimeline(asset_id, drop_t, lane)`.
- OS audio files → `addedAudioDropped(path, drop_t, lane)`.
- OS video files → `videoFileDropped(path, drop_t)` — imports and inserts at `drop_t`, pushing past any clip already under the cursor.

Lane is derived from the drop `y`: Audio Track 1 rect → `lane=0`, otherwise `lane=1`.

Videos don't have lanes — they always go on the single video track; `_insert_clip_at` advances `start_t` past any clip that contains the requested position.

## Export (`exporter.py`)

`ExportJob.audio_tracks: list[AudioTrack]` — one per `AddedAudio`. For each track:

```
[i:a] atrim=duration=play_dur, asetpts=PTS-STARTPTS,
      [adelay=pre_ms:all=1,]          # skipped when offset = 0
      apad=whole_dur=total,
      volume=V
      [extra_aN]
```

All `extra_aN` are `amix`'d into `extra_mix`; then mixed with the clip audio (`[a_out]volume=orig_vol[orig_a]; [orig_a][extra_mix]amix=...`). `replace=True` on any track replaces the clip audio entirely.

The clip-audio branch is gated by `has_audio and not muted and linked_audio and not audio_removed` — all four need to be truthy for the clip's audio to contribute; otherwise silence.

## Undo

Full-state snapshots in `self._undo_stack` (app.py). Each entry is a dict with cloned clips, **cloned added_audios**, play/UI toggles, playhead, selection. `_restore_added_audios` diffs the current vs snapshot by id and tears down / creates `QMediaPlayer` instances so they stay in sync.

When you add new fields to `Clip` or `AddedAudio`, they need to flow through `clone()` — nothing else.

## Conventions that have bitten me

- **Unlink does NOT mute.** The orange waveform stays, `move_audio` drag repositions it. `audio_removed` is the "delete audio" action (via Delete on the selected audio block) — different thing.
- **Moving a video preserves unlinked audio's absolute position.** `move_clip` in timeline_widget adjusts `audio_offset` by `old_audio_abs - new_start` when `linked_audio` is False.
- **Thumbnails map into `[src_start, src_end]`, not the full asset.** Trimmed clips should show frames that are actually in the clip.
- **Snap targets** in `_snap_clip_start`: `0.0`, `self._playhead`, every other clip's start and end. Tolerance 10 px.
- **Mouse wheel** zooms (or shift+scrolls); it does NOT resize tracks. Track resize is drag-only via the video/audio divider.
- **Added audio doesn't loop** during playback. It plays once over `[offset, offset+duration]`, then pauses. The player's loop mode must stay at the default.
- **Don't call `setSource(QUrl())` per tick** — it's expensive. Hide the video item for gaps instead.
- **Clip bin tiles** need `setGridSize` or they squash on first paint. Placeholders use a play-triangle / speaker icon until the real thumb arrives.

## Threading

Thumbnail and waveform workers are `QObject` subclasses moved onto `QThread`s. Keep Python refs (`self._thumb_threads`, `self._added_wave_workers`, etc.) until the thread's `finished` signal — double-deletion via Python GC + Qt `deleteLater` crashes PySide6. Cancel + `quit()` + `wait()` on close.
