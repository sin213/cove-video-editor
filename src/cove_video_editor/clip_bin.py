"""Library / clip bin on the left side of the window.

Holds every imported media asset. Each asset sits on one of two tabs — Videos
or Audio — and is shown as a thumbnail tile. Double-click (or drag onto the
timeline) to add it to the sequence. Drop files from the OS to import.
"""
from __future__ import annotations

from PySide6.QtCore import QMimeData, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDrag,
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .clip import MediaAsset


ASSET_MIME = "application/x-cove-asset-id"


class AssetList(QListWidget):
    """Icon-grid list with drag-to-timeline AND drag-from-OS support."""

    deleteRequested = Signal(str)        # asset id
    filesDropped = Signal(list)          # list[str]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setViewMode(QListWidget.IconMode)
        self.setFlow(QListWidget.LeftToRight)
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setIconSize(QSize(120, 68))
        # Without an explicit grid, the first layout pass stretches the
        # single tile across the available row and squashes its height; fix
        # each cell at a stable thumb+label footprint.
        self.setGridSize(QSize(140, 106))
        self.setSpacing(10)
        self.setUniformItemSizes(True)
        self.setWordWrap(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setStyleSheet(
            "QListWidget { background:#1a1b1f; border:none; color:#dfe2e8; }"
            "QListWidget::item:selected { background:#2a4c7a; border-radius:4px; }"
        )
        self._drop_highlight = False

    # --- key handling ---------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: ANN001
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            item = self.currentItem()
            if item is not None:
                asset_id = item.data(Qt.UserRole)
                if asset_id:
                    self.deleteRequested.emit(str(asset_id))
                    event.accept()
                    return
        super().keyPressEvent(event)

    # --- outgoing drag (item -> timeline) -------------------------------

    def mimeData(self, items):  # noqa: ANN001
        md = QMimeData()
        if items:
            asset_id = items[0].data(Qt.UserRole)
            md.setData(ASSET_MIME, str(asset_id).encode())
            md.setText(str(items[0].text()))
        return md

    def startDrag(self, supported):  # noqa: ANN001
        item = self.currentItem()
        if not item:
            return
        drag = QDrag(self)
        md = QMimeData()
        md.setData(ASSET_MIME, str(item.data(Qt.UserRole)).encode())
        md.setText(item.text())
        drag.setMimeData(md)
        icon = item.icon()
        if not icon.isNull():
            drag.setPixmap(icon.pixmap(120, 68))
        drag.exec(Qt.CopyAction)

    # --- incoming drop (OS files) ---------------------------------------

    def _urls_if_files(self, event) -> list[str] | None:  # noqa: ANN001
        md = event.mimeData()
        if not md.hasUrls():
            return None
        paths = [u.toLocalFile() for u in md.urls() if u.toLocalFile()]
        return paths or None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._urls_if_files(event):
            event.acceptProposedAction()
            self._drop_highlight = True
            self.viewport().update()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._urls_if_files(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event) -> None:  # noqa: ANN001
        self._drop_highlight = False
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._drop_highlight = False
        self.viewport().update()
        paths = self._urls_if_files(event)
        if paths:
            event.acceptProposedAction()
            self.filesDropped.emit(paths)
            return
        super().dropEvent(event)

    # --- empty-state placeholder ----------------------------------------

    def paintEvent(self, event) -> None:  # noqa: ANN001
        super().paintEvent(event)
        if self.count() > 0 and not self._drop_highlight:
            return
        vp = self.viewport()
        p = QPainter(vp)
        p.setRenderHint(QPainter.Antialiasing, True)

        if self._drop_highlight:
            overlay = QColor(95, 180, 255, 40)
            p.fillRect(vp.rect(), overlay)

        if self.count() == 0:
            # Large icon + text; matches the VideoPad pattern.
            cx, cy = vp.width() // 2, vp.height() // 2
            icon_color = QColor("#3a414f") if not self._drop_highlight else QColor("#5fb4ff")
            pen = QPen(icon_color, 3)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)

            box_w, box_h = 76, 86
            box = QRect(cx - box_w // 2, cy - box_h // 2 - 14, box_w, box_h)
            p.drawRoundedRect(box.adjusted(0, 0, -1, -1), 8, 8)
            # little page-corner fold
            p.drawLine(box.right() - 22, box.top() + 4, box.right() - 4, box.top() + 22)
            # arrow pointing down
            arr_cx = box.center().x()
            arr_top = box.top() + 22
            arr_bot = box.bottom() - 12
            p.drawLine(arr_cx, arr_top, arr_cx, arr_bot - 10)
            head = QPolygonF([
                QPointF(arr_cx - 10, arr_bot - 12),
                QPointF(arr_cx + 10, arr_bot - 12),
                QPointF(arr_cx, arr_bot),
            ])
            p.setBrush(QBrush(icon_color))
            p.drawPolygon(head)

            p.setPen(QColor("#7a8294") if not self._drop_highlight else QColor("#cfe4ff"))
            f = p.font(); f.setPointSize(11); p.setFont(f)
            text_rect = QRect(0, cy + box_h // 2 - 10, vp.width(), 40)
            p.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop, "Drag files or folders here")
        p.end()


class ClipBin(QWidget):
    """Left-side library with Videos / Audio tabs."""

    assetActivated = Signal(str)         # double-click
    assetDeleteRequested = Signal(str)   # Delete / Backspace
    addClicked = Signal(str)             # "video" or "audio" — + buttons
    filesDropped = Signal(list)          # list[str] — dropped from OS

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(6, 4, 6, 0)
        title = QLabel("Media")
        title.setStyleSheet("color:#dfe2e8; font-weight:600;")
        header.addWidget(title)
        header.addStretch(1)
        self.add_video_btn = QPushButton("+ Video")
        self.add_audio_btn = QPushButton("+ Audio")
        for b in (self.add_video_btn, self.add_audio_btn):
            b.setStyleSheet(
                "QPushButton { background:#2a2f3a; color:#dfe2e8; border:1px solid #39404d;"
                " border-radius:4px; padding:3px 8px; }"
                "QPushButton:hover { background:#353c4a; }"
            )
        self.add_video_btn.clicked.connect(lambda: self.addClicked.emit("video"))
        self.add_audio_btn.clicked.connect(lambda: self.addClicked.emit("audio"))
        header.addWidget(self.add_video_btn)
        header.addWidget(self.add_audio_btn)
        root.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            "QTabBar::tab { background:#23252b; color:#cfd0d4; padding:4px 12px; }"
            "QTabBar::tab:selected { background:#2a4c7a; color:white; }"
            "QTabWidget::pane { border:1px solid #2a2f3a; background:#1a1b1f; }"
        )
        self.video_list = AssetList()
        self.audio_list = AssetList()
        self.video_list.itemDoubleClicked.connect(self._on_activated)
        self.audio_list.itemDoubleClicked.connect(self._on_activated)
        self.video_list.deleteRequested.connect(self.assetDeleteRequested)
        self.audio_list.deleteRequested.connect(self.assetDeleteRequested)
        self.video_list.filesDropped.connect(self.filesDropped)
        self.audio_list.filesDropped.connect(self.filesDropped)
        self.tabs.addTab(self.video_list, "Videos")
        self.tabs.addTab(self.audio_list, "Audio")
        root.addWidget(self.tabs, stretch=1)

    # Drops on the panel outside the list (e.g. the header / tab bar)
    # still route in as imports.
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
            if paths:
                event.acceptProposedAction()
                self.filesDropped.emit(paths)

    def add_asset(self, asset: MediaAsset) -> None:
        lst = self.video_list if asset.kind == "video" else self.audio_list
        item = QListWidgetItem(asset.path.name)
        item.setData(Qt.UserRole, asset.id)
        item.setToolTip(str(asset.path))
        if asset.thumb is not None and not asset.thumb.isNull():
            pm = QPixmap.fromImage(asset.thumb).scaled(
                120, 68, Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
            item.setIcon(QIcon(pm))
        else:
            item.setIcon(QIcon(_make_placeholder_pixmap(asset.kind)))
        lst.addItem(item)
        self.tabs.setCurrentWidget(lst)

    def remove_asset(self, asset_id: str) -> None:
        for lst in (self.video_list, self.audio_list):
            for i in range(lst.count() - 1, -1, -1):
                it = lst.item(i)
                if it and it.data(Qt.UserRole) == asset_id:
                    lst.takeItem(i)
            lst.viewport().update()

    def set_asset_thumb(self, asset_id: str, img) -> None:  # noqa: ANN001
        for lst in (self.video_list, self.audio_list):
            for i in range(lst.count()):
                it = lst.item(i)
                if it and it.data(Qt.UserRole) == asset_id:
                    pm = QPixmap.fromImage(img).scaled(
                        120, 68, Qt.KeepAspectRatio, Qt.SmoothTransformation,
                    )
                    it.setIcon(QIcon(pm))
                    return

    def _on_activated(self, item: QListWidgetItem) -> None:
        asset_id = item.data(Qt.UserRole)
        if asset_id:
            self.assetActivated.emit(asset_id)


def _make_placeholder_pixmap(kind: str) -> QPixmap:
    """Tile-sized pixmap shown before a real thumbnail is available, so the
    clip bin tiles never look like bare text."""
    pm = QPixmap(120, 68)
    pm.fill(QColor("#1d2026"))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    border = QPen(QColor("#39404d"), 1)
    p.setPen(border)
    p.drawRect(0, 0, 119, 67)
    p.setPen(Qt.NoPen)
    icon_color = QColor("#5fb4ff") if kind == "video" else QColor("#ffb067")
    p.setBrush(icon_color)
    if kind == "video":
        # Play triangle, centered.
        poly = QPolygonF([
            QPointF(50, 22),
            QPointF(50, 46),
            QPointF(74, 34),
        ])
        p.drawPolygon(poly)
    else:
        # Simple speaker silhouette.
        p.drawRoundedRect(48, 24, 8, 20, 1.5, 1.5)
        horn = QPolygonF([
            QPointF(56, 24),
            QPointF(70, 16),
            QPointF(70, 52),
            QPointF(56, 44),
        ])
        p.drawPolygon(horn)
    p.end()
    return pm
