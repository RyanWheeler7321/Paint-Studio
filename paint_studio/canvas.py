from __future__ import annotations

import logging
import math
import time

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QBrush,
    QEnterEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QBitmap,
    QPolygonF,
    QRegion,
    QTabletEvent,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import QApplication, QWidget

from .core import CleanShape, PaintDocument, StrokeSample, clean_stroke
from .core.document import TransformSession


class CanvasWidget(QWidget):
    brush_size_changed = Signal(int)
    color_sampled = Signal(QColor)
    color_used = Signal(QColor)
    zoom_changed = Signal(int)
    view_changed = Signal()
    background_changed = Signal(bool)

    def __init__(self, document: PaintDocument, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.document = document
        self.zoom = 1.0
        self.pan = QPointF()
        self.mirror_horizontal = False
        self.mirror_vertical = False
        self._interaction = ""
        self._tablet_active = False
        self._space_down = False
        self._shift_down = False
        self._space_used = False
        self._cursor_override = False
        self._pointer_position = QPointF()
        self._pointer_inside = False
        self._drag_start = QPointF()
        self._drag_base_pan = QPointF()
        self._drag_start_zoom = 1.0
        self._drag_anchor_document = QPointF()
        self._drag_start_size = self.document.brush_size
        self._crop_start_rect = QRect()
        self._crop_edge = ""
        self._crop_moved = False
        self.checkerboard_background = False
        self.tool = "brush"
        self._selection_points: list[QPointF] = []
        self._selection_action = "replace"
        self.transform_session: TransformSession | None = None
        self._transform_matrix = QTransform()
        self._transform_start_matrix = QTransform()
        self._transform_mode = ""
        self._transform_handle = ""
        self._transform_start_world = QPointF()
        self._transform_pivot = QPointF()
        self._selection_cache_key = 0
        self._selection_region = QRegion()
        self._shape_start = QPointF()
        self._shape_current = QPointF()
        self.smart_shape_enabled = True
        self.smart_shape_hold_ms = 700
        self._smart_shape_timer = QTimer(self)
        self._smart_shape_timer.setSingleShot(True)
        self._smart_shape_timer.setInterval(self.smart_shape_hold_ms)
        self._smart_shape_timer.timeout.connect(self._activate_smart_shape)
        self._smart_shape_redraw_timer = QTimer(self)
        self._smart_shape_redraw_timer.setSingleShot(True)
        self._smart_shape_redraw_timer.timeout.connect(self._flush_smart_shape_adjustment)
        self._smart_shape_flash_timer = QTimer(self)
        self._smart_shape_flash_timer.setSingleShot(True)
        self._smart_shape_flash_timer.setInterval(110)
        self._smart_shape_flash_timer.timeout.connect(self._finish_smart_shape_flash)
        self._smart_shape_flash_points: tuple[QPointF, ...] = ()
        self._smart_shape_samples: list[StrokeSample] = []
        self._smart_shape: CleanShape | None = None
        self._smart_shape_hold_anchor = QPointF()
        self._smart_shape_last_adjust = QPointF()
        self._smart_shape_last_adjust_timestamp = -1
        self._smart_shape_last_redraw_at = 0.0
        self._smart_shape_pending_position: QPointF | None = None
        self._smart_shape_pending_timestamp = -1
        self._smart_shape_redraw_ms: list[float] = []
        self._smart_shape_suppressed = False
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.document.changed.connect(self._document_changed)
        self.document.canvas_bounds_changed.connect(self._canvas_bounds_changed)
        self.document.selection_changed.connect(self._selection_changed)
        self.document.brush_changed.connect(lambda _brush, _color: self.update())
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def fit_document(self) -> None:
        margin = 64.0
        available_width = max(1.0, self.width() - margin * 2.0)
        available_height = max(1.0, self.height() - margin * 2.0)
        self.zoom = min(
            1.0,
            available_width / self.document.width,
            available_height / self.document.height,
        )
        self.pan = QPointF()
        self._emit_zoom()
        self.view_changed.emit()
        self.update()

    def document_rect(self) -> QRectF:
        width = self.document.width * self.zoom
        height = self.document.height * self.zoom
        center = QPointF(self.width() * 0.5, self.height() * 0.5) + self.pan
        return QRectF(center.x() - width * 0.5, center.y() - height * 0.5, width, height)

    def to_document(self, position: QPointF) -> QPointF:
        rect = self.document_rect()
        x = (position.x() - rect.left()) / self.zoom
        y = (position.y() - rect.top()) / self.zoom
        return QPointF(self.document.width - x if self.mirror_horizontal else x,
                       self.document.height - y if self.mirror_vertical else y)

    def _mirror_delta(self, delta: QPointF) -> QPointF:
        return QPointF(-delta.x() if self.mirror_horizontal else delta.x(),
                       -delta.y() if self.mirror_vertical else delta.y())

    def set_mirroring(self, *, horizontal: bool | None = None, vertical: bool | None = None) -> None:
        self._finish_active_brush_stroke()
        if horizontal is not None:
            self.mirror_horizontal = bool(horizontal)
        if vertical is not None:
            self.mirror_vertical = bool(vertical)
        self.view_changed.emit()
        self.update()
        logging.getLogger("paintstudio").info(
            "view mirror horizontal=%s vertical=%s", self.mirror_horizontal, self.mirror_vertical
        )

    def _world_screen_transform(self) -> QTransform:
        target = self.document_rect()
        canvas = self.document.canvas_rect
        sx = -self.zoom if self.mirror_horizontal else self.zoom
        sy = -self.zoom if self.mirror_vertical else self.zoom
        return QTransform(sx, 0, 0, sy,
                          (target.right() if self.mirror_horizontal else target.left()) - canvas.x() * sx,
                          (target.bottom() if self.mirror_vertical else target.top()) - canvas.y() * sy)

    def to_world(self, position: QPointF) -> QPointF:
        local = self.to_document(position)
        canvas = self.document.canvas_rect
        return local + QPointF(canvas.x(), canvas.y())

    def visible_document_rect(self) -> QRectF:
        visible = self.document_rect().intersected(QRectF(self.rect()))
        if visible.isEmpty():
            return QRectF()
        top_left = self.to_document(visible.topLeft())
        bottom_right = self.to_document(visible.bottomRight())
        return (
            QRectF(top_left, bottom_right)
            .normalized()
            .intersected(QRectF(0, 0, self.document.width, self.document.height))
        )

    def center_on_document(self, position: QPointF) -> None:
        document_center = QPointF(self.document.width * 0.5, self.document.height * 0.5)
        self.pan = -self._mirror_delta(position - document_center) * self.zoom
        self.view_changed.emit()
        self.update()

    def zoom_by(self, factor: float) -> None:
        center = QPointF(self.width() * 0.5, self.height() * 0.5)
        self._set_zoom_at(center, self.zoom * factor)

    def activate_brush_tool(self) -> None:
        self._finish_active_brush_stroke()
        if self.transform_session is not None:
            self.commit_transform()
        self.tool = "brush"
        self._selection_points.clear()
        self._update_system_cursor()
        self.update()

    def _note_color_used(self) -> None:
        brush = self.document.brush_settings
        if not brush.eraser and brush.engine != "blur":
            self.color_used.emit(QColor(self.document.brush_color))

    def release_native_cursor(self) -> None:
        self._set_cursor_override(False)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def refresh_native_cursor(self) -> None:
        self._update_system_cursor()

    def activate_freehand_selection(self) -> None:
        self._finish_active_brush_stroke()
        if self.transform_session is not None:
            self.commit_transform()
        self.tool = "selection"
        self._selection_points.clear()
        self._update_system_cursor()
        self.update()

    def begin_transform(self, layer_ids: list[str]) -> bool:
        self._finish_active_brush_stroke()
        if self.transform_session is not None:
            self.commit_transform()
        session = self.document.begin_transform(layer_ids)
        if session is None:
            return False
        self.document.prepare_transform_preview(session)
        self.transform_session = session
        self._transform_matrix = QTransform()
        self._transform_pivot = QPointF(session.bounds.center())
        self.tool = "transform"
        self._update_system_cursor()
        self.update()
        return True

    def commit_transform(self) -> bool:
        if self.transform_session is None:
            return False
        self.document.preview_transform(self.transform_session, self._transform_matrix)
        self.document.commit_transform(self.transform_session)
        self.transform_session = None
        self._transform_mode = ""
        self.update()
        return True

    def cancel_transform(self) -> bool:
        if self.transform_session is None:
            return False
        self.document.cancel_transform(self.transform_session)
        self.transform_session = None
        self._transform_mode = ""
        self.update()
        return True

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#17181b"))
        target = self.document_rect()
        shadow = target.adjusted(-2.0, -2.0, 2.0, 2.0)
        painter.fillRect(shadow, QColor("#08090a"))
        if self.checkerboard_background:
            self._draw_checkerboard(painter, target)
        else:
            painter.fillRect(target, QColor("#202124"))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.zoom < 1.0)
        base_image = (
            self.transform_session.preview_base
            if self.transform_session is not None and self.transform_session.preview_base is not None
            else self.document.image
        )
        painter.save()
        painter.setTransform(self._world_screen_transform())
        painter.drawImage(
            QRectF(self.document.canvas_rect),
            base_image,
            QRectF(self.document.world_to_image_rect(self.document.canvas_rect)),
        )
        painter.restore()
        self._draw_transform_preview(painter)
        self._draw_shape_preview(painter)
        self._draw_smart_shape_flash(painter)
        self._draw_selection(painter)
        self._draw_transform(painter)
        self._draw_brush_cursor(painter)
        painter.end()

    def _document_changed(self, rect) -> None:
        mapped = self._world_screen_transform().mapRect(QRectF(rect)).adjusted(-3, -3, 3, 3)
        self.update(mapped.toAlignedRect())

    def _canvas_bounds_changed(self, _rect: QRect) -> None:
        self.view_changed.emit()
        self.update()

    def _selection_changed(self) -> None:
        self._selection_cache_key = 0
        self._selection_region = QRegion()
        self.update()

    def set_checkerboard_background(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.checkerboard_background:
            return
        self.checkerboard_background = enabled
        self.background_changed.emit(enabled)
        self.update()

    def set_smart_shape_enabled(self, enabled: bool) -> None:
        self.smart_shape_enabled = bool(enabled)
        self._smart_shape_timer.stop()
        if not enabled and self._interaction == "smart_shape":
            self._restore_smart_shape_freehand()

    def _draw_checkerboard(self, painter: QPainter, rect: QRectF) -> None:
        cell = max(4.0, 12.0 * min(1.0, self.zoom))
        painter.fillRect(rect, QColor("#2a2b2f"))
        painter.save()
        painter.setClipRect(rect)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#3a3b40"))
        left = math.floor(rect.left() / cell) * cell
        top = math.floor(rect.top() / cell) * cell
        rows = math.ceil(rect.height() / cell) + 2
        columns = math.ceil(rect.width() / cell) + 2
        for row in range(rows):
            for column in range(columns):
                if (row + column) % 2 == 0:
                    painter.drawRect(QRectF(left + column * cell, top + row * cell, cell, cell))
        painter.restore()

    def event(self, event: QEvent) -> bool:
        if event.type() in (
            QEvent.Type.TabletPress,
            QEvent.Type.TabletMove,
            QEvent.Type.TabletRelease,
        ):
            self._handle_tablet(event)
            return True
        return super().event(event)

    def eventFilter(self, watched, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.ApplicationDeactivate, QEvent.Type.WindowDeactivate):
            self._set_cursor_override(False)
            self._finish_active_brush_stroke()
        if event.type() not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            return False
        if QApplication.activeWindow() is not self.window():
            return False
        key_event = event
        if not isinstance(key_event, QKeyEvent):
            return False
        if (
            event.type() == QEvent.Type.KeyPress
            and not key_event.isAutoRepeat()
            and key_event.key() == Qt.Key.Key_Escape
            and self._interaction == "smart_shape"
        ):
            self._restore_smart_shape_freehand()
            return True
        if event.type() == QEvent.Type.KeyPress and not key_event.isAutoRepeat() and self.transform_session is not None:
            if key_event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.commit_transform()
                return True
            if key_event.key() == Qt.Key.Key_Escape:
                self.cancel_transform()
                return True
        if key_event.key() not in (Qt.Key.Key_Space, Qt.Key.Key_Shift):
            return False
        if key_event.isAutoRepeat():
            return True
        down = event.type() == QEvent.Type.KeyPress
        if key_event.key() == Qt.Key.Key_Space:
            if down:
                self._space_used = False
            self._space_down = down
            if not down and not self._space_used and not self._interaction:
                self.fit_document()
        else:
            self._shift_down = down
        self._update_system_cursor()
        self.update()
        return key_event.key() == Qt.Key.Key_Space

    def enterEvent(self, event: QEnterEvent) -> None:
        self._pointer_inside = True
        self._pointer_position = event.position()
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._update_system_cursor()
        self.update()

    def leaveEvent(self, event: QEvent) -> None:
        if not self._interaction:
            self._pointer_inside = False
        self._update_system_cursor()
        self.update()
        super().leaveEvent(event)

    def hideEvent(self, event: QEvent) -> None:
        self._finish_active_brush_stroke()
        self._set_cursor_override(False)
        super().hideEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._tablet_active:
            event.accept()
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._pointer_position = event.position()
        if self._begin_pointer(
            event.position(),
            event.button(),
            event.modifiers(),
            1.0,
            event.timestamp(),
        ):
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._pointer_position = event.position()
        self._pointer_inside = True
        if self._continue_pointer(event.position(), event.modifiers(), 1.0, event.timestamp()):
            event.accept()
        self._update_system_cursor()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._tablet_active:
            event.accept()
            return
        self._pointer_position = event.position()
        if self._end_pointer(event.button()):
            event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1.0 / 1.15
        self._set_zoom_at(event.position(), self.zoom * factor)
        event.accept()

    def _handle_tablet(self, event: QTabletEvent) -> None:
        self._pointer_position = event.position()
        self._pointer_inside = True
        if event.type() == QEvent.Type.TabletPress:
            self._tablet_active = True
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._begin_pointer(
                event.position(),
                Qt.MouseButton.LeftButton,
                event.modifiers(),
                event.pressure(),
                event.timestamp(),
                event.xTilt(),
                event.yTilt(),
            )
        elif event.type() == QEvent.Type.TabletMove:
            self._continue_pointer(
                event.position(),
                event.modifiers(),
                event.pressure(),
                event.timestamp(),
                event.xTilt(),
                event.yTilt(),
            )
        elif event.type() == QEvent.Type.TabletRelease:
            self._end_pointer(Qt.MouseButton.LeftButton)
            self._tablet_active = False
        self._update_system_cursor()
        self.update()
        event.accept()

    def _begin_pointer(
        self,
        position: QPointF,
        button: Qt.MouseButton,
        modifiers: Qt.KeyboardModifier,
        pressure: float,
        timestamp: int,
        tilt_x: float = 0.0,
        tilt_y: float = 0.0,
    ) -> bool:
        if button == Qt.MouseButton.LeftButton and self._space_down and modifiers & Qt.KeyboardModifier.ShiftModifier:
            edge = self._crop_target_at(position)
            if self.mirror_horizontal:
                edge = edge.replace("left", "TEMP").replace("right", "left").replace("TEMP", "right")
            if self.mirror_vertical:
                edge = edge.replace("top", "TEMP").replace("bottom", "top").replace("TEMP", "bottom")
            self._space_used = True
            self._interaction = f"crop_{edge}"
            self._crop_edge = edge
            self._crop_moved = False
            self._crop_start_rect = self.document.canvas_rect
            self._drag_start = QPointF(position)
            self._drag_base_pan = QPointF(self.pan)
        elif (
            button == Qt.MouseButton.LeftButton and self._space_down and modifiers & Qt.KeyboardModifier.ControlModifier
        ):
            self._space_used = True
            self._interaction = "zoom"
            self._drag_start = QPointF(position)
            self._drag_start_zoom = self.zoom
            self._drag_anchor_document = self.to_document(position)
        elif button == Qt.MouseButton.MiddleButton or (button == Qt.MouseButton.LeftButton and self._space_down):
            if self._space_down:
                self._space_used = True
            self._interaction = "pan"
            self._drag_start = QPointF(position)
            self._drag_base_pan = QPointF(self.pan)
        elif button == Qt.MouseButton.LeftButton and self.tool == "transform" and self.transform_session is not None:
            if not self._begin_transform_pointer(position):
                return False
        elif button == Qt.MouseButton.LeftButton and modifiers & Qt.KeyboardModifier.ControlModifier:
            self._interaction = "sample"
            self._sample_color(position)
        elif button == Qt.MouseButton.LeftButton and self.tool == "selection":
            self._interaction = "selection"
            self._selection_points = [self.to_world(position)]
            if modifiers & Qt.KeyboardModifier.ShiftModifier and modifiers & Qt.KeyboardModifier.AltModifier:
                self._selection_action = "intersect"
            elif modifiers & Qt.KeyboardModifier.ShiftModifier:
                self._selection_action = "add"
            elif modifiers & Qt.KeyboardModifier.AltModifier:
                self._selection_action = "subtract"
            else:
                self._selection_action = "replace"
        elif button == Qt.MouseButton.LeftButton and modifiers & Qt.KeyboardModifier.ShiftModifier:
            self._interaction = "size"
            self._drag_start = QPointF(position)
            self._drag_start_size = self.document.brush_size
        elif (
            button == Qt.MouseButton.LeftButton
            and self.tool == "brush"
            and self.document.brush_settings.engine == "shape"
        ):
            self._interaction = "shape"
            self._shape_start = self.to_world(position)
            self._shape_current = QPointF(self._shape_start)
            self._note_color_used()
        elif button == Qt.MouseButton.LeftButton:
            point = self.to_world(position)
            if self.document.brush_settings.engine == "fill":
                self._note_color_used()
                self.document.flood_fill(point)
                self._interaction = "fill"
                self.update()
                return True
            self._interaction = "paint"
            self._note_color_used()
            sample = StrokeSample(point.x(), point.y(), pressure, float(timestamp), tilt_x, tilt_y)
            self.document.begin_stroke(sample)
            self._begin_smart_shape_candidate(sample, position)
        else:
            return False
        self._pointer_inside = True
        self._update_system_cursor()
        self.update()
        return True

    def _continue_pointer(
        self,
        position: QPointF,
        modifiers: Qt.KeyboardModifier,
        pressure: float,
        timestamp: int,
        tilt_x: float = 0.0,
        tilt_y: float = 0.0,
    ) -> bool:
        if self._interaction == "paint":
            point = self.to_world(position)
            sample = StrokeSample(point.x(), point.y(), pressure, float(timestamp), tilt_x, tilt_y)
            self.document.add_sample(sample)
            self._continue_smart_shape_candidate(sample, position)
        elif self._interaction == "smart_shape":
            self._queue_smart_shape_adjustment(position, timestamp)
        elif self._interaction == "pan":
            self.pan = self._drag_base_pan + (position - self._drag_start)
            self.view_changed.emit()
        elif self._interaction == "zoom":
            dy = position.y() - self._drag_start.y()
            target_zoom = self._drag_start_zoom * math.exp(-dy * 0.01)
            self._set_zoom_from_anchor(self._drag_start, self._drag_anchor_document, target_zoom)
        elif self._interaction == "size":
            value = round(
                max(
                    1.0,
                    min(
                        5000.0,
                        self._drag_start_size * math.exp((position.x() - self._drag_start.x()) * 0.0087),
                    ),
                )
            )
            self.document.set_brush(size=float(value))
            self.brush_size_changed.emit(value)
        elif self._interaction.startswith("crop_"):
            self._update_crop(position)
        elif self._interaction == "sample":
            if modifiers & Qt.KeyboardModifier.ControlModifier:
                self._sample_color(position)
        elif self._interaction == "selection":
            point = self.to_world(position)
            if not self._selection_points or (point - self._selection_points[-1]).manhattanLength() >= max(
                0.5, 1.5 / self.zoom
            ):
                self._selection_points.append(point)
        elif self._interaction == "transform":
            self._update_transform_pointer(position, modifiers)
        elif self._interaction == "shape":
            self._shape_current = self.to_world(position)
        else:
            return False
        self.update()
        return True

    def _end_pointer(self, button: Qt.MouseButton) -> bool:
        if not self._interaction:
            return False
        if button not in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
            return False
        if self._interaction == "paint":
            self._smart_shape_timer.stop()
            self.document.end_stroke()
            self._clear_smart_shape_state()
        elif self._interaction == "smart_shape":
            self._apply_final_smart_shape_adjustment()
            self.document.end_stroke()
            self._log_smart_shape_result()
            self._clear_smart_shape_state()
        elif self._interaction.startswith("crop_"):
            if self._crop_moved:
                self.document.commit_canvas_rect(self._crop_start_rect)
            else:
                self._reset_canvas_to_2k()
            self._crop_edge = ""
        elif self._interaction == "selection":
            self.document.apply_freehand_selection(self._selection_points, self._selection_action)
            self._selection_points.clear()
        elif self._interaction == "transform":
            self._transform_mode = ""
        elif self._interaction == "shape":
            self.document.commit_shape(self._shape_start, self._shape_current)
        self._interaction = ""
        if not QRectF(self.rect()).contains(self._pointer_position):
            self._pointer_inside = False
        self._update_system_cursor()
        self.update()
        return True

    def _begin_smart_shape_candidate(self, sample: StrokeSample, position: QPointF) -> None:
        self._smart_shape_timer.stop()
        self._smart_shape_redraw_timer.stop()
        self._smart_shape_flash_timer.stop()
        self._smart_shape_flash_points = ()
        self._smart_shape_samples = [sample]
        self._smart_shape = None
        self._smart_shape_hold_anchor = QPointF(position)
        self._smart_shape_last_adjust = QPointF(position)
        self._smart_shape_last_adjust_timestamp = -1
        self._smart_shape_pending_position = None
        self._smart_shape_redraw_ms.clear()
        self._smart_shape_suppressed = not self.smart_shape_enabled
        if self.smart_shape_enabled:
            self._smart_shape_timer.start(self.smart_shape_hold_ms)

    def _continue_smart_shape_candidate(self, sample: StrokeSample, position: QPointF) -> None:
        if self._smart_shape_suppressed or not self.smart_shape_enabled:
            return
        if not self._smart_shape_samples:
            self._smart_shape_samples.append(sample)
        else:
            previous = self._smart_shape_samples[-1]
            minimum_world_distance = max(0.18, 0.55 / max(self.zoom, 0.05))
            if math.hypot(sample.x - previous.x, sample.y - previous.y) >= minimum_world_distance:
                self._smart_shape_samples.append(sample)
        hold_delta = position - self._smart_shape_hold_anchor
        if math.hypot(hold_delta.x(), hold_delta.y()) >= 3.0:
            self._smart_shape_hold_anchor = QPointF(position)
            self._smart_shape_timer.start(self.smart_shape_hold_ms)

    def _activate_smart_shape(self) -> None:
        if not self.smart_shape_enabled or self._smart_shape_suppressed or self._interaction != "paint":
            return
        shape = clean_stroke(self._smart_shape_samples)
        if shape is None:
            return
        elapsed = self.document.replace_active_stroke(self._stroke_samples(shape.points))
        if elapsed is None:
            return
        self._smart_shape = shape
        self._smart_shape_redraw_ms.append(elapsed)
        self._smart_shape_last_adjust = QPointF(self._pointer_position)
        self._smart_shape_last_adjust_timestamp = -1
        self._smart_shape_last_redraw_at = time.perf_counter()
        self._smart_shape_pending_position = None
        self._interaction = "smart_shape"
        self._start_smart_shape_flash(shape.points)
        self.update()

    def _queue_smart_shape_adjustment(self, position: QPointF, timestamp: int) -> None:
        if self._smart_shape is None:
            return
        if (position - self._smart_shape_last_adjust).manhattanLength() < 0.65:
            return
        self._smart_shape_pending_position = QPointF(position)
        self._smart_shape_pending_timestamp = int(timestamp)
        last_cost = self._smart_shape_redraw_ms[-1] if self._smart_shape_redraw_ms else 0.0
        interval_ms = max(8.0, min(50.0, last_cost * 0.9))
        elapsed_ms = (time.perf_counter() - self._smart_shape_last_redraw_at) * 1000.0
        if elapsed_ms >= interval_ms:
            self._smart_shape_redraw_timer.stop()
            self._flush_smart_shape_adjustment()
        elif not self._smart_shape_redraw_timer.isActive():
            self._smart_shape_redraw_timer.start(max(1, math.ceil(interval_ms - elapsed_ms)))

    def _flush_smart_shape_adjustment(self) -> None:
        position = self._smart_shape_pending_position
        if self._interaction != "smart_shape" or position is None:
            return
        timestamp = self._smart_shape_pending_timestamp
        self._smart_shape_pending_position = None
        self._apply_smart_shape_adjustment(position, timestamp, force=True)

    def _apply_smart_shape_adjustment(self, position: QPointF, timestamp: int, *, force: bool = False) -> None:
        shape = self._smart_shape
        if shape is None:
            return
        distance = (position - self._smart_shape_last_adjust).manhattanLength()
        if not force and distance < 0.65:
            return
        world = self.to_world(position)
        adjusted = shape.adjusted(world.x(), world.y())
        elapsed = self.document.replace_active_stroke(self._stroke_samples(adjusted))
        if elapsed is None:
            return
        if len(self._smart_shape_redraw_ms) < 512:
            self._smart_shape_redraw_ms.append(elapsed)
        self._smart_shape_last_adjust = QPointF(position)
        self._smart_shape_last_adjust_timestamp = int(timestamp)
        self._smart_shape_last_redraw_at = time.perf_counter()
        if self._smart_shape_flash_timer.isActive():
            self._smart_shape_flash_points = tuple(QPointF(point.x, point.y) for point in adjusted)

    def _restore_smart_shape_freehand(self) -> None:
        if self._interaction != "smart_shape":
            return
        self.document.replace_active_stroke(self._smart_shape_samples)
        self._smart_shape_timer.stop()
        self._smart_shape_redraw_timer.stop()
        self._smart_shape_pending_position = None
        self._smart_shape = None
        self._smart_shape_suppressed = True
        self._interaction = "paint"
        self.update()

    def _finish_active_brush_stroke(self) -> None:
        if self._interaction == "paint":
            self._smart_shape_timer.stop()
            self.document.end_stroke()
            self._clear_smart_shape_state()
            self._interaction = ""
        elif self._interaction == "smart_shape":
            self._apply_final_smart_shape_adjustment()
            self.document.end_stroke()
            self._log_smart_shape_result()
            self._clear_smart_shape_state()
            self._interaction = ""
        elif self._interaction == "shape":
            self.document.commit_shape(self._shape_start, self._shape_current)
            self._interaction = ""

    def _apply_final_smart_shape_adjustment(self) -> None:
        self._smart_shape_redraw_timer.stop()
        self._smart_shape_pending_position = None
        if (self._pointer_position - self._smart_shape_last_adjust).manhattanLength() < 0.01:
            return
        self._apply_smart_shape_adjustment(
            self._pointer_position,
            self._smart_shape_last_adjust_timestamp + 16,
            force=True,
        )

    def _clear_smart_shape_state(self) -> None:
        self._smart_shape_timer.stop()
        self._smart_shape_redraw_timer.stop()
        self._smart_shape_samples.clear()
        self._smart_shape = None
        self._smart_shape_pending_position = None
        self._smart_shape_redraw_ms.clear()
        self._smart_shape_suppressed = False

    def _log_smart_shape_result(self) -> None:
        shape = self._smart_shape
        if shape is None:
            return
        redraw_mean = sum(self._smart_shape_redraw_ms) / max(1, len(self._smart_shape_redraw_ms))
        redraw_max = max(self._smart_shape_redraw_ms, default=0.0)
        logging.getLogger("paintstudio.smart_shape").info(
            "smart shape complete kind=%s closed=%s raw_points=%s clean_points=%s corners=%s "
            "straight_runs=%s recognition_ms=%.2f redraws=%s redraw_mean_ms=%.2f redraw_max_ms=%.2f",
            shape.kind,
            shape.closed,
            shape.source_point_count,
            len(shape.points),
            shape.corner_count,
            shape.straight_segment_count,
            shape.elapsed_ms,
            len(self._smart_shape_redraw_ms),
            redraw_mean,
            redraw_max,
        )

    def _start_smart_shape_flash(self, points) -> None:
        self._smart_shape_flash_points = tuple(QPointF(point.x, point.y) for point in points)
        self._smart_shape_flash_timer.start()

    def _finish_smart_shape_flash(self) -> None:
        self._smart_shape_flash_points = ()
        self.update()

    def _draw_smart_shape_flash(self, painter: QPainter) -> None:
        if not self._smart_shape_flash_timer.isActive() or len(self._smart_shape_flash_points) < 2:
            return
        path = QPainterPath(self._world_to_screen(self._smart_shape_flash_points[0]))
        for point in self._smart_shape_flash_points[1:]:
            path.lineTo(self._world_to_screen(point))
        color = self.document.brush_color
        opposite = QColor(255 - color.red(), 255 - color.green(), 255 - color.blue(), 255)
        if math.sqrt(
            (opposite.red() - color.red()) ** 2
            + (opposite.green() - color.green()) ** 2
            + (opposite.blue() - color.blue()) ** 2
        ) < 96.0:
            opposite = QColor("#ffffff" if color.lightness() < 128 else "#000000")
        pen = QPen(opposite, max(2.0, self.document.brush_size * self.zoom))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.restore()

    @staticmethod
    def _stroke_samples(points) -> tuple[StrokeSample, ...]:
        return tuple(StrokeSample(point.x, point.y, point.pressure, point.timestamp_ms) for point in points)

    def _sample_color(self, position: QPointF) -> None:
        point = self.to_world(position)
        image_point = self.document.world_to_image_point(point)
        x = math.floor(image_point.x())
        y = math.floor(image_point.y())
        if x < 0 or y < 0 or x >= self.document.image.width() or y >= self.document.image.height():
            return
        color = self.document.image.pixelColor(x, y)
        if not color.isValid():
            return
        self.document.set_brush(color=color)
        self.color_sampled.emit(color)

    def _set_zoom_at(self, position: QPointF, target_zoom: float) -> None:
        self._set_zoom_from_anchor(position, self.to_document(position), target_zoom)

    def _set_zoom_from_anchor(self, position: QPointF, document_anchor: QPointF, target_zoom: float) -> None:
        self.zoom = max(0.05, min(32.0, target_zoom))
        document_center = QPointF(self.document.width * 0.5, self.document.height * 0.5)
        desired_center = position - self._mirror_delta(document_anchor - document_center) * self.zoom
        self.pan = desired_center - QPointF(self.width() * 0.5, self.height() * 0.5)
        self._emit_zoom()
        self.view_changed.emit()
        self.update()

    def _emit_zoom(self) -> None:
        self.zoom_changed.emit(round(self.zoom * 100.0))

    def _update_system_cursor(self) -> None:
        if self._interaction == "pan":
            self._set_cursor_override(False)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        crop_edge = (
            self._crop_edge if self._interaction.startswith("crop_") else self._crop_target_at(self._pointer_position)
        )
        if self._space_down and self._shift_down:
            self._set_cursor_override(False)
            if crop_edge in ("left", "right"):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif crop_edge in ("top", "bottom"):
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            elif crop_edge in ("top_left", "bottom_right"):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif crop_edge in ("top_right", "bottom_left"):
                self.setCursor(Qt.CursorShape.SizeBDiagCursor)
            else:
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            return
        if self._interaction == "zoom" or (
            self._space_down and QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._set_cursor_override(False)
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            return
        if self._space_down:
            self._set_cursor_override(False)
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            return
        if self.tool == "selection":
            self._set_cursor_override(False)
            self.setCursor(Qt.CursorShape.CrossCursor)
            return
        if self.tool == "brush" and self.document.brush_settings.engine in ("shape", "fill"):
            self._set_cursor_override(False)
            self.setCursor(Qt.CursorShape.CrossCursor)
            return
        if self.tool == "transform":
            self._set_cursor_override(False)
            mode, handle = self._transform_hit(self._pointer_position)
            if mode in ("move", "pivot"):
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            elif mode == "rotate":
                self.setCursor(Qt.CursorShape.CrossCursor)
            elif handle in ("top", "bottom"):
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            elif handle in ("left", "right"):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif handle in ("top_left", "bottom_right"):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif handle:
                self.setCursor(Qt.CursorShape.SizeBDiagCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        if self._pointer_inside:
            self._set_cursor_override(True)
            self.setCursor(Qt.CursorShape.BlankCursor)
            return
        self._set_cursor_override(False)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def _set_cursor_override(self, hidden: bool) -> None:
        app = QApplication.instance()
        if app is None or hidden == self._cursor_override:
            return
        if hidden:
            app.setOverrideCursor(Qt.CursorShape.BlankCursor)
        else:
            app.restoreOverrideCursor()
        self._cursor_override = hidden

    def _draw_brush_cursor(self, painter: QPainter) -> None:
        if (
            self.tool != "brush"
            or self.document.brush_settings.engine in ("shape", "fill")
            or not self._pointer_inside
            or self._interaction in ("pan", "zoom")
            or self._interaction.startswith("crop_")
        ):
            return
        center = self._pointer_position
        radius = max(0.5, self.document.brush_size * self.zoom * 0.5)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for color, width in ((QColor(0, 0, 0, 210), 3.0), (QColor(255, 255, 255, 235), 1.0)):
            painter.setPen(QPen(color, width))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, radius, radius)
            cross = max(4.0, min(8.0, radius * 0.35))
            painter.drawLine(QPointF(center.x() - cross, center.y()), QPointF(center.x() + cross, center.y()))
            painter.drawLine(QPointF(center.x(), center.y() - cross), QPointF(center.x(), center.y() + cross))
        if self.document.eraser_enabled:
            offset = max(9.0, radius * 0.72)
            painter.translate(center + QPointF(offset, offset))
            painter.rotate(-35.0)
            painter.setPen(QPen(QColor(16, 17, 20), 2.0))
            painter.setBrush(QColor("#ff7dd6"))
            painter.drawRoundedRect(QRectF(-7.0, -4.0, 14.0, 8.0), 2.0, 2.0)
            painter.setPen(QPen(QColor(255, 255, 255, 220), 1.0))
            painter.drawLine(QPointF(1.5, -3.0), QPointF(1.5, 3.0))
        painter.restore()

    def _draw_shape_preview(self, painter: QPainter) -> None:
        if self._interaction != "shape":
            return
        brush = self.document.brush_settings
        rect = QRectF(self._world_to_screen(self._shape_start), self._world_to_screen(self._shape_current)).normalized()
        color = self.document.brush_color
        color.setAlphaF(brush.opacity)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if brush.fill_mode == "border":
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(color, max(1.0, brush.border_width * self.zoom)))
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
        if brush.primitive == "ellipse":
            painter.drawEllipse(rect)
        else:
            painter.drawRoundedRect(rect, brush.corner_radius * self.zoom, brush.corner_radius * self.zoom)
        painter.restore()

    def _world_to_screen(self, point: QPointF) -> QPointF:
        return self._world_screen_transform().map(point)

    def _draw_selection(self, painter: QPainter) -> None:
        path = QPainterPath()
        if self.document.selection_mask is not None:
            key = self.document.selection_mask.cacheKey()
            if key != self._selection_cache_key:
                self._selection_region = QRegion(QBitmap.fromImage(self.document.selection_mask.createAlphaMask()))
                self._selection_cache_key = key
            region = self._selection_region
            expanded = QRegion(region)
            inner_edge = QRegion()
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                shifted = region.translated(dx, dy)
                expanded = expanded.united(shifted)
                inner_edge = inner_edge.united(region.subtracted(shifted))
            path.addRegion(inner_edge.united(expanded.subtracted(region)))
            screen_transform = QTransform.fromTranslate(*self.document.origin) * self._world_screen_transform()
            if self.transform_session is not None and self.transform_session.selection_before is not None:
                image_to_world = QTransform.fromTranslate(
                    self.transform_session.origin_before[0],
                    self.transform_session.origin_before[1],
                )
                world_to_image = QTransform.fromTranslate(-self.document.origin[0], -self.document.origin[1])
                path = (image_to_world * self._transform_matrix * world_to_image).map(path)
            path = screen_transform.map(path)
        if not path.isEmpty():
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.fillPath(path, QColor(0, 0, 0, 235))
            painter.fillPath(path, QBrush(QColor(255, 255, 255, 245), Qt.BrushStyle.Dense4Pattern))
            painter.restore()
        if len(self._selection_points) >= 2:
            preview = QPainterPath(self._world_to_screen(self._selection_points[0]))
            for point in self._selection_points[1:]:
                preview.lineTo(self._world_to_screen(point))
            painter.save()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#ff4db8"), 1.5, Qt.PenStyle.DashLine))
            painter.drawPath(preview)
            painter.restore()

    def _transform_original_quad(self) -> QPolygonF:
        if self.transform_session is None:
            return QPolygonF()
        rect = self.transform_session.bounds
        return QPolygonF([rect.topLeft(), rect.topRight(), rect.bottomRight(), rect.bottomLeft()])

    def _draw_transform_preview(self, painter: QPainter) -> None:
        session = self.transform_session
        if session is None or session.preview_image is None or session.preview_image.isNull():
            return
        world_to_screen = self._world_screen_transform()
        preview_to_world = QTransform.fromTranslate(session.preview_origin[0], session.preview_origin[1])
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setTransform(preview_to_world * self._transform_matrix * world_to_screen)
        painter.drawImage(0, 0, session.preview_image)
        painter.restore()

    def _transform_world_quad(self, matrix: QTransform | None = None) -> QPolygonF:
        return (matrix or self._transform_matrix).map(self._transform_original_quad())

    def _transform_screen_quad(self) -> QPolygonF:
        return QPolygonF([self._world_to_screen(point) for point in self._transform_world_quad()])

    def _transform_handles(self) -> dict[str, QPointF]:
        quad = self._transform_screen_quad()
        if len(quad) != 4:
            return {}
        return {
            "top_left": quad[0],
            "top": (quad[0] + quad[1]) * 0.5,
            "top_right": quad[1],
            "right": (quad[1] + quad[2]) * 0.5,
            "bottom_right": quad[2],
            "bottom": (quad[2] + quad[3]) * 0.5,
            "bottom_left": quad[3],
            "left": (quad[3] + quad[0]) * 0.5,
        }

    def _draw_transform(self, painter: QPainter) -> None:
        if self.transform_session is None:
            return
        quad = self._transform_screen_quad()
        if len(quad) != 4:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#ff4db8"), 1.5))
        painter.drawPolygon(quad)
        for point in self._transform_handles().values():
            painter.setPen(QPen(QColor("#17181b"), 1.5))
            painter.setBrush(QColor("#f2f2f4"))
            painter.drawRect(QRectF(point.x() - 5.0, point.y() - 5.0, 10.0, 10.0))
        pivot = self._world_to_screen(self._transform_matrix.map(self._transform_pivot))
        painter.setPen(QPen(QColor("#ff4db8"), 1.2))
        painter.drawLine(pivot - QPointF(7, 0), pivot + QPointF(7, 0))
        painter.drawLine(pivot - QPointF(0, 7), pivot + QPointF(0, 7))
        painter.restore()

    def _transform_hit(self, position: QPointF) -> tuple[str, str]:
        if self.transform_session is None:
            return "", ""
        pivot = self._world_to_screen(self._transform_matrix.map(self._transform_pivot))
        if math.hypot(position.x() - pivot.x(), position.y() - pivot.y()) <= 9.0:
            return "pivot", ""
        for name, point in self._transform_handles().items():
            distance = math.hypot(position.x() - point.x(), position.y() - point.y())
            if distance <= 8.0:
                return "scale", name
            if name in {"top_left", "top_right", "bottom_right", "bottom_left"} and distance <= 25.0:
                return "rotate", name
        quad = self._transform_screen_quad()
        if not quad.containsPoint(position, Qt.FillRule.OddEvenFill):
            edge_distance = min(
                self._distance_to_segment(position, quad[index], quad[(index + 1) % 4]) for index in range(4)
            )
            if edge_distance <= 34.0:
                return "rotate", ""
        if quad.containsPoint(position, Qt.FillRule.OddEvenFill):
            return "move", ""
        return "", ""

    def _distance_to_segment(self, point: QPointF, start: QPointF, end: QPointF) -> float:
        dx = end.x() - start.x()
        dy = end.y() - start.y()
        length_squared = dx * dx + dy * dy
        if length_squared <= 1e-9:
            return math.hypot(point.x() - start.x(), point.y() - start.y())
        amount = ((point.x() - start.x()) * dx + (point.y() - start.y()) * dy) / length_squared
        amount = max(0.0, min(1.0, amount))
        closest = QPointF(start.x() + dx * amount, start.y() + dy * amount)
        return math.hypot(point.x() - closest.x(), point.y() - closest.y())

    def _begin_transform_pointer(self, position: QPointF) -> bool:
        mode, handle = self._transform_hit(position)
        if not mode:
            return False
        self._interaction = "transform"
        self._transform_mode = mode
        self._transform_handle = handle
        self._transform_start_world = self.to_world(position)
        self._transform_start_matrix = QTransform(self._transform_matrix)
        return True

    def _update_transform_pointer(self, position: QPointF, modifiers: Qt.KeyboardModifier) -> None:
        if self.transform_session is None:
            return
        current = self.to_world(position)
        start = self._transform_start_matrix
        if self._transform_mode == "move":
            delta = current - self._transform_start_world
            world_delta = QTransform.fromTranslate(delta.x(), delta.y())
            matrix = start * world_delta
        elif self._transform_mode == "pivot":
            inverse, invertible = self._transform_matrix.inverted()
            if not invertible:
                return
            pivot = inverse.map(current)
            if modifiers & Qt.KeyboardModifier.AltModifier:
                rect = self.transform_session.bounds
                pivot.setX(max(rect.left(), min(rect.right(), pivot.x())))
                pivot.setY(max(rect.top(), min(rect.bottom(), pivot.y())))
            self._transform_pivot = pivot
            self.update()
            return
        elif self._transform_mode == "rotate":
            pivot = start.map(self._transform_pivot)
            first_angle = math.atan2(
                self._transform_start_world.y() - pivot.y(),
                self._transform_start_world.x() - pivot.x(),
            )
            current_angle = math.atan2(current.y() - pivot.y(), current.x() - pivot.x())
            delta_degrees = math.degrees(current_angle - first_angle)
            rotation_only = QTransform()
            rotation_only.rotate(delta_degrees)
            rotation = (
                QTransform.fromTranslate(-pivot.x(), -pivot.y())
                * rotation_only
                * QTransform.fromTranslate(pivot.x(), pivot.y())
            )
            matrix = start * rotation
        elif self._transform_mode == "scale":
            if modifiers & Qt.KeyboardModifier.ControlModifier:
                original = self._transform_original_quad()
                destination = start.map(original)
                corner_index = {
                    "top_left": 0,
                    "top_right": 1,
                    "bottom_right": 2,
                    "bottom_left": 3,
                }.get(self._transform_handle)
                if corner_index is not None:
                    destination[corner_index] = current
                else:
                    delta = current - self._transform_start_world
                    edge_indices = {
                        "top": (0, 1),
                        "right": (1, 2),
                        "bottom": (2, 3),
                        "left": (3, 0),
                    }.get(self._transform_handle)
                    if edge_indices is None:
                        return
                    for index in edge_indices:
                        destination[index] += delta
                matrix = QTransform.quadToQuad(original, destination)
            else:
                inverse, invertible = start.inverted()
                if not invertible:
                    return
                local = inverse.map(current)
                rect = self.transform_session.bounds
                handles = {
                    "top_left": rect.topLeft(),
                    "top": QPointF(rect.center().x(), rect.top()),
                    "top_right": rect.topRight(),
                    "right": QPointF(rect.right(), rect.center().y()),
                    "bottom_right": rect.bottomRight(),
                    "bottom": QPointF(rect.center().x(), rect.bottom()),
                    "bottom_left": rect.bottomLeft(),
                    "left": QPointF(rect.left(), rect.center().y()),
                }
                opposites = {
                    "top_left": rect.bottomRight(),
                    "top": QPointF(rect.center().x(), rect.bottom()),
                    "top_right": rect.bottomLeft(),
                    "right": QPointF(rect.left(), rect.center().y()),
                    "bottom_right": rect.topLeft(),
                    "bottom": QPointF(rect.center().x(), rect.top()),
                    "bottom_left": rect.topRight(),
                    "left": QPointF(rect.right(), rect.center().y()),
                }
                handle = handles[self._transform_handle]
                anchor = opposites[self._transform_handle]
                sx = (
                    1.0 if abs(handle.x() - anchor.x()) < 1e-6 else (local.x() - anchor.x()) / (handle.x() - anchor.x())
                )
                sy = (
                    1.0 if abs(handle.y() - anchor.y()) < 1e-6 else (local.y() - anchor.y()) / (handle.y() - anchor.y())
                )
                if modifiers & Qt.KeyboardModifier.ShiftModifier and "_" in self._transform_handle:
                    uniform = sx if abs(sx) >= abs(sy) else sy
                    sx = sy = uniform
                sx = math.copysign(max(0.01, abs(sx)), sx)
                sy = math.copysign(max(0.01, abs(sy)), sy)
                local_scale = (
                    QTransform.fromTranslate(-anchor.x(), -anchor.y())
                    * QTransform.fromScale(sx, sy)
                    * QTransform.fromTranslate(anchor.x(), anchor.y())
                )
                matrix = local_scale * start
        else:
            return
        self._transform_matrix = matrix
        self.update()

    def _crop_target_at(self, position: QPointF) -> str:
        rect = self.document_rect()
        inside = rect.contains(position)
        if inside and rect.width() > 0 and rect.height() > 0:
            nx = (position.x() - rect.left()) / rect.width()
            ny = (position.y() - rect.top()) / rect.height()
            horizontal = "left" if nx < 0.32 else "right" if nx > 0.68 else ""
            vertical = "top" if ny < 0.32 else "bottom" if ny > 0.68 else ""
            if horizontal and vertical:
                return f"{vertical}_{horizontal}"
            if not horizontal and not vertical:
                return "auto"
        else:
            horizontal = "left" if position.x() < rect.left() else "right" if position.x() > rect.right() else ""
            vertical = "top" if position.y() < rect.top() else "bottom" if position.y() > rect.bottom() else ""
            if horizontal and vertical:
                return f"{vertical}_{horizontal}"
        distances = {
            "left": abs(position.x() - rect.left()),
            "right": abs(position.x() - rect.right()),
            "top": abs(position.y() - rect.top()),
            "bottom": abs(position.y() - rect.bottom()),
        }
        return min(distances, key=distances.get)

    def _update_crop(self, position: QPointF) -> None:
        start = QRect(self._crop_start_rect)
        dx = round((position.x() - self._drag_start.x()) / self.zoom)
        dy = round((position.y() - self._drag_start.y()) / self.zoom)
        dx = -dx if self.mirror_horizontal else dx
        dy = -dy if self.mirror_vertical else dy
        screen_dx = position.x() - self._drag_start.x()
        screen_dy = position.y() - self._drag_start.y()
        if math.hypot(screen_dx, screen_dy) < 2.0:
            return
        if self._crop_edge == "auto":
            horizontal = "left" if dx < 0 else "right"
            vertical = "top" if dy < 0 else "bottom"
            if min(abs(screen_dx), abs(screen_dy)) >= max(abs(screen_dx), abs(screen_dy)) * 0.35:
                self._crop_edge = f"{vertical}_{horizontal}"
            else:
                self._crop_edge = horizontal if abs(screen_dx) >= abs(screen_dy) else vertical
            self._interaction = f"crop_{self._crop_edge}"
        self._crop_moved = True
        left, top = start.x(), start.y()
        right = start.x() + start.width()
        bottom = start.y() + start.height()
        if "left" in self._crop_edge:
            left = min(right - 1, left + dx)
        elif "right" in self._crop_edge:
            right = max(left + 1, right + dx)
        if "top" in self._crop_edge:
            top = min(bottom - 1, top + dy)
        elif "bottom" in self._crop_edge:
            bottom = max(top + 1, bottom + dy)
        updated = QRect(left, top, right - left, bottom - top)
        old_center = QPointF(self.document.canvas_rect.center())
        if self.document.preview_canvas_rect(updated):
            new_center = QPointF(self.document.canvas_rect.center())
            self.pan = self._drag_base_pan + self._mirror_delta(new_center - old_center) * self.zoom
            self._drag_base_pan = QPointF(self.pan)
            self.view_changed.emit()
            self.update()

    def _reset_canvas_to_2k(self) -> None:
        before = QRect(self._crop_start_rect)
        center = QPointF(
            before.x() + before.width() * 0.5,
            before.y() + before.height() * 0.5,
        )
        target = QRect(round(center.x() - 1024), round(center.y() - 1024), 2048, 2048)
        if self.document.preview_canvas_rect(target):
            self.document.commit_canvas_rect(before)
            self.view_changed.emit()
            self.update()
