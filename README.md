# Cove Video Editor

A lightweight offline video editor — a videopad-style timeline with multi-clip
video, dual audio tracks, trim / split / crop / region operations, and a
one-shot ffmpeg export. Built with
[PySide6](https://wiki.qt.io/Qt_for_Python). Fully offline, no cloud, no
accounts.

One codebase, native builds for Windows and Linux: a Windows installer and
portable exe, plus a Linux AppImage and `.deb`. Every `v*` tag cuts all four
artifacts via GitHub Actions.

![Python](https://img.shields.io/badge/python-3.10%2B-orange?style=flat-square&logo=python)
![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20Linux-informational?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)

---

## Features

### Timeline

- **Multi-clip video track** — drag any number of videos onto the timeline,
  place them at the cursor, snap to playhead / clip edges / t=0 while dragging.
  Overlaps auto-resolve to the right.
- **Dual audio tracks**
  - **Audio Track 1** — sits next to the clip audio; drop independent audio
    clips here to fill gaps between video clips.
  - **Audio Track 2** — dedicated overlay lane for sound effects, music, or
    anything you want mixed on top.
- **Drag the red playhead** anywhere on the ruler to scrub; click inside the
  tracks to move it too. Playhead-aware snapping while moving clips.
- **Region select / delete / crop / export** — shift-drag any time range; delete
  it (ripple), keep only it (crop-to-selection), or export just that slice.
- **Split at playhead** (`S`) — cut the clip under the playhead into two.
- **Per-clip trim handles** on the video row, with the right edge anchored so
  the trim feels like videopad. Thumbnails follow the trimmed range.
- **Drag the video/audio divider** to resize video vs. audio track heights
  without losing screen real estate.
- **Scroll wheel zooms** around the cursor; **shift+scroll** pans horizontally.
- **Undo** up to 80 steps (`Ctrl+Z`).

### Audio model

- **Linked clip audio** (blue) — plays locked to the video.
- **Unlink** (chain chip) → audio becomes **orange** and draggable along the
  audio row. The video moves independently; the audio's absolute position is
  preserved when you move the video.
- **Delete an unlinked clip's audio** with `Delete` — the track shows
  `(audio deleted — chain chip to restore)`; click the chain chip to bring it
  back.
- **Waveforms** are rendered from real peak data with linear interpolation at
  high zoom, not a scaled bitmap.
- **Multiple added-audio clips per lane** — drop as many as you want. Each is
  independently selectable, draggable, deletable, and keeps its natural
  duration (no loops, no stretches).

### Playback

- **Timer-driven playhead** — playback works over gaps (preview goes black and
  audio keeps going) and on audio-only timelines (no video clip required).
- **Dedicated player for unlinked audio** — plays at the offset, silent
  outside the shifted range.
- **Aux players resync on scrub** so moving the playhead during playback
  doesn't leave audio stuck on the old position.
- **Black preview in gaps** — the video item hides when the playhead isn't on
  any clip, instead of freezing on the last frame.

### Media bin

- **+ Video / + Audio** to import; drop files directly from the OS anywhere
  on the window or timeline.
- **Placeholder thumbnails** show a play-triangle (video) or speaker (audio)
  glyph until the real thumb is extracted — tiles never look like bare text.
- **Delete** removes the asset and any clips/audios that used it.
- **Drag a bin tile onto the timeline** — video lands at the drop x, audio
  lands on the lane under the cursor.

### Editing

- **Crop tool** — toggle an overlay on the preview, drag corners/edges to
  size, drag inside to move, reset with one click. Exports respect the crop.
- **Clip properties dialog** (double-click a clip) — fine-tune speed
  (0.25×–4×), trim start / end with numeric input, or mute the clip.
- **Region context menu** (right-click) — delete, crop-to-selection, or export
  only the selected region.

### Export

- **MP4 (H.264 + AAC), MP4 (H.265), MKV, WebM (VP9 + Opus), MOV, AVI, GIF,
  MP3, WAV** — all driven from the same ffmpeg filtergraph.
- **Region export** — export only the selected range by piping the final map
  through `-ss` / `-t`.
- **Audio Track 1 + 2 mix** — every added-audio clip is `atrim`'d to its own
  duration, `adelay`'d to its offset, `apad`'ed to the timeline length, then
  `amix`'d together. Optional "replace original audio" swap.
- **Live progress + ETA** from `ffmpeg -progress pipe:1` with EMA smoothing.
- **Bundled ffmpeg** — no separate install required in the release builds.

---

## Install a prebuilt release

Head to the [Releases page](https://github.com/Sin213/cove-video-editor/releases)
and grab the artifact for your OS:

| OS      | Artifact                                      | Notes                                        |
| ------- | --------------------------------------------- | -------------------------------------------- |
| Windows | `cove-video-editor-<version>-Setup.exe`       | Inno Setup installer (Start Menu + Desktop)  |
| Windows | `cove-video-editor-<version>-Portable.exe`    | Single-file, no install                      |
| Linux   | `Cove-Video-Editor-<version>-x86_64.AppImage` | `chmod +x` and run                           |
| Linux   | `cove-video-editor_<version>_amd64.deb`       | `sudo apt install ./cove-video-editor_*.deb` |

`ffmpeg` and `ffprobe` are **bundled inside every artifact** — no additional
installs needed.

> **Windows SmartScreen** may warn on first launch because the exe isn't
> signed. Click **More info → Run anyway**.

---

## Keyboard + mouse cheatsheet

| Action                              | Shortcut                                |
| ----------------------------------- | --------------------------------------- |
| Play / pause                        | `Space`                                 |
| Next / previous frame               | `.` / `,`                               |
| Split at playhead                   | `S`                                     |
| Merge selected clip with next / previous | `M` / `Shift+M`                    |
| Jump to selected clip start / end   | `[` / `]`                               |
| Previous / next clip edge           | `Alt+,` / `Alt+.`                       |
| Jump to sequence start / end        | `Home` / `End`                          |
| Delete selected region / audio / clip | `Delete` / `Backspace`                |
| Undo / Redo                         | `Ctrl+Z` / `Ctrl+Y`                     |
| Exit crop mode                      | `Esc`                                   |
| Region-select                       | Shift-drag, or drag in empty timeline   |
| Seek                                | Click anywhere on the ruler, or drag    |
| Zoom in / out                       | Mouse wheel                             |
| Pan horizontally                    | Shift + wheel                           |
| Resize video vs. audio track heights | Drag the divider between them          |

---

## Running from source (Linux)

Python 3.10+. On Arch:

```bash
sudo pacman -S python pyside6 ffmpeg
python -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python -m cove_video_editor
```

On Debian / Ubuntu:

```bash
sudo apt install python3 python3-pyside6.qtwidgets ffmpeg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python -m cove_video_editor
```

---

## Running from source (Windows)

Python 3.10+ from [python.org](https://www.python.org/downloads/) (tick
**"Add python.exe to PATH"** during install).

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# ffmpeg via winget…
winget install Gyan.FFmpeg
# …or drop ffmpeg.exe + ffprobe.exe somewhere on PATH.

$env:PYTHONPATH = "src"
.venv\Scripts\python -m cove_video_editor
```

---

## Building release artifacts yourself

PyInstaller can't cross-compile, so each platform has its own script. Both
download ffmpeg automatically.

### Linux — AppImage + .deb

```bash
bash scripts/build-release.sh
# Output in release/:
#   Cove-Video-Editor-1.0.0-x86_64.AppImage
#   cove-video-editor_1.0.0_amd64.deb
```

Override the version with `VERSION=1.2.0 bash scripts/build-release.sh`.

### Windows — Setup.exe + Portable.exe

Requires [Inno Setup 6](https://jrsoftware.org/isdl.php) (pre-installed on
GitHub Actions' `windows-latest`).

```powershell
.\build.ps1 -Version 1.0.0
# Output in release\:
#   cove-video-editor-1.0.0-Setup.exe
#   cove-video-editor-1.0.0-Portable.exe
```

### Automated release via GitHub Actions

Push a tag matching `v*` (e.g. `v1.0.0`) and `.github/workflows/release.yml`
runs the Linux + Windows jobs in parallel and attaches all four artifacts to
the GitHub Release created for the tag.

---

## How it works

```
src/cove_video_editor/
├── __main__.py        entry point + dark theme + FFmpeg backend selection
├── app.py             MainWindow: players, timer-driven playback, undo, export glue
├── timeline_widget.py timeline canvas: ruler, video, Audio Track 1, Audio Track 2
├── clip.py            Clip, AddedAudio, MediaAsset + region ops
├── clip_bin.py        left-side media library with drag source
├── crop_overlay.py    draggable crop rect with rule-of-thirds guides
├── thumbnails.py      QThread workers for thumbnails + peak waveforms
├── exporter.py        builds the ffmpeg filtergraph, runs it with progress
├── ffmpeg_utils.py    ffprobe wrapper + format table + binary resolution
└── assets/            icon

packaging/
├── installer.iss                  Inno Setup script
├── launcher.py                    PyInstaller entry point
└── cove-video-editor.desktop      Linux desktop entry

build.ps1                          Windows Setup.exe + Portable.exe builder
scripts/build-release.sh           Linux AppImage + .deb builder
.github/workflows/release.yml      Cross-platform release CI
```

Playback is driven by a `QTimer`; the main `QMediaPlayer` is a passive
renderer whose position is slaved to the timeline. A dedicated second player
handles unlinked-clip audio (shifted to the user's offset), and each added
audio clip has its own `QMediaPlayer` that plays in its range and pauses
outside it — so the playhead can scrub across gaps, audio-only spans, or
past the last video, with the preview going black.

Export is one ffmpeg invocation per job: each clip becomes an input, a
`filter_complex` graph trims / crops / scales / speed-adjusts / concatenates
them, and each audio track is placed with `atrim + adelay + apad` before
being `amix`'d with the clip audio (or replacing it). Progress is parsed
from `ffmpeg -progress pipe:1`; ETA is derived from elapsed time vs.
completion percentage with an EMA smoother.

---

## Credits

- [Qt for Python (PySide6)](https://wiki.qt.io/Qt_for_Python) — UI toolkit.
- [FFmpeg](https://ffmpeg.org/) — every video frame, audio sample, and filter.
- [Inno Setup](https://jrsoftware.org/isinfo.php) — the `Setup.exe` installer.

---

## Licensing

- Cove Video Editor is **MIT** — see `LICENSE`.
- The bundled `ffmpeg` / `ffprobe` binaries are the **gyan.dev
  release-essentials** (Windows) and **johnvansickle.com static** (Linux)
  builds, both **GPLv3**. Cove Video Editor shells out to these binaries
  rather than linking, so the app's MIT licensing stands. If you redistribute
  release artifacts, comply with the ffmpeg GPL terms — most commonly by
  keeping `FFMPEG-LICENSE.txt` alongside the binary and pointing recipients
  at [ffmpeg.org](https://ffmpeg.org/) for sources.
