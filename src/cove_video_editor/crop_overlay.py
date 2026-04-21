from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget


HANDLE_SIZE = 10
HIT_PAD = 14
MIN_NORMALIZED = 0.05


class CropOverlay(QWidget):
    """Draggable crop rectangle in normalized 0..1 source coords.

    Renders on top of a video widget, accounts for letterboxing so the rect
    always tracks the actual video pixels rather than the widget area.
    """

    cropChanged = Signal(QRectF)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self._video_aspect: float = 16 / 9
        self._rect_norm: QRectF = QRectF(0.0, 0.0, 1.0, 1.0)
        self._drag_target: str | None = None
        self._drag_start_widget: QPointF | None = None
        self._drag_start_rect: QRectF | None = None

    def set_video_aspect(self, aspect: float) -> None:
        if aspect > 0:
            self._video_aspect = aspect
            self.update()

    def set_normalized_rect(self, rect: QRectF) -> None:
        self._rect_norm = self._clamp(QRectF(rect))
        self.update()

    def normalized_rect(self) -> QRectF:
        return QRectF(self._rect_norm)

    def reset(self) -> None:
        self._rect_norm = QRectF(0.0, 0.0, 1.0, 1.0)
        self.update()
        self.cropChanged.emit(self.normalized_rect())

    def _video_display_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return QRectF(0, 0, 0, 0)
        widget_aspect = w / h
        if widget_aspect > self._video_aspect:
            actual_h = float(h)
            actual_w = h * self._video_aspect
            x = (w - actual_w) / 2
            y = 0.0
        else:
            actual_w = float(w)
            actual_h = w / self._video_aspect
            x = 0.0
            y = (h - actual_h) / 2
        return QRectF(x, y, actual_w, actual_h)

    def _crop_rect_widget(self) -> QRectF:
        v = self._video_display_rect()
        n = self._rect_norm
        return QRectF(
            v.x() + n.x() * v.width(),
            v.y() + n.y() * v.height(),
            n.width() * v.width(),
            n.height() * v.height(),
        )

    def _handle_centers(self, c: QRectF) -> dict[str, QPointF]:
        cx = (c.left() + c.right()) / 2
        cy = (c.top() + c.bottom()) / 2
        return {
            "tl": QPointF(c.left(), c.top()),
            "tr": QPointF(c.right(), c.top()),
            "bl": QPointF(c.left(), c.bottom()),
            "br": QPointF(c.right(), c.bottom()),
            "t":  QPointF(cx, c.top()),
            "b":  QPointF(cx, c.bottom()),
            "l":  QPointF(c.left(), cy),
            "r":  QPointF(c.right(), cy),
        }

    def paintEvent(self, _event) -> None:  # noqa: ANN001
        if not self.isVisible():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        v = self._video_display_rect()
        c = self._crop_rect_widget()

        dim = QColor(0, 0, 0, 150)
        if c.top() > v.top():
            p.fillRect(QRectF(v.left(), v.top(), v.width(), c.top() - v.top()), dim)
        if c.bottom() < v.bottom():
            p.fillRect(QRectF(v.left(), c.bottom(), v.width(), v.bottom() - c.bottom()), dim)
        p.fillRect(QRectF(v.left(), c.top(), c.left() - v.left(), c.height()), dim)
        p.fillRect(QRectF(c.right(), c.top(), v.right() - c.right(), c.height()), dim)

        thirds_pen = QPen(QColor(255, 255, 255, 80), 1, Qt.DashLine)
        p.setPen(thirds_pen)
        for i in (1, 2):
            x = c.left() + c.width() * i / 3
            p.drawLine(QPointF(x, c.top()), QPointF(x, c.bottom()))
            y = c.top() + c.height() * i / 3
            p.drawLine(QPointF(c.left(), y), QPointF(c.right(), y))

        border_pen = QPen(QColor("#5fb4ff"))
        border_pen.setWidth(2)
        p.setPen(border_pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(c)

        p.setBrush(QColor("#5fb4ff"))
        p.setPen(QPen(QColor("#0d1216"), 1))
        s = HANDLE_SIZE
        for pt in self._handle_centers(c).values():
            p.drawRect(QRectF(pt.x() - s / 2, pt.y() - s / 2, s, s))

        p.end()

    def _hit_test(self, pos: QPointF) -> str | None:
        c = self._crop_rect_widget()
        for name, center in self._handle_centers(c).items():
            if (abs(pos.x() - center.x()) <= HIT_PAD
                    and abs(pos.y() - center.y()) <= HIT_PAD):
                return name
        if c.contains(pos):
            return "move"
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton:
            return
        target = self._hit_test(event.position())
        if target:
            self._drag_target = target
            self._drag_start_widget = event.position()
            self._drag_start_rect = QRectF(self._rect_norm)
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_target:
            self._apply_drag(event.position())
        else:
            self.setCursor(_cursor_for(self._hit_test(event.position())))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_target:
            self._drag_target = None
            self.cropChanged.emit(self.normalized_rect())

    def _apply_drag(self, pos: QPointF) -> None:
        v = self._video_display_rect()
        if v.width() <= 0 or v.height() <= 0 or self._drag_start_widget is None:
            return
        dx = (pos.x() - self._drag_start_widget.x()) / v.width()
        dy = (pos.y() - self._drag_start_widget.y()) / v.height()
        r = QRectF(self._drag_start_rect)
        target = self._drag_target

        if target == "move":
            r.translate(dx, dy)
        else:
            if "l" in target:
                r.setLeft(min(r.right() - MIN_NORMALIZED, r.left() + dx))
            if "r" in target:
                r.setRight(max(r.left() + MIN_NORMALIZED, r.right() + dx))
            if "t" in target:
                r.setTop(min(r.bottom() - MIN_NORMALIZED, r.top() + dy))
            if "b" in target:
                r.setBottom(max(r.top() + MIN_NORMALIZED, r.bottom() + dy))

        self._rect_norm = self._clamp(r)
        self.update()

    def _clamp(self, r: QRectF) -> QRectF:
        if r.width() < MIN_NORMALIZED:
            r.setWidth(MIN_NORMALIZED)
        if r.height() < MIN_NORMALIZED:
            r.setHeight(MIN_NORMALIZED)
        if r.left() < 0:
            r.translate(-r.left(), 0)
        if r.top() < 0:
            r.translate(0, -r.top())
        if r.right() > 1:
            r.translate(1 - r.right(), 0)
        if r.bottom() > 1:
            r.translate(0, 1 - r.bottom())
        return QRectF(
            max(0.0, r.left()),
            max(0.0, r.top()),
            min(1.0 - max(0.0, r.left()), r.width()),
            min(1.0 - max(0.0, r.top()), r.height()),
        )


def _cursor_for(target: str | None) -> Qt.CursorShape:
    return {
        "move": Qt.SizeAllCursor,
        "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
        "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor,
        "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
        "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor,
    }.get(target, Qt.ArrowCursor)
