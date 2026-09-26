from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
import ctypes

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QIcon, QImage, QKeySequence, QPainter, QTransform  # noqa: E402
from PySide6.QtNetwork import QLocalSocket  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from paint_studio.canvas import CanvasWidget  # noqa: E402
from paint_studio.backend import PaintingBackend  # noqa: E402
from paint_studio.app import (  # noqa: E402
    PAINT_STUDIO_TABLET_FLAGS,
    TABLET_DISABLE_PENBARRELFEEDBACK,
    TABLET_DISABLE_PENTAPFEEDBACK,
    TABLET_DISABLE_PRESSANDHOLD,
    WM_TABLET_QUERYSYSTEMGESTURESTATUS,
    InstanceServer,
    NativePenFeedbackFilter,
    _WindowsMessage,
    application_icon_path,
)
from paint_studio.core import (  # noqa: E402
    BRUSH_PRESETS,
    BrushSettings,
    PaintDocument,
    StrokeBundle,
    StrokeCommand,
    StrokeSample,
    clean_stroke,
)
from paint_studio.panels import BrushPanel, CanvasOverview, LayerRow, LayersPanel  # noqa: E402
from paint_studio.storage import (  # noqa: E402
    DocumentBundleStore,
    RecoverySnapshot,
    RecoveryStore,
    RecoveryWriter,
    default_data_root,
    default_document_path,
    default_documents_root,
)
from paint_studio.window import MainWindow  # noqa: E402


APP = QApplication.instance() or QApplication([])


SAMPLES = [StrokeSample(100 + index * 4.5, 100 + index * 2.25, 0.2 + index / 80.0, index * 4.0) for index in range(48)]


class PaintCoreChecks(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows native tablet message")
    def test_filter_disables_legacy_pen_feedback(self) -> None:
        native_filter = NativePenFeedbackFilter()
        message = _WindowsMessage()
        message.message = WM_TABLET_QUERYSYSTEMGESTURESTATUS
        handled, result = native_filter.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message))
        self.assertTrue(handled)
        self.assertEqual(result, PAINT_STUDIO_TABLET_FLAGS)
        self.assertEqual(
            result,
            TABLET_DISABLE_PRESSANDHOLD | TABLET_DISABLE_PENTAPFEEDBACK | TABLET_DISABLE_PENBARRELFEEDBACK,
        )

    def test_stroke_replay_is_deterministic(self) -> None:
        first = PaintDocument(512, 512)
        second = PaintDocument(512, 512)
        first.set_brush(color=QColor("#e3008c"), size=36)
        second.set_brush(color=QColor("#e3008c"), size=36)
        first.replay(SAMPLES)
        second.replay(SAMPLES)
        self.assertEqual(first.pixel_hash(), second.pixel_hash())

    def test_smart_shape_preserves_arbitrary_corners_curves_and_spikes(self) -> None:
        def polygon(sides: int, *, radius_x: float = 72.0, radius_y: float = 56.0) -> list[StrokeSample]:
            vertices = [
                QPointF(
                    120.0 + math.cos(-math.pi * 0.5 + index * math.tau / sides) * radius_x,
                    120.0 + math.sin(-math.pi * 0.5 + index * math.tau / sides) * radius_y,
                )
                for index in range(sides)
            ]
            samples: list[StrokeSample] = []
            steps = max(6, 84 // sides)
            for side, (start, end) in enumerate(zip(vertices, (*vertices[1:], vertices[0]))):
                for step in range(steps):
                    amount = step / steps
                    wobble = math.sin((side * steps + step) * 2.31) * 1.35
                    samples.append(
                        StrokeSample(
                            start.x() + (end.x() - start.x()) * amount + wobble,
                            start.y() + (end.y() - start.y()) * amount - wobble * 0.55,
                            0.35 + 0.6 * (len(samples) % 17) / 16.0,
                            float(len(samples)),
                        )
                    )
            samples.append(StrokeSample(vertices[0].x() + 1.5, vertices[0].y() - 0.8, 0.8, len(samples)))
            return samples

        started = time.perf_counter()
        for sides in (3, 4, 6, 7, 10):
            shape = clean_stroke(polygon(sides))
            self.assertIsNotNone(shape)
            assert shape is not None
            self.assertTrue(shape.closed)
            self.assertGreaterEqual(shape.corner_count, max(3, sides - 2))
            self.assertGreaterEqual(shape.straight_segment_count, max(2, sides - 3))

        oval = [
            StrokeSample(
                160.0 + math.cos(index * math.tau / 120.0) * 84.0 + math.sin(index * 1.73) * 1.8,
                130.0 + math.sin(index * math.tau / 120.0) * 47.0 + math.cos(index * 2.17) * 1.4,
                0.4 + index / 240.0,
                float(index),
            )
            for index in range(120)
        ]
        oval.append(StrokeSample(oval[0].x + 1.0, oval[0].y, 0.9, 120.0))
        oval_shape = clean_stroke(oval)
        self.assertIsNotNone(oval_shape)
        assert oval_shape is not None
        self.assertEqual(oval_shape.kind, "ellipse")

        mixed: list[StrokeSample] = []
        for index in range(32):
            angle = math.pi - index * math.pi / 31.0
            mixed.append(
                StrokeSample(
                    120.0 + math.cos(angle) * 55.0,
                    100.0 - math.sin(angle) * 34.0,
                    0.75,
                    float(len(mixed)),
                )
            )
        mixed_vertices = [
            QPointF(175, 100),
            QPointF(205, 126),
            QPointF(174, 142),
            QPointF(174, 184),
            QPointF(65, 184),
            QPointF(65, 100),
        ]
        for start, end in zip(mixed_vertices, mixed_vertices[1:]):
            for step in range(10):
                amount = step / 10.0
                wobble = math.sin(len(mixed) * 1.91) * 1.1
                mixed.append(
                    StrokeSample(
                        start.x() + (end.x() - start.x()) * amount + wobble,
                        start.y() + (end.y() - start.y()) * amount - wobble * 0.4,
                        0.75,
                        float(len(mixed)),
                    )
                )
        mixed.append(StrokeSample(65.5, 99.4, 0.75, float(len(mixed))))
        mixed_shape = clean_stroke(mixed)
        self.assertIsNotNone(mixed_shape)
        assert mixed_shape is not None
        self.assertTrue(mixed_shape.closed)
        self.assertIn(mixed_shape.kind, {"mixed", "cornered"})
        self.assertGreaterEqual(mixed_shape.corner_count, 4)
        self.assertGreaterEqual(mixed_shape.straight_segment_count, 2)
        self.assertLess((time.perf_counter() - started) * 1000.0, 80.0)

    def test_smart_shape_replacement_uses_brush_engine_and_one_undo(self) -> None:
        rough = tuple(
            StrokeSample(
                120.0 + math.cos(index * math.tau / 48.0) * 58.0 + math.sin(index * 1.7) * 1.4,
                120.0 + math.sin(index * math.tau / 48.0) * 43.0 + math.cos(index * 2.1) * 1.2,
                0.45 + index / 110.0,
                float(index),
            )
            for index in range(48)
        )
        rough = (*rough, StrokeSample(rough[0].x + 1.0, rough[0].y, 0.9, 48.0))
        shape = clean_stroke(rough)
        self.assertIsNotNone(shape)
        assert shape is not None
        clean_samples = tuple(
            StrokeSample(point.x, point.y, point.pressure, point.timestamp_ms) for point in shape.points
        )
        for preset in (preset for preset in BRUSH_PRESETS if preset.engine != "shape"):
            document = PaintDocument(256, 256)
            if preset.engine == "blur":
                document.select_brush_preset("ink")
                document.set_brush(color=QColor("#e3008c"), size=70.0)
                document.replay((StrokeSample(120, 120),))
            document.select_brush_preset(preset.preset_id)
            document.set_brush(color=QColor("#e3008c"), size=18.0)
            before = document.pixel_hash()
            document.begin_stroke(rough[0])
            for sample in rough[1:]:
                document.add_sample(sample)
            elapsed = document.replace_active_stroke(clean_samples)
            self.assertIsNotNone(elapsed, preset.name)
            self.assertLess(elapsed, 250.0, preset.name)
            document.end_stroke()
            after = document.pixel_hash()
            self.assertNotEqual(after, before, preset.name)
            self.assertTrue(document.undo(), preset.name)
            self.assertEqual(document.pixel_hash(), before, preset.name)
            self.assertTrue(document.redo(), preset.name)
            self.assertEqual(document.pixel_hash(), after, preset.name)

    def test_smart_shape_keeps_polygon_topology_across_density_rounding_and_gaps(self) -> None:
        def rounded_polygon(
            vertices: list[QPointF],
            *,
            density: int,
            gap_samples: int = 0,
        ) -> list[StrokeSample]:
            count = len(vertices)
            round_fraction = 0.13

            def between(first: QPointF, second: QPointF, amount: float) -> QPointF:
                return first + (second - first) * amount

            entry = [
                between(vertices[index], vertices[(index - 1) % count], round_fraction)
                for index in range(count)
            ]
            exit_points = [
                between(vertices[index], vertices[(index + 1) % count], round_fraction)
                for index in range(count)
            ]
            samples: list[StrokeSample] = []
            for index in range(count):
                next_index = (index + 1) % count
                start, end = exit_points[index], entry[next_index]
                for step in range(density):
                    amount = step / density
                    wobble = math.sin((len(samples) + 1) * 2.7) * 1.2
                    point = between(start, end, amount)
                    samples.append(
                        StrokeSample(
                            point.x() + wobble,
                            point.y() - wobble * 0.4,
                            0.8,
                            float(len(samples)),
                        )
                    )
                start, corner, end = entry[next_index], vertices[next_index], exit_points[next_index]
                for step in range(1, 6):
                    amount = step / 5.0
                    inverse = 1.0 - amount
                    samples.append(
                        StrokeSample(
                            inverse * inverse * start.x()
                            + 2.0 * inverse * amount * corner.x()
                            + amount * amount * end.x(),
                            inverse * inverse * start.y()
                            + 2.0 * inverse * amount * corner.y()
                            + amount * amount * end.y(),
                            0.8,
                            float(len(samples)),
                        )
                    )
            return samples[:-gap_samples] if gap_samples else samples

        shapes = (
            (
                "trapezoid",
                [QPointF(55, 45), QPointF(195, 45), QPointF(165, 175), QPointF(80, 175)],
                4,
                14,
                0,
            ),
            (
                "rounded hexagon",
                [
                    QPointF(80, 30),
                    QPointF(170, 25),
                    QPointF(225, 95),
                    QPointF(185, 180),
                    QPointF(85, 185),
                    QPointF(30, 105),
                ],
                6,
                34,
                12,
            ),
            (
                "asymmetric septagon",
                [
                    QPointF(100, 25),
                    QPointF(180, 38),
                    QPointF(225, 90),
                    QPointF(205, 165),
                    QPointF(130, 195),
                    QPointF(55, 170),
                    QPointF(25, 90),
                ],
                7,
                14,
                0,
            ),
            (
                "rounded decagon",
                [
                    QPointF(
                        130 + math.cos(-math.pi * 0.5 + index * math.tau / 10.0) * 100,
                        115 + math.sin(-math.pi * 0.5 + index * math.tau / 10.0) * 80,
                    )
                    for index in range(10)
                ],
                10,
                14,
                0,
            ),
        )
        for name, vertices, expected_corners, density, gap in shapes:
            shape = clean_stroke(rounded_polygon(vertices, density=density, gap_samples=gap))
            self.assertIsNotNone(shape, name)
            assert shape is not None
            self.assertTrue(shape.closed, name)
            self.assertEqual(shape.corner_count, expected_corners, name)

    def test_commit_does_not_change_live_pixels(self) -> None:
        document = PaintDocument(512, 512)
        document.begin_stroke(SAMPLES[0])
        for sample in SAMPLES[1:]:
            document.add_sample(sample)
        live_hash = document.pixel_hash()
        document.end_stroke()
        self.assertEqual(live_hash, document.pixel_hash())

    def test_undo_redo_round_trip(self) -> None:
        document = PaintDocument(512, 512)
        blank_hash = document.pixel_hash()
        document.replay(SAMPLES)
        painted_hash = document.pixel_hash()
        self.assertNotEqual(blank_hash, painted_hash)
        self.assertTrue(document.undo())
        self.assertEqual(blank_hash, document.pixel_hash())
        self.assertTrue(document.redo())
        self.assertEqual(painted_hash, document.pixel_hash())

    def test_recovery_round_trip(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="paintstudio-check-"))
        try:
            store = RecoveryStore(root)
            document = PaintDocument(512, 512)
            document.replay(SAMPLES)
            store.write(RecoverySnapshot(document.revision, document.image.copy()))
            recovered = store.load()
            self.assertIsNotNone(recovered)
            assert recovered is not None
            image, revision = recovered
            restored = PaintDocument(1, 1)
            restored.replace_image(image)
            self.assertEqual(document.pixel_hash(), restored.pixel_hash())
            self.assertEqual(document.revision, revision)
            finished: list[int] = []
            restored.stroke_finished.connect(finished.append)
            restored.restore_flat_image(image, revision)
            self.assertEqual(restored.revision, revision)
            self.assertEqual(finished, [])
            writer = RecoveryWriter(store)
            writer.mark_saved(revision)
            self.assertTrue(writer.flush(revision, timeout=0.05))
            writer.close()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_recovery_keeps_three_recent_bundles(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="paintstudio-recovery-retention-check-"))
        try:
            store = RecoveryStore(root)
            document = PaintDocument(64, 64)
            for revision in range(1, 5):
                document.set_brush(color=QColor("#111111"), size=float(4 + revision))
                document.replay((StrokeSample(8 * revision, 8 * revision),))
                store.write(
                    RecoverySnapshot(
                        revision,
                        document.image.copy(),
                        tuple(node.clone() for node in document.roots),
                        document.active_layer_id,
                    )
                )
            bundles = sorted(path.name for path in root.glob("document-*"))
            self.assertEqual(bundles, ["document-00000002", "document-00000003", "document-00000004"])
            recovered = store.load_snapshot()
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.revision, 4)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_layered_recovery_round_trip(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="paintstudio-layer-check-"))
        try:
            store = RecoveryStore(root)
            document = PaintDocument(256, 256)
            upper_id = document.add_paint_layer("Highlights")
            document.set_layer_property(upper_id, "opacity", 0.63)
            document.set_layer_property(upper_id, "blend_mode", "Screen")
            document.set_brush(color=QColor("#ff40aa"), size=28)
            document.replay((StrokeSample(-6, 80), *SAMPLES[:12]))
            document.preview_canvas_rect(QRect(-10, 12, 220, 200))
            document.commit_canvas_rect(QRect(0, 0, 256, 256))
            document.apply_freehand_selection((QPointF(0, 20), QPointF(100, 20), QPointF(100, 120), QPointF(0, 120)))
            from paint_studio.storage import RecoverySnapshot

            store.write(
                RecoverySnapshot(
                    document.revision,
                    document.image.copy(),
                    tuple(node.clone() for node in document.roots),
                    document.active_layer_id,
                    document.origin,
                    document.canvas_rect.getRect(),
                    QImage(document.selection_mask),
                )
            )
            recovered = store.load_snapshot()
            self.assertIsNotNone(recovered)
            assert recovered is not None
            restored = PaintDocument(1, 1)
            restored.restore_layers(
                list(recovered.roots),
                recovered.active_layer_id,
                recovered.revision,
                origin=recovered.origin,
                canvas_rect=QRect(*recovered.canvas_rect),
                selection=recovered.selection,
            )
            restored_upper = restored.find_layer(upper_id)
            self.assertIsNotNone(restored_upper)
            assert restored_upper is not None
            self.assertAlmostEqual(restored_upper.opacity, 0.63)
            self.assertEqual(restored_upper.blend_mode, "Screen")
            self.assertEqual(restored.origin, document.origin)
            self.assertEqual(restored.canvas_rect, document.canvas_rect)
            self.assertEqual(restored.selection_bounds, document.selection_bounds)
            self.assertEqual(restored.pixel_hash(), document.pixel_hash())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_editable_document_bundle_round_trip(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="paintstudio-document-check-"))
        try:
            document = PaintDocument(192, 128)
            upper_id = document.add_paint_layer("Upper")
            document.set_layer_property(upper_id, "clipping", True)
            document.set_brush(color=QColor("#36a4ff"), size=22)
            document.replay((StrokeSample(-8, 20), StrokeSample(170, 100)))
            document.preview_canvas_rect(QRect(-12, 8, 176, 104))
            document.commit_canvas_rect(QRect(0, 0, 192, 128))
            document.apply_freehand_selection((QPointF(4, 12), QPointF(80, 12), QPointF(80, 70), QPointF(4, 70)))
            store = DocumentBundleStore()
            path = store.write(
                root / "test.paintstudio",
                RecoverySnapshot(
                    document.revision,
                    document.image.copy(),
                    tuple(node.clone() for node in document.roots),
                    document.active_layer_id,
                    document.origin,
                    document.canvas_rect.getRect(),
                    QImage(document.selection_mask),
                ),
            )
            snapshot = store.load(path)
            restored = PaintDocument(1, 1)
            restored.restore_layers(
                list(snapshot.roots),
                snapshot.active_layer_id,
                snapshot.revision,
                origin=snapshot.origin,
                canvas_rect=QRect(*snapshot.canvas_rect),
                selection=snapshot.selection,
            )
            restored_upper = restored.find_layer(upper_id)
            self.assertIsNotNone(restored_upper)
            assert restored_upper is not None
            self.assertTrue(restored_upper.clipping)
            self.assertEqual(restored.origin, document.origin)
            self.assertEqual(restored.canvas_rect, document.canvas_rect)
            self.assertEqual(restored.selection_bounds, document.selection_bounds)
            self.assertEqual(restored.pixel_hash(), document.pixel_hash())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_default_editable_document_path_uses_paint_studio_data_documents(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="paintstudio-default-document-check-"))
        try:
            app_root = root / "app"
            app_root.mkdir()
            with patch("paint_studio.storage.APP_ROOT", app_root), patch(
                "paint_studio.storage.QStandardPaths.writableLocation", return_value=str(root / "docs")
            ):
                self.assertEqual(default_data_root(), root / "docs" / "PaintStudioData")
                (app_root / "PaintStudioData").mkdir()
                self.assertEqual(default_data_root(), app_root / "PaintStudioData")
            documents = default_documents_root(root)
            path = default_document_path(root)
            self.assertEqual(documents, root / "documents")
            self.assertEqual(path, documents / "painting.paintstudio")
            document = PaintDocument(48, 32)
            written = DocumentBundleStore().write(
                path,
                RecoverySnapshot(
                    document.revision,
                    document.image.copy(),
                    tuple(node.clone() for node in document.roots),
                    document.active_layer_id,
                ),
            )
            self.assertEqual(written, path)
            self.assertTrue(path.is_file())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_stroke_bundle_is_serializable_batched_and_one_undo(self) -> None:
        document = PaintDocument(512, 512)
        strokes = tuple(
            StrokeCommand.from_samples(
                (
                    StrokeSample(30.0, 30.0 + index * 9.0, 0.5),
                    StrokeSample(480.0, 40.0 + index * 9.0, 0.9),
                ),
                brush=BrushSettings(size=12.0, spacing=0.12),
                color="#e3008c",
            )
            for index in range(30)
        )
        bundle = StrokeBundle(strokes, "check bundle")
        restored_bundle = StrokeBundle.from_dict(bundle.to_dict())
        changed: list[object] = []
        document.changed.connect(changed.append)
        blank_hash = document.pixel_hash()
        result = document.execute_bundle(restored_bundle)
        painted_hash = document.pixel_hash()
        self.assertEqual(result.stroke_count, 30)
        self.assertEqual(result.sample_count, 60)
        self.assertEqual(document.revision, 1)
        self.assertEqual(len(changed), 1)
        self.assertNotEqual(blank_hash, painted_hash)
        self.assertTrue(document.undo())
        self.assertEqual(document.pixel_hash(), blank_hash)
        self.assertTrue(document.redo())
        self.assertEqual(document.pixel_hash(), painted_hash)

    def test_backend_dispatches_agent_facing_commands(self) -> None:
        document = PaintDocument(256, 192)
        backend = PaintingBackend(document, Path(tempfile.mkdtemp(prefix="paintstudio-api-check-")))
        try:
            layer_response = backend.dispatch({"command": "add_layer", "name": "Agent Paint"})
            self.assertTrue(layer_response["ok"])
            bundle = StrokeBundle(
                (
                    StrokeCommand.from_samples(
                        (StrokeSample(20, 20), StrokeSample(220, 170)),
                        color="#36a4ff",
                    ),
                ),
                "API check",
            )
            stroke_response = backend.dispatch({"command": "stroke_bundle", "bundle": bundle.to_dict()})
            self.assertTrue(stroke_response["ok"])
            self.assertEqual(stroke_response["stroke_count"], 1)
            info = backend.dispatch({"command": "canvas_info"})
            self.assertEqual(info["width"], 256)
            self.assertEqual(info["height"], 192)
            self.assertEqual(info["layer_count"], 2)
            png = backend.dispatch({"command": "canvas_png"})
            self.assertTrue(Path(str(png["path"])).exists())
            self.assertTrue(Path(str(png["check_path"])).exists())
            self.assertTrue(png["check_valid"])
            check = backend.dispatch({"command": "canvas_check"})
            self.assertTrue(check["ok"])
            self.assertTrue(check["check_valid"])
            self.assertEqual(check["check_revision"], png["check_revision"])
            self.assertEqual(check["check_revision"], check["revision"])
            Path(str(png["path"])).write_bytes(b"stale")
            stale = backend.dispatch({"command": "canvas_check"})
            self.assertTrue(stale["ok"])
            self.assertFalse(stale["check_valid"])
            self.assertIn("unreadable", str(stale["check_error"]).lower())
        finally:
            shutil.rmtree(backend.output_root, ignore_errors=True)

    def test_bridge_check_never_relabels_stale_document_pixels(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="paintstudio-bridge-stale-check-"))
        try:
            document = PaintDocument(128, 96)
            backend = PaintingBackend(document, root)
            exported = backend.dispatch({"command": "canvas_png"})
            self.assertTrue(exported["ok"])
            self.assertTrue(exported["check_valid"])
            exported_revision = int(exported["check_revision"])
            exported_hash = str(exported["check_pixel_hash"])

            document.set_brush(color=QColor("#e3008c"), size=16)
            document.replay((StrokeSample(16, 16), StrokeSample(96, 72)))
            self.assertGreater(document.revision, exported_revision)
            stale = backend.dispatch({"command": "canvas_check"})
            self.assertTrue(stale["ok"])
            self.assertFalse(stale["check_valid"])
            self.assertEqual(stale["check_revision"], exported_revision)
            self.assertEqual(stale["check_pixel_hash"], exported_hash)
            self.assertEqual(stale["revision"], document.revision)
            self.assertIn("document revision", str(stale["check_error"]))
            self.assertIn("document pixel hash", str(stale["check_error"]))

            document.reset(64, 48)
            resized = backend.dispatch({"command": "canvas_check"})
            self.assertTrue(resized["ok"])
            self.assertFalse(resized["check_valid"])
            self.assertEqual(resized["check_revision"], exported_revision)
            self.assertIn("document dimensions", str(resized["check_error"]))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_bridge_check_rejects_interrupted_pair_and_missing_files(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="paintstudio-bridge-interrupted-check-"))
        try:
            backend = PaintingBackend(PaintDocument(64, 64), root)
            self.assertTrue(backend.dispatch({"command": "canvas_png"})["check_valid"])
            backend.pending_path.write_text('{"schema":1,"transaction_id":"interrupted"}\n', encoding="utf-8")
            interrupted = backend.dispatch({"command": "canvas_check"})
            self.assertTrue(interrupted["ok"])
            self.assertFalse(interrupted["check_valid"])
            self.assertIn("incomplete", str(interrupted["check_error"]).lower())
            backend.pending_path.unlink()
            backend.check_path.unlink()
            missing = backend.dispatch({"command": "canvas_check"})
            self.assertFalse(missing["ok"])
            self.assertIn("missing", str(missing["error"]).lower())

            blocked_root = root / "blocked-root"
            blocked_root.write_bytes(b"not a directory")
            failed_export = PaintingBackend(PaintDocument(16, 16), blocked_root).dispatch({"command": "canvas_png"})
            self.assertFalse(failed_export["ok"])
            self.assertIn("already exists", str(failed_export["error"]).lower())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_bridge_exports_only_current_canvas_bounds(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="paintstudio-cropped-bridge-check-"))
        try:
            document = PaintDocument(96, 64)
            document.set_brush(color=QColor("#e3008c"), size=18)
            document.replay((StrokeSample(-5, 24), StrokeSample(80, 24)))
            document.preview_canvas_rect(QRect(-8, 8, 72, 40))
            document.commit_canvas_rect(QRect(0, 0, 96, 64))
            result = PaintingBackend(document, root).dispatch({"command": "canvas_png"})
            self.assertTrue(result["check_valid"])
            self.assertEqual((result["png_width"], result["png_height"]), (72, 40))
            self.assertEqual(result["check_pixel_hash"], document.pixel_hash())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_local_socket_exposes_backend(self) -> None:
        window = MainWindow(recover=False, background=True)
        server = InstanceServer(window, APP, f"paintstudio-check-{uuid.uuid4().hex}")
        socket = QLocalSocket()
        try:
            socket.connectToServer(server.server.serverName())
            self.assertTrue(socket.waitForConnected(1000))
            socket.write(b'{"command":"canvas_info"}\n')
            socket.flush()
            socket.waitForBytesWritten(1000)
            APP.processEvents()
            self.assertTrue(socket.waitForReadyRead(1000))
            response = json.loads(bytes(socket.readAll()))
            self.assertTrue(response["ok"])
            self.assertEqual(response["width"], 2048)
            self.assertEqual(response["height"], 2048)
        finally:
            socket.disconnectFromServer()
            server.server.close()
            window.quit_application()
            APP.processEvents()

    def test_layers_are_functional_and_undoable(self) -> None:
        document = PaintDocument(256, 256)
        base_id = document.active_layer_id
        upper_id = document.add_paint_layer("Upper")
        self.assertEqual(document.active_layer_id, upper_id)
        document.set_brush(color=QColor("#e3008c"), size=48)
        document.replay((StrokeSample(128, 128), StrokeSample(160, 128)))
        painted_hash = document.pixel_hash()
        document.set_layer_property(upper_id, "visible", False)
        self.assertNotEqual(document.pixel_hash(), painted_hash)
        self.assertTrue(document.undo())
        self.assertEqual(document.pixel_hash(), painted_hash)
        group_id = document.group_layer(upper_id)
        self.assertIsNotNone(group_id)
        group = document.find_layer(group_id)
        self.assertIsNotNone(group)
        assert group is not None
        self.assertTrue(group.is_group)
        self.assertEqual(group.children[0].layer_id, upper_id)
        self.assertIsNotNone(document.find_layer(base_id))

    def test_new_document_is_one_empty_alpha_layer(self) -> None:
        document = PaintDocument(96, 64)
        self.assertEqual(document.brush_color.name(), "#ffffff")
        layers = [node for node in document.iter_layers() if not node.is_group]
        self.assertEqual(len(layers), 1)
        self.assertEqual(document.canvas_image().pixelColor(48, 32).alpha(), 0)
        canvas = CanvasWidget(document)
        self.assertFalse(canvas.checkerboard_background)
        self.assertFalse(any(preset.eraser or preset.preset_id == "eraser" for preset in BRUSH_PRESETS))
        self.assertEqual(
            [preset.preset_id for preset in BRUSH_PRESETS],
            ["air", "ink", "paint", "shape", "blur", "stamp", "fill"],
        )

    def test_shape_blur_and_stamp_engines_are_functional(self) -> None:
        shape = PaintDocument(128, 128)
        shape.select_brush_preset("shape")
        shape.set_brush(color=QColor("#e3008c"), primitive="ellipse", fill_mode="border", border_width=8)
        self.assertTrue(shape.commit_shape(QPointF(24, 28), QPointF(104, 96)))
        self.assertGreater(shape.canvas_image().pixelColor(64, 28).alpha(), 0)
        self.assertEqual(shape.canvas_image().pixelColor(64, 62).alpha(), 0)
        self.assertTrue(shape.undo())
        self.assertEqual(shape.canvas_image().pixelColor(64, 28).alpha(), 0)

        source = QImage(128, 128, QImage.Format.Format_ARGB32_Premultiplied)
        source.fill(QColor("black"))
        painter = QPainter(source)
        painter.fillRect(QRect(64, 0, 64, 128), QColor("white"))
        painter.end()
        blur = PaintDocument(1, 1)
        blur.replace_image(source)
        blur.select_brush_preset("blur")
        blur.set_brush(size=52, blur_radius=18, blur_strength=0.9)
        before = blur.canvas_image().pixelColor(62, 64).red()
        blur.replay((StrokeSample(64, 64),))
        self.assertGreater(blur.canvas_image().pixelColor(62, 64).red(), before)

        first = PaintDocument(128, 128)
        second = PaintDocument(128, 128)
        for document in (first, second):
            document.select_brush_preset("stamp")
            document.set_brush(
                color=QColor("#e3008c"),
                tip_shape="star",
                scatter=0.35,
                size_variation=0.4,
                rotation_variation=90,
                hue_variation=50,
                alpha_variation=0.3,
            )
            document.replay((StrokeSample(64, 64, 1.0, 1234.0),))
        self.assertEqual(first.pixel_hash(), second.pixel_hash())
        self.assertNotEqual(first.pixel_hash(), PaintDocument(128, 128).pixel_hash())

    def test_eraser_mode_toggles_independently_of_brush_preset(self) -> None:
        document = PaintDocument(64, 64)
        self.assertFalse(document.eraser_enabled)
        self.assertTrue(document.toggle_eraser())
        document.select_brush_preset("marker")
        self.assertTrue(document.eraser_enabled)
        self.assertFalse(document.toggle_eraser())

    def test_freehand_selection_modifiers_constrain_paint(self) -> None:
        document = PaintDocument(96, 64)
        first = (QPointF(8, 8), QPointF(42, 8), QPointF(42, 42), QPointF(8, 42))
        second = (QPointF(30, 8), QPointF(72, 8), QPointF(72, 42), QPointF(30, 42))
        self.assertTrue(document.apply_freehand_selection(first, "replace"))
        self.assertTrue(document.apply_freehand_selection(second, "add"))
        self.assertTrue(document.selection_contains(QPointF(60, 20)))
        self.assertTrue(document.apply_freehand_selection(second, "subtract"))
        self.assertFalse(document.selection_contains(QPointF(60, 20)))
        self.assertTrue(document.apply_freehand_selection(second, "intersect"))
        self.assertTrue(document.selection_bounds.isEmpty())

        document.apply_freehand_selection(first, "replace")
        document.set_brush(color=QColor("#e3008c"), size=18)
        document.replay((StrokeSample(40, 24), StrokeSample(70, 24)))
        self.assertGreater(document.canvas_image().pixelColor(35, 24).alpha(), 0)
        self.assertEqual(document.canvas_image().pixelColor(55, 24).alpha(), 0)

    def test_transform_selection_and_recursive_group_commit_cancel(self) -> None:
        document = PaintDocument(96, 96)
        base_id = document.active_layer_id
        document.set_brush(color=QColor("#ff0000"), size=10)
        document.replay((StrokeSample(20, 20),))
        upper_id = document.add_paint_layer("Upper")
        document.set_brush(color=QColor("#00ff00"), size=10)
        document.replay((StrokeSample(40, 40),))
        group_id = document.group_layers((base_id, upper_id))
        assert group_id is not None
        session = document.begin_transform((group_id,))
        assert session is not None
        document.preview_transform(session, QTransform.fromTranslate(12, 6))
        self.assertEqual(document.canvas_image().pixelColor(20, 20).alpha(), 0)
        self.assertGreater(document.canvas_image().pixelColor(32, 26).alpha(), 0)
        self.assertGreater(document.canvas_image().pixelColor(52, 46).alpha(), 0)
        document.cancel_transform(session)
        self.assertGreater(document.canvas_image().pixelColor(20, 20).alpha(), 0)

        document.apply_freehand_selection((QPointF(12, 12), QPointF(28, 12), QPointF(28, 28), QPointF(12, 28)))
        session = document.begin_transform((group_id,))
        assert session is not None
        document.preview_transform(session, QTransform.fromTranslate(20, 0))
        document.commit_transform(session)
        self.assertEqual(document.canvas_image().pixelColor(20, 20).alpha(), 0)
        self.assertGreater(document.canvas_image().pixelColor(40, 20).alpha(), 0)
        self.assertGreater(document.canvas_image().pixelColor(40, 40).alpha(), 0)
        self.assertEqual(document.selection_bounds.x(), 32)
        self.assertTrue(document.undo())
        self.assertGreater(document.canvas_image().pixelColor(20, 20).alpha(), 0)
        self.assertEqual(document.selection_bounds.x(), 12)

    def test_transform_handles_scale_rotate_and_perspective(self) -> None:
        document = PaintDocument(256, 256)
        document.set_brush(color=QColor("#e3008c"), size=90)
        document.replay((StrokeSample(128, 128),))
        canvas = CanvasWidget(document)
        canvas.resize(600, 500)
        canvas.fit_document()
        self.assertTrue(canvas.begin_transform([document.active_layer_id]))
        session = canvas.transform_session
        assert session is not None
        corner = canvas._transform_handles()["top_left"]
        anchor = session.bounds.bottomRight()
        self.assertTrue(canvas._begin_transform_pointer(corner))
        canvas._update_transform_pointer(
            corner - QPointF(24, 24),
            Qt.KeyboardModifier.ShiftModifier,
        )
        mapped_anchor = canvas._transform_matrix.map(anchor)
        self.assertAlmostEqual(mapped_anchor.x(), anchor.x(), places=4)
        self.assertAlmostEqual(mapped_anchor.y(), anchor.y(), places=4)
        self.assertAlmostEqual(abs(canvas._transform_matrix.m11()), abs(canvas._transform_matrix.m22()), places=4)
        canvas.cancel_transform()

        self.assertTrue(canvas.begin_transform([document.active_layer_id]))
        corner = canvas._transform_handles()["top_left"]
        canvas._begin_transform_pointer(corner)
        canvas._update_transform_pointer(corner + QPointF(-20, 12), Qt.KeyboardModifier.ControlModifier)
        self.assertFalse(canvas._transform_matrix.isAffine())
        canvas.cancel_transform()

        self.assertTrue(canvas.begin_transform([document.active_layer_id]))
        handles = canvas._transform_handles()
        center = canvas._transform_screen_quad().boundingRect().center()
        edge = handles["top"]
        direction = edge - center
        length = math.hypot(direction.x(), direction.y())
        rotate_start = edge + direction / length * 22.0
        self.assertEqual(canvas._transform_hit(rotate_start)[0], "rotate")
        canvas._begin_transform_pointer(rotate_start)
        canvas._update_transform_pointer(rotate_start + QPointF(18, -12), Qt.KeyboardModifier.NoModifier)
        self.assertFalse(canvas._transform_matrix.isIdentity())
        canvas.cancel_transform()

    def test_paint_outside_canvas_survives_crop_and_end_trim(self) -> None:
        document = PaintDocument(64, 64)
        document.set_brush(color=QColor("#e3008c"), size=16)
        document.replay((StrokeSample(-4, 32),))
        self.assertLess(document.backing_rect.left(), 0)
        outside = document.world_to_image_point(QPointF(-4, 32)).toPoint()
        self.assertGreater(document.image.pixelColor(outside).alpha(), 0)

        initial = document.canvas_rect
        document.preview_canvas_rect(QRect(10, 0, 54, 64))
        self.assertTrue(document.commit_canvas_rect(initial))
        cropped = document.canvas_rect
        document.preview_canvas_rect(QRect(-16, 0, 80, 64))
        self.assertTrue(document.commit_canvas_rect(cropped))
        self.assertGreater(document.canvas_image().pixelColor(12, 32).alpha(), 0)

        expanded = document.canvas_rect
        document.preview_canvas_rect(QRect(0, 0, 64, 64))
        document.commit_canvas_rect(expanded)
        self.assertTrue(document.trim_to_canvas())
        self.assertEqual(document.backing_rect, document.canvas_rect)
        self.assertTrue(document.undo())
        self.assertLess(document.backing_rect.left(), 0)

    def test_layer_quick_commands_are_undoable(self) -> None:
        document = PaintDocument(64, 64)
        document.set_brush(color=QColor("#ff4db8"), size=12)
        document.replay((StrokeSample(32, 32),))
        visible = document.canvas_image()
        stamped_id = document.stamp_visible()
        stamped = document.find_layer(stamped_id)
        assert stamped is not None
        self.assertEqual(document.layer_canvas_image(stamped), visible)
        self.assertTrue(document.clear_active_layer())
        self.assertEqual(document.layer_canvas_image(stamped).pixelColor(32, 32).alpha(), 0)
        self.assertTrue(document.undo())
        self.assertGreater(document.layer_canvas_image(document.find_layer(stamped_id)).pixelColor(32, 32).alpha(), 0)
        base_id = document.roots[0].layer_id
        group_id = document.group_layers((base_id, stamped_id))
        self.assertIsNotNone(group_id)
        assert group_id is not None
        self.assertEqual(len(document.find_layer(group_id).children), 2)

    def test_layers_relocate_by_drag_order_and_group_target(self) -> None:
        document = PaintDocument(128, 128)
        base_id = document.active_layer_id
        middle_id = document.add_paint_layer("Middle")
        upper_id = document.add_paint_layer("Upper")
        self.assertTrue(document.relocate_layer(base_id, upper_id, "above"))
        self.assertEqual([node.layer_id for node in document.roots], [middle_id, upper_id, base_id])
        group_id = document.add_group("Group")
        self.assertTrue(document.relocate_layer(middle_id, group_id, "inside"))
        group = document.find_layer(group_id)
        self.assertIsNotNone(group)
        assert group is not None
        self.assertEqual([node.layer_id for node in group.children], [middle_id])
        self.assertFalse(document.relocate_layer(group_id, middle_id, "inside"))
        self.assertTrue(document.undo())
        self.assertIn(middle_id, [node.layer_id for node in document.roots])

    def test_alpha_lock_preserves_existing_alpha(self) -> None:
        document = PaintDocument(128, 128)
        layer_id = document.add_paint_layer("Alpha")
        document.set_brush(color=QColor("#e3008c"), size=42)
        document.replay((StrokeSample(64, 64),))
        layer = document.find_layer(layer_id)
        assert layer is not None and layer.image is not None
        alpha_before = layer.image.pixelColor(64, 64).alpha()
        document.set_layer_property(layer_id, "alpha_locked", True)
        document.set_brush(color=QColor("#20a8ff"))
        document.replay((StrokeSample(64, 64),))
        self.assertEqual(layer.image.pixelColor(64, 64).alpha(), alpha_before)
        self.assertEqual(layer.image.pixelColor(4, 4).alpha(), 0)

    def test_blur_and_shape_respect_alpha_lock(self) -> None:
        source = QImage(128, 128, QImage.Format.Format_ARGB32_Premultiplied)
        source.fill(Qt.GlobalColor.transparent)
        painter = QPainter(source)
        painter.fillRect(QRect(0, 0, 64, 128), QColor("black"))
        painter.fillRect(QRect(64, 0, 32, 128), QColor("white"))
        painter.end()
        blur = PaintDocument(1, 1)
        blur.replace_image(source)
        blur.set_layer_property(blur.active_layer_id, "alpha_locked", True)
        blur.select_brush_preset("blur")
        blur.set_brush(size=52, blur_radius=18, blur_strength=0.9)
        before = blur.canvas_image().pixelColor(62, 64).red()
        blur.replay((StrokeSample(64, 64), StrokeSample(96, 64)))
        self.assertGreater(blur.canvas_image().pixelColor(62, 64).red(), before)
        self.assertEqual(blur.canvas_image().pixelColor(92, 64).alpha(), 255)
        self.assertEqual(blur.canvas_image().pixelColor(100, 64).alpha(), 0)

        shape = PaintDocument(128, 128)
        shape.select_brush_preset("ink")
        shape.set_brush(color=QColor("#e3008c"), size=42)
        shape.replay((StrokeSample(64, 64),))
        shape.set_layer_property(shape.active_layer_id, "alpha_locked", True)
        shape.select_brush_preset("shape")
        shape.set_brush(color=QColor("#20a8ff"), primitive="rect", fill_mode="fill")
        self.assertTrue(shape.commit_shape(QPointF(16, 16), QPointF(112, 112)))
        self.assertEqual(shape.canvas_image().pixelColor(64, 64).name(), "#20a8ff")
        self.assertEqual(shape.canvas_image().pixelColor(64, 64).alpha(), 255)
        self.assertEqual(shape.canvas_image().pixelColor(24, 24).alpha(), 0)
        painted = shape.pixel_hash()
        shape.toggle_eraser()
        self.assertFalse(shape.commit_shape(QPointF(16, 16), QPointF(112, 112)))
        self.assertEqual(shape.pixel_hash(), painted)

    def test_clipping_masks_upper_layer_to_lower_content(self) -> None:
        document = PaintDocument(160, 160)
        document.select_brush_preset("ink")
        document.set_layer_property(document.active_layer_id, "visible", False)
        lower_id = document.add_paint_layer("Lower")
        document.set_active_layer(lower_id)
        document.set_brush(color=QColor("#ffffff"), size=32)
        document.replay((StrokeSample(80, 80),))
        upper_id = document.add_paint_layer("Clipped")
        document.set_active_layer(upper_id)
        document.set_brush(color=QColor("#e3008c"), size=110)
        document.replay((StrokeSample(80, 80),))
        self.assertGreater(document.image.pixelColor(30, 80).alpha(), 0)
        document.set_layer_property(upper_id, "clipping", True)
        self.assertEqual(document.image.pixelColor(30, 80).alpha(), 0)
        self.assertEqual(document.image.pixelColor(80, 80).name(), "#e3008c")

    def test_canvas_gestures_keep_their_anchors(self) -> None:
        document = PaintDocument(200, 200)
        canvas = CanvasWidget(document)
        canvas.resize(600, 400)
        canvas.fit_document()
        anchor = QPointF(300.0, 200.0)

        canvas._begin_pointer(
            anchor,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ShiftModifier,
            1.0,
            0,
        )
        canvas._continue_pointer(
            anchor + QPointF(40.0, 0.0),
            Qt.KeyboardModifier.ShiftModifier,
            1.0,
            1,
        )
        canvas._end_pointer(Qt.MouseButton.LeftButton)
        self.assertEqual(document.brush_size, 34.0)

        document.set_brush(size=500)
        canvas._begin_pointer(
            anchor,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ShiftModifier,
            1.0,
            2,
        )
        canvas._continue_pointer(
            anchor + QPointF(40.0, 0.0),
            Qt.KeyboardModifier.ShiftModifier,
            1.0,
            3,
        )
        canvas._end_pointer(Qt.MouseButton.LeftButton)
        self.assertGreater(document.brush_size, 700)
        document.set_brush(size=9000)
        self.assertEqual(document.brush_size, 5000)

        document_anchor = canvas.to_document(anchor)
        canvas._space_down = True
        canvas._begin_pointer(
            anchor,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ControlModifier,
            1.0,
            2,
        )
        canvas._continue_pointer(
            anchor - QPointF(0.0, 100.0),
            Qt.KeyboardModifier.ControlModifier,
            1.0,
            3,
        )
        self.assertGreater(canvas.zoom, 1.0)
        anchored_after = canvas.to_document(anchor)
        self.assertAlmostEqual(document_anchor.x(), anchored_after.x(), places=5)
        self.assertAlmostEqual(document_anchor.y(), anchored_after.y(), places=5)
        canvas._end_pointer(Qt.MouseButton.LeftButton)

    def test_shift_space_drags_canvas_edge_without_trimming(self) -> None:
        document = PaintDocument(100, 80)
        canvas = CanvasWidget(document)
        canvas.resize(600, 400)
        canvas.fit_document()
        left_edge = canvas.document_rect().center()
        left_edge.setX(canvas.document_rect().left())
        canvas._space_down = True
        canvas._shift_down = True
        self.assertTrue(
            canvas._begin_pointer(
                left_edge,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.ShiftModifier,
                1.0,
                0,
            )
        )
        canvas._continue_pointer(
            left_edge - QPointF(24, 0),
            Qt.KeyboardModifier.ShiftModifier,
            1.0,
            1,
        )
        canvas._end_pointer(Qt.MouseButton.LeftButton)
        self.assertEqual(document.canvas_rect, QRect(-24, 0, 124, 80))
        self.assertLessEqual(document.backing_rect.left(), -24)

    def test_shift_space_works_from_center_corners_outside_and_tap_resets_2k(self) -> None:
        document = PaintDocument(3000, 2600)
        canvas = CanvasWidget(document)
        canvas.resize(900, 700)
        canvas.fit_document()
        canvas._space_down = True
        canvas._shift_down = True

        original = document.canvas_rect
        center = canvas.document_rect().center()
        self.assertTrue(
            canvas._begin_pointer(
                center,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.ShiftModifier,
                1.0,
                0,
            )
        )
        canvas._end_pointer(Qt.MouseButton.LeftButton)
        self.assertEqual(document.canvas_rect.size(), QSize(2048, 2048))
        self.assertTrue(document.undo())
        self.assertEqual(document.canvas_rect, original)

        canvas._begin_pointer(
            center,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ShiftModifier,
            1.0,
            1,
        )
        canvas._continue_pointer(
            center + QPointF(45, 35),
            Qt.KeyboardModifier.ShiftModifier,
            1.0,
            2,
        )
        canvas._end_pointer(Qt.MouseButton.LeftButton)
        self.assertGreater(document.canvas_rect.width(), original.width())
        self.assertGreater(document.canvas_rect.height(), original.height())
        self.assertTrue(document.undo())

        outside = canvas.document_rect().topLeft() - QPointF(80, 70)
        canvas._begin_pointer(
            outside,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ShiftModifier,
            1.0,
            3,
        )
        self.assertEqual(canvas._crop_edge, "top_left")
        canvas._continue_pointer(
            outside - QPointF(24, 16),
            Qt.KeyboardModifier.ShiftModifier,
            1.0,
            4,
        )
        canvas._end_pointer(Qt.MouseButton.LeftButton)
        self.assertLess(document.canvas_rect.left(), original.left())
        self.assertLess(document.canvas_rect.top(), original.top())
        self.assertTrue(document.undo())
        self.assertEqual(document.canvas_rect, original)

    def test_control_click_samples_document_color(self) -> None:
        document = PaintDocument(200, 200)
        document.image.setPixelColor(100, 100, QColor("#36a4ff"))
        canvas = CanvasWidget(document)
        canvas.resize(600, 400)
        canvas.fit_document()
        sampled: list[QColor] = []
        canvas.color_sampled.connect(sampled.append)
        canvas._begin_pointer(
            QPointF(300.0, 200.0),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ControlModifier,
            1.0,
            0,
        )
        canvas._end_pointer(Qt.MouseButton.LeftButton)
        self.assertEqual(document.brush_color.name(), "#36a4ff")
        self.assertEqual(sampled[-1].name(), "#36a4ff")

    def test_selection_tool_keeps_space_pan_ctrl_sample_and_shift_add(self) -> None:
        document = PaintDocument(200, 200)
        image = QImage(200, 200, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("#36a4ff"))
        document.replace_image(image)
        self.assertTrue(
            document.apply_freehand_selection((QPointF(10, 10), QPointF(60, 10), QPointF(60, 60), QPointF(10, 60)))
        )
        selected = document.selection_bounds
        canvas = CanvasWidget(document)
        canvas.resize(600, 400)
        canvas.fit_document()
        canvas.activate_freehand_selection()
        anchor = QPointF(300.0, 200.0)

        canvas._space_down = True
        canvas._begin_pointer(anchor, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, 1.0, 0)
        canvas._continue_pointer(anchor + QPointF(30.0, 20.0), Qt.KeyboardModifier.NoModifier, 1.0, 1)
        canvas._end_pointer(Qt.MouseButton.LeftButton)
        canvas._space_down = False
        self.assertEqual(canvas.pan, QPointF(30.0, 20.0))
        self.assertEqual(document.selection_bounds, selected)

        canvas._begin_pointer(anchor, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, 1.0, 2)
        canvas._end_pointer(Qt.MouseButton.LeftButton)
        self.assertEqual(document.brush_color.name(), "#36a4ff")
        self.assertEqual(document.selection_bounds, selected)

        # Shift still adds to the selection here instead of resizing the brush.
        size = document.brush_size
        lasso = [
            canvas._world_to_screen(point)
            for point in (QPointF(100, 100), QPointF(150, 100), QPointF(150, 150), QPointF(100, 150))
        ]
        canvas._begin_pointer(lasso[0], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, 1.0, 3)
        for index, point in enumerate(lasso[1:], 4):
            canvas._continue_pointer(point, Qt.KeyboardModifier.ShiftModifier, 1.0, index)
        canvas._end_pointer(Qt.MouseButton.LeftButton)
        self.assertEqual(document.brush_size, size)
        self.assertTrue(document.selection_contains(QPointF(30, 30)))
        self.assertTrue(document.selection_contains(QPointF(125, 125)))

    def test_shape_brush_keeps_space_pan_ctrl_sample_and_shift_size(self) -> None:
        document = PaintDocument(200, 200)
        image = QImage(200, 200, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("#36a4ff"))
        document.replace_image(image)
        document.select_brush_preset("shape")
        canvas = CanvasWidget(document)
        canvas.resize(600, 400)
        canvas.fit_document()
        anchor = QPointF(300.0, 200.0)

        canvas._space_down = True
        canvas._begin_pointer(anchor, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, 1.0, 0)
        canvas._continue_pointer(anchor + QPointF(30.0, 20.0), Qt.KeyboardModifier.NoModifier, 1.0, 1)
        canvas._end_pointer(Qt.MouseButton.LeftButton)
        canvas._space_down = False
        self.assertEqual(canvas.pan, QPointF(30.0, 20.0))

        canvas._begin_pointer(anchor, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, 1.0, 2)
        canvas._continue_pointer(anchor + QPointF(40.0, 30.0), Qt.KeyboardModifier.ControlModifier, 1.0, 3)
        canvas._end_pointer(Qt.MouseButton.LeftButton)
        self.assertEqual(document.brush_color.name(), "#36a4ff")

        size = document.brush_size
        canvas._begin_pointer(anchor, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, 1.0, 4)
        canvas._continue_pointer(anchor + QPointF(40.0, 30.0), Qt.KeyboardModifier.ShiftModifier, 1.0, 5)
        canvas._end_pointer(Qt.MouseButton.LeftButton)
        self.assertGreater(document.brush_size, size)
        self.assertFalse(document.can_undo)

    def test_canvas_hold_cleans_adjusts_and_commits_one_smart_shape(self) -> None:
        document = PaintDocument(360, 300)
        document.select_brush_preset("ink")
        document.set_brush(color=QColor("#e3008c"), size=12.0)
        canvas = CanvasWidget(document)
        canvas.smart_shape_hold_ms = 24
        canvas.resize(720, 600)
        canvas.fit_document()
        vertices = [
            QPointF(110, 70),
            QPointF(205, 76),
            QPointF(250, 135),
            QPointF(210, 215),
            QPointF(115, 208),
            QPointF(72, 134),
        ]
        rough: list[QPointF] = []
        for side, (start, end) in enumerate(zip(vertices, (*vertices[1:], vertices[0]))):
            for step in range(9):
                amount = step / 9.0
                wobble = math.sin((side * 9 + step) * 2.03) * 1.3
                rough.append(
                    QPointF(
                        start.x() + (end.x() - start.x()) * amount + wobble,
                        start.y() + (end.y() - start.y()) * amount - wobble * 0.45,
                    )
                )
        rough.append(vertices[0] + QPointF(1.0, -0.5))
        screen = [canvas._world_to_screen(point) for point in rough]
        before = document.pixel_hash()
        self.assertTrue(
            canvas._begin_pointer(
                screen[0],
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                0.55,
                0,
            )
        )
        for index, point in enumerate(screen[1:], 1):
            canvas._pointer_position = QPointF(point)
            canvas._continue_pointer(point, Qt.KeyboardModifier.NoModifier, 0.55 + index / 200.0, index * 4)
        self.assertTrue(canvas._smart_shape_timer.isActive())
        QTest.qWait(40)
        APP.processEvents()
        self.assertEqual(canvas._interaction, "smart_shape")
        self.assertIsNotNone(canvas._smart_shape)
        self.assertTrue(canvas._smart_shape_flash_timer.isActive())
        self.assertGreater(len(canvas._smart_shape_flash_points), 2)
        held = QPointF(canvas._pointer_position)
        canvas._pointer_position = held + QPointF(34, 26)
        canvas._continue_pointer(
            canvas._pointer_position,
            Qt.KeyboardModifier.NoModifier,
            0.9,
            400,
        )
        canvas._end_pointer(Qt.MouseButton.LeftButton)
        self.assertEqual(canvas._interaction, "")
        self.assertIsNone(canvas._smart_shape)
        after = document.pixel_hash()
        self.assertNotEqual(after, before)
        self.assertTrue(document.undo())
        self.assertEqual(document.pixel_hash(), before)
        self.assertTrue(document.redo())
        self.assertEqual(document.pixel_hash(), after)

    def test_window_is_frameless_with_paint_layout(self) -> None:
        window = MainWindow(recover=False, background=True)
        try:
            window.resize(1200, 800)
            window.show()
            APP.processEvents()
            self.assertTrue(window.windowFlags() & Qt.WindowType.FramelessWindowHint)
            self.assertIsNotNone(window.findChild(type(window.title_bar), "TitleBar"))
            self.assertIsNotNone(window.findChild(type(window.canvas), ""))
            self.assertIsNotNone(window.findChild(type(window.centralWidget()), "WindowFrame"))
            self.assertIsNotNone(window.findChild(BrushPanel, "BrushPanel"))
            self.assertIsNotNone(window.findChild(LayersPanel, "LayersPanel"))
            self.assertIsNotNone(window.findChild(CanvasOverview, "CanvasOverview"))
            self.assertTrue(application_icon_path().is_file())
            self.assertFalse(QIcon(str(application_icon_path())).isNull())
            self.assertFalse(hasattr(window, "undo_button"))
            self.assertFalse(hasattr(window.layers_panel, "add_button"))
            self.assertEqual(
                window.brush_panel.color_selector.square.size(),
                window.layers_panel.overview.size(),
            )
            self.assertEqual(window.layers_panel.overview.width(), window.layers_panel.overview.height())
            self.assertEqual(
                [action.text() for action in window.title_bar.menu_button.menu().actions() if not action.isSeparator()],
                [
                    "New",
                    "Open",
                    "Open Recent",
                    "Save",
                    "Export PNG",
                    "Undo",
                    "Redo",
                    "New Layer",
                    "Group Selected Layers",
                    "Copy Visible to New Layer",
                    "Clear Current Layer",
                    "Toggle Layer Visibility",
                    "Toggle Eraser Mode",
                    "Quick Color",
                    "Trim to Canvas",
                    "Brush Tool",
                    "Smart Shape",
                    "Freehand Selection Tool",
                    "Transform",
                    "Select All",
                    "Deselect",
                    "Invert Selection",
                    "Checkerboard Background",
                    "Quit Paint Studio",
                ],
            )
            shortcuts = {key: action.shortcut() for key, action in window.command_actions.items()}
            self.assertEqual(shortcuts["add_layer"], QKeySequence(Qt.Key.Key_Insert))
            self.assertEqual(shortcuts["group_layers"], QKeySequence("Ctrl+G"))
            self.assertEqual(shortcuts["stamp_visible"], QKeySequence(Qt.Key.Key_Home))
            self.assertEqual(shortcuts["clear_layer"], QKeySequence(Qt.Key.Key_Delete))
            self.assertEqual(shortcuts["trim"], QKeySequence(Qt.Key.Key_End))
            self.assertEqual(shortcuts["toggle_visibility"], QKeySequence(Qt.Key.Key_Apostrophe))
            self.assertEqual(shortcuts["eraser_mode"], QKeySequence(Qt.Key.Key_E))
            self.assertEqual(shortcuts["quick_color"], QKeySequence(Qt.Key.Key_C))
            self.assertEqual(shortcuts["freehand_selection"], QKeySequence(Qt.Key.Key_Q))
            self.assertEqual(shortcuts["transform"], QKeySequence("Ctrl+T"))
            self.assertGreaterEqual(len(window.findChildren(LayerRow)), 1)
            window.document.set_brush(size=77.0, opacity=0.42, flow=0.38)
            self.assertEqual(window.brush_panel.size.value(), 77)
            self.assertEqual(window.brush_panel.opacity.value(), 42)
            self.assertEqual(window.brush_panel.flow.value(), 38)
            self.assertEqual(window.brush_panel.size.focusPolicy(), Qt.FocusPolicy.NoFocus)
            opacity_bar = window.layers_panel.opacity
            QTest.mousePress(opacity_bar, Qt.MouseButton.LeftButton, pos=QPoint(opacity_bar.width() // 2, 10))
            QTest.mouseRelease(opacity_bar, Qt.MouseButton.LeftButton, pos=QPoint(opacity_bar.width() // 2, 10))
            APP.processEvents()
            self.assertAlmostEqual(window.document.active_layer.opacity, opacity_bar.value() / 100.0, places=2)
            self.assertLess(opacity_bar.value(), 60)
        finally:
            window.quit_application()
            APP.processEvents()

    def test_close_tucks_resident_window_without_shutting_down(self) -> None:
        window = MainWindow(recover=False, background=True)
        try:
            window.show()
            APP.processEvents()
            window.close()
            APP.processEvents()
            self.assertFalse(window.isVisible())
            self.assertFalse(window._shutdown_complete)
        finally:
            window.quit_application()
            APP.processEvents()

    def test_layer_and_canvas_shortcuts_dispatch(self) -> None:
        window = MainWindow(recover=False, background=True)
        try:
            window.show()
            window.canvas.setFocus()
            APP.processEvents()

            window.canvas.zoom = 0.5
            window.canvas.pan = QPointF(40, 30)
            QTest.keyClick(window.canvas, Qt.Key.Key_Space)
            APP.processEvents()
            self.assertEqual(window.canvas.pan, QPointF())

            for key, preset_id in zip(
                (Qt.Key.Key_1, Qt.Key.Key_2, Qt.Key.Key_3, Qt.Key.Key_4, Qt.Key.Key_5, Qt.Key.Key_6),
                ("air", "ink", "paint", "shape", "blur", "stamp"),
            ):
                QTest.keyClick(window.canvas, key)
                APP.processEvents()
                self.assertEqual(window.document.brush_settings.preset_id, preset_id)
            self.assertTrue(window.brush_panel.scatter.isVisible())
            self.assertFalse(window.brush_panel.blur_strength.isVisible())
            QTest.keyClick(window.canvas, Qt.Key.Key_2)
            APP.processEvents()
            self.assertTrue(window.brush_panel.brush_tip.isVisible())
            self.assertFalse(window.brush_panel.brush_tip.button("texture").isVisible())
            window.brush_panel.brush_tip.button("square").click()
            self.assertEqual(window.document.brush_settings.tip_shape, "square")
            QTest.keyClick(window.canvas, Qt.Key.Key_6)
            APP.processEvents()
            stamp_before = window.document.stamp_tip_image.cacheKey()
            pad = window.brush_panel.stamp_pad
            QTest.mousePress(pad, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
            QTest.mouseMove(pad, QPoint(120, 80))
            QTest.mouseRelease(pad, Qt.MouseButton.LeftButton, pos=QPoint(120, 80))
            APP.processEvents()
            self.assertNotEqual(window.document.stamp_tip_image.cacheKey(), stamp_before)
            self.assertEqual(window.document.brush_settings.tip_shape, "custom")
            self.assertTrue(window.brush_panel.stamp_custom.isVisible())
            window.brush_panel.stamp_clear.click()
            APP.processEvents()
            cleared = window.document.stamp_tip_image
            self.assertTrue(all(cleared.pixelColor(x, y).alpha() == 0 for x in range(0, 128, 8) for y in range(0, 128, 8)))
            window.brush_panel.stamp_tip.button("circle").click()
            APP.processEvents()
            self.assertEqual(window.document.brush_settings.tip_shape, "circle")
            self.assertFalse(window.brush_panel.stamp_custom.isVisible())
            window.document.set_brush(color=QColor("#12ab34"))
            QTest.mousePress(window.canvas, Qt.MouseButton.LeftButton, pos=QPoint(200, 200))
            QTest.mouseRelease(window.canvas, Qt.MouseButton.LeftButton, pos=QPoint(200, 200))
            APP.processEvents()
            self.assertEqual(window.brush_panel.recent_color_names()[0], "#12ab34")
            window.brush_panel.picker.popup.show_below(window.brush_panel.picker, "stamp")
            APP.processEvents()
            window.brush_panel.picker.popup.options["paint"].clicked.emit("paint")
            APP.processEvents()
            self.assertEqual(window.document.brush_settings.preset_id, "paint")
            self.assertFalse(window.brush_panel.picker.popup.isVisible())
            QTest.keyClick(window.canvas, Qt.Key.Key_6)
            APP.processEvents()

            window.canvas._pointer_inside = True
            window.canvas.refresh_native_cursor()
            self.assertEqual(APP.overrideCursor().shape(), Qt.CursorShape.BlankCursor)
            QTest.keyClick(window.canvas, Qt.Key.Key_C)
            APP.processEvents()
            self.assertTrue(window.quick_color_popup.isVisible())
            self.assertEqual(APP.overrideCursor().shape(), Qt.CursorShape.ArrowCursor)
            self.assertEqual(
                window.quick_color_popup.selector.square.cursor().shape(),
                Qt.CursorShape.CrossCursor,
            )
            hue = window.quick_color_popup.selector.hue_strip
            QTest.mouseClick(hue, Qt.MouseButton.LeftButton, pos=QPoint(hue.width() - 2, hue.height() // 2))
            APP.processEvents()
            self.assertTrue(window.quick_color_popup.isVisible())
            square = window.quick_color_popup.selector.square
            QTest.mouseClick(square, Qt.MouseButton.LeftButton, pos=QPoint(square.width() - 8, 8))
            APP.processEvents()
            self.assertFalse(window.quick_color_popup.isVisible())

            QTest.keyClick(window.canvas, Qt.Key.Key_E)
            APP.processEvents()
            self.assertTrue(window.document.eraser_enabled)
            self.assertEqual(window.canvas.tool, "brush")
            window.canvas._pointer_inside = True
            window.canvas._update_system_cursor()
            self.assertIsNotNone(APP.overrideCursor())
            self.assertEqual(APP.overrideCursor().shape(), Qt.CursorShape.BlankCursor)
            QTest.keyClick(window.canvas, Qt.Key.Key_E)
            QTest.keyClick(window.canvas, Qt.Key.Key_Q)
            self.assertEqual(window.canvas.tool, "selection")
            self.assertFalse(window.canvas._cursor_override)
            self.assertEqual(window.canvas.cursor().shape(), Qt.CursorShape.CrossCursor)
            lasso = [QPointF(10, 10), QPointF(60, 10), QPointF(60, 60), QPointF(10, 60)]
            screen_lasso = [window.canvas._world_to_screen(point) for point in lasso]
            window.canvas._begin_pointer(
                screen_lasso[0], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, 1.0, 0
            )
            for index, point in enumerate(screen_lasso[1:], 1):
                window.canvas._continue_pointer(point, Qt.KeyboardModifier.NoModifier, 1.0, index)
            window.canvas._end_pointer(Qt.MouseButton.LeftButton)
            self.assertFalse(window.document.selection_bounds.isEmpty())
            window.document.clear_selection()
            QTest.keyClick(window.canvas, Qt.Key.Key_B)
            self.assertEqual(window.canvas.tool, "brush")

            QTest.keyClick(window.canvas, Qt.Key.Key_Insert)
            APP.processEvents()
            self.assertEqual(sum(1 for node in window.document.iter_layers() if not node.is_group), 2)

            active = window.document.find_layer(window.document.active_layer_id)
            assert active is not None
            QTest.keyClick(window.canvas, Qt.Key.Key_Apostrophe)
            APP.processEvents()
            self.assertFalse(active.visible)
            QTest.keyClick(window.canvas, Qt.Key.Key_Apostrophe)

            layer_ids = [node.layer_id for node in window.document.roots]
            window.layers_panel._row_selected(layer_ids[0], Qt.KeyboardModifier.NoModifier)
            window.layers_panel._row_selected(layer_ids[1], Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(set(window.layers_panel.selected_layer_ids()), set(layer_ids))
            QTest.keyClick(window.canvas, Qt.Key.Key_G, Qt.KeyboardModifier.ControlModifier)
            APP.processEvents()
            self.assertEqual(len(window.document.roots), 1)
            self.assertTrue(window.document.roots[0].is_group)

            QTest.keyClick(window.canvas, Qt.Key.Key_Home)
            APP.processEvents()
            self.assertEqual(len(window.document.roots), 2)
            window.document.set_brush(color=QColor("#e3008c"), size=100)
            window.document.replay((StrokeSample(32, 32),))
            current = window.document.active_layer
            assert current is not None
            self.assertGreater(window.document.layer_canvas_image(current).pixelColor(32, 32).alpha(), 0)
            QTest.keyClick(window.canvas, Qt.Key.Key_T, Qt.KeyboardModifier.ControlModifier)
            APP.processEvents()
            self.assertIsNotNone(window.canvas.transform_session)
            transform_center = window.canvas._transform_screen_quad().boundingRect().center() + QPointF(0, 12)
            self.assertTrue(
                window.canvas._begin_pointer(
                    transform_center,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                    1.0,
                    0,
                )
            )
            window.canvas._continue_pointer(
                transform_center + QPointF(20, 10),
                Qt.KeyboardModifier.NoModifier,
                1.0,
                1,
            )
            window.canvas._end_pointer(Qt.MouseButton.LeftButton)
            self.assertFalse(window.canvas._transform_matrix.isIdentity())
            QTest.keyClick(window.canvas, Qt.Key.Key_Escape)
            self.assertIsNone(window.canvas.transform_session)
            QTest.keyClick(window.canvas, Qt.Key.Key_Delete)
            APP.processEvents()
            current = window.document.active_layer
            assert current is not None
            self.assertEqual(window.document.layer_canvas_image(current).pixelColor(32, 32).alpha(), 0)

            window.document.preview_canvas_rect(QRect(8, 8, 48, 48))
            window.document.commit_canvas_rect(QRect(0, 0, 2048, 2048))
            QTest.keyClick(window.canvas, Qt.Key.Key_End)
            APP.processEvents()
            self.assertEqual(window.document.backing_rect, window.document.canvas_rect)
        finally:
            window.quit_application()
            APP.processEvents()

    def test_canvas_overview_navigates_and_fits(self) -> None:
        window = MainWindow(recover=False, background=True)
        try:
            window.resize(1200, 800)
            window.canvas.resize(600, 600)
            window.canvas.zoom = 1.0
            window.canvas.center_on_document(QPointF(300, 400))
            visible = window.canvas.visible_document_rect()
            self.assertAlmostEqual(visible.center().x(), 300, delta=1)
            self.assertAlmostEqual(visible.center().y(), 400, delta=1)
            window.layers_panel.overview.resize(280, 170)
            window.layers_panel.overview._target = QRectF(55, 5, 160, 160)
            window.layers_panel.overview._navigate(window.layers_panel.overview._target.center())
            visible = window.canvas.visible_document_rect()
            self.assertAlmostEqual(visible.center().x(), window.document.width / 2, delta=1)
            self.assertAlmostEqual(visible.center().y(), window.document.height / 2, delta=1)
        finally:
            window.quit_application()
            APP.processEvents()


class NewPaintWorkflowChecks(unittest.TestCase):
    def test_q_delete_respects_selection_and_undo(self):
        window = MainWindow(recover=False, background=True)
        try:
            image = QImage(96, 64, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor("red"))
            window.document.replace_image(image)
            window.show()
            APP.processEvents()
            canvas = window.canvas
            canvas.zoom = 3
            canvas.setFocus()
            QTest.keyClick(canvas, Qt.Key.Key_Q)
            points = [QPointF(10, 10), QPointF(40, 10), QPointF(40, 40), QPointF(10, 40)]
            screen = [canvas._world_to_screen(p).toPoint() for p in points]
            QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=screen[0])
            for point in screen[1:]:
                QTest.mouseMove(canvas, point)
            QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=screen[0])
            self.assertIsNotNone(window.document.selection_mask)
            QTest.keyClick(canvas, Qt.Key.Key_Delete)
            self.assertEqual(window.document.canvas_image().pixelColor(20, 20).alpha(), 0)
            self.assertEqual(window.document.canvas_image().pixelColor(70, 20), QColor("red"))
            QTest.keyClick(canvas, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(window.document.canvas_image(), image)
        finally:
            window.quit_application()
            APP.processEvents()

    def test_clear_group_uses_soft_selection(self):
        document = PaintDocument(32, 32)
        first = document.active_layer_id
        second = document.add_paint_layer("Second")
        for node in document.iter_layers():
            node.image.fill(QColor("red"))
        group = document.group_layers([first, second])
        document.set_active_layer(group)
        document.selection_mask = QImage(32, 32, QImage.Format.Format_ARGB32_Premultiplied)
        document.selection_mask.fill(QColor(255, 255, 255, 128))
        document.clear_active_layer()
        for node in document.iter_layers():
            if node.image is not None:
                self.assertIn(node.image.pixelColor(10, 10).alpha(), (127, 128))

    def test_reset_shortcuts_archive_every_document_and_keep_canvas_on_failed_save(self):
        with tempfile.TemporaryDirectory(prefix="paintstudio-projects-check-") as directory:
            window = MainWindow(recover=False, background=True, projects_root=Path(directory))
            try:
                window.show()
                APP.processEvents()
                window.canvas.setFocus()
                for count, key in enumerate((Qt.Key.Key_N, Qt.Key.Key_W, Qt.Key.Key_N, Qt.Key.Key_W), 1):
                    image = QImage(64, 48, QImage.Format.Format_ARGB32_Premultiplied)
                    image.fill(QColor("red"))
                    window.document.replace_image(image)
                    window.document.add_paint_layer("Hidden authored layer")
                    window.document.find_layer(window.document.active_layer_id).image.fill(QColor("blue"))
                    window.document.set_layer_property(window.document.active_layer_id, "visible", False)
                    window.document.apply_freehand_selection([QPointF(5, 5), QPointF(20, 5), QPointF(20, 20)])
                    expected = window._document_snapshot()
                    QTest.keyClick(window.canvas, key, Qt.KeyboardModifier.ControlModifier)
                    projects = window.project_library.list_projects()
                    self.assertEqual(len(projects), count)
                    restored, undo, _redo = window.project_library.load(projects[0].project_id)
                    self.assertEqual(restored.image.convertToFormat(expected.image.format()), expected.image)
                    self.assertEqual(restored.selection.convertToFormat(expected.selection.format()), expected.selection)
                    self.assertEqual(restored.roots[1].image.convertToFormat(expected.roots[1].image.format()), expected.roots[1].image)
                    self.assertFalse(restored.roots[1].visible)
                    self.assertGreaterEqual(len(undo), 3)
                    self.assertEqual(window.document.width, 2048)
                    self.assertEqual(window.document.canvas_image().pixelColor(0, 0).alpha(), 0)
                # A blank canvas is not archived.
                window.new_document()
                self.assertEqual(len(window.project_library.list_projects()), 4)
                image = QImage(64, 48, QImage.Format.Format_ARGB32_Premultiplied)
                image.fill(QColor("green"))
                window.document.replace_image(image)
                window.document.add_paint_layer("Unsaved")
                before = window._document_snapshot()
                with patch.object(window.project_library, "save", side_effect=OSError("disk full")):
                    with patch("paint_studio.window.QMessageBox.warning") as warning:
                        window.new_document()
                        warning.assert_called_once()
                self.assertEqual(window.document.revision, before.revision)
                self.assertEqual(window.document.image, before.image)
            finally:
                window.quit_application()
                APP.processEvents()

    def test_close_archives_to_blank_and_open_recent_restores_history(self):
        with tempfile.TemporaryDirectory(prefix="paintstudio-projects-check-") as directory:
            window = MainWindow(recover=False, background=True, projects_root=Path(directory))
            try:
                window.show()
                APP.processEvents()
                window.document.set_brush(color=QColor("red"), size=12.0)
                for y in (100, 140, 180):
                    window.document.begin_stroke(StrokeSample(100, y, 1.0, 0.0))
                    window.document.add_sample(StrokeSample(300, y, 1.0, 16.0))
                    window.document.end_stroke()
                window.document.undo()
                painted_hash = window.document.pixel_hash()
                window.close()
                APP.processEvents()
                self.assertFalse(window.isVisible())
                self.assertFalse(window._shutdown_complete)
                self.assertEqual(window.document.canvas_image().pixelColor(200, 100).alpha(), 0)
                self.assertFalse(window.document.can_undo)
                window.show_recent_projects()
                APP.processEvents()
                popup = window.recent_projects_popup
                self.assertEqual(popup.list.count(), 1)
                project_id = str(popup.list.item(0).data(Qt.ItemDataRole.UserRole))
                popup.hide()
                window.open_project(project_id)
                self.assertEqual(window.document.pixel_hash(), painted_hash)
                self.assertTrue(window.document.can_redo)
                window.document.redo()
                self.assertTrue(window.document.undo())
                self.assertTrue(window.document.undo())
                self.assertTrue(window.document.undo())
                self.assertEqual(window.document.canvas_image().pixelColor(200, 100).alpha(), 0)
                # Closing a reopened project updates it in place.
                window.document.redo()
                window.close()
                APP.processEvents()
                self.assertEqual(len(window.project_library.list_projects()), 1)
            finally:
                window.quit_application()
                APP.processEvents()

    def test_border_mode_outlines_whole_stroke_and_soft_shaped_tips_paint(self):
        document = PaintDocument(400, 200)
        document.select_brush_preset("ink")
        document.set_brush(color=QColor("red"), size=40.0, spacing=0.1, fill_mode="border", border_width=4)
        document.set_brush(pressure_size=False)
        document.begin_stroke(StrokeSample(60, 100, 1.0, 0.0))
        for index in range(1, 15):
            document.add_sample(StrokeSample(60 + index * 20, 100, 1.0, index * 8.0))
        document.end_stroke()
        image = document.canvas_image()
        # One outline around the whole stroke: edges painted, the inside and dab joins left open.
        self.assertGreater(image.pixelColor(200, 81).alpha(), 200)
        self.assertGreater(image.pixelColor(200, 119).alpha(), 200)
        for x in range(120, 300, 3):
            self.assertEqual(image.pixelColor(x, 100).alpha(), 0, x)
            self.assertEqual(image.pixelColor(x, 90).alpha(), 0, x)
        self.assertTrue(document.undo())
        self.assertEqual(document.canvas_image().pixelColor(200, 81).alpha(), 0)

        for preset, tip in (("paint", "square"), ("ink", "triangle"), ("ink", "diamond")):
            document = PaintDocument(200, 200)
            document.select_brush_preset(preset)
            document.set_brush(color=QColor("red"), size=60.0, hardness=0.3, tip_shape=tip, fill_mode="fill")
            document.begin_stroke(StrokeSample(100, 100, 1.0, 0.0))
            document.end_stroke()
            self.assertGreater(document.canvas_image().pixelColor(100, 100).alpha(), 200, (preset, tip))

    def test_fill_region_selection_reference_and_undo(self):
        document = PaintDocument(64, 48)
        image = QImage(64, 48, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("white"))
        painter = QPainter(image)
        painter.fillRect(QRect(31, 0, 2, 48), QColor("black"))
        painter.end()
        document.replace_image(image)
        document.select_brush_preset("fill")
        document.set_brush(color=QColor("red"), fill_threshold=0)
        self.assertTrue(document.flood_fill(QPointF(5, 5)))
        self.assertEqual(document.canvas_image().pixelColor(5, 5), QColor("red"))
        self.assertEqual(document.canvas_image().pixelColor(40, 5), QColor("white"))
        document.undo()
        self.assertEqual(document.canvas_image(), image)
        document.add_paint_layer("Fill underneath lines")
        document.set_brush(fill_reference="visible")
        document.apply_freehand_selection([QPointF(3, 3), QPointF(50, 3), QPointF(50, 25), QPointF(3, 25)])
        document.flood_fill(QPointF(5, 5))
        layer = document.find_layer(document.active_layer_id).image
        self.assertEqual(layer.pixelColor(5, 5), QColor("red"))
        self.assertEqual(layer.pixelColor(40, 5).alpha(), 0)
        self.assertEqual(layer.pixelColor(5, 35).alpha(), 0)
        document.undo()
        self.assertEqual(document.find_layer(document.active_layer_id).image.pixelColor(5, 5).alpha(), 0)

    def test_f_fill_and_mirror_render_input_zoom_and_buttons(self):
        window = MainWindow(recover=False, background=True)
        try:
            image = QImage(80, 60, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor("white"))
            painter = QPainter(image)
            painter.fillRect(QRect(0, 0, 25, 20), QColor("red"))
            painter.end()
            window.document.replace_image(image)
            window.show()
            APP.processEvents()
            canvas = window.canvas
            canvas.zoom = 3
            canvas.setFocus()
            QTest.keyClick(canvas, Qt.Key.Key_M)
            self.assertTrue(canvas.mirror_horizontal)
            self.assertTrue(window.title_bar.mirror_buttons[0].isChecked())
            QTest.mouseClick(window.title_bar.mirror_buttons[1], Qt.MouseButton.LeftButton)
            self.assertTrue(canvas.mirror_vertical)
            world = QPointF(10.5, 10.5)
            screen = canvas._world_to_screen(world)
            self.assertLess((canvas.to_world(screen) - world).manhattanLength(), 0.001)
            rendered = canvas.grab().toImage()
            self.assertEqual(rendered.pixelColor(screen.toPoint()), QColor("red"))
            self.assertEqual(window.document.canvas_image(), image)
            canvas._set_zoom_at(screen, 4)
            self.assertLess((canvas.to_world(screen) - world).manhattanLength(), 0.001)
            canvas.setFocus()
            QTest.keyClick(canvas, Qt.Key.Key_F)
            self.assertEqual(window.document.brush_settings.engine, "fill")
            window.document.set_brush(color=QColor("blue"), fill_threshold=0)
            QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=screen.toPoint())
            self.assertEqual(window.document.canvas_image().pixelColor(10, 10), QColor("blue"))
            self.assertEqual(window.document.canvas_image().pixelColor(60, 40), QColor("white"))
            window.document.undo()
            self.assertEqual(window.document.canvas_image(), image)
            canvas.activate_freehand_selection()
            window.brush_panel.picker.select("air")
            window.brush_panel.picker.select("fill")
            self.assertEqual(canvas.tool, "brush")
            self.assertEqual(window.document.brush_settings.engine, "fill")
        finally:
            window.quit_application()
            APP.processEvents()


def main() -> int:
    started = time.perf_counter()
    # Keep test windows out of the real data folder
    data_root = Path(tempfile.mkdtemp(prefix="paintstudio-check-"))
    (data_root / "PaintStudioData").mkdir()
    storage_patch = patch("paint_studio.storage.APP_ROOT", data_root)
    storage_patch.start()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PaintCoreChecks)
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(NewPaintWorkflowChecks))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {
        "ok": result.wasSuccessful(),
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
    }
    storage_patch.stop()
    shutil.rmtree(data_root, ignore_errors=True)
    print(json.dumps(summary, indent=2))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
