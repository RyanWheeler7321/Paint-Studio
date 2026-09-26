from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import logging
import math
import time
from typing import Iterable, Iterator

from PySide6.QtCore import QPointF, QRect, QRectF, Signal, QObject, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QRadialGradient, QTransform

from .brush import BRUSH_PRESETS, BrushSettings, brush_preset
from .layers import BLEND_COMPOSITION, LayerNode, transparent_image


TILE_SIZE = 256
OUTLINE_ENGINES = frozenset({"air", "ink", "paint"})


@dataclass(frozen=True, slots=True)
class StrokeSample:
    x: float
    y: float
    pressure: float = 1.0
    timestamp_ms: float = 0.0
    tilt_x: float = 0.0
    tilt_y: float = 0.0


@dataclass(frozen=True, slots=True)
class StrokeCommand:
    samples: tuple[StrokeSample, ...]
    brush: BrushSettings | None = None
    color: str | None = None
    layer_id: str | None = None

    @classmethod
    def from_samples(
        cls,
        samples: Iterable[StrokeSample],
        *,
        brush: BrushSettings | None = None,
        color: str | None = None,
        layer_id: str | None = None,
    ) -> "StrokeCommand":
        return cls(tuple(samples), brush, color, layer_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "samples": [
                {
                    "x": sample.x,
                    "y": sample.y,
                    "pressure": sample.pressure,
                    "timestamp_ms": sample.timestamp_ms,
                    "tilt_x": sample.tilt_x,
                    "tilt_y": sample.tilt_y,
                }
                for sample in self.samples
            ],
            "brush": self.brush.to_dict() if self.brush else None,
            "color": self.color,
            "layer_id": self.layer_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "StrokeCommand":
        raw_samples = data.get("samples", [])
        samples = tuple(
            StrokeSample(
                float(sample["x"]),
                float(sample["y"]),
                float(sample.get("pressure", 1.0)),
                float(sample.get("timestamp_ms", 0.0)),
                float(sample.get("tilt_x", 0.0)),
                float(sample.get("tilt_y", 0.0)),
            )
            for sample in raw_samples
            if isinstance(sample, dict) and "x" in sample and "y" in sample
        )
        raw_brush = data.get("brush")
        brush = BrushSettings.from_dict(raw_brush) if isinstance(raw_brush, dict) else None
        return cls(samples, brush, str(data["color"]) if data.get("color") else None, str(data["layer_id"]) if data.get("layer_id") else None)


@dataclass(frozen=True, slots=True)
class StrokeBundle:
    strokes: tuple[StrokeCommand, ...]
    name: str = "Stroke bundle"

    @classmethod
    def from_strokes(cls, strokes: Iterable[StrokeCommand], *, name: str = "Stroke bundle") -> "StrokeBundle":
        return cls(tuple(strokes), name)

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "strokes": [stroke.to_dict() for stroke in self.strokes]}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "StrokeBundle":
        raw_strokes = data.get("strokes", [])
        strokes = tuple(StrokeCommand.from_dict(stroke) for stroke in raw_strokes if isinstance(stroke, dict))
        return cls(strokes, str(data.get("name", "Stroke bundle")))


@dataclass(frozen=True, slots=True)
class BundleResult:
    revision: int
    stroke_count: int
    sample_count: int
    dab_count: int
    dirty: QRect
    elapsed_ms: float


@dataclass(slots=True)
class TilePatch:
    layer_id: str
    x: int
    y: int
    before: QImage
    after: QImage


@dataclass(slots=True)
class HistoryEntry:
    patches: list[TilePatch] | None = None
    roots_before: list[LayerNode] | None = None
    roots_after: list[LayerNode] | None = None
    active_before: str | None = None
    active_after: str | None = None
    origin_before: tuple[int, int] | None = None
    origin_after: tuple[int, int] | None = None
    canvas_before: QRect | None = None
    canvas_after: QRect | None = None
    selection_before: QImage | None = None
    selection_after: QImage | None = None


@dataclass(slots=True)
class TransformSession:
    before_roots: list[LayerNode]
    active_before: str
    target_layer_ids: tuple[str, ...]
    source_images: dict[str, QImage]
    origin_before: tuple[int, int]
    canvas_before: QRect
    selection_before: QImage | None
    bounds: QRectF
    matrix: QTransform
    preview_image: QImage | None = None
    preview_base: QImage | None = None
    preview_origin: tuple[int, int] = (0, 0)


class OutlineStroke:
    """Border mode keeps every dab of one stroke so the outline follows the whole stroke."""

    def __init__(self, layer_id: str) -> None:
        self.layer_id = layer_id
        self.base_tiles: dict[tuple[int, int], QImage] = {}
        self.dabs: list[tuple[StrokeSample, float, QRect]] = []


class StrokeTransaction:
    def __init__(
        self,
        document: "PaintDocument",
        layer: LayerNode | None = None,
        brush: BrushSettings | None = None,
        color: QColor | None = None,
    ) -> None:
        self.document = document
        self.before_tiles: dict[tuple[str, int, int], QImage] = {}
        self.current_tiles: set[tuple[str, int, int]] = set()
        self.dirty = QRect()
        self.current_dirty = QRect()
        self.layer_id = layer.layer_id if layer is not None else None
        self.brush = brush
        self.color = QColor(color) if color is not None else None
        self.outline: OutlineStroke | None = None

    def capture_before(self, layer: LayerNode, bounds: QRect) -> None:
        if layer.image is None:
            return
        clipped = bounds.intersected(self.document.backing_rect)
        if clipped.isEmpty():
            return
        for tile_y in range(clipped.top() // TILE_SIZE, clipped.bottom() // TILE_SIZE + 1):
            for tile_x in range(clipped.left() // TILE_SIZE, clipped.right() // TILE_SIZE + 1):
                key = (layer.layer_id, tile_x, tile_y)
                if key not in self.before_tiles:
                    world_rect = self.document.tile_rect(tile_x, tile_y)
                    self.before_tiles[key] = layer.image.copy(self.document.world_to_image_rect(world_rect))
                self.current_tiles.add(key)
        self.dirty = clipped if self.dirty.isEmpty() else self.dirty.united(clipped)
        self.current_dirty = clipped if self.current_dirty.isEmpty() else self.current_dirty.united(clipped)

    def restore_current(self) -> QRect:
        dirty = QRect(self.current_dirty)
        self.outline = None
        for layer_id, tile_x, tile_y in self.current_tiles:
            layer = self.document.find_layer(layer_id)
            before = self.before_tiles.get((layer_id, tile_x, tile_y))
            if layer is None or layer.image is None or before is None:
                continue
            self.document._replace_tile(
                layer.image,
                self.document.world_to_image_rect(self.document.tile_rect(tile_x, tile_y)),
                before,
            )
        self.current_tiles.clear()
        self.current_dirty = QRect()
        return dirty

    def finish(self) -> HistoryEntry | None:
        patches: list[TilePatch] = []
        for (layer_id, tile_x, tile_y), before in self.before_tiles.items():
            layer = self.document.find_layer(layer_id)
            if layer is None or layer.image is None:
                continue
            tile_rect = self.document.tile_rect(tile_x, tile_y)
            after = layer.image.copy(self.document.world_to_image_rect(tile_rect))
            if before != after:
                patches.append(TilePatch(layer_id, tile_rect.x(), tile_rect.y(), before, after))
        return HistoryEntry(patches=patches) if patches else None


class PaintDocument(QObject):
    changed = Signal(QRect)
    history_changed = Signal(bool, bool)
    stroke_finished = Signal(int)
    layers_changed = Signal()
    active_layer_changed = Signal(str)
    brush_changed = Signal(object, QColor)
    canvas_bounds_changed = Signal(QRect)
    selection_changed = Signal()
    stamp_tip_changed = Signal(QImage)

    def __init__(self, width: int = 2048, height: int = 2048, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._origin_x = 0
        self._origin_y = 0
        self._canvas_rect = QRect(0, 0, width, height)
        self.image = transparent_image(width, height)
        base = transparent_image(width, height)
        initial = LayerNode("Paint Layer 1", image=base)
        self.roots: list[LayerNode] = [initial]
        self.active_layer_id = initial.layer_id
        self.revision = 0
        self._undo: list[HistoryEntry] = []
        self._redo: list[HistoryEntry] = []
        self._transaction: StrokeTransaction | None = None
        self._last_sample: StrokeSample | None = None
        self._distance_carry = 0.0
        self._color = QColor("#ffffff")
        self._brush_profiles = {preset.preset_id: preset for preset in BRUSH_PRESETS}
        self._brush = self._brush_profiles[BRUSH_PRESETS[0].preset_id]
        self.selection_mask: QImage | None = None
        self.stamp_tip_image = self._default_stamp_tip()
        self._defer_depth = 0
        self._deferred_dirty = QRect()
        self._composite_defer_depth = 0
        self._deferred_composite_dirty = QRect()
        self._recompose(self.backing_rect, emit=False)

    @property
    def width(self) -> int:
        return self._canvas_rect.width()

    @property
    def height(self) -> int:
        return self._canvas_rect.height()

    @property
    def canvas_rect(self) -> QRect:
        return QRect(self._canvas_rect)

    @property
    def backing_rect(self) -> QRect:
        return QRect(self._origin_x, self._origin_y, self.image.width(), self.image.height())

    @property
    def origin(self) -> tuple[int, int]:
        return self._origin_x, self._origin_y

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def brush_size(self) -> float:
        return self._brush.size

    @property
    def brush_color(self) -> QColor:
        return QColor(self._color)

    @property
    def brush_settings(self) -> BrushSettings:
        return self._brush

    @property
    def eraser_enabled(self) -> bool:
        return self._brush.eraser

    @property
    def active_layer(self) -> LayerNode | None:
        layer = self.find_layer(self.active_layer_id)
        return layer if layer is not None and not layer.is_group else None

    def set_brush(
        self,
        *,
        color: QColor | None = None,
        size: float | None = None,
        eraser: bool | None = None,
        opacity: float | None = None,
        flow: float | None = None,
        spacing: float | None = None,
        hardness: float | None = None,
        pressure_size: bool | None = None,
        pressure_opacity: bool | None = None,
        tip_shape: str | None = None,
        fill_mode: str | None = None,
        primitive: str | None = None,
        border_width: int | None = None,
        corner_radius: int | None = None,
        texture_strength: float | None = None,
        tilt_stretch: bool | None = None,
        blur_radius: int | None = None,
        blur_strength: float | None = None,
        scatter: float | None = None,
        size_variation: float | None = None,
        rotation_variation: float | None = None,
        hue_variation: float | None = None,
        alpha_variation: float | None = None,
        size_x_variation: float | None = None,
        size_y_variation: float | None = None,
        value_variation: float | None = None,
        saturation_variation: float | None = None,
        flip_x: bool | None = None,
        flip_y: bool | None = None,
        follow_rotation: bool | None = None,
        fill_threshold: int | None = None,
        fill_reference: str | None = None,
    ) -> None:
        if color is not None:
            self._color = QColor(color)
        values: dict[str, object] = {}
        for key, value in (
            ("size", size),
            ("eraser", eraser),
            ("opacity", opacity),
            ("flow", flow),
            ("spacing", spacing),
            ("hardness", hardness),
            ("pressure_size", pressure_size),
            ("pressure_opacity", pressure_opacity),
            ("tip_shape", tip_shape),
            ("fill_mode", fill_mode),
            ("primitive", primitive),
            ("border_width", border_width),
            ("corner_radius", corner_radius),
            ("texture_strength", texture_strength),
            ("tilt_stretch", tilt_stretch),
            ("blur_radius", blur_radius),
            ("blur_strength", blur_strength),
            ("scatter", scatter),
            ("size_variation", size_variation),
            ("rotation_variation", rotation_variation),
            ("hue_variation", hue_variation),
            ("alpha_variation", alpha_variation),
            ("size_x_variation", size_x_variation),
            ("size_y_variation", size_y_variation),
            ("value_variation", value_variation),
            ("saturation_variation", saturation_variation),
            ("flip_x", flip_x),
            ("flip_y", flip_y),
            ("follow_rotation", follow_rotation),
            ("fill_threshold", fill_threshold),
            ("fill_reference", fill_reference),
        ):
            if value is not None:
                values[key] = value
        if values:
            self._brush = self._brush.changed(**values)
            self._brush_profiles[self._brush.preset_id] = self._brush.changed(eraser=False)
        self.brush_changed.emit(self._brush, QColor(self._color))

    def select_brush_preset(self, preset_id: str) -> None:
        self._brush = self._brush_profiles.get(preset_id, brush_preset(preset_id)).changed(eraser=self._brush.eraser)
        self.brush_changed.emit(self._brush, QColor(self._color))

    def toggle_eraser(self) -> bool:
        self._brush = self._brush.changed(eraser=not self._brush.eraser)
        self.brush_changed.emit(self._brush, QColor(self._color))
        return self._brush.eraser

    def brush_profiles(self) -> dict[str, dict[str, object]]:
        return {preset_id: profile.to_dict() for preset_id, profile in self._brush_profiles.items()}

    def restore_brush_profiles(self, profiles: dict[str, object]) -> None:
        for preset_id, values in profiles.items():
            if preset_id not in self._brush_profiles or not isinstance(values, dict):
                continue
            restored = BrushSettings.from_dict(values)
            if restored.preset_id == preset_id:
                self._brush_profiles[preset_id] = restored.changed(eraser=False)
        current_id = self._brush.preset_id
        self._brush = self._brush_profiles.get(current_id, self._brush)

    def set_stamp_tip_image(self, image: QImage) -> None:
        converted = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        if converted.isNull():
            return
        self.stamp_tip_image = converted
        self.stamp_tip_changed.emit(QImage(converted))

    def _default_stamp_tip(self) -> QImage:
        image = transparent_image(128, 128)
        path = QPainterPath()
        center = QPointF(64, 64)
        for index in range(10):
            angle = math.radians(-90 + index * 36)
            radius = 50 if index % 2 == 0 else 21
            point = center + QPointF(math.cos(angle), math.sin(angle)) * radius
            path.moveTo(point) if index == 0 else path.lineTo(point)
        path.closeSubpath()
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillPath(path, QColor("white"))
        painter.end()
        return image

    def tile_rect(self, tile_x: int, tile_y: int) -> QRect:
        x = tile_x * TILE_SIZE
        y = tile_y * TILE_SIZE
        return QRect(x, y, TILE_SIZE, TILE_SIZE).intersected(self.backing_rect)

    def world_to_image_rect(self, rect: QRect) -> QRect:
        return rect.translated(-self._origin_x, -self._origin_y)

    def world_to_image_point(self, point: QPointF) -> QPointF:
        return point - QPointF(self._origin_x, self._origin_y)

    def canvas_image(self) -> QImage:
        return self.image.copy(self.world_to_image_rect(self._canvas_rect))

    def layer_canvas_image(self, layer: LayerNode) -> QImage:
        if layer.image is None:
            return QImage()
        return layer.image.copy(self.world_to_image_rect(self._canvas_rect))

    @property
    def has_selection(self) -> bool:
        return self.selection_mask is not None and not self.selection_bounds.isEmpty()

    @property
    def selection_bounds(self) -> QRect:
        if self.selection_mask is None or self.selection_mask.isNull():
            return QRect()
        bounds = self._mask_bounds(self.selection_mask)
        return bounds.translated(self._origin_x, self._origin_y)

    def selection_contains(self, point: QPointF) -> bool:
        if self.selection_mask is None:
            return True
        image_point = self.world_to_image_point(point).toPoint()
        if not self.selection_mask.rect().contains(image_point):
            return False
        return self.selection_mask.pixelColor(image_point).alpha() > 0

    def apply_freehand_selection(self, points: Iterable[QPointF], action: str = "replace") -> bool:
        source = tuple(QPointF(point) for point in points)
        if len(source) < 3:
            if action == "replace":
                return self.clear_selection()
            return False
        if action not in {"replace", "add", "subtract", "intersect"}:
            return False
        path = QPainterPath(source[0])
        for point in source[1:]:
            path.lineTo(point)
        path.closeSubpath()
        world_bounds = path.boundingRect().toAlignedRect().adjusted(-2, -2, 2, 2)
        self._ensure_world_rect(world_bounds)
        before = QImage(self.selection_mask) if self.selection_mask is not None else None
        shape = transparent_image(self.image.width(), self.image.height())
        painter = QPainter(shape)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(-self._origin_x, -self._origin_y)
        painter.fillPath(path, QColor(255, 255, 255, 255))
        painter.end()

        if action == "replace" or self.selection_mask is None:
            combined = shape if action != "subtract" else transparent_image(self.image.width(), self.image.height())
        else:
            combined = QImage(self.selection_mask)
            painter = QPainter(combined)
            if action == "add":
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            elif action == "subtract":
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
            else:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.drawImage(0, 0, shape)
            painter.end()
        self.selection_mask = combined if not self._mask_bounds(combined).isEmpty() else None
        self._push_selection_history(before)
        return True

    def select_all(self) -> bool:
        before = QImage(self.selection_mask) if self.selection_mask is not None else None
        self.selection_mask = transparent_image(self.image.width(), self.image.height())
        painter = QPainter(self.selection_mask)
        painter.fillRect(self.world_to_image_rect(self._canvas_rect), QColor(255, 255, 255, 255))
        painter.end()
        self._push_selection_history(before)
        return True

    def clear_selection(self) -> bool:
        if self.selection_mask is None:
            return False
        before = QImage(self.selection_mask)
        self.selection_mask = None
        self._push_selection_history(before)
        return True

    def invert_selection(self) -> bool:
        before = QImage(self.selection_mask) if self.selection_mask is not None else None
        mask = QImage(self.selection_mask) if self.selection_mask is not None else transparent_image(
            self.image.width(), self.image.height()
        )
        canvas = self.world_to_image_rect(self._canvas_rect)
        result = transparent_image(self.image.width(), self.image.height())
        painter = QPainter(result)
        painter.fillRect(canvas, QColor(255, 255, 255, 255))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
        painter.drawImage(0, 0, mask)
        painter.end()
        self.selection_mask = result
        self._push_selection_history(before)
        return True

    def _push_selection_history(self, before: QImage | None) -> None:
        entry = HistoryEntry(
            selection_before=before,
            selection_after=QImage(self.selection_mask) if self.selection_mask is not None else None,
        )
        self.selection_changed.emit()
        self._push_history(entry, self.selection_bounds)

    def begin_transform(self, layer_ids: Iterable[str]) -> TransformSession | None:
        requested = tuple(dict.fromkeys(layer_ids)) or (self.active_layer_id,)
        targets: list[LayerNode] = []
        seen: set[str] = set()
        for layer_id in requested:
            node = self.find_layer(layer_id)
            if node is None:
                continue
            for child in self.iter_layers([node]):
                if child.image is not None and child.layer_id not in seen:
                    seen.add(child.layer_id)
                    targets.append(child)
        if not targets:
            return None
        bounds = self.selection_bounds if self.has_selection else self._opaque_bounds(targets)
        if bounds.isEmpty():
            return None
        return TransformSession(
            before_roots=self._tree_snapshot(),
            active_before=self.active_layer_id,
            target_layer_ids=tuple(layer.layer_id for layer in targets),
            source_images={layer.layer_id: QImage(layer.image) for layer in targets if layer.image is not None},
            origin_before=self.origin,
            canvas_before=self.canvas_rect,
            selection_before=QImage(self.selection_mask) if self.selection_mask is not None else None,
            bounds=QRectF(bounds),
            matrix=QTransform(),
        )

    def prepare_transform_preview(self, session: TransformSession) -> None:
        target_ids = set(session.target_layer_ids)
        preview_roots = [node.clone() for node in session.before_roots]
        for node in self.iter_layers(preview_roots):
            if node.image is not None and node.layer_id not in target_ids:
                node.image.fill(0)
        preview_full = self._compose_nodes(preview_roots, QRect(0, 0, self.image.width(), self.image.height()))
        if session.selection_before is not None:
            painter = QPainter(preview_full)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.drawImage(0, 0, session.selection_before)
            painter.end()
        preview_world = session.bounds.toAlignedRect().intersected(self.backing_rect)
        preview_image_rect = preview_world.translated(-session.origin_before[0], -session.origin_before[1])
        session.preview_image = preview_full.copy(preview_image_rect)
        session.preview_origin = (preview_world.x(), preview_world.y())

        base_roots = [node.clone() for node in session.before_roots]
        for layer in self.iter_layers(base_roots):
            if layer.layer_id not in target_ids or layer.image is None:
                continue
            if session.selection_before is None:
                layer.image.fill(0)
                continue
            painter = QPainter(layer.image)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
            painter.drawImage(0, 0, session.selection_before)
            painter.end()
        session.preview_base = self._compose_nodes(
            base_roots,
            QRect(0, 0, self.image.width(), self.image.height()),
        )

    def preview_transform(self, session: TransformSession, matrix: QTransform) -> bool:
        mapped = matrix.mapRect(session.bounds).toAlignedRect().adjusted(-2, -2, 2, 2)
        self._ensure_world_rect(mapped)
        source_origin = session.origin_before
        destination_origin = self.origin
        image_transform = (
            QTransform.fromTranslate(source_origin[0], source_origin[1])
            * matrix
            * QTransform.fromTranslate(-destination_origin[0], -destination_origin[1])
        )
        for layer_id in session.target_layer_ids:
            layer = self.find_layer(layer_id)
            source = session.source_images.get(layer_id)
            if layer is None or source is None:
                continue
            result = transparent_image(self.image.width(), self.image.height())
            selected = QImage(source)
            if session.selection_before is not None:
                base = QImage(source)
                painter = QPainter(base)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
                painter.drawImage(0, 0, session.selection_before)
                painter.end()
                painter = QPainter(result)
                painter.drawImage(
                    source_origin[0] - destination_origin[0],
                    source_origin[1] - destination_origin[1],
                    base,
                )
                painter.end()
                painter = QPainter(selected)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
                painter.drawImage(0, 0, session.selection_before)
                painter.end()
            painter = QPainter(result)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.setTransform(image_transform)
            painter.drawImage(0, 0, selected)
            painter.end()
            layer.image = result
        if session.selection_before is not None:
            transformed_selection = transparent_image(self.image.width(), self.image.height())
            painter = QPainter(transformed_selection)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.setTransform(image_transform)
            painter.drawImage(0, 0, session.selection_before)
            painter.end()
            self.selection_mask = transformed_selection
            self.selection_changed.emit()
        session.matrix = QTransform(matrix)
        self._recompose(self.backing_rect, emit=False)
        self.changed.emit(self.backing_rect)
        return True

    def commit_transform(self, session: TransformSession) -> bool:
        entry = HistoryEntry(
            roots_before=session.before_roots,
            roots_after=self._tree_snapshot(),
            active_before=session.active_before,
            active_after=self.active_layer_id,
            origin_before=session.origin_before,
            origin_after=self.origin,
            canvas_before=session.canvas_before,
            canvas_after=self.canvas_rect,
            selection_before=session.selection_before,
            selection_after=QImage(self.selection_mask) if self.selection_mask is not None else None,
        )
        self._push_history(entry, self.backing_rect)
        return True

    def cancel_transform(self, session: TransformSession) -> None:
        first = next((node.image for node in self.iter_layers(session.before_roots) if node.image is not None), None)
        if first is None:
            return
        self.roots = [node.clone() for node in session.before_roots]
        self.active_layer_id = session.active_before
        self._origin_x, self._origin_y = session.origin_before
        self._canvas_rect = QRect(session.canvas_before)
        self.selection_mask = QImage(session.selection_before) if session.selection_before is not None else None
        self.image = transparent_image(first.width(), first.height())
        self._recompose(self.backing_rect, emit=False)
        self.layers_changed.emit()
        self.active_layer_changed.emit(self.active_layer_id)
        self.canvas_bounds_changed.emit(self.canvas_rect)
        self.selection_changed.emit()
        self.changed.emit(self.backing_rect)

    def _opaque_bounds(self, layers: Iterable[LayerNode]) -> QRect:
        bounds = QRect()
        for layer in layers:
            if layer.image is None:
                continue
            region = self._mask_bounds(layer.image)
            if region.isEmpty():
                continue
            world = region.translated(self._origin_x, self._origin_y)
            bounds = world if bounds.isEmpty() else bounds.united(world)
        return bounds

    def _mask_bounds(self, image: QImage) -> QRect:
        if image.isNull() or image.width() <= 0 or image.height() <= 0:
            return QRect()
        converted = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        data = bytes(converted.constBits()[: converted.sizeInBytes()])
        alpha = data[3::4]
        minimum_x = converted.width()
        minimum_y = converted.height()
        maximum_x = -1
        maximum_y = -1
        for y in range(converted.height()):
            row = alpha[y * converted.width() : (y + 1) * converted.width()]
            stripped_left = row.lstrip(b"\x00")
            if not stripped_left:
                continue
            left = len(row) - len(stripped_left)
            right = len(row.rstrip(b"\x00")) - 1
            minimum_x = min(minimum_x, left)
            maximum_x = max(maximum_x, right)
            minimum_y = min(minimum_y, y)
            maximum_y = max(maximum_y, y)
        if maximum_x < minimum_x or maximum_y < minimum_y:
            return QRect()
        return QRect(minimum_x, minimum_y, maximum_x - minimum_x + 1, maximum_y - minimum_y + 1)

    def iter_layers(self, nodes: list[LayerNode] | None = None) -> Iterator[LayerNode]:
        for node in self.roots if nodes is None else nodes:
            yield node
            yield from self.iter_layers(node.children)

    def find_layer(self, layer_id: str | None) -> LayerNode | None:
        if layer_id is None:
            return None
        return next((node for node in self.iter_layers() if node.layer_id == layer_id), None)

    def _find_container(self, layer_id: str) -> tuple[list[LayerNode], int] | None:
        def visit(nodes: list[LayerNode]) -> tuple[list[LayerNode], int] | None:
            for index, node in enumerate(nodes):
                if node.layer_id == layer_id:
                    return nodes, index
                found = visit(node.children)
                if found is not None:
                    return found
            return None

        return visit(self.roots)

    def set_active_layer(self, layer_id: str) -> bool:
        layer = self.find_layer(layer_id)
        if layer is None:
            return False
        if layer_id == self.active_layer_id:
            return True
        self.active_layer_id = layer_id
        self.active_layer_changed.emit(layer_id)
        return True

    def begin_stroke(self, sample: StrokeSample) -> None:
        if self._transaction is not None:
            self.end_stroke()
        layer = self.active_layer
        if layer is None:
            return
        self._transaction = StrokeTransaction(self, layer, self._brush, self._color)
        self._last_sample = self._normalized_sample(sample)
        self._distance_carry = 0.0
        self._draw_dabs((self._last_sample,), layer, self._brush, self._color)

    def add_sample(self, sample: StrokeSample) -> None:
        transaction = self._transaction
        if transaction is None or self._last_sample is None:
            return
        layer = self.find_layer(transaction.layer_id)
        brush = transaction.brush
        color = transaction.color
        if layer is None or brush is None or color is None:
            return
        current = self._normalized_sample(sample)
        dabs, carry = self._interpolate_segment(self._last_sample, current, self._distance_carry, brush)
        self._distance_carry = carry
        self._last_sample = current
        if dabs:
            self._draw_dabs(dabs, layer, brush, color)

    def replace_active_stroke(self, samples: Iterable[StrokeSample]) -> float | None:
        """Replace the provisional stroke without closing its undo transaction."""
        transaction = self._transaction
        if transaction is None:
            return None
        layer = self.find_layer(transaction.layer_id)
        brush = transaction.brush
        color = transaction.color
        source = tuple(self._normalized_sample(sample) for sample in samples)
        if layer is None or brush is None or color is None or not source:
            return None
        started = time.perf_counter()
        previous_dirty = transaction.restore_current()
        self._last_sample = source[-1]
        self._distance_carry = 0.0
        with self.defer_updates(), self.defer_composite():
            if not previous_dirty.isEmpty():
                self._recompose(previous_dirty)
            self._draw_dabs(self._stroke_dabs(source, brush), layer, brush, color)
        return (time.perf_counter() - started) * 1000.0

    def end_stroke(self) -> None:
        if self._transaction is None:
            return
        dirty = QRect(self._transaction.dirty)
        entry = self._transaction.finish()
        self._transaction = None
        self._last_sample = None
        self._distance_carry = 0.0
        if entry is not None:
            self._push_history(entry, dirty, emit_changed=False)

    def cancel_stroke(self) -> None:
        if self._transaction is None:
            return
        dirty = self._transaction.restore_current()
        self._transaction = None
        self._last_sample = None
        self._distance_carry = 0.0
        if not dirty.isEmpty():
            self._recompose(dirty)

    def replay(self, samples: Iterable[StrokeSample]) -> None:
        self.execute_bundle(StrokeBundle((StrokeCommand.from_samples(samples),), "Replay"))

    def execute_bundle(self, bundle: StrokeBundle) -> BundleResult:
        started = time.perf_counter()
        transaction = StrokeTransaction(self)
        previous_transaction = self._transaction
        self._transaction = transaction
        stroke_count = 0
        sample_count = 0
        dab_count = 0
        with self.defer_updates(), self.defer_composite():
            for command in bundle.strokes:
                if not command.samples:
                    continue
                layer = self.find_layer(command.layer_id) if command.layer_id else self.active_layer
                if layer is None or layer.is_group or layer.image is None:
                    continue
                brush = (command.brush or self._brush).normalized()
                color = QColor(command.color) if command.color else QColor(self._color)
                dabs = self._stroke_dabs(command.samples, brush)
                if not dabs:
                    continue
                transaction.outline = None
                self._draw_dabs(dabs, layer, brush, color)
                stroke_count += 1
                sample_count += len(command.samples)
                dab_count += len(dabs)
        entry = transaction.finish()
        self._transaction = previous_transaction
        if entry is not None:
            self._push_history(entry, transaction.dirty, emit_changed=False)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        result = BundleResult(self.revision, stroke_count, sample_count, dab_count, QRect(transaction.dirty), elapsed_ms)
        logging.getLogger("paintstudio.core").info(
            "stroke bundle complete name=%r strokes=%s samples=%s dabs=%s elapsed_ms=%.1f revision=%s",
            bundle.name,
            stroke_count,
            sample_count,
            dab_count,
            elapsed_ms,
            self.revision,
        )
        return result

    @contextmanager
    def defer_updates(self) -> Iterator[None]:
        self._defer_depth += 1
        try:
            yield
        finally:
            self._defer_depth -= 1
            if self._defer_depth == 0 and not self._deferred_dirty.isEmpty():
                dirty = QRect(self._deferred_dirty)
                self._deferred_dirty = QRect()
                self.changed.emit(dirty)

    @contextmanager
    def defer_composite(self) -> Iterator[None]:
        self._composite_defer_depth += 1
        try:
            yield
        finally:
            self._composite_defer_depth -= 1
            if self._composite_defer_depth == 0 and not self._deferred_composite_dirty.isEmpty():
                dirty = QRect(self._deferred_composite_dirty)
                self._deferred_composite_dirty = QRect()
                self._recompose(dirty)

    def add_paint_layer(self, name: str | None = None, *, parent_id: str | None = None) -> str:
        before, active_before = self._tree_snapshot(), self.active_layer_id
        layer = LayerNode(
            name or self._next_layer_name(),
            image=transparent_image(self.image.width(), self.image.height()),
        )
        parent = self.find_layer(parent_id)
        target = parent.children if parent is not None and parent.is_group else self.roots
        target.append(layer)
        self.active_layer_id = layer.layer_id
        self._finish_tree_change(before, active_before)
        return layer.layer_id

    def add_paint_layer_with_bundle(self, name: str, bundle: StrokeBundle) -> tuple[str, BundleResult]:
        """Add and paint one layer as a single structural undo operation."""
        before, active_before = self._tree_snapshot(), self.active_layer_id
        layer = LayerNode(name, image=transparent_image(self.image.width(), self.image.height()))
        self.roots.append(layer)
        self.active_layer_id = layer.layer_id
        targeted = StrokeBundle(
            tuple(replace(command, layer_id=layer.layer_id) for command in bundle.strokes),
            bundle.name,
        )
        result = self.execute_bundle(targeted)
        if result.stroke_count == 0:
            self.roots = before
            self.active_layer_id = active_before
            self._recompose(self.backing_rect)
            return layer.layer_id, result
        self._undo[-1] = HistoryEntry(
            roots_before=before,
            roots_after=self._tree_snapshot(),
            active_before=active_before,
            active_after=layer.layer_id,
        )
        self.layers_changed.emit()
        self.active_layer_changed.emit(layer.layer_id)
        return layer.layer_id, result

    def add_group(self, name: str | None = None) -> str:
        before, active_before = self._tree_snapshot(), self.active_layer_id
        group = LayerNode(name or self._next_group_name(), kind="group")
        self.roots.append(group)
        self._finish_tree_change(before, active_before)
        return group.layer_id

    def relocate_layer(self, layer_id: str, target_id: str | None, placement: str) -> bool:
        """Move a layer relative to another visible layer, or to the top of the root stack."""
        if placement not in {"above", "below", "inside", "root_top"}:
            return False
        source_found = self._find_container(layer_id)
        source = self.find_layer(layer_id)
        if source_found is None or source is None or target_id == layer_id:
            return False

        target_container: list[LayerNode]
        target_index: int
        if placement == "root_top" or target_id is None:
            target_container = self.roots
            target_index = len(target_container)
        else:
            target = self.find_layer(target_id)
            if target is None:
                return False
            if source.is_group and any(node.layer_id == target_id for node in self.iter_layers(source.children)):
                return False
            if placement == "inside":
                if not target.is_group:
                    return False
                target_container = target.children
                target_index = len(target_container)
            else:
                target_found = self._find_container(target_id)
                if target_found is None:
                    return False
                target_container, target_position = target_found
                target_index = target_position + (1 if placement == "above" else 0)

        source_container, source_index = source_found
        if source_container is target_container and source_index < target_index:
            target_index -= 1
        if source_container is target_container and source_index == target_index:
            return False

        before, active_before = self._tree_snapshot(), self.active_layer_id
        moved = source_container.pop(source_index)
        target_index = max(0, min(target_index, len(target_container)))
        target_container.insert(target_index, moved)
        self._finish_tree_change(before, active_before)
        return True

    def group_layer(self, layer_id: str) -> str | None:
        found = self._find_container(layer_id)
        if found is None:
            return None
        before, active_before = self._tree_snapshot(), self.active_layer_id
        container, index = found
        layer = container.pop(index)
        group = LayerNode("Group", kind="group", children=[layer])
        container.insert(index, group)
        self._finish_tree_change(before, active_before)
        return group.layer_id

    def group_layers(self, layer_ids: Iterable[str]) -> str | None:
        selected = set(layer_ids)
        if not selected:
            return None
        found = [self._find_container(layer_id) for layer_id in selected]
        if any(item is None for item in found):
            return None
        containers = [item[0] for item in found if item is not None]
        if not containers or any(container is not containers[0] for container in containers[1:]):
            return None
        container = containers[0]
        indices = sorted(index for item in found if item is not None for index in (item[1],))
        before, active_before = self._tree_snapshot(), self.active_layer_id
        children = [container[index] for index in indices]
        for index in reversed(indices):
            container.pop(index)
        group = LayerNode(self._next_group_name(), kind="group", children=children)
        container.insert(indices[0], group)
        self._finish_tree_change(before, active_before)
        return group.layer_id

    def clear_active_layer(self) -> bool:
        selected = self.find_layer(self.active_layer_id)
        if selected is None:
            return False
        targets = [node for node in self.iter_layers([selected]) if node.image is not None]
        if not targets:
            return False
        before, active_before = self._tree_snapshot(), self.active_layer_id
        for layer in targets:
            if self.selection_mask is None:
                layer.image.fill(0)
            else:
                painter = QPainter(layer.image)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
                painter.drawImage(0, 0, self.selection_mask)
                painter.end()
        self._finish_tree_change(before, active_before)
        logging.getLogger("paintstudio").info(
            "clear layers=%s selected=%s revision=%s", len(targets), self.selection_mask is not None, self.revision
        )
        return True

    def flood_fill(self, point: QPointF) -> bool:
        from .fill import contiguous_mask

        x, y = math.floor(point.x()), math.floor(point.y())
        if not self.canvas_rect.contains(x, y):
            return False
        layer = self.find_layer(self.active_layer_id)
        brush = self.brush_settings
        if layer is None or layer.image is None or (brush.eraser and layer.alpha_locked):
            return False
        started = time.perf_counter()
        crop = self.world_to_image_rect(self.canvas_rect)
        reference = self.image if brush.fill_reference == "visible" else layer.image
        mask = contiguous_mask(
            reference.copy(crop), x - self.canvas_rect.x(), y - self.canvas_rect.y(),
            brush.fill_threshold,
            self.selection_mask.copy(crop) if self.selection_mask is not None else None,
        )
        before, active_before = self._tree_snapshot(), self.active_layer_id
        paint = transparent_image(mask.width(), mask.height())
        paint.fill(self.brush_color)
        painter = QPainter(paint)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawImage(0, 0, mask)
        painter.end()
        painter = QPainter(layer.image)
        painter.setOpacity(brush.opacity)
        if brush.eraser:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
        elif layer.alpha_locked:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
        painter.drawImage(crop.topLeft(), paint)
        painter.end()
        self._finish_tree_change(before, active_before)
        logging.getLogger("paintstudio").info(
            "fill reference=%s threshold=%s selected=%s elapsed_ms=%.1f revision=%s",
            brush.fill_reference, brush.fill_threshold, self.selection_mask is not None,
            (time.perf_counter() - started) * 1000, self.revision,
        )
        return True

    def stamp_visible(self, name: str = "Visible Copy") -> str:
        before, active_before = self._tree_snapshot(), self.active_layer_id
        layer_image = transparent_image(self.image.width(), self.image.height())
        painter = QPainter(layer_image)
        painter.drawImage(self.world_to_image_rect(self._canvas_rect).topLeft(), self.canvas_image())
        painter.end()
        layer = LayerNode(name, image=layer_image)
        self.roots.append(layer)
        self.active_layer_id = layer.layer_id
        self._finish_tree_change(before, active_before)
        return layer.layer_id

    def duplicate_layer(self, layer_id: str) -> str | None:
        found = self._find_container(layer_id)
        if found is None:
            return None
        before, active_before = self._tree_snapshot(), self.active_layer_id
        container, index = found
        duplicate = container[index].clone()
        self._renew_ids(duplicate)
        duplicate.name = f"{duplicate.name} Copy"
        container.insert(index + 1, duplicate)
        paint = next((node for node in self.iter_layers([duplicate]) if not node.is_group), None)
        if paint is not None:
            self.active_layer_id = paint.layer_id
        self._finish_tree_change(before, active_before)
        return duplicate.layer_id

    def remove_layer(self, layer_id: str) -> bool:
        found = self._find_container(layer_id)
        removed = self.find_layer(layer_id)
        if found is None or removed is None:
            return False
        total_paint = sum(1 for node in self.iter_layers() if not node.is_group)
        removed_paint = sum(1 for node in self.iter_layers([removed]) if not node.is_group)
        if total_paint - removed_paint < 1:
            return False
        before, active_before = self._tree_snapshot(), self.active_layer_id
        container, index = found
        removed = container.pop(index)
        removed_ids = {node.layer_id for node in self.iter_layers([removed])}
        if self.active_layer_id in removed_ids:
            replacement = next((node for node in reversed(list(self.iter_layers())) if not node.is_group), None)
            if replacement is not None:
                self.active_layer_id = replacement.layer_id
        self._finish_tree_change(before, active_before)
        return True

    def move_layer(self, layer_id: str, delta: int) -> bool:
        found = self._find_container(layer_id)
        if found is None:
            return False
        container, index = found
        target = index + delta
        if target < 0 or target >= len(container):
            return False
        before, active_before = self._tree_snapshot(), self.active_layer_id
        container[index], container[target] = container[target], container[index]
        self._finish_tree_change(before, active_before)
        return True

    def set_layer_property(self, layer_id: str, property_name: str, value: object) -> bool:
        layer = self.find_layer(layer_id)
        if layer is None or property_name not in {
            "name",
            "visible",
            "opacity",
            "blend_mode",
            "alpha_locked",
            "clipping",
        }:
            return False
        if property_name == "opacity":
            value = max(0.0, min(float(value), 1.0))
        if getattr(layer, property_name) == value:
            return True
        before, active_before = self._tree_snapshot(), self.active_layer_id
        setattr(layer, property_name, value)
        self._finish_tree_change(before, active_before)
        return True

    def undo(self) -> bool:
        if self._transaction is not None or not self._undo:
            return False
        entry = self._undo.pop()
        dirty = self._apply_history(entry, forward=False)
        self._redo.append(entry)
        self._revision_changed(dirty)
        return True

    def history_state(self) -> tuple[list[HistoryEntry], list[HistoryEntry]]:
        return list(self._undo), list(self._redo)

    def restore_history(self, undo: list[HistoryEntry], redo: list[HistoryEntry]) -> None:
        # Call right after restore_layers, entries must describe that exact document.
        self._undo = list(undo)[-100:]
        self._redo = list(redo)
        self.history_changed.emit(self.can_undo, self.can_redo)

    def redo(self) -> bool:
        if self._transaction is not None or not self._redo:
            return False
        entry = self._redo.pop()
        dirty = self._apply_history(entry, forward=True)
        self._undo.append(entry)
        self._revision_changed(dirty)
        return True

    def replace_image(self, image: QImage) -> None:
        converted = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        if converted.isNull():
            raise ValueError("Cannot load an empty image")
        self._origin_x = 0
        self._origin_y = 0
        self._canvas_rect = QRect(0, 0, converted.width(), converted.height())
        self.selection_mask = None
        layer = LayerNode("Paint Layer 1", image=converted)
        self.roots = [layer]
        self.active_layer_id = layer.layer_id
        self.image = transparent_image(converted.width(), converted.height())
        self._undo.clear()
        self._redo.clear()
        self._recompose(self.backing_rect, emit=False)
        self._revision_changed(layers=True)
        self.canvas_bounds_changed.emit(self.canvas_rect)
        self.selection_changed.emit()

    def restore_layers(
        self,
        roots: list[LayerNode],
        active_layer_id: str,
        revision: int,
        *,
        origin: tuple[int, int] = (0, 0),
        canvas_rect: QRect | None = None,
        selection: QImage | None = None,
    ) -> None:
        paint_layers = [node for node in self.iter_layers(roots) if not node.is_group and node.image is not None]
        if not paint_layers:
            raise ValueError("A document needs at least one paint layer")
        first = paint_layers[0].image
        assert first is not None
        if any(
            node.image is not None and node.image.size() != first.size()
            for node in paint_layers
        ):
            raise ValueError("All paint layers must share one backing size")
        self._origin_x, self._origin_y = int(origin[0]), int(origin[1])
        self._canvas_rect = QRect(canvas_rect) if canvas_rect is not None else QRect(
            self._origin_x,
            self._origin_y,
            first.width(),
            first.height(),
        )
        self.roots = [node.clone() for node in roots]
        self.selection_mask = QImage(selection) if selection is not None and not selection.isNull() else None
        self.active_layer_id = active_layer_id if self.find_layer(active_layer_id) is not None else paint_layers[-1].layer_id
        self.image = transparent_image(first.width(), first.height())
        self.revision = revision
        self._undo.clear()
        self._redo.clear()
        self._ensure_world_rect(self._canvas_rect)
        self._recompose(self.backing_rect, emit=False)
        self.history_changed.emit(False, False)
        self.layers_changed.emit()
        self.active_layer_changed.emit(self.active_layer_id)
        self.canvas_bounds_changed.emit(self.canvas_rect)
        self.selection_changed.emit()
        self.changed.emit(self.backing_rect)

    def restore_flat_image(self, image: QImage, revision: int) -> None:
        converted = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        if converted.isNull():
            raise ValueError("Cannot restore an empty image")
        layer = LayerNode("Paint Layer 1", image=converted)
        self.restore_layers([layer], layer.layer_id, revision)

    def reset(self, width: int = 2048, height: int = 2048) -> None:
        image = transparent_image(width, height)
        self.replace_image(image)

    def pixel_hash(self) -> str:
        image = self.canvas_image()
        bits = image.constBits()
        return hashlib.sha256(bytes(bits[: image.sizeInBytes()])).hexdigest()

    def preview_canvas_rect(self, rect: QRect) -> bool:
        normalized = self._valid_canvas_rect(rect)
        if normalized == self._canvas_rect:
            return False
        self._ensure_world_rect(normalized)
        self._canvas_rect = normalized
        self.canvas_bounds_changed.emit(self.canvas_rect)
        return True

    def commit_canvas_rect(self, before: QRect) -> bool:
        before = self._valid_canvas_rect(before)
        if before == self._canvas_rect:
            return False
        entry = HistoryEntry(
            origin_before=self.origin,
            origin_after=self.origin,
            canvas_before=QRect(before),
            canvas_after=self.canvas_rect,
        )
        self._push_history(entry, self.canvas_rect)
        return True

    def trim_to_canvas(self) -> bool:
        crop = self.world_to_image_rect(self._canvas_rect)
        if crop == self.image.rect():
            return False
        before, active_before = self._tree_snapshot(), self.active_layer_id
        origin_before = self.origin
        canvas_before = self.canvas_rect
        selection_before = QImage(self.selection_mask) if self.selection_mask is not None else None
        for node in self.iter_layers():
            if node.image is not None:
                node.image = node.image.copy(crop)
        if self.selection_mask is not None:
            self.selection_mask = self.selection_mask.copy(crop)
        self.image = self.image.copy(crop)
        self._origin_x = self._canvas_rect.x()
        self._origin_y = self._canvas_rect.y()
        self._canvas_rect = QRect(self._origin_x, self._origin_y, crop.width(), crop.height())
        self._recompose(self.backing_rect, emit=False)
        entry = HistoryEntry(
            roots_before=before,
            roots_after=self._tree_snapshot(),
            active_before=active_before,
            active_after=self.active_layer_id,
            origin_before=origin_before,
            origin_after=self.origin,
            canvas_before=canvas_before,
            canvas_after=self.canvas_rect,
            selection_before=selection_before,
            selection_after=QImage(self.selection_mask) if self.selection_mask is not None else None,
        )
        self.layers_changed.emit()
        self.canvas_bounds_changed.emit(self.canvas_rect)
        self.selection_changed.emit()
        self._push_history(entry, self.backing_rect)
        return True

    def _valid_canvas_rect(self, rect: QRect) -> QRect:
        rect = QRect(rect).normalized()
        return QRect(rect.x(), rect.y(), max(1, rect.width()), max(1, rect.height()))

    def _ensure_world_rect(self, rect: QRect) -> None:
        rect = QRect(rect)
        current = self.backing_rect
        if current.contains(rect):
            return
        left = current.left() if rect.left() >= current.left() else math.floor(rect.left() / TILE_SIZE) * TILE_SIZE
        top = current.top() if rect.top() >= current.top() else math.floor(rect.top() / TILE_SIZE) * TILE_SIZE
        current_right = current.right() + 1
        current_bottom = current.bottom() + 1
        right = (
            current_right
            if rect.right() < current_right
            else math.ceil((rect.right() + 1) / TILE_SIZE) * TILE_SIZE
        )
        bottom = (
            current_bottom
            if rect.bottom() < current_bottom
            else math.ceil((rect.bottom() + 1) / TILE_SIZE) * TILE_SIZE
        )
        expanded = QRect(left, top, right - left, bottom - top)
        offset = QPointF(current.x() - expanded.x(), current.y() - expanded.y()).toPoint()
        for node in self.iter_layers():
            if node.image is None:
                continue
            replacement = transparent_image(expanded.width(), expanded.height())
            painter = QPainter(replacement)
            painter.drawImage(offset, node.image)
            painter.end()
            node.image = replacement
        if self.selection_mask is not None:
            replacement = transparent_image(expanded.width(), expanded.height())
            painter = QPainter(replacement)
            painter.drawImage(offset, self.selection_mask)
            painter.end()
            self.selection_mask = replacement
        composite = transparent_image(expanded.width(), expanded.height())
        painter = QPainter(composite)
        painter.drawImage(offset, self.image)
        painter.end()
        self.image = composite
        self._origin_x = expanded.x()
        self._origin_y = expanded.y()

    def _normalized_sample(self, sample: StrokeSample) -> StrokeSample:
        return StrokeSample(
            sample.x,
            sample.y,
            min(max(sample.pressure, 0.01), 1.0),
            sample.timestamp_ms,
            sample.tilt_x,
            sample.tilt_y,
        )

    def _stroke_dabs(self, samples: tuple[StrokeSample, ...], brush: BrushSettings) -> list[StrokeSample]:
        normalized = [self._normalized_sample(sample) for sample in samples]
        if not normalized:
            return []
        dabs = [normalized[0]]
        carry = 0.0
        for previous, current in zip(normalized, normalized[1:]):
            segment, carry = self._interpolate_segment(previous, current, carry, brush)
            dabs.extend(segment)
        return dabs

    def _interpolate_segment(
        self,
        previous: StrokeSample,
        current: StrokeSample,
        carry: float,
        brush: BrushSettings,
    ) -> tuple[list[StrokeSample], float]:
        dx, dy = current.x - previous.x, current.y - previous.y
        distance = math.hypot(dx, dy)
        if distance <= 0.0001:
            return [], carry
        average_pressure = (previous.pressure + current.pressure) * 0.5 if brush.pressure_size else 1.0
        spacing = max(0.5, brush.size * max(0.05, average_pressure) * brush.spacing)
        next_distance = spacing - carry if carry > 0.0 else spacing
        dabs: list[StrokeSample] = []
        while next_distance <= distance:
            t = next_distance / distance
            dabs.append(
                StrokeSample(
                    previous.x + dx * t,
                    previous.y + dy * t,
                    previous.pressure + (current.pressure - previous.pressure) * t,
                    previous.timestamp_ms + (current.timestamp_ms - previous.timestamp_ms) * t,
                    previous.tilt_x + (current.tilt_x - previous.tilt_x) * t,
                    previous.tilt_y + (current.tilt_y - previous.tilt_y) * t,
                )
            )
            next_distance += spacing
        traveled = distance - (next_distance - spacing)
        return dabs, max(0.0, min(spacing, traveled))

    def _draw_dabs(
        self,
        dabs: Iterable[StrokeSample],
        layer: LayerNode,
        brush: BrushSettings,
        color: QColor,
    ) -> None:
        if self._transaction is None or layer.image is None:
            return
        source = tuple(dabs)
        if not source:
            return
        if brush.engine == "blur" and not brush.eraser:
            self._draw_blur_dabs(source, layer, brush)
            return
        world_bounds = QRect()
        for sample in source:
            pressure_size = sample.pressure if brush.pressure_size else 1.0
            radius = max(1.0, brush.size * pressure_size) * 0.5
            if brush.engine == "stamp":
                radius *= 1.0 + brush.size_variation + brush.scatter
            elif brush.engine == "paint" and brush.tilt_stretch:
                radius *= 2.6
            bounds = QRect(
                math.floor(sample.x - radius - 2.0),
                math.floor(sample.y - radius - 2.0),
                math.ceil(radius * 2.0 + 5.0),
                math.ceil(radius * 2.0 + 5.0),
            )
            world_bounds = bounds if world_bounds.isEmpty() else world_bounds.united(bounds)
        self._ensure_world_rect(world_bounds)

        prepared: list[tuple[QPointF, StrokeSample, float, QRect]] = []
        dirty = QRect()
        for sample in source:
            pressure_size = sample.pressure if brush.pressure_size else 1.0
            diameter = max(1.0, brush.size * pressure_size)
            radius = diameter * 0.5
            if brush.engine == "stamp":
                radius *= 1.0 + brush.size_variation + brush.scatter
            extent = radius * 2.6 if brush.engine == "paint" and brush.tilt_stretch else radius
            left = math.floor(sample.x - extent - 2.0)
            top = math.floor(sample.y - extent - 2.0)
            right = math.ceil(sample.x + extent + 2.0)
            bottom = math.ceil(sample.y + extent + 2.0)
            world_rect = QRect(
                left,
                top,
                right - left + 1,
                bottom - top + 1,
            ).intersected(self.backing_rect)
            if world_rect.isEmpty():
                continue
            prepared.append((self.world_to_image_point(QPointF(sample.x, sample.y)), sample, radius, world_rect))
            dirty = world_rect if dirty.isEmpty() else dirty.united(world_rect)
        if not prepared:
            return
        if brush.fill_mode == "border" and brush.engine in OUTLINE_ENGINES:
            self._draw_outline_dabs(prepared, dirty, layer, brush, color)
            return
        self._transaction.capture_before(layer, dirty)
        masked = self.selection_mask is not None
        buffered = masked or brush.eraser
        image_dirty = self.world_to_image_rect(dirty)
        target = transparent_image(image_dirty.width(), image_dirty.height()) if buffered else layer.image
        draw_offset = QPointF(image_dirty.x(), image_dirty.y()) if buffered else QPointF()
        painter = QPainter(target)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if brush.eraser and layer.alpha_locked:
            painter.end()
            return
        if layer.alpha_locked and not buffered:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
        self._paint_dabs(painter, [(point - draw_offset, sample, radius) for point, sample, radius, _ in prepared], brush, color)
        painter.end()
        if buffered:
            mask = self.selection_mask.copy(image_dirty) if self.selection_mask is not None else None
        if masked and mask is not None:
            painter = QPainter(target)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.drawImage(0, 0, mask)
            painter.end()
        if buffered:
            painter = QPainter(layer.image)
            if brush.eraser:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
            elif layer.alpha_locked:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
            painter.drawImage(image_dirty.topLeft(), target)
            painter.end()
        self._recompose(dirty)

    def _draw_outline_dabs(
        self,
        prepared: list[tuple[QPointF, StrokeSample, float, QRect]],
        dirty: QRect,
        layer: LayerNode,
        brush: BrushSettings,
        color: QColor,
    ) -> None:
        # Rebuilt from pre-stroke pixels each redraw so overlapping dabs share one outline.
        transaction = self._transaction
        assert transaction is not None and layer.image is not None
        if brush.eraser and layer.alpha_locked:
            return
        outline = transaction.outline
        if outline is None or outline.layer_id != layer.layer_id:
            outline = transaction.outline = OutlineStroke(layer.layer_id)
        tiles = [
            (tile_x, tile_y)
            for tile_y in range(dirty.top() // TILE_SIZE, dirty.bottom() // TILE_SIZE + 1)
            for tile_x in range(dirty.left() // TILE_SIZE, dirty.right() // TILE_SIZE + 1)
        ]
        for key in tiles:
            if key not in outline.base_tiles:
                outline.base_tiles[key] = layer.image.copy(self.world_to_image_rect(self.tile_rect(*key)))
        transaction.capture_before(layer, dirty)
        outline.dabs.extend((sample, radius, rect) for _point, sample, radius, rect in prepared)

        image_dirty = self.world_to_image_rect(dirty)
        painter = QPainter(layer.image)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.setClipRect(image_dirty)
        for key in tiles:
            painter.drawImage(self.world_to_image_rect(self.tile_rect(*key)).topLeft(), outline.base_tiles[key])
        painter.end()

        offset = QPointF(image_dirty.topLeft())
        touching = [
            (self.world_to_image_point(QPointF(sample.x, sample.y)) - offset, sample, radius)
            for sample, radius, rect in outline.dabs
            if rect.intersects(dirty)
        ]
        stroke = transparent_image(image_dirty.width(), image_dirty.height())
        painter = QPainter(stroke)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._paint_dabs(painter, touching, brush, color)
        inner = [(point, sample, radius - brush.border_width) for point, sample, radius in touching if radius - brush.border_width >= 0.5]
        if inner:
            knockout = replace(brush, opacity=1.0, flow=1.0, pressure_opacity=False, texture_strength=0.0)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
            self._paint_dabs(painter, inner, knockout, QColor("black"))
        painter.end()
        if self.selection_mask is not None:
            painter = QPainter(stroke)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.drawImage(0, 0, self.selection_mask.copy(image_dirty))
            painter.end()
        painter = QPainter(layer.image)
        if brush.eraser:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
        elif layer.alpha_locked:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
        painter.drawImage(image_dirty.topLeft(), stroke)
        painter.end()
        self._recompose(dirty)

    def _paint_dabs(
        self,
        painter: QPainter,
        dabs: list[tuple[QPointF, StrokeSample, float]],
        brush: BrushSettings,
        color: QColor,
    ) -> None:
        previous_stamp_point: QPointF | None = None
        for image_point, sample, radius in dabs:
            image_point = QPointF(image_point)
            alpha_pressure = sample.pressure if brush.pressure_opacity else 1.0
            dab_color = QColor(color)
            dab_color.setAlphaF(brush.opacity * brush.flow * alpha_pressure)
            rotation = 0.0
            scale_x = 1.0
            scale_y = 1.0
            paint_tilt_transform = False
            if brush.engine == "stamp":
                size_noise = self._brush_noise(sample, 1)
                if brush.follow_rotation and previous_stamp_point is not None:
                    rotation = math.degrees(
                        math.atan2(
                            image_point.y() - previous_stamp_point.y(),
                            image_point.x() - previous_stamp_point.x(),
                        )
                    )
                rotation += self._brush_noise(sample, 2) * brush.rotation_variation
                scatter_angle = self._brush_noise(sample, 3) * math.pi
                scatter_distance = abs(self._brush_noise(sample, 4)) * brush.scatter * radius
                image_point += QPointF(math.cos(scatter_angle), math.sin(scatter_angle)) * scatter_distance
                radius *= max(0.05, 1.0 + size_noise * brush.size_variation)
                scale_x = max(0.05, 1.0 + self._brush_noise(sample, 7) * brush.size_x_variation)
                scale_y = max(0.05, 1.0 + self._brush_noise(sample, 8) * brush.size_y_variation)
                if brush.flip_x and self._brush_noise(sample, 9) < 0:
                    scale_x *= -1
                if brush.flip_y and self._brush_noise(sample, 10) < 0:
                    scale_y *= -1
                hue, saturation, value, alpha = dab_color.getHsvF()
                hue = 0.0 if hue < 0 else hue
                hue = (hue + self._brush_noise(sample, 5) * brush.hue_variation / 360.0) % 1.0
                saturation = max(
                    0.0,
                    min(1.0, saturation + self._brush_noise(sample, 11) * brush.saturation_variation),
                )
                value = max(0.0, min(1.0, value + self._brush_noise(sample, 12) * brush.value_variation))
                alpha *= max(0.0, 1.0 - abs(self._brush_noise(sample, 6)) * brush.alpha_variation)
                dab_color = QColor.fromHsvF(hue, saturation, value, alpha)
            elif brush.engine == "paint" and brush.tilt_stretch:
                tilt = min(1.0, math.hypot(sample.tilt_x, sample.tilt_y) / 60.0)
                if tilt > 0.01:
                    painter.save()
                    painter.translate(image_point)
                    painter.rotate(math.degrees(math.atan2(sample.tilt_y, sample.tilt_x)))
                    painter.scale(1.0 + tilt * 1.6, 1.0 / (1.0 + tilt * 0.35))
                    painter.translate(-image_point)
                    paint_tilt_transform = True
            shaped_tip = brush.engine == "stamp" or brush.tip_shape in {"square", "diamond", "triangle"}
            if brush.hardness >= 0.995:
                painter.setPen(dab_color)
                painter.setBrush(dab_color)
            else:
                # Shaped tips draw around a translated origin, so their gradient is centered there.
                gradient = QRadialGradient(QPointF() if shaped_tip else image_point, radius)
                gradient.setColorAt(0.0, dab_color)
                gradient.setColorAt(brush.hardness, dab_color)
                edge = QColor(dab_color)
                edge.setAlpha(0)
                gradient.setColorAt(1.0, edge)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(gradient)
            if brush.engine == "stamp":
                self._draw_stamp_tip(
                    painter,
                    image_point,
                    radius,
                    brush.tip_shape,
                    rotation,
                    scale_x,
                    scale_y,
                    dab_color,
                )
                previous_stamp_point = QPointF(image_point)
            elif brush.tip_shape in {"square", "diamond", "triangle"}:
                self._draw_stamp_tip(painter, image_point, radius, brush.tip_shape, 0.0)
            else:
                painter.drawEllipse(image_point, radius, radius)
                if brush.engine == "paint" and brush.tip_shape == "texture" and brush.texture_strength > 0.0:
                    texture = QColor(dab_color)
                    texture.setAlphaF(dab_color.alphaF() * brush.texture_strength * 0.45)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(texture)
                    for index in range(5):
                        angle = self._brush_noise(sample, 10 + index) * math.pi
                        distance = abs(self._brush_noise(sample, 20 + index)) * radius * 0.75
                        grain = max(0.8, radius * (0.035 + 0.025 * index))
                        painter.drawEllipse(
                            image_point + QPointF(math.cos(angle), math.sin(angle)) * distance,
                            grain,
                            grain * 0.55,
                        )
            if paint_tilt_transform:
                painter.restore()

    def _brush_noise(self, sample: StrokeSample, salt: int) -> float:
        value = math.sin(sample.x * 12.9898 + sample.y * 78.233 + sample.timestamp_ms * 0.013 + salt * 37.719)
        return value

    def _draw_stamp_tip(
        self,
        painter: QPainter,
        center: QPointF,
        radius: float,
        shape: str,
        rotation: float,
        scale_x: float = 1.0,
        scale_y: float = 1.0,
        color: QColor | None = None,
    ) -> None:
        painter.save()
        painter.translate(center)
        painter.rotate(rotation)
        painter.scale(scale_x, scale_y)
        if shape == "custom":
            source = QImage(self.stamp_tip_image)
            if color is not None:
                tinted = transparent_image(source.width(), source.height())
                tint_painter = QPainter(tinted)
                tint_painter.fillRect(tinted.rect(), color)
                tint_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
                tint_painter.drawImage(0, 0, source)
                tint_painter.end()
                source = tinted
            painter.drawImage(QRectF(-radius, -radius, radius * 2.0, radius * 2.0), source)
        elif shape == "circle":
            painter.drawEllipse(QPointF(), radius, radius)
        elif shape == "square":
            painter.drawRect(QRectF(-radius, -radius, radius * 2.0, radius * 2.0))
        else:
            sides = 4 if shape == "diamond" else 3 if shape == "triangle" else 10
            path = QPainterPath()
            for index in range(sides):
                angle = math.radians(-90 + index * 360.0 / sides)
                distance = radius if shape != "star" or index % 2 == 0 else radius * 0.42
                point = QPointF(math.cos(angle) * distance, math.sin(angle) * distance)
                path.moveTo(point) if index == 0 else path.lineTo(point)
            path.closeSubpath()
            painter.drawPath(path)
        painter.restore()

    def _draw_blur_dabs(self, dabs: tuple[StrokeSample, ...], layer: LayerNode, brush: BrushSettings) -> None:
        dirty = QRect()
        for sample in dabs:
            pressure = sample.pressure if brush.pressure_size else 1.0
            radius = max(2.0, brush.size * pressure * 0.5)
            world_rect = QRectF(
                sample.x - radius,
                sample.y - radius,
                radius * 2.0,
                radius * 2.0,
            ).toAlignedRect()
            self._ensure_world_rect(world_rect)
            image_rect = self.world_to_image_rect(world_rect)
            self._transaction.capture_before(layer, world_rect)
            original = layer.image.copy(image_rect)
            divisor = max(2, round(1 + brush.blur_radius * brush.blur_strength * 0.28))
            reduced = original.scaled(
                max(1, original.width() // divisor),
                max(1, original.height() // divisor),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            blurred = reduced.scaled(
                original.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            mask = transparent_image(original.width(), original.height())
            mask_color = QColor(255, 255, 255)
            opacity_pressure = sample.pressure if brush.pressure_opacity else 1.0
            mask_color.setAlphaF(brush.blur_strength * opacity_pressure)
            painter = QPainter(mask)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(mask_color)
            painter.drawEllipse(QRectF(mask.rect()))
            painter.end()
            if self.selection_mask is not None:
                painter = QPainter(mask)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
                painter.drawImage(0, 0, self.selection_mask.copy(image_rect))
                painter.end()
            painter = QPainter(blurred)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.drawImage(0, 0, mask)
            painter.end()
            painter = QPainter(layer.image)
            if layer.alpha_locked:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
            painter.drawImage(image_rect.topLeft(), blurred)
            painter.end()
            dirty = world_rect if dirty.isEmpty() else dirty.united(world_rect)
        if not dirty.isEmpty():
            self._recompose(dirty)

    def commit_shape(self, start: QPointF, end: QPointF) -> bool:
        layer = self.active_layer
        brush = self._brush
        if layer is None or layer.image is None or (brush.eraser and layer.alpha_locked):
            return False
        world_rect = QRectF(start, end).normalized().toAlignedRect()
        if world_rect.width() < 1 or world_rect.height() < 1:
            return False
        pad = brush.border_width + 3
        dirty = world_rect.adjusted(-pad, -pad, pad, pad)
        self._ensure_world_rect(dirty)
        transaction = StrokeTransaction(self)
        transaction.capture_before(layer, dirty)
        image_dirty = self.world_to_image_rect(dirty)
        local_rect = QRectF(self.world_to_image_rect(world_rect)).translated(-image_dirty.x(), -image_dirty.y())
        target = transparent_image(image_dirty.width(), image_dirty.height())
        painter = QPainter(target)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = QColor(self._color)
        color.setAlphaF(brush.opacity)
        if brush.fill_mode == "border":
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(color, brush.border_width))
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
        if brush.primitive == "ellipse":
            painter.drawEllipse(local_rect)
        else:
            painter.drawRoundedRect(local_rect, brush.corner_radius, brush.corner_radius)
        painter.end()
        if self.selection_mask is not None:
            painter = QPainter(target)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.drawImage(0, 0, self.selection_mask.copy(image_dirty))
            painter.end()
        painter = QPainter(layer.image)
        if brush.eraser:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
        elif layer.alpha_locked:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
        painter.drawImage(image_dirty.topLeft(), target)
        painter.end()
        self._transaction = transaction
        self._recompose(dirty)
        self.end_stroke()
        return True

    def _recompose(self, dirty: QRect, *, emit: bool = True) -> None:
        dirty = dirty.intersected(self.backing_rect)
        if dirty.isEmpty():
            return
        if self._composite_defer_depth:
            self._deferred_composite_dirty = (
                dirty if self._deferred_composite_dirty.isEmpty() else self._deferred_composite_dirty.united(dirty)
            )
            return
        image_dirty = self.world_to_image_rect(dirty)
        composite = self._compose_nodes(self.roots, image_dirty)
        painter = QPainter(self.image)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.drawImage(image_dirty.topLeft(), composite)
        painter.end()
        if emit:
            if self._defer_depth:
                self._deferred_dirty = dirty if self._deferred_dirty.isEmpty() else self._deferred_dirty.united(dirty)
            else:
                self.changed.emit(dirty)

    def _compose_nodes(self, nodes: list[LayerNode], rect: QRect) -> QImage:
        result = transparent_image(rect.width(), rect.height())
        for node in nodes:
            if not node.visible or node.opacity <= 0.0:
                continue
            if node.is_group:
                source = self._compose_nodes(node.children, rect)
            elif node.image is not None:
                source = node.image.copy(rect)
            else:
                continue
            if node.clipping and not result.isNull():
                mask = QImage(result)
                clip_painter = QPainter(source)
                clip_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
                clip_painter.drawImage(0, 0, mask)
                clip_painter.end()
            painter = QPainter(result)
            painter.setOpacity(node.opacity)
            painter.setCompositionMode(BLEND_COMPOSITION.get(node.blend_mode, BLEND_COMPOSITION["Normal"]))
            painter.drawImage(0, 0, source)
            painter.end()
        return result

    def _push_history(
        self,
        entry: HistoryEntry,
        dirty: QRect | None = None,
        *,
        emit_changed: bool = True,
    ) -> None:
        self._undo.append(entry)
        if len(self._undo) > 100:
            del self._undo[0]
        self._redo.clear()
        self._revision_changed(dirty, emit_changed=emit_changed)

    def _apply_history(self, entry: HistoryEntry, *, forward: bool) -> QRect:
        if entry.patches is not None:
            dirty = QRect()
            for patch in entry.patches:
                layer = self.find_layer(patch.layer_id)
                if layer is None or layer.image is None:
                    continue
                image = patch.after if forward else patch.before
                rect = QRect(patch.x, patch.y, image.width(), image.height())
                self._ensure_world_rect(rect)
                self._replace_tile(layer.image, self.world_to_image_rect(rect), image)
                dirty = rect if dirty.isEmpty() else dirty.united(rect)
            self._recompose(dirty, emit=False)
        else:
            source = entry.roots_after if forward else entry.roots_before
            if source is not None:
                origin = entry.origin_after if forward else entry.origin_before
                canvas = entry.canvas_after if forward else entry.canvas_before
                first = next((node.image for node in self.iter_layers(source) if node.image is not None), None)
                if first is None:
                    return QRect()
                self._origin_x, self._origin_y = origin or self.origin
                self._canvas_rect = QRect(canvas) if canvas is not None else self.canvas_rect
                self.roots = [node.clone() for node in source]
                self.active_layer_id = entry.active_after if forward else entry.active_before
                self.image = transparent_image(first.width(), first.height())
                self._recompose(self.backing_rect, emit=False)
                self.layers_changed.emit()
                self.active_layer_changed.emit(self.active_layer_id or "")
                self.canvas_bounds_changed.emit(self.canvas_rect)
                selection = entry.selection_after if forward else entry.selection_before
                if entry.selection_before is not None or entry.selection_after is not None:
                    self.selection_mask = QImage(selection) if selection is not None else None
                    self.selection_changed.emit()
                dirty = self.backing_rect
            elif entry.selection_before is not None or entry.selection_after is not None:
                selection = entry.selection_after if forward else entry.selection_before
                self.selection_mask = QImage(selection) if selection is not None else None
                self.selection_changed.emit()
                dirty = self.backing_rect
            else:
                canvas = entry.canvas_after if forward else entry.canvas_before
                if canvas is None:
                    return QRect()
                self._ensure_world_rect(canvas)
                self._canvas_rect = QRect(canvas)
                self.canvas_bounds_changed.emit(self.canvas_rect)
                dirty = self.canvas_rect
        return dirty

    def _tree_snapshot(self) -> list[LayerNode]:
        return [node.clone() for node in self.roots]

    def _finish_tree_change(self, before: list[LayerNode], active_before: str) -> None:
        entry = HistoryEntry(
            roots_before=before,
            roots_after=self._tree_snapshot(),
            active_before=active_before,
            active_after=self.active_layer_id,
            origin_before=self.origin,
            origin_after=self.origin,
            canvas_before=self.canvas_rect,
            canvas_after=self.canvas_rect,
        )
        self._recompose(self.backing_rect, emit=False)
        self.layers_changed.emit()
        self.active_layer_changed.emit(self.active_layer_id)
        self._push_history(entry)

    def _revision_changed(
        self,
        dirty: QRect | None = None,
        *,
        layers: bool = False,
        emit_changed: bool = True,
    ) -> None:
        self.revision += 1
        self.history_changed.emit(self.can_undo, self.can_redo)
        if layers:
            self.layers_changed.emit()
            self.active_layer_changed.emit(self.active_layer_id)
        if emit_changed:
            self.changed.emit(dirty if dirty is not None and not dirty.isEmpty() else self.backing_rect)
        self.stroke_finished.emit(self.revision)

    def _replace_tile(self, target: QImage, rect: QRect, tile: QImage) -> None:
        painter = QPainter(target)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.drawImage(rect.topLeft(), tile)
        painter.end()

    def _next_layer_name(self) -> str:
        return f"Paint Layer {sum(1 for node in self.iter_layers() if not node.is_group) + 1}"

    def _next_group_name(self) -> str:
        return f"Group {sum(1 for node in self.iter_layers() if node.is_group) + 1}"

    def _renew_ids(self, node: LayerNode) -> None:
        from .layers import new_layer_id

        node.layer_id = new_layer_id()
        for child in node.children:
            self._renew_ids(child)
