import faulthandler
import os
import sys

faulthandler.enable()

# Qt's default media backend on Linux is GStreamer, which crashes on AV1 /
# unusual codecs. PySide6 6.5+ ships an FFmpeg-based backend that covers
# everything we need — opt into it before QApplication touches the plugins.
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from .app import MainWindow  # noqa: E402


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")

    BG        = QColor(30, 30, 34)
    BG_ALT    = QColor(40, 40, 44)
    BG_INPUT  = QColor(20, 20, 24)
    BG_BUTTON = QColor(45, 45, 50)
    FG        = QColor(220, 220, 220)
    FG_DIM    = QColor(120, 120, 120)
    ACCENT    = QColor(80, 140, 220)

    p = QPalette()
    p.setColor(QPalette.Window,          BG)
    p.setColor(QPalette.WindowText,      FG)
    p.setColor(QPalette.Base,            BG_INPUT)
    p.setColor(QPalette.AlternateBase,   BG_ALT)
    p.setColor(QPalette.ToolTipBase,     BG_ALT)
    p.setColor(QPalette.ToolTipText,     FG)
    p.setColor(QPalette.Text,            FG)
    p.setColor(QPalette.Button,          BG_BUTTON)
    p.setColor(QPalette.ButtonText,      FG)
    p.setColor(QPalette.BrightText,      QColor(255, 80, 80))
    p.setColor(QPalette.Link,            ACCENT)
    p.setColor(QPalette.Highlight,       ACCENT)
    p.setColor(QPalette.HighlightedText, BG_INPUT)
    p.setColor(QPalette.PlaceholderText, FG_DIM)
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, FG_DIM)
    app.setPalette(p)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Cove Video Editor")
    app.setOrganizationName("Cove")
    apply_dark_theme(app)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
