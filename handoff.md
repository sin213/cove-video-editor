# Handoff: Audio editing (split, trim, merge, delete, region ops)

## What changed

Added full editing support for standalone audio clips (`AddedAudio`) on the timeline — previously only play/pause/stop worked.

## Files changed

- `src/cove_video_editor/clip.py` — Added `src_start`/`src_end` trim fields to `AddedAudio`, plus `src_span`, `timeline_length`, `src_for_timeline()`. Added `split_added_audio()` function.
- `src/cove_video_editor/timeline_widget.py` — Trim handles on selected audio clips (`trim_added_l`/`trim_added_r` drag modes), `addedAudioRangeChanged` signal, waveform draws from `src_start`, tile rect uses trimmed length, always emits `clipSelected` on audio select.
- `src/cove_video_editor/app.py` — `_split_at_playhead` handles audio, `_split_added_audio_at` helper, `_merge_added_audio` for audio merge, `_delete_selected_clip` falls through to audio, region delete/crop operate on audio, playback constrained to selected region, merge button grays out when no merge is possible, `_can_merge`/`_has_mergeable_audio_neighbour` helpers.
- `src/cove_video_editor/exporter.py` — `AudioTrack.src_start` field, `atrim=start=...:end=...` in filtergraph.

## Verification

- `python -c "import ast; ast.parse(open('src/cove_video_editor/clip.py').read())"` — all 4 files
- `PYTHONPATH=src python -m pytest tests/ -x -q` — 9 passed
- Manual: drop audio file, split (S key / button), trim edges, delete, region select + delete/crop, merge after split, region-constrained playback
