from __future__ import annotations

from datetime import datetime
import math

from PySide6.QtCore import QItemSelectionModel, QPoint, QPointF, QRectF, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QCursor,
    QFont,
    QIcon,
    QImage,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .canvas import CanvasWidget
from .core import BLEND_MODES, BRUSH_PRESETS, BrushSettings, LayerNode, PaintDocument

RECENT_COLUMN_WIDTH = 26
# Brush panel content is 248px wide: color square + 6px gap + recent-color column.
PANEL_SQUARE_SIZE = 248 - 6 - RECENT_COLUMN_WIDTH
CHOICE_LABEL_WIDTH = 46
STAMP_PAD_SIZE = 132
GLYPH_SCALE = 3.0
RECENT_THUMB_WIDTH = 88
RECENT_THUMB_HEIGHT = 66


def _checker(painter: QPainter, rect: QRectF, cell: int = 8, *, dark: bool = False) -> None:
    painter.fillRect(rect, QColor("#2a2b2f" if dark else "#d0d0d0"))
    painter.setBrush(QColor("#3a3b40" if dark else "#f2f2f2"))
    painter.setPen(Qt.PenStyle.NoPen)
    left, top = int(rect.left()), int(rect.top())
    for y in range(top, int(rect.bottom()) + 1, cell):
        for x in range(left, int(rect.right()) + 1, cell):
            if ((x - left) // cell + (y - top) // cell) % 2 == 0:
                painter.drawRect(x, y, cell, cell)


def _tool_icon(kind: str) -> QIcon:
    pixmap = QPixmap(22, 22)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor("#d7d8dc"), 1.6)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if kind == "visible":
        painter.drawPath(_eye_path())
        painter.setBrush(QColor("#d7d8dc"))
        painter.drawEllipse(QPointF(11, 11), 2.4, 2.4)
    elif kind == "alpha":
        painter.drawRoundedRect(QRectF(5, 9, 12, 9), 2, 2)
        painter.drawArc(QRectF(7, 3, 8, 10), 0, 180 * 16)
        painter.drawLine(11, 12, 11, 16)
    elif kind == "clip":
        painter.drawRoundedRect(QRectF(3, 5, 9, 9), 1.5, 1.5)
        painter.drawRoundedRect(QRectF(10, 9, 9, 9), 1.5, 1.5)
        painter.drawLine(7, 17, 15, 4)
        painter.drawLine(12, 4, 15, 4)
        painter.drawLine(15, 4, 15, 7)
    elif kind == "group":
        painter.setBrush(QColor("#353842"))
        painter.drawRoundedRect(QRectF(3, 7, 16, 11), 2, 2)
        painter.drawPath(_folder_tab_path())
    painter.end()
    return QIcon(pixmap)


def _eye_path():
    from PySide6.QtGui import QPainterPath

    path = QPainterPath(QPointF(2.5, 11))
    path.cubicTo(6, 5.5, 16, 5.5, 19.5, 11)
    path.cubicTo(16, 16.5, 6, 16.5, 2.5, 11)
    path.closeSubpath()
    return path


def _folder_tab_path():
    from PySide6.QtGui import QPainterPath

    path = QPainterPath(QPointF(4, 7))
    path.lineTo(6, 4)
    path.lineTo(11, 4)
    path.lineTo(13, 7)
    return path


class SaturationValueSquare(QWidget):
    changed = Signal(float, float)
    released = Signal()

    def __init__(self, side: int = PANEL_SQUARE_SIZE) -> None:
        super().__init__()
        self.hue = 0.0
        self.saturation = 0.0
        self.value = 0.07
        self.setFixedSize(side, side)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return width

    def resizeEvent(self, event) -> None:
        if self.height() != self.width():
            self.setFixedHeight(self.width())
        super().resizeEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        horizontal = QLinearGradient(rect.topLeft(), rect.topRight())
        horizontal.setColorAt(0.0, QColor("white"))
        horizontal.setColorAt(1.0, QColor.fromHsvF(self.hue, 1.0, 1.0))
        painter.fillRect(rect, horizontal)
        vertical = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        vertical.setColorAt(0.0, QColor(0, 0, 0, 0))
        vertical.setColorAt(1.0, QColor(0, 0, 0, 255))
        painter.fillRect(rect, vertical)
        point = QPointF(rect.left() + self.saturation * rect.width(), rect.bottom() - self.value * rect.height())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("black"), 3))
        painter.drawEllipse(point, 5, 5)
        painter.setPen(QPen(QColor("white"), 1.2))
        painter.drawEllipse(point, 5, 5)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._set_from_position(event.position())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._set_from_position(event.position())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._set_from_position(event.position())
            self.released.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _set_from_position(self, position: QPointF) -> None:
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        self.saturation = max(0.0, min(1.0, (position.x() - rect.left()) / max(1.0, rect.width())))
        self.value = max(0.0, min(1.0, 1.0 - (position.y() - rect.top()) / max(1.0, rect.height())))
        self.changed.emit(self.saturation, self.value)
        self.update()


class HueStrip(QWidget):
    changed = Signal(float)

    def __init__(self) -> None:
        super().__init__()
        self.hue = 0.0
        self.setFixedHeight(16)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        gradient = QLinearGradient(rect.topLeft(), rect.topRight())
        for index in range(7):
            gradient.setColorAt(index / 6.0, QColor.fromHsvF(index / 6.0, 1.0, 1.0))
        painter.fillRect(rect, gradient)
        x = rect.left() + rect.width() * self.hue
        painter.setPen(QPen(QColor("black"), 3))
        painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        painter.setPen(QPen(QColor("white"), 1))
        painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._set_from_x(event.position().x())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._set_from_x(event.position().x())

    def _set_from_x(self, x: float) -> None:
        self.hue = max(0.0, min(1.0, x / max(1, self.width() - 1)))
        self.changed.emit(self.hue)
        self.update()


class RecentColors(QWidget):
    picked = Signal(QColor)
    SLOTS = 8

    def __init__(self, height: int = PANEL_SQUARE_SIZE) -> None:
        super().__init__()
        self.colors: list[QColor] = []
        self.setObjectName("RecentColors")
        self.setFixedSize(RECENT_COLUMN_WIDTH, height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_colors(self, colors) -> None:
        self.colors = [QColor(color) for color in colors][: self.SLOTS]
        self.update()

    def _slot_rect(self, index: int) -> QRectF:
        gap = 4.0
        height = (self.height() - gap * (self.SLOTS - 1)) / self.SLOTS
        return QRectF(0.5, index * (height + gap) + 0.5, self.width() - 1.0, height - 1.0)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for index in range(self.SLOTS):
            rect = self._slot_rect(index)
            if index < len(self.colors):
                painter.setPen(QPen(QColor("#3a3d45"), 1))
                painter.setBrush(self.colors[index])
            else:
                painter.setPen(QPen(QColor("#25272d"), 1))
                painter.setBrush(QColor("#16171a"))
            painter.drawRoundedRect(rect, 3, 3)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        for index, color in enumerate(self.colors):
            if self._slot_rect(index).contains(event.position()):
                self.picked.emit(QColor(color))
                break
        event.accept()


class ColorSelector(QWidget):
    color_changed = Signal(QColor)

    def __init__(
        self,
        color: QColor,
        *,
        square_size: int = PANEL_SQUARE_SIZE,
        show_swatch: bool = True,
        accessory: QWidget | None = None,
    ) -> None:
        super().__init__()
        self.square = SaturationValueSquare(square_size)
        self.hue_strip = HueStrip()
        self.swatch = QFrame()
        self.swatch.setObjectName("ColorSwatch")
        self.swatch.setFixedHeight(18)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        if accessory is None:
            layout.addWidget(self.square, 0, Qt.AlignmentFlag.AlignHCenter)
        else:
            top = QHBoxLayout()
            top.setContentsMargins(0, 0, 0, 0)
            top.setSpacing(6)
            top.addWidget(self.square)
            top.addWidget(accessory)
            layout.addLayout(top)
        layout.addWidget(self.hue_strip)
        if show_swatch:
            layout.addWidget(self.swatch)
        else:
            self.swatch.hide()
        self.square.changed.connect(self._changed)
        self.hue_strip.changed.connect(self._hue_changed)
        self.set_color(color)

    def set_color(self, color: QColor) -> None:
        hue, saturation, value, _alpha = color.getHsvF()
        hue = 0.0 if hue < 0 else hue
        self.square.hue = hue
        self.square.saturation = saturation
        self.square.value = value
        self.hue_strip.hue = hue
        self._update_swatch(color)
        self.square.update()
        self.hue_strip.update()

    def _hue_changed(self, hue: float) -> None:
        self.square.hue = hue
        self.square.update()
        self._emit_color()

    def _changed(self, _saturation: float, _value: float) -> None:
        self._emit_color()

    def _emit_color(self) -> None:
        color = QColor.fromHsvF(self.hue_strip.hue, self.square.saturation, self.square.value)
        self._update_swatch(color)
        self.color_changed.emit(color)

    def _update_swatch(self, color: QColor) -> None:
        self.swatch.setStyleSheet(f"background: {color.name()}; border: 1px solid #4a4d56; border-radius: 3px;")


class QuickColorPopup(QWidget):
    dismissed = Signal()

    def __init__(self, document: PaintDocument) -> None:
        super().__init__(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.document = document
        self._cursor_override = False
        self.setObjectName("QuickColorPopup")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        self.selector = ColorSelector(document.brush_color, square_size=184, show_swatch=False)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.selector.setCursor(Qt.CursorShape.ArrowCursor)
        self.selector.square.setCursor(Qt.CursorShape.CrossCursor)
        self.selector.hue_strip.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.selector)
        self.selector.color_changed.connect(lambda color: document.set_brush(color=color))
        self.selector.square.released.connect(self.hide)
        self.setStyleSheet(
            """
            #QuickColorPopup { background: #111215; border: 1px solid #454955; border-radius: 4px; }
            """
        )
        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    def show_at_cursor(self) -> None:
        self.selector.set_color(self.document.brush_color)
        if not self._cursor_override:
            QApplication.setOverrideCursor(Qt.CursorShape.ArrowCursor)
            self._cursor_override = True
        cursor = QCursor.pos()
        screen = QApplication.screenAt(cursor) or QApplication.primaryScreen()
        available = screen.availableGeometry()
        x = cursor.x() - self.width() // 2
        y = cursor.y() - (8 + self.selector.square.height() // 2)
        x = max(available.left(), min(x, available.right() - self.width() + 1))
        y = max(available.top(), min(y, available.bottom() - self.height() + 1))
        self.move(x, y)
        self.show()
        self.raise_()

    def hideEvent(self, event) -> None:
        if self._cursor_override:
            QApplication.restoreOverrideCursor()
            self._cursor_override = False
        super().hideEvent(event)
        self.dismissed.emit()


def _glyph_pixmap(size: int, draw) -> QPixmap:
    # Drawn at 3x so icons stay crisp on the 150% monitors.
    pixmap = QPixmap(round(size * GLYPH_SCALE), round(size * GLYPH_SCALE))
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(GLYPH_SCALE)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    draw(painter, float(size))
    painter.end()
    return pixmap


def _polygon_path(center: QPointF, radius: float, shape: str) -> QPainterPath:
    sides = 4 if shape == "diamond" else 3 if shape == "triangle" else 10
    path = QPainterPath()
    for index in range(sides):
        angle = math.radians(-90 + index * 360.0 / sides)
        distance = radius if shape != "star" or index % 2 == 0 else radius * 0.42
        point = QPointF(center.x() + math.cos(angle) * distance, center.y() + math.sin(angle) * distance)
        path.moveTo(point) if index == 0 else path.lineTo(point)
    path.closeSubpath()
    return path


def _glyph_curve() -> QPainterPath:
    path = QPainterPath(QPointF(4.5, 24.5))
    path.cubicTo(QPointF(11.0, 31.0), QPointF(17.0, 5.0), QPointF(27.5, 7.5))
    return path


def _curve_points(path: QPainterPath, offset: float = 0.0, steps: int = 40) -> list[QPointF]:
    points = []
    for index in range(steps + 1):
        t = index / steps
        point = path.pointAtPercent(t)
        if offset:
            angle = math.radians(path.angleAtPercent(t))
            point += QPointF(math.sin(angle), math.cos(angle)) * offset
        points.append(point)
    return points


def _brush_glyph(engine: str, size: int = 28) -> QPixmap:
    def draw(painter: QPainter, side: float) -> None:
        painter.scale(side / 32.0, side / 32.0)
        ink = QColor("#eceef2")
        painter.setPen(Qt.PenStyle.NoPen)
        if engine == "air":
            gradient = QRadialGradient(QPointF(16, 16), 14.5)
            for stop, alpha in ((0.0, 235), (0.3, 170), (0.65, 60), (1.0, 0)):
                gradient.setColorAt(stop, QColor(236, 238, 242, alpha))
            painter.setBrush(gradient)
            painter.drawEllipse(QPointF(16, 16), 14.5, 14.5)
        elif engine == "ink":
            painter.setBrush(ink)
            curve = _glyph_curve()
            points = _curve_points(curve, steps=64)
            for index, point in enumerate(points):
                t = index / (len(points) - 1)
                radius = 0.55 + 2.9 * math.sin(math.pi * t) ** 0.9
                painter.drawEllipse(point, radius, radius)
        elif engine == "paint":
            curve = _glyph_curve()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for offset, alpha, width in ((-3.0, 110, 1.3), (-1.5, 210, 1.6), (0.0, 255, 1.8), (1.5, 190, 1.6), (3.0, 95, 1.3)):
                points = _curve_points(curve, offset)
                path = QPainterPath(points[0])
                for point in points[1:]:
                    path.lineTo(point)
                painter.setPen(QPen(QColor(236, 238, 242, alpha), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawPath(path)
        elif engine == "shape":
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(ink, 2.2))
            painter.drawRoundedRect(QRectF(4.5, 5.5, 17, 15), 2, 2)
            painter.setBrush(QColor(236, 238, 242, 140))
            painter.drawEllipse(QPointF(21.5, 21), 7, 7)
        elif engine == "blur":
            gradient = QRadialGradient(QPointF(16, 16), 14)
            for stop, alpha in ((0.0, 90), (0.45, 70), (1.0, 0)):
                gradient.setColorAt(stop, QColor(236, 238, 242, alpha))
            painter.setBrush(gradient)
            painter.drawEllipse(QPointF(16, 16), 14, 14)
            drop = QPainterPath(QPointF(16, 6.5))
            drop.cubicTo(QPointF(19, 11), QPointF(22.5, 14.5), QPointF(22.5, 18.5))
            drop.cubicTo(QPointF(22.5, 22.5), QPointF(19.5, 25), QPointF(16, 25))
            drop.cubicTo(QPointF(12.5, 25), QPointF(9.5, 22.5), QPointF(9.5, 18.5))
            drop.cubicTo(QPointF(9.5, 14.5), QPointF(13, 11), QPointF(16, 6.5))
            painter.setBrush(ink)
            painter.drawPath(drop)
        elif engine == "stamp":
            painter.setBrush(ink)
            painter.drawPath(_polygon_path(QPointF(13.5, 17.5), 11, "star"))
            painter.setBrush(QColor(236, 238, 242, 150))
            painter.drawPath(_polygon_path(QPointF(26, 7.5), 4.8, "star"))
            painter.drawPath(_polygon_path(QPointF(26.5, 25), 3.8, "star"))
        elif engine == "fill":
            painter.save()
            painter.translate(13.5, 17)
            painter.rotate(-28)
            bucket = QPainterPath(QPointF(-8, -5))
            bucket.lineTo(8, -5)
            bucket.lineTo(6, 9)
            bucket.lineTo(-6, 9)
            bucket.closeSubpath()
            painter.setBrush(QColor(236, 238, 242, 60))
            painter.setPen(QPen(ink, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(bucket)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawArc(QRectF(-6, -12, 12, 13), 0, 180 * 16)
            painter.restore()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ink)
            drop = QPainterPath(QPointF(26.5, 15))
            drop.cubicTo(QPointF(28.5, 19), QPointF(30, 21), QPointF(30, 23))
            drop.cubicTo(QPointF(30, 25.2), QPointF(28.4, 26.5), QPointF(26.5, 26.5))
            drop.cubicTo(QPointF(24.6, 26.5), QPointF(23, 25.2), QPointF(23, 23))
            drop.cubicTo(QPointF(23, 21), QPointF(24.5, 19), QPointF(26.5, 15))
            painter.drawPath(drop)
        else:
            painter.setBrush(ink)
            painter.drawEllipse(QPointF(16, 16), 10, 10)

    return _glyph_pixmap(size, draw)


def _tip_glyph(shape: str, size: int = 18) -> QIcon:
    def draw(painter: QPainter, side: float) -> None:
        painter.scale(side / 18.0, side / 18.0)
        ink = QColor("#e6e7ea")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        center = QPointF(9, 9)
        if shape == "square":
            painter.drawRect(QRectF(3, 3, 12, 12))
        elif shape in {"diamond", "triangle", "star"}:
            painter.drawPath(_polygon_path(QPointF(9, 9.8 if shape == "triangle" else 9), 7.5, shape))
        elif shape == "texture":
            painter.drawEllipse(center, 7, 7)
            painter.setBrush(QColor("#1c1e23"))
            for x, y, radius in ((6.2, 7.0, 1.3), (11.2, 8.2, 1.1), (8.4, 11.8, 1.0), (11.6, 11.8, 0.8)):
                painter.drawEllipse(QPointF(x, y), radius, radius)
        elif shape == "rect":
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(ink, 1.8))
            painter.drawRoundedRect(QRectF(2.5, 4, 13, 10), 1.5, 1.5)
        elif shape == "ellipse":
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(ink, 1.8))
            painter.drawEllipse(QRectF(2.5, 4, 13, 10))
        else:
            painter.drawEllipse(center, 7, 7)

    return QIcon(_glyph_pixmap(size, draw))


def _stamp_glyph(image: QImage, size: int = 18) -> QIcon:
    def draw(painter: QPainter, side: float) -> None:
        tinted = QImage(image.size(), QImage.Format.Format_ARGB32_Premultiplied)
        tinted.fill(QColor("#e6e7ea"))
        tint = QPainter(tinted)
        tint.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        tint.drawImage(0, 0, image)
        tint.end()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(QRectF(0.5, 0.5, side - 1.0, side - 1.0), tinted)
        painter.setPen(QPen(QColor("#5a5d66"), 0.8, Qt.PenStyle.DotLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(0.5, 0.5, side - 1.0, side - 1.0))

    return QIcon(_glyph_pixmap(size, draw))


class BarSlider(QWidget):
    """Full-width value bar: drag anywhere to set, double-click to type an exact value."""

    value_changed = Signal(int)
    committed = Signal(int)

    def __init__(
        self,
        title: str,
        minimum: int,
        maximum: int,
        value: int,
        suffix: str = "",
        *,
        curve: float = 1.0,
    ) -> None:
        super().__init__()
        self.title = title
        self.minimum = minimum
        self.maximum = maximum
        self.suffix = suffix
        self.curve = curve
        self._value = self._clamp(value)
        self._value_before_press = self._value
        self._dragging = False
        self._editor: QSpinBox | None = None
        self.setObjectName("BarSlider")
        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # No keyboard focus: Home/End/arrows must stay canvas shortcuts.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.SizeHorCursor)

    def value(self) -> int:
        return self._value

    def set_value(self, value: float) -> None:
        value = self._clamp(value)
        if value == self._value:
            return
        self._value = value
        self.update()
        self.value_changed.emit(value)

    def sync_value(self, value: float) -> None:
        self._value = self._clamp(value)
        self.update()

    def _clamp(self, value: float) -> int:
        return max(self.minimum, min(self.maximum, int(round(value))))

    def _track(self) -> QRectF:
        return QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

    def _fraction(self) -> float:
        span = self.maximum - self.minimum
        if span <= 0:
            return 0.0
        return ((self._value - self.minimum) / span) ** (1.0 / self.curve)

    def _value_at(self, x: float) -> float:
        track = self._track()
        t = max(0.0, min(1.0, (x - track.left()) / max(1.0, track.width())))
        return self.minimum + (self.maximum - self.minimum) * t**self.curve

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = self._track()
        outline = QPainterPath()
        outline.addRoundedRect(track, 3, 3)
        painter.fillPath(outline, QColor("#1c1e23"))
        fraction = self._fraction()
        if fraction > 0.0:
            painter.save()
            painter.setClipPath(outline)
            fill = QRectF(track.left(), track.top(), track.width() * fraction, track.height())
            painter.fillRect(fill, QColor("#7a0f55"))
            painter.fillRect(QRectF(fill.right() - 2.0, track.top(), 2.0, track.height()), QColor("#e3008c"))
            painter.restore()
        painter.setPen(QPen(QColor("#30333b"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(outline)
        text = track.adjusted(8, 0, -8, 0)
        painter.setPen(QColor("#e4e5e9"))
        painter.drawText(text, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.title)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(text, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, f"{self._value}{self.suffix}")
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._editor is not None:
            super().mousePressEvent(event)
            return
        self._value_before_press = self._value
        self._dragging = True
        self.set_value(self._value_at(event.position().x()))
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            self.set_value(self._value_at(event.position().x()))
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self.committed.emit(self._value)
            event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        # The first click of the double-click already moved the value, put it back before typing.
        self._dragging = False
        self.set_value(self._value_before_press)
        self._open_editor()
        event.accept()

    def wheelEvent(self, event) -> None:
        event.ignore()

    def _open_editor(self) -> None:
        editor = QSpinBox(self)
        editor.setObjectName("BarSliderEditor")
        editor.setRange(self.minimum, self.maximum)
        editor.setSuffix(self.suffix)
        editor.setValue(self._value)
        editor.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        editor.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        editor.setGeometry(self.rect())
        editor.editingFinished.connect(self._finish_editor)
        self._editor = editor
        editor.show()
        editor.setFocus(Qt.FocusReason.MouseFocusReason)
        editor.selectAll()

    def _finish_editor(self) -> None:
        editor = self._editor
        if editor is None:
            return
        self._editor = None
        value = editor.value()
        editor.hide()
        editor.deleteLater()
        self.set_value(value)
        self.committed.emit(self._value)
        canvas = self.window().findChild(CanvasWidget)
        if canvas is not None:
            canvas.setFocus(Qt.FocusReason.OtherFocusReason)


class ChoiceRow(QWidget):
    """One-click horizontal choice strip, optionally labelled on the left."""

    changed = Signal(str)

    def __init__(self, title: str | None, options: tuple[tuple[str, str, QIcon | None], ...]) -> None:
        super().__init__()
        self._buttons: dict[str, QToolButton] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        if title:
            label = QLabel(title)
            label.setFixedWidth(CHOICE_LABEL_WIDTH)
            layout.addWidget(label)
        strip = QWidget()
        strip.setObjectName("ChoiceStrip")
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(2, 2, 2, 2)
        strip_layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for value, text, icon in options:
            button = QToolButton()
            button.setObjectName("ChoiceButton")
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setFixedHeight(24)
            if icon is not None:
                button.setIcon(icon)
                button.setIconSize(QSize(18, 18))
                button.setToolTip(text)
            else:
                button.setText(text)
            button.clicked.connect(lambda _checked=False, choice=value: self.changed.emit(choice))
            self._group.addButton(button)
            strip_layout.addWidget(button)
            self._buttons[value] = button
        layout.addWidget(strip, 1)

    def value(self) -> str:
        return next((value for value, button in self._buttons.items() if button.isChecked()), "")

    def set_value(self, value: str) -> None:
        button = self._buttons.get(value)
        if button is None:
            # Stored value has no button here, so leave every button unchecked.
            self._group.setExclusive(False)
            for other in self._buttons.values():
                other.setChecked(False)
            self._group.setExclusive(True)
            return
        with QSignalBlocker(button):
            button.setChecked(True)

    def button(self, value: str) -> QToolButton:
        return self._buttons[value]

    def set_option_visible(self, value: str, visible: bool) -> None:
        self._buttons[value].setVisible(visible)


def _chip(text: str) -> QToolButton:
    button = QToolButton()
    button.setObjectName("ChipButton")
    button.setText(text)
    button.setCheckable(True)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    button.setFixedHeight(26)
    return button


def _row(*widgets: QWidget) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    for widget in widgets:
        layout.addWidget(widget)
    return row


class StampPad(QWidget):
    changed = Signal(QImage)

    def __init__(self, image: QImage) -> None:
        super().__init__()
        self.image = QImage(image)
        self._last = QPointF()
        # Square to match the square tip image, so a drawn circle stamps as a circle.
        self.setFixedSize(STAMP_PAD_SIZE, STAMP_PAD_SIZE)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_image(self, image: QImage) -> None:
        self.image = QImage(image)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(self.rect(), QColor("#18191d"))
        painter.drawImage(QRectF(self.rect()).adjusted(1, 1, -1, -1), self.image)
        painter.setPen(QPen(QColor("#3a3d45"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            self._last = event.position()
            self._draw(event.position(), erase=event.button() == Qt.MouseButton.RightButton)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & (Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton):
            erase = bool(event.buttons() & Qt.MouseButton.RightButton)
            self._draw(event.position(), erase=erase)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            self.changed.emit(QImage(self.image))

    def _draw(self, position: QPointF, *, erase: bool) -> None:
        inner = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        scale = self.image.width() / max(1.0, inner.width())
        start = (self._last - inner.topLeft()) * scale
        end = (position - inner.topLeft()) * scale
        painter = QPainter(self.image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Clear
            if erase
            else QPainter.CompositionMode.CompositionMode_SourceOver
        )
        painter.setPen(
            QPen(QColor("white"), max(4.0, self.image.width() * 0.055), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        )
        painter.drawLine(start, end)
        painter.end()
        self._last = QPointF(position)
        self.update()


class BrushOption(QWidget):
    clicked = Signal(str)

    def __init__(self, preset: BrushSettings, key: str) -> None:
        super().__init__()
        self.preset_id = preset.preset_id
        self.name = preset.name
        self.key = key
        self.current = False
        self._glyph = _brush_glyph(preset.engine, 26)
        self.setFixedHeight(40)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.current:
            painter.setPen(QPen(QColor("#e3008c"), 1))
            painter.setBrush(QColor("#3b1830"))
            painter.drawRoundedRect(rect, 4, 4)
        elif self.underMouse():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#25272d"))
            painter.drawRoundedRect(rect, 4, 4)
        painter.drawPixmap(QPointF(10, (self.height() - 26) / 2), self._glyph)
        painter.setPen(QColor("#ffffff" if self.current else "#dcdde1"))
        painter.drawText(rect.adjusted(46, 0, -30, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.name)
        painter.setPen(QColor("#6e717a"))
        painter.drawText(rect.adjusted(0, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, self.key)
        painter.end()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.preset_id)
            event.accept()


class BrushPickerPopup(QFrame):
    chosen = Signal(str)

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("BrushPickerPopup")
        self.setStyleSheet("#BrushPickerPopup { background: #141518; border: 1px solid #454955; border-radius: 4px; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)
        self.options: dict[str, BrushOption] = {}
        for index, preset in enumerate(BRUSH_PRESETS, 1):
            option = BrushOption(preset, str(index))
            option.clicked.connect(self._chosen)
            layout.addWidget(option)
            self.options[preset.preset_id] = option

    def show_below(self, anchor: QWidget, current_id: str) -> None:
        for preset_id, option in self.options.items():
            option.current = preset_id == current_id
            option.update()
        self.setFixedWidth(anchor.width())
        self.adjustSize()
        self.move(anchor.mapToGlobal(QPoint(0, anchor.height() + 4)))
        self.show()

    def _chosen(self, preset_id: str) -> None:
        self.hide()
        self.chosen.emit(preset_id)


class BrushPicker(QWidget):
    """Current brush as one wide bar, click it to list every brush."""

    preset_selected = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("BrushPicker")
        self.setFixedHeight(46)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preset_id = ""
        self.name = ""
        self.eraser = False
        self._glyph = QPixmap()
        self._glyphs = {preset.preset_id: _brush_glyph(preset.engine, 28) for preset in BRUSH_PRESETS}
        self.popup = BrushPickerPopup()
        self.popup.chosen.connect(self.select)

    def set_brush(self, brush: BrushSettings) -> None:
        self.preset_id = brush.preset_id
        self.name = brush.name
        self.eraser = brush.eraser
        self._glyph = self._glyphs.get(brush.preset_id, QPixmap())
        self.update()

    def select(self, preset_id: str) -> None:
        self.preset_selected.emit(preset_id)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        open_or_hover = self.underMouse() or self.popup.isVisible()
        painter.setPen(QPen(QColor("#4b4f5a" if open_or_hover else "#33363e"), 1))
        painter.setBrush(QColor("#1f2126" if open_or_hover else "#1a1b20"))
        painter.drawRoundedRect(rect, 4, 4)
        if not self._glyph.isNull():
            painter.drawPixmap(QPointF(10, (self.height() - 28) / 2), self._glyph)
        font = painter.font()
        font.setPointSizeF(font.pointSizeF() * 1.12)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor("#f2f2f4"))
        name_rect = rect.adjusted(48, 0, -30, 0)
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.name)
        if self.eraser:
            name_width = painter.fontMetrics().horizontalAdvance(self.name)
            painter.setFont(self.font())
            tag_width = painter.fontMetrics().horizontalAdvance("Eraser") + 12
            tag = QRectF(name_rect.left() + name_width + 10, rect.center().y() - 9, tag_width, 18)
            painter.setPen(QPen(QColor("#e3008c"), 1))
            painter.setBrush(QColor("#3b1830"))
            painter.drawRoundedRect(tag, 9, 9)
            painter.setPen(QColor("#ffd6ee"))
            painter.drawText(tag, Qt.AlignmentFlag.AlignCenter, "Eraser")
        chevron_x = rect.right() - 16
        chevron_y = rect.center().y()
        painter.setPen(QPen(QColor("#a9abb2"), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(chevron_x - 4, chevron_y - 2), QPointF(chevron_x, chevron_y + 2))
        painter.drawLine(QPointF(chevron_x, chevron_y + 2), QPointF(chevron_x + 4, chevron_y - 2))
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if self.popup.isVisible():
            self.popup.hide()
        else:
            self.popup.show_below(self, self.preset_id)
        self.update()
        event.accept()


class BrushPanel(QWidget):
    recent_colors_changed = Signal(list)

    def __init__(self, document: PaintDocument) -> None:
        super().__init__()
        self.document = document
        self.setObjectName("BrushPanel")
        self.setFixedWidth(270)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # No scrollbar so the color square keeps its width, the wheel still scrolls.
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(11, 10, 11, 12)
        layout.setSpacing(6)
        self.recent_colors = RecentColors()
        self.color_selector = ColorSelector(document.brush_color, accessory=self.recent_colors)
        layout.addWidget(self.color_selector)
        layout.addSpacing(8)
        self.picker = BrushPicker()
        layout.addWidget(self.picker)
        layout.addSpacing(4)

        tip_options = tuple(
            (shape, shape.title(), _tip_glyph(shape)) for shape in ("circle", "square", "diamond", "triangle", "texture")
        )
        self.brush_tip = ChoiceRow("Tip", tip_options)
        self.brush_mode = ChoiceRow("Mode", (("fill", "Fill", None), ("border", "Border", None)))
        self.shape_primitive = ChoiceRow(
            "Shape",
            (("rect", "Rectangle", _tip_glyph("rect")), ("ellipse", "Ellipse", _tip_glyph("ellipse"))),
        )
        self.shape_mode = ChoiceRow("Mode", (("fill", "Fill", None), ("border", "Border", None)))
        self.fill_reference = ChoiceRow("Sample", (("current", "Layer", None), ("visible", "All Visible", None)))
        stamp_options = (("custom", "Custom", _stamp_glyph(document.stamp_tip_image)),) + tuple(
            (shape, shape.title(), _tip_glyph(shape)) for shape in ("circle", "square", "diamond", "triangle", "star")
        )
        self.stamp_tip = ChoiceRow("Tip", stamp_options)
        self.stamp_pad = StampPad(document.stamp_tip_image)
        self.stamp_clear = QToolButton()
        self.stamp_clear.setObjectName("ChipButton")
        self.stamp_clear.setText("Clear")
        self.stamp_clear.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.stamp_clear.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.stamp_clear.setFixedHeight(26)
        self.stamp_custom = QWidget()
        stamp_custom_layout = QHBoxLayout(self.stamp_custom)
        stamp_custom_layout.setContentsMargins(CHOICE_LABEL_WIDTH + 6, 0, 0, 0)
        stamp_custom_layout.setSpacing(6)
        stamp_custom_layout.addWidget(self.stamp_pad, 0, Qt.AlignmentFlag.AlignTop)
        stamp_side = QVBoxLayout()
        stamp_side.setContentsMargins(0, 0, 0, 0)
        stamp_side.addWidget(self.stamp_clear)
        stamp_side.addStretch(1)
        stamp_custom_layout.addLayout(stamp_side, 1)

        settings = document.brush_settings
        self.size = BarSlider("Size", 1, 5000, round(document.brush_size), " px", curve=3.0)
        self.opacity = BarSlider("Opacity", 0, 100, round(settings.opacity * 100), "%")
        self.flow = BarSlider("Flow", 1, 100, round(settings.flow * 100), "%")
        self.spacing = BarSlider("Spacing", 2, 200, round(settings.spacing * 100), "%")
        self.hardness = BarSlider("Hardness", 0, 100, round(settings.hardness * 100), "%")
        self.texture = BarSlider("Texture", 0, 100, round(settings.texture_strength * 100), "%")
        self.border_width = BarSlider("Border", 1, 240, settings.border_width, " px", curve=2.0)
        self.corner_radius = BarSlider("Corners", 0, 128, settings.corner_radius, " px")
        self.blur_radius = BarSlider("Blur Radius", 1, 120, settings.blur_radius, " px")
        self.blur_strength = BarSlider("Strength", 1, 100, round(settings.blur_strength * 100), "%")
        self.fill_threshold = BarSlider("Threshold", 0, 255, settings.fill_threshold)
        self.scatter = BarSlider("Scatter", 0, 100, round(settings.scatter * 100), "%")
        self.size_variation = BarSlider("Size Jitter", 0, 100, round(settings.size_variation * 100), "%")
        self.size_x_variation = BarSlider("Width Jitter", 0, 100, round(settings.size_x_variation * 100), "%")
        self.size_y_variation = BarSlider("Height Jitter", 0, 100, round(settings.size_y_variation * 100), "%")
        self.rotation_variation = BarSlider("Rotation Jitter", 0, 180, round(settings.rotation_variation), "°")
        self.hue_variation = BarSlider("Hue Jitter", 0, 180, round(settings.hue_variation), "°")
        self.saturation_variation = BarSlider(
            "Saturation Jitter", 0, 100, round(settings.saturation_variation * 100), "%"
        )
        self.value_variation = BarSlider("Value Jitter", 0, 100, round(settings.value_variation * 100), "%")
        self.alpha_variation = BarSlider("Alpha Jitter", 0, 100, round(settings.alpha_variation * 100), "%")

        self.pressure_size = _chip("Pressure Size")
        self.pressure_opacity = _chip("Pressure Opacity")
        self.pressure_widget = _row(self.pressure_size, self.pressure_opacity)
        self.tilt_stretch = _chip("Tilt Stretch")
        self.flip_x = _chip("Flip X")
        self.flip_y = _chip("Flip Y")
        self.follow_rotation = _chip("Follow Stroke")
        self.stamp_flags = _row(self.flip_x, self.flip_y, self.follow_rotation)

        self.settings_controls = (
            self.brush_tip,
            self.brush_mode,
            self.shape_primitive,
            self.shape_mode,
            self.fill_reference,
            self.stamp_tip,
            self.stamp_custom,
            self.size,
            self.opacity,
            self.flow,
            self.spacing,
            self.hardness,
            self.texture,
            self.border_width,
            self.corner_radius,
            self.blur_radius,
            self.blur_strength,
            self.fill_threshold,
            self.scatter,
            self.size_variation,
            self.size_x_variation,
            self.size_y_variation,
            self.rotation_variation,
            self.hue_variation,
            self.saturation_variation,
            self.value_variation,
            self.alpha_variation,
            self.pressure_widget,
            self.tilt_stretch,
            self.stamp_flags,
        )
        for control in self.settings_controls:
            layout.addWidget(control)
        layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        self.color_selector.color_changed.connect(lambda color: document.set_brush(color=color))
        self.recent_colors.picked.connect(self._recent_color_picked)
        self.picker.preset_selected.connect(document.select_brush_preset)
        self.size.value_changed.connect(lambda value: document.set_brush(size=float(value)))
        self.opacity.value_changed.connect(lambda value: document.set_brush(opacity=value / 100.0))
        self.flow.value_changed.connect(lambda value: document.set_brush(flow=value / 100.0))
        self.spacing.value_changed.connect(lambda value: document.set_brush(spacing=value / 100.0))
        self.hardness.value_changed.connect(lambda value: document.set_brush(hardness=value / 100.0))
        self.texture.value_changed.connect(lambda value: document.set_brush(texture_strength=value / 100.0))
        self.border_width.value_changed.connect(lambda value: document.set_brush(border_width=value))
        self.corner_radius.value_changed.connect(lambda value: document.set_brush(corner_radius=value))
        self.blur_radius.value_changed.connect(lambda value: document.set_brush(blur_radius=value))
        self.blur_strength.value_changed.connect(lambda value: document.set_brush(blur_strength=value / 100.0))
        self.fill_threshold.value_changed.connect(lambda value: document.set_brush(fill_threshold=value))
        self.scatter.value_changed.connect(lambda value: document.set_brush(scatter=value / 100.0))
        self.size_variation.value_changed.connect(lambda value: document.set_brush(size_variation=value / 100.0))
        self.size_x_variation.value_changed.connect(lambda value: document.set_brush(size_x_variation=value / 100.0))
        self.size_y_variation.value_changed.connect(lambda value: document.set_brush(size_y_variation=value / 100.0))
        self.rotation_variation.value_changed.connect(lambda value: document.set_brush(rotation_variation=float(value)))
        self.hue_variation.value_changed.connect(lambda value: document.set_brush(hue_variation=float(value)))
        self.saturation_variation.value_changed.connect(
            lambda value: document.set_brush(saturation_variation=value / 100.0)
        )
        self.value_variation.value_changed.connect(lambda value: document.set_brush(value_variation=value / 100.0))
        self.alpha_variation.value_changed.connect(lambda value: document.set_brush(alpha_variation=value / 100.0))
        self.brush_tip.changed.connect(lambda value: document.set_brush(tip_shape=value))
        self.brush_mode.changed.connect(lambda value: document.set_brush(fill_mode=value))
        self.shape_primitive.changed.connect(lambda value: document.set_brush(primitive=value))
        self.shape_mode.changed.connect(lambda value: document.set_brush(fill_mode=value))
        self.fill_reference.changed.connect(lambda value: document.set_brush(fill_reference=value))
        self.stamp_tip.changed.connect(lambda value: document.set_brush(tip_shape=value))
        self.stamp_pad.changed.connect(self._stamp_pad_changed)
        self.stamp_clear.clicked.connect(self._clear_stamp)
        self.pressure_size.toggled.connect(lambda value: document.set_brush(pressure_size=value))
        self.pressure_opacity.toggled.connect(lambda value: document.set_brush(pressure_opacity=value))
        self.tilt_stretch.toggled.connect(lambda value: document.set_brush(tilt_stretch=value))
        self.flip_x.toggled.connect(lambda value: document.set_brush(flip_x=value))
        self.flip_y.toggled.connect(lambda value: document.set_brush(flip_y=value))
        self.follow_rotation.toggled.connect(lambda value: document.set_brush(follow_rotation=value))
        document.brush_changed.connect(self.sync_brush)
        document.stamp_tip_changed.connect(self._stamp_tip_changed)
        self.sync_brush(document.brush_settings, document.brush_color)

    def recent_color_names(self) -> list[str]:
        return [color.name() for color in self.recent_colors.colors]

    def set_recent_colors(self, names) -> None:
        colors = [QColor(str(name)) for name in names or ()]
        self.recent_colors.set_colors([color for color in colors if color.isValid()])

    def add_recent_color(self, color: QColor) -> None:
        name = QColor(color).name()
        names = [name] + [existing for existing in self.recent_color_names() if existing != name]
        if names == self.recent_color_names():
            return
        self.set_recent_colors(names[: RecentColors.SLOTS])
        self.recent_colors_changed.emit(self.recent_color_names())

    def _recent_color_picked(self, color: QColor) -> None:
        self.document.set_brush(color=color)

    def _stamp_pad_changed(self, image: QImage) -> None:
        self.document.set_stamp_tip_image(image)
        self.document.set_brush(tip_shape="custom")

    def _clear_stamp(self) -> None:
        blank = QImage(self.document.stamp_tip_image.size(), QImage.Format.Format_ARGB32_Premultiplied)
        blank.fill(Qt.GlobalColor.transparent)
        self.document.set_stamp_tip_image(blank)
        self.document.set_brush(tip_shape="custom")

    def _stamp_tip_changed(self, image: QImage) -> None:
        self.stamp_pad.set_image(image)
        self.stamp_tip.button("custom").setIcon(_stamp_glyph(image))

    def sync_brush(self, brush: BrushSettings, color: QColor) -> None:
        self.picker.set_brush(brush)
        for control, value in (
            (self.size, brush.size),
            (self.opacity, brush.opacity * 100),
            (self.flow, brush.flow * 100),
            (self.spacing, brush.spacing * 100),
            (self.hardness, brush.hardness * 100),
            (self.texture, brush.texture_strength * 100),
            (self.border_width, brush.border_width),
            (self.corner_radius, brush.corner_radius),
            (self.blur_radius, brush.blur_radius),
            (self.blur_strength, brush.blur_strength * 100),
            (self.fill_threshold, brush.fill_threshold),
            (self.scatter, brush.scatter * 100),
            (self.size_variation, brush.size_variation * 100),
            (self.size_x_variation, brush.size_x_variation * 100),
            (self.size_y_variation, brush.size_y_variation * 100),
            (self.rotation_variation, brush.rotation_variation),
            (self.hue_variation, brush.hue_variation),
            (self.saturation_variation, brush.saturation_variation * 100),
            (self.value_variation, brush.value_variation * 100),
            (self.alpha_variation, brush.alpha_variation * 100),
        ):
            control.sync_value(value)
        for button, checked in (
            (self.pressure_size, brush.pressure_size),
            (self.pressure_opacity, brush.pressure_opacity),
            (self.tilt_stretch, brush.tilt_stretch),
            (self.flip_x, brush.flip_x),
            (self.flip_y, brush.flip_y),
            (self.follow_rotation, brush.follow_rotation),
        ):
            with QSignalBlocker(button):
                button.setChecked(checked)
        self.brush_tip.set_option_visible("texture", brush.engine == "paint")
        self.brush_tip.set_value(brush.tip_shape)
        self.brush_mode.set_value(brush.fill_mode)
        self.shape_primitive.set_value("ellipse" if brush.primitive == "ellipse" else "rect")
        self.shape_mode.set_value(brush.fill_mode)
        self.fill_reference.set_value(brush.fill_reference)
        self.stamp_tip.set_value(brush.tip_shape)
        visible = {
            "air": {
                self.brush_mode,
                self.size,
                self.opacity,
                self.flow,
                self.spacing,
                self.hardness,
                self.pressure_widget,
            },
            "ink": {
                self.brush_tip,
                self.brush_mode,
                self.size,
                self.opacity,
                self.spacing,
                self.hardness,
                self.pressure_widget,
            },
            "paint": {
                self.brush_tip,
                self.brush_mode,
                self.size,
                self.opacity,
                self.flow,
                self.spacing,
                self.hardness,
                self.texture,
                self.pressure_widget,
                self.tilt_stretch,
            },
            "shape": {self.shape_primitive, self.shape_mode, self.opacity, self.corner_radius},
            "blur": {self.size, self.spacing, self.blur_radius, self.blur_strength, self.pressure_widget},
            "fill": {self.fill_reference, self.opacity, self.fill_threshold},
            "stamp": {
                self.stamp_tip,
                self.size,
                self.opacity,
                self.spacing,
                self.scatter,
                self.size_variation,
                self.size_x_variation,
                self.size_y_variation,
                self.rotation_variation,
                self.hue_variation,
                self.saturation_variation,
                self.value_variation,
                self.alpha_variation,
                self.pressure_widget,
                self.stamp_flags,
            },
        }.get(brush.engine, set())
        if brush.engine == "stamp" and brush.tip_shape == "custom":
            visible.add(self.stamp_custom)
        if brush.engine in {"shape", "air", "ink", "paint"} and brush.fill_mode == "border":
            visible.add(self.border_width)
        for control in self.settings_controls:
            control.setVisible(control in visible)
        self.color_selector.set_color(color)


class RecentProjectsPopup(QFrame):
    """Scrollable list of autosaved projects under the title menu."""

    chosen = Signal(str)

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("RecentProjectsPopup")
        self.setStyleSheet(
            """
            #RecentProjectsPopup { background: #141518; border: 1px solid #454955; border-radius: 4px; }
            #RecentProjectsList { background: transparent; border: 0; outline: 0; color: #dcdde1; }
            #RecentProjectsList::item { padding: 5px; border: 1px solid transparent; border-radius: 4px; }
            #RecentProjectsList::item:hover { background: #25272d; }
            #RecentProjectsList::item:selected { background: #3b1830; border-color: #e3008c; color: white; }
            #RecentProjectsEmpty { color: #8a8d95; padding: 18px; }
            QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
            QScrollBar::handle:vertical { background: #3a3d45; border-radius: 3px; min-height: 24px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.list = QListWidget()
        self.list.setObjectName("RecentProjectsList")
        self.list.setIconSize(QSize(RECENT_THUMB_WIDTH, RECENT_THUMB_HEIGHT))
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.itemClicked.connect(self._clicked)
        layout.addWidget(self.list)
        self.empty = QLabel("No saved projects yet")
        self.empty.setObjectName("RecentProjectsEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty)
        self.setFixedWidth(360)

    def show_projects(self, projects, anchor: QWidget, current_id: str | None = None) -> None:
        self.list.clear()
        for project in projects:
            item = QListWidgetItem(self._thumbnail(project.thumbnail_path), self._label(project))
            item.setData(Qt.ItemDataRole.UserRole, project.project_id)
            self.list.addItem(item)
            if project.project_id == current_id:
                item.setSelected(True)
        has_projects = self.list.count() > 0
        self.list.setVisible(has_projects)
        self.empty.setVisible(not has_projects)
        row_height = RECENT_THUMB_HEIGHT + 14
        visible_rows = min(max(self.list.count(), 1), 7)
        self.setFixedHeight(visible_rows * row_height + 12 if has_projects else 64)
        position = anchor.mapToGlobal(QPoint(0, anchor.height() + 2))
        screen = anchor.screen()
        if screen is not None:
            area = screen.availableGeometry()
            position.setX(max(area.left(), min(position.x(), area.right() - self.width())))
            position.setY(max(area.top(), min(position.y(), area.bottom() - self.height())))
        self.move(position)
        self.show()
        self.list.scrollToTop()

    def _label(self, project) -> str:
        stamp = datetime.fromtimestamp(project.modified)
        today = datetime.now().date()
        if stamp.date() == today:
            when = f"Today  {stamp.strftime('%I:%M %p').lstrip('0')}"
        else:
            when = f"{stamp.strftime('%b')} {stamp.day}  {stamp.strftime('%I:%M %p').lstrip('0')}"
        return f"{when}\n{project.width} × {project.height}"

    def _thumbnail(self, path) -> QIcon:
        pixmap = QPixmap(round(RECENT_THUMB_WIDTH * GLYPH_SCALE), round(RECENT_THUMB_HEIGHT * GLYPH_SCALE))
        pixmap.setDevicePixelRatio(GLYPH_SCALE)
        pixmap.fill(QColor("#1c1e23"))
        image = QImage(str(path)) if path else QImage()
        if not image.isNull():
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            bounds = QRectF(0, 0, RECENT_THUMB_WIDTH, RECENT_THUMB_HEIGHT)
            scale = min(bounds.width() / image.width(), bounds.height() / image.height())
            target = QRectF(0, 0, image.width() * scale, image.height() * scale)
            target.moveCenter(bounds.center())
            painter.drawImage(target, image)
            painter.end()
        return QIcon(pixmap)

    def _clicked(self, item: QListWidgetItem) -> None:
        self.hide()
        self.chosen.emit(str(item.data(Qt.ItemDataRole.UserRole)))


class CanvasOverview(QWidget):
    def __init__(self, document: PaintDocument, canvas: CanvasWidget) -> None:
        super().__init__()
        self.document = document
        self.canvas = canvas
        self._target = QRectF()
        self.setObjectName("CanvasOverview")
        self.setFixedSize(PANEL_SQUARE_SIZE, PANEL_SQUARE_SIZE)
        document.stroke_finished.connect(lambda _revision: self.update())
        document.layers_changed.connect(self.update)
        canvas.view_changed.connect(self.update)
        canvas.background_changed.connect(lambda _enabled: self.update())

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return width

    def resizeEvent(self, event) -> None:
        if self.height() != self.width():
            self.setFixedHeight(self.width())
        super().resizeEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#18191d"))
        available = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        image = self.document.canvas_image()
        size = image.size()
        scale = min(available.width() / max(1, size.width()), available.height() / max(1, size.height()))
        self._target = QRectF(0, 0, size.width() * scale, size.height() * scale)
        self._target.moveCenter(available.center())
        if self.canvas.checkerboard_background:
            _checker(painter, self._target, 6, dark=True)
        else:
            painter.fillRect(self._target, QColor("#202124"))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(self._target, image)
        painter.setPen(QPen(QColor("#565a66"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self._target)
        visible = self.canvas.visible_document_rect()
        if not visible.isEmpty():
            viewport = QRectF(
                self._target.left() + visible.left() / size.width() * self._target.width(),
                self._target.top() + visible.top() / size.height() * self._target.height(),
                visible.width() / size.width() * self._target.width(),
                visible.height() / size.height() * self._target.height(),
            )
            painter.setPen(QPen(QColor("#ff4db8"), 1.5))
            painter.setBrush(QColor(227, 0, 140, 28))
            painter.drawRect(viewport)
        painter.setPen(QPen(QColor("#2e3037"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._navigate(event.position())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._navigate(event.position())

    def wheelEvent(self, event) -> None:
        self.canvas.zoom_by(1.15 if event.angleDelta().y() > 0 else 1.0 / 1.15)
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.canvas.fit_document()
            event.accept()

    def _navigate(self, position: QPointF) -> None:
        if self._target.isEmpty():
            return
        point = QPointF(
            (position.x() - self._target.left()) / self._target.width() * self.document.width,
            (position.y() - self._target.top()) / self._target.height() * self.document.height,
        )
        point.setX(max(0.0, min(float(self.document.width), point.x())))
        point.setY(max(0.0, min(float(self.document.height), point.y())))
        self.canvas.center_on_document(point)


class LayerRow(QWidget):
    selected = Signal(str, object)
    context_requested = Signal(str, object)
    drag_requested = Signal(str)
    rename_requested = Signal(str)

    def __init__(self, document: PaintDocument, layer: LayerNode) -> None:
        super().__init__()
        self.document = document
        self.layer_id = layer.layer_id
        self._drag_start = QPoint()
        self.setObjectName("LayerRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(4)
        self.visible = QToolButton()
        self.visible.setObjectName("LayerIconButton")
        self.visible.setIcon(_tool_icon("visible"))
        self.visible.setToolTip("Visibility")
        self.visible.setCheckable(True)
        self.visible.setChecked(layer.visible)
        layout.addWidget(self.visible)
        thumbnail = QLabel()
        thumbnail.setObjectName("LayerThumbnail")
        thumbnail.setFixedSize(34, 34)
        if layer.is_group:
            thumbnail.setPixmap(_tool_icon("group").pixmap(24, 24))
            thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        elif layer.image is not None:
            thumbnail.setPixmap(self._thumbnail(document.layer_canvas_image(layer)))
        layout.addWidget(thumbnail)
        self.name = QLabel(layer.name)
        self.name.setObjectName("LayerName")
        self.name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.name, 1)
        self.alpha = QToolButton()
        self.alpha.setObjectName("LayerIconButton")
        self.alpha.setIcon(_tool_icon("alpha"))
        self.alpha.setToolTip("Lock alpha")
        self.alpha.setCheckable(True)
        self.alpha.setChecked(layer.alpha_locked)
        self.alpha.setEnabled(not layer.is_group)
        layout.addWidget(self.alpha)
        self.clip = QToolButton()
        self.clip.setObjectName("LayerIconButton")
        self.clip.setIcon(_tool_icon("clip"))
        self.clip.setToolTip("Clipping mask")
        self.clip.setCheckable(True)
        self.clip.setChecked(layer.clipping)
        layout.addWidget(self.clip)
        self.visible.toggled.connect(lambda value: document.set_layer_property(layer.layer_id, "visible", value))
        self.alpha.toggled.connect(lambda value: document.set_layer_property(layer.layer_id, "alpha_locked", value))
        self.clip.toggled.connect(lambda value: document.set_layer_property(layer.layer_id, "clipping", value))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.selected.emit(self.layer_id, event.modifiers())
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            distance = (event.position().toPoint() - self._drag_start).manhattanLength()
            if distance >= QApplication.startDragDistance():
                self.drag_requested.emit(self.layer_id)
                self._drag_start = QPoint()
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.rename_requested.emit(self.layer_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        self.selected.emit(self.layer_id, Qt.KeyboardModifier.NoModifier)
        self.context_requested.emit(self.layer_id, event.globalPos())
        event.accept()

    def _thumbnail(self, image: QImage) -> QPixmap:
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor("#d7d7d7"))
        painter = QPainter(pixmap)
        _checker(painter, QRectF(0, 0, 32, 32), 5)
        scaled = image.scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        painter.drawImage((32 - scaled.width()) // 2, (32 - scaled.height()) // 2, scaled)
        painter.end()
        return pixmap


class LayerTree(QTreeWidget):
    layer_drop_requested = Signal(str, object, str)

    def dropEvent(self, event) -> None:
        source = self.currentItem()
        if source is None:
            event.ignore()
            return
        source_id = str(source.data(0, Qt.ItemDataRole.UserRole))
        target = self.itemAt(event.position().toPoint())
        target_id: str | None = None
        placement = "root_top"
        if target is not None:
            target_id = str(target.data(0, Qt.ItemDataRole.UserRole))
            indicator = self.dropIndicatorPosition()
            if indicator == QAbstractItemView.DropIndicatorPosition.AboveItem:
                placement = "above"
            elif indicator == QAbstractItemView.DropIndicatorPosition.BelowItem:
                placement = "below"
            elif bool(target.data(0, Qt.ItemDataRole.UserRole + 1)):
                placement = "inside"
            else:
                placement = "above" if event.position().y() < self.visualItemRect(target).center().y() else "below"
        self.layer_drop_requested.emit(source_id, target_id, placement)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()


class LayersPanel(QWidget):
    def __init__(self, document: PaintDocument, canvas: CanvasWidget) -> None:
        super().__init__()
        self.document = document
        self._items: dict[str, QTreeWidgetItem] = {}
        self.setObjectName("LayersPanel")
        self.setFixedWidth(254)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 10, 11, 12)
        layout.setSpacing(8)
        self.overview = CanvasOverview(document, canvas)
        layout.addWidget(self.overview, 0, Qt.AlignmentFlag.AlignHCenter)
        properties = QGridLayout()
        properties.setContentsMargins(0, 0, 0, 0)
        properties.setHorizontalSpacing(6)
        properties.addWidget(QLabel("Blend"), 0, 0)
        self.blend = QComboBox()
        self.blend.addItems(BLEND_MODES)
        properties.addWidget(self.blend, 0, 1)
        properties.setColumnStretch(1, 1)
        self.opacity = BarSlider("Opacity", 0, 100, 100, "%")
        properties.addWidget(self.opacity, 1, 0, 1, 2)
        layout.addLayout(properties)
        self.tree = LayerTree()
        self.tree.setObjectName("LayerTree")
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(16)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setRootIsDecorated(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.tree.setDropIndicatorShown(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        layout.addWidget(self.tree, 1)
        self.tree.currentItemChanged.connect(self._selection_changed)
        self.tree.customContextMenuRequested.connect(self._tree_context_menu)
        self.tree.layer_drop_requested.connect(self.document.relocate_layer)
        self.blend.currentTextChanged.connect(self._blend_changed)
        self.opacity.committed.connect(self._commit_opacity)
        document.layers_changed.connect(self.rebuild)
        document.active_layer_changed.connect(self.select_layer)
        document.stroke_finished.connect(lambda _revision: self.rebuild())
        self.rebuild()

    def rebuild(self) -> None:
        selected_ids = set(self.selected_layer_ids()) or {self.document.active_layer_id}
        with QSignalBlocker(self.tree):
            self.tree.clear()
            self._items.clear()
            self._append_nodes(self.document.roots, self.tree.invisibleRootItem())
            self.tree.expandAll()
            for layer_id in selected_ids:
                item = self._items.get(layer_id)
                if item is not None:
                    item.setSelected(True)
        self.select_layer(self.document.active_layer_id)

    def _append_nodes(self, nodes: list[LayerNode], parent: QTreeWidgetItem) -> None:
        for layer in reversed(nodes):
            item = QTreeWidgetItem(parent)
            item.setData(0, Qt.ItemDataRole.UserRole, layer.layer_id)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, layer.is_group)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled)
            item.setSizeHint(0, QSize(0, 42))
            row = LayerRow(self.document, layer)
            row.selected.connect(self._row_selected)
            row.context_requested.connect(self._show_layer_menu)
            row.drag_requested.connect(self._start_layer_drag)
            row.rename_requested.connect(self._rename_layer)
            self.tree.setItemWidget(item, 0, row)
            self._items[layer.layer_id] = item
            if layer.children:
                self._append_nodes(layer.children, item)

    def current_layer_id(self) -> str | None:
        item = self.tree.currentItem()
        return str(item.data(0, Qt.ItemDataRole.UserRole)) if item is not None else None

    def selected_layer_ids(self) -> list[str]:
        return [str(item.data(0, Qt.ItemDataRole.UserRole)) for item in self.tree.selectedItems()]

    def select_layer(self, layer_id: str) -> None:
        item = self._items.get(layer_id)
        if item is None:
            return
        with QSignalBlocker(self.tree):
            if not item.isSelected():
                self.tree.clearSelection()
                item.setSelected(True)
            self.tree.setCurrentItem(item, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.document.set_active_layer(layer_id)
        self._sync_properties(layer_id)

    def _row_selected(self, layer_id: str, modifiers: Qt.KeyboardModifier) -> None:
        item = self._items.get(layer_id)
        if item is None:
            return
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            item.setSelected(not item.isSelected())
            if not item.isSelected():
                remaining = self.tree.selectedItems()
                if remaining:
                    self.select_layer(str(remaining[-1].data(0, Qt.ItemDataRole.UserRole)))
                else:
                    item.setSelected(True)
                return
        elif modifiers & Qt.KeyboardModifier.ShiftModifier and self.tree.currentItem() is not None:
            anchor = self.tree.indexOfTopLevelItem(self.tree.currentItem())
            target = self.tree.indexOfTopLevelItem(item)
            if anchor >= 0 and target >= 0:
                for index in range(min(anchor, target), max(anchor, target) + 1):
                    self.tree.topLevelItem(index).setSelected(True)
            else:
                item.setSelected(True)
        else:
            self.tree.clearSelection()
            item.setSelected(True)
        self.select_layer(layer_id)

    def _selection_changed(self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        if current is None:
            return
        layer_id = str(current.data(0, Qt.ItemDataRole.UserRole))
        self.document.set_active_layer(layer_id)
        self._sync_properties(layer_id)

    def _sync_properties(self, layer_id: str) -> None:
        layer = self.document.find_layer(layer_id)
        if layer is None:
            return
        with QSignalBlocker(self.blend):
            self.blend.setCurrentText(layer.blend_mode)
        self.opacity.sync_value(round(layer.opacity * 100))

    def _blend_changed(self, value: str) -> None:
        layer_id = self.current_layer_id()
        if layer_id:
            self.document.set_layer_property(layer_id, "blend_mode", value)

    def _commit_opacity(self, value: int) -> None:
        layer_id = self.current_layer_id()
        if layer_id:
            self.document.set_layer_property(layer_id, "opacity", value / 100.0)

    def _start_layer_drag(self, layer_id: str) -> None:
        self.select_layer(layer_id)
        self.tree.startDrag(Qt.DropAction.MoveAction)

    def _tree_context_menu(self, position: QPoint) -> None:
        item = self.tree.itemAt(position)
        layer_id = str(item.data(0, Qt.ItemDataRole.UserRole)) if item is not None else ""
        if layer_id:
            self.select_layer(layer_id)
        self._show_layer_menu(layer_id, self.tree.viewport().mapToGlobal(position))

    def _show_layer_menu(self, layer_id: str, global_position: QPoint) -> None:
        layer = self.document.find_layer(layer_id)
        menu = QMenu(self)
        new_layer = menu.addAction("New Layer")
        new_layer.triggered.connect(
            lambda: self.document.add_paint_layer(parent_id=layer_id if layer and layer.is_group else None)
        )
        new_group = menu.addAction("New Group")
        new_group.triggered.connect(self.document.add_group)
        if layer is not None:
            menu.addSeparator()
            duplicate = menu.addAction("Duplicate")
            duplicate.triggered.connect(lambda: self.document.duplicate_layer(layer_id))
            group = menu.addAction("Group")
            group.triggered.connect(lambda: self.document.group_layer(layer_id))
            rename = menu.addAction("Rename")
            rename.triggered.connect(lambda: self._rename_layer(layer_id))
            menu.addSeparator()
            delete = menu.addAction("Delete")
            total_paint = sum(1 for node in self.document.iter_layers() if not node.is_group)
            selected_paint = sum(1 for node in self.document.iter_layers([layer]) if not node.is_group)
            delete.setEnabled(total_paint - selected_paint >= 1)
            delete.triggered.connect(lambda: self.document.remove_layer(layer_id))
        menu.exec(global_position)

    def _rename_layer(self, layer_id: str) -> None:
        layer = self.document.find_layer(layer_id)
        if layer is None:
            return
        name, accepted = QInputDialog.getText(self, "Rename Layer", "Name", text=layer.name)
        if accepted and name.strip():
            self.document.set_layer_property(layer_id, "name", name.strip())
