"""Auto-updater backed by the GitHub releases API.

Philosophy: absolutely never silently replace the user's binary. We check
the latest release in the background, tell the user about it, and let them
kick off the actual install.

For AppImage installs we *can* do the download-and-swap end-to-end (the
kernel keeps the running mmap alive across an overwrite, so replacing the
file and re-execing works). For every other packaging (Windows Setup or
Portable, .deb, source) we just open the GitHub release page — the user
runs the installer themselves from there.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal


GITHUB_REPO = "Sin213/cove-video-editor"
LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


@dataclass
class UpdateInfo:
    latest_version: str
    release_url: str
    asset_name: str | None = None
    asset_url: str | None = None
    asset_size: int = 0


def _parse_version(v: str) -> tuple[int, int, int]:
    v = v.strip().lstrip("vV")
    out: list[int] = []
    for part in v.split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
        if len(out) == 3:
            break
    while len(out) < 3:
        out.append(0)
    return (out[0], out[1], out[2])


def version_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def bundle_kind() -> str:
    """Rough detection of how this instance was packaged so we can pick the
    matching release asset."""
    if os.environ.get("APPIMAGE"):
        return "appimage"
    if sys.platform == "win32":
        exe_path = Path(sys.executable).resolve()
        if not getattr(sys, "frozen", False):
            return "source"
        exe_str = str(exe_path)
        if "Program Files" in exe_str or r"AppData\Local" in exe_str:
            return "win-setup"
        return "win-portable"
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
        return "deb"
    return "source"


def preferred_asset(kind: str, assets: list[dict]) -> dict | None:
    def first_match(predicate) -> dict | None:
        return next((a for a in assets if predicate(a["name"].lower())), None)

    if kind == "appimage":
        return first_match(lambda n: n.endswith(".appimage"))
    if kind == "deb":
        return first_match(lambda n: n.endswith(".deb"))
    if kind == "win-setup":
        return first_match(lambda n: "setup" in n and n.endswith(".exe"))
    if kind == "win-portable":
        return first_match(lambda n: "portable" in n and n.endswith(".exe"))
    return None


def fetch_latest_release(timeout: float = 8.0) -> dict | None:
    req = urllib.request.Request(
        LATEST_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "cove-video-editor-updater",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except Exception:  # noqa: BLE001
        return None


class UpdateCheckWorker(QObject):
    updateAvailable = Signal(object)   # UpdateInfo
    noUpdate = Signal()
    failed = Signal(str)

    def __init__(self, current_version: str) -> None:
        super().__init__()
        self._current = current_version

    def run(self) -> None:
        data = fetch_latest_release()
        if data is None:
            self.failed.emit("could not reach the releases API")
            return
        tag = data.get("tag_name") or ""
        if not tag:
            self.failed.emit("release had no tag_name")
            return
        latest = tag.lstrip("vV")
        if not version_newer(latest, self._current):
            self.noUpdate.emit()
            return
        assets = data.get("assets") or []
        asset = preferred_asset(bundle_kind(), assets)
        info = UpdateInfo(
            latest_version=latest,
            release_url=(
                data.get("html_url")
                or f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}"
            ),
            asset_name=asset["name"] if asset else None,
            asset_url=asset["browser_download_url"] if asset else None,
            asset_size=int(asset["size"]) if asset else 0,
        )
        self.updateAvailable.emit(info)


class DownloadWorker(QObject):
    """Stream a URL to a destination file, emitting progress as percentage."""

    progress = Signal(int)           # 0–100
    finished = Signal(str)           # destination path
    failed = Signal(str)

    def __init__(self, url: str, dest: Path) -> None:
        super().__init__()
        self._url = url
        self._dest = dest
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            req = urllib.request.Request(
                self._url, headers={"User-Agent": "cove-video-editor-updater"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                written = 0
                self._dest.parent.mkdir(parents=True, exist_ok=True)
                with open(self._dest, "wb") as f:
                    while True:
                        if self._cancelled:
                            raise RuntimeError("cancelled")
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        written += len(chunk)
                        if total > 0:
                            self.progress.emit(int(written * 100 / total))
            self.finished.emit(str(self._dest))
        except Exception as exc:  # noqa: BLE001
            try:
                self._dest.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            self.failed.emit(str(exc))


def start_check(current_version: str) -> tuple[QThread, UpdateCheckWorker]:
    thread = QThread()
    worker = UpdateCheckWorker(current_version)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.updateAvailable.connect(thread.quit)
    worker.noUpdate.connect(thread.quit)
    worker.failed.connect(thread.quit)
    return thread, worker


def start_download(url: str, dest: Path) -> tuple[QThread, DownloadWorker]:
    thread = QThread()
    worker = DownloadWorker(url, dest)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    return thread, worker


def swap_in_appimage(new_path: Path) -> Path:
    """Replace the running AppImage with `new_path`, leave it executable, and
    return its final path. Caller is responsible for relaunching."""
    current = os.environ.get("APPIMAGE")
    if not current:
        raise RuntimeError("APPIMAGE env var not set — not an AppImage install")
    target = Path(current).resolve()
    shutil.move(str(new_path), str(target))
    mode = os.stat(target).st_mode
    os.chmod(target, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def relaunch(path: Path) -> None:
    """Spawn `path` detached from the current process, then return so the
    caller can quit the Qt app cleanly."""
    # start_new_session detaches from our process group so the child survives
    # our exit — the running process keeps the old binary mmap'd while the
    # new one takes over the path on disk.
    subprocess.Popen(
        [str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
