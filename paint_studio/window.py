from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QEvent, QIODevice, QRect, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QImage, QKeySequence, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .canvas import CanvasWidget
from .core import BRUSH_PRESETS, PaintDocument
from .panels import BrushPanel, LayersPanel, QuickColorPopup, RecentProjectsPopup
from .projects import ProjectLibrary, ProjectWriter
from .storage import (
    DocumentBundleStore,
    RecoverySnapshot,
    RecoveryStore,
    RecoveryWriter,
    default_document_path,
)


class ResizeHandle(QWidget):
    def __init__(self, window: "MainWindow", edges: Qt.Edge, cursor: Qt.CursorShape) -> None:
        super().__init__()
        self.window_owner = window
        self.edges = edges
        self.setCursor(cursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self.window_owner.isMaximized():
            handle = self.window_owner.windowHandle()
            if handle is not None:
                handle.startSystemResize(self.edges)
            event.accept()


class WindowFrame(QWidget):
    def __init__(self, window: "MainWindow", content: QWidget) -> None:
        super().__init__()
        self.setObjectName("WindowFrame")
        self.handles: list[ResizeHandle] = []
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        specs = (
            (0, 0, Qt.Edge.TopEdge | Qt.Edge.LeftEdge, Qt.CursorShape.SizeFDiagCursor),
            (0, 1, Qt.Edge.TopEdge, Qt.CursorShape.SizeVerCursor),
            (0, 2, Qt.Edge.TopEdge | Qt.Edge.RightEdge, Qt.CursorShape.SizeBDiagCursor),
            (1, 0, Qt.Edge.LeftEdge, Qt.CursorShape.SizeHorCursor),
            (1, 2, Qt.Edge.RightEdge, Qt.CursorShape.SizeHorCursor),
            (2, 0, Qt.Edge.BottomEdge | Qt.Edge.LeftEdge, Qt.CursorShape.SizeBDiagCursor),
            (2, 1, Qt.Edge.BottomEdge, Qt.CursorShape.SizeVerCursor),
            (2, 2, Qt.Edge.BottomEdge | Qt.Edge.RightEdge, Qt.CursorShape.SizeFDiagCursor),
        )
        for row, column, edges, cursor in specs:
            handle = ResizeHandle(window, edges, cursor)
            self.handles.append(handle)
            layout.addWidget(handle, row, column)
        layout.addWidget(content, 1, 1)
        layout.setRowStretch(1, 1)
        layout.setColumnStretch(1, 1)
        self.set_resizing_enabled(True)

    def set_resizing_enabled(self, enabled: bool) -> None:
        for index, handle in enumerate(self.handles):
            handle.setVisible(enabled)
            if index in (1, 6):
                handle.setFixedHeight(5 if enabled else 0)
            elif index in (3, 4):
                handle.setFixedWidth(5 if enabled else 0)
            else:
                handle.setFixedSize(5 if enabled else 0, 5 if enabled else 0)


class TitleBar(QWidget):
    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window_owner = window
        self.setObjectName("TitleBar")
        self.setFixedHeight(34)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 0, 0)
        layout.setSpacing(0)
        self.menu_button = QToolButton()
        self.menu_button.setObjectName("WindowTitleButton")
        self.menu_button.setText("Paint Studio")
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_button.setMenu(self._build_menu())
        self.menu_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(self.menu_button)
        layout.addStretch(1)
        self.mirror_buttons = []
        for name, text in (("mirror_horizontal", "Mirror H"), ("mirror_vertical", "Mirror V")):
            button = QToolButton()
            button.setObjectName("MirrorButton")
            button.setDefaultAction(window.command_actions[name])
            button.setText(text)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            layout.addWidget(button)
            self.mirror_buttons.append(button)
        self.minimize_button = self._window_button("-", "Minimize")
        self.maximize_button = self._window_button("[]", "Maximize")
        self.close_button = self._window_button("x", "Close", close=True)
        self.minimize_button.clicked.connect(window.showMinimized)
        self.maximize_button.clicked.connect(window.toggle_maximized)
        self.close_button.clicked.connect(window.close)
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)

    def _build_menu(self) -> QMenu:
        menu = QMenu(self)
        for name in ("new", "open", "open_recent", "save", "export"):
            menu.addAction(self.window_owner.command_actions[name])
        menu.addSeparator()
        menu.addAction(self.window_owner.command_actions["undo"])
        menu.addAction(self.window_owner.command_actions["redo"])
        menu.addSeparator()
        for name in (
            "add_layer",
            "group_layers",
            "stamp_visible",
            "clear_layer",
            "toggle_visibility",
            "eraser_mode",
            "quick_color",
            "trim",
        ):
            menu.addAction(self.window_owner.command_actions[name])
        menu.addSeparator()
        for name in ("brush_tool", "smart_shape", "freehand_selection", "transform", "select_all", "deselect", "invert_selection"):
            menu.addAction(self.window_owner.command_actions[name])
        menu.addSeparator()
        menu.addAction(self.window_owner.command_actions["checkerboard"])
        menu.addSeparator()
        menu.addAction(self.window_owner.command_actions["quit"])
        return menu

    def _window_button(self, text: str, tooltip: str, *, close: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("CloseButton" if close else "WindowButton")
        button.setToolTip(tooltip)
        button.setFixedSize(44, 34)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self.window_owner.isMaximized():
            handle = self.window_owner.windowHandle()
            if handle is not None:
                handle.startSystemMove()
            event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.window_owner.toggle_maximized()
            event.accept()


class MainWindow(QMainWindow):
    def __init__(self, *, recover: bool = True, background: bool = False, projects_root: Path | None = None) -> None:
        super().__init__()
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("PaintStudioWindow")
        self.setWindowTitle("Paint Studio")
        self.setMinimumSize(760, 520)
        self._settings_enabled = recover
        self.document = PaintDocument()
        if self._settings_enabled:
            self._restore_brush_profiles()
            self._restore_stamp_tip()
        self.recovery_store = RecoveryStore()
        self.recovery_writer = RecoveryWriter(self.recovery_store)
        self._recovery_enabled = recover
        self.document_store = DocumentBundleStore()
        # Closed paintings get archived here, test windows without recovery skip it.
        self.project_library = ProjectLibrary(projects_root) if recover or projects_root is not None else None
        self.project_writer = ProjectWriter(self.project_library) if self.project_library is not None else None
        self._project_id: str | None = None
        self._project_saved_revision = -1
        self._document_path: Path | None = None
        self._last_recovery_revision = 0
        self._allow_quit = False
        self._shutdown_complete = False
        self._restoring = False
        self.recovery_timer = QTimer(self)
        self.recovery_timer.setSingleShot(True)
        self.recovery_timer.setInterval(750)
        self.recovery_timer.timeout.connect(self._queue_recovery_current)
        self.brush_settings_timer = QTimer(self)
        self.brush_settings_timer.setSingleShot(True)
        self.brush_settings_timer.setInterval(400)
        self.brush_settings_timer.timeout.connect(self._save_brush_profiles)
        self._create_actions()
        self._build_ui()
        self._connect_document()
        if recover:
            self._restoring = True
            self._restore_recovery()
            self._restoring = False
            self._restore_document_path()
        self._restore_geometry()
        QTimer.singleShot(0, self.canvas.fit_document)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown)
        if not background:
            self.show()

    def _create_actions(self) -> None:
        specs = (
            ("new", "New", QKeySequence.StandardKey.New, self.new_document),
            ("new_alias", "New (Ctrl+W)", QKeySequence("Ctrl+W"), self.new_document),
            ("fill_tool", "Fill", QKeySequence("F"), lambda: self.select_brush("fill")),
            ("mirror_horizontal", "Mirror View Horizontally (M)", QKeySequence("M"), self.toggle_mirror_horizontal),
            ("mirror_vertical", "Mirror View Vertically", None, self.toggle_mirror_vertical),
            ("open", "Open", QKeySequence.StandardKey.Open, self.open_document),
            ("open_recent", "Open Recent", None, self.show_recent_projects),
            ("save", "Save", QKeySequence.StandardKey.Save, self.save_document),
            ("export", "Export PNG", None, self.export_image),
            ("undo", "Undo", QKeySequence.StandardKey.Undo, self.document.undo),
            ("redo", "Redo", QKeySequence.StandardKey.Redo, self.document.redo),
            ("add_layer", "New Layer", QKeySequence(Qt.Key.Key_Insert), self.add_layer),
            ("group_layers", "Group Selected Layers", QKeySequence("Ctrl+G"), self.group_selected_layers),
            ("stamp_visible", "Copy Visible to New Layer", QKeySequence(Qt.Key.Key_Home), self.stamp_visible),
            ("clear_layer", "Clear Current Layer", QKeySequence(Qt.Key.Key_Delete), self.document.clear_active_layer),
            (
                "toggle_visibility",
                "Toggle Layer Visibility",
                QKeySequence(Qt.Key.Key_Apostrophe),
                self.toggle_active_layer_visibility,
            ),
            ("trim", "Trim to Canvas", QKeySequence(Qt.Key.Key_End), self.document.trim_to_canvas),
            ("checkerboard", "Checkerboard Background", None, self.toggle_checkerboard),
            ("smart_shape", "Smart Shape", None, self.toggle_smart_shape),
            ("eraser_mode", "Toggle Eraser Mode", QKeySequence(Qt.Key.Key_E), self.toggle_eraser_mode),
            ("quick_color", "Quick Color", QKeySequence(Qt.Key.Key_C), self.show_quick_color),
            ("brush_tool", "Brush Tool", QKeySequence(Qt.Key.Key_B), self.activate_brush_tool),
            (
                "freehand_selection",
                "Freehand Selection Tool",
                QKeySequence(Qt.Key.Key_Q),
                self.activate_freehand_selection,
            ),
            ("transform", "Transform", QKeySequence("Ctrl+T"), self.begin_transform),
            ("select_all", "Select All", QKeySequence.StandardKey.SelectAll, self.document.select_all),
            ("deselect", "Deselect", QKeySequence("Ctrl+Shift+A"), self.document.clear_selection),
            ("invert_selection", "Invert Selection", QKeySequence("Ctrl+Shift+I"), self.document.invert_selection),
            ("quit", "Quit Paint Studio", QKeySequence.StandardKey.Quit, self.quit_application),
        )
        self.command_actions: dict[str, QAction] = {}
        for key, title, shortcut, callback in specs:
            action = QAction(title, self)
            if shortcut is not None:
                action.setShortcut(shortcut)
            action.triggered.connect(callback)
            self.addAction(action)
            self.command_actions[key] = action
        self.command_actions["checkerboard"].setCheckable(True)
        self.command_actions["smart_shape"].setCheckable(True)
        self.command_actions["eraser_mode"].setCheckable(True)
        self.command_actions["mirror_horizontal"].setCheckable(True)
        self.command_actions["mirror_vertical"].setCheckable(True)
        self.command_actions["mirror_horizontal"].setIconText("Mirror H")
        self.command_actions["mirror_vertical"].setIconText("Mirror V")
        self.command_actions["undo"].setEnabled(False)
        self.command_actions["redo"].setEnabled(False)
        for index, preset in enumerate(BRUSH_PRESETS, 1):
            action = QAction(f"Brush {index}: {preset.name}", self)
            action.setShortcut(QKeySequence(str(index)))
            action.triggered.connect(lambda _checked=False, preset_id=preset.preset_id: self.select_brush(preset_id))
            self.addAction(action)
            self.command_actions[f"brush_{index}"] = action

    def _build_ui(self) -> None:
        self.setStyleSheet(self._stylesheet())
        content = QWidget()
        content.setObjectName("WindowContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.title_bar = TitleBar(self)
        root.addWidget(self.title_bar)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        self.brush_panel = BrushPanel(self.document)
        body_layout.addWidget(self.brush_panel)
        self.canvas = CanvasWidget(self.document)
        self.quick_color_popup = QuickColorPopup(self.document)
        self.quick_color_popup.dismissed.connect(self._quick_color_dismissed)
        self.recent_projects_popup = RecentProjectsPopup()
        self.recent_projects_popup.chosen.connect(self.open_project)
        checkerboard = bool(QSettings("PaintStudio", "PaintStudio").value("checkerboardBackground", False, type=bool))
        smart_shape = bool(QSettings("PaintStudio", "PaintStudio").value("smartShapeEnabled", True, type=bool))
        self.command_actions["checkerboard"].setChecked(checkerboard)
        self.command_actions["smart_shape"].setChecked(smart_shape)
        self.canvas.set_checkerboard_background(checkerboard)
        self.canvas.set_smart_shape_enabled(smart_shape)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body_layout.addWidget(self.canvas, 1)
        self.layers_panel = LayersPanel(self.document, self.canvas)
        body_layout.addWidget(self.layers_panel)
        root.addWidget(body, 1)
        root.addWidget(self._build_status_bar())

        self.window_frame = WindowFrame(self, content)
        self.setCentralWidget(self.window_frame)
        self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(26)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(9, 0, 10, 0)
        self.size_label = QLabel()
        layout.addWidget(self.size_label)
        layout.addStretch(1)
        self.zoom_label = QLabel("100%")
        layout.addWidget(self.zoom_label)
        return bar

    def _stylesheet(self) -> str:
        return """
            QMainWindow, #WindowFrame, #WindowContent { background: #17181b; }
            #WindowFrame { border: 1px solid #343740; }
            #TitleBar { background: #0d0e10; border-bottom: 1px solid #2a2c31; }
            #WindowTitleButton { color: #f0f0f2; background: transparent; border: 0; padding: 5px 8px 5px 0; font-weight: 600; }
            #MirrorButton { color: #b9bbc1; background: transparent; border: 1px solid transparent; border-radius: 3px; padding: 4px 8px; }
            #MirrorButton:hover { background: #24262b; }
            #MirrorButton:checked { color: white; background: #3b1830; border-color: #e3008c; }
            #WindowTitleButton:hover { color: white; }
            #WindowButton, #CloseButton { color: #cfd0d4; background: transparent; border: 0; border-radius: 0; padding: 0; }
            #WindowButton:hover { background: #292b30; }
            #CloseButton:hover { color: white; background: #c42b4e; }
            #StatusBar { background: #111215; }
            #StatusBar { border-top: 1px solid #292b31; }
            #BrushPanel { background: #111215; border-right: 1px solid #292b31; }
            #LayersPanel { background: #111215; border-left: 1px solid #292b31; }
            #LayerTree { background: #18191d; border: 1px solid #2e3037; border-radius: 3px; outline: 0; }
            #LayerTree::item { padding: 0; border: 0; }
            #LayerTree::item:selected { background: #3b1830; }
            #LayerRow { background: transparent; }
            #LayerThumbnail { background: #ddd; border: 1px solid #555861; }
            #LayerName { color: #d5d6da; }
            #LayerIconButton { background: transparent; border: 0; padding: 2px; }
            #LayerIconButton:hover { background: #343740; }
            #LayerIconButton:checked { background: #54213f; border: 1px solid #e3008c; }
            #CanvasOverview { border: 1px solid #2e3037; border-radius: 3px; }
            #ChoiceStrip { background: #1a1b20; border: 1px solid #30333b; border-radius: 4px; }
            #ChoiceButton { color: #b9bbc1; background: transparent; border: 1px solid transparent; border-radius: 3px; padding: 0 4px; }
            #ChoiceButton:hover { color: #eeeef0; background: #25272d; }
            #ChoiceButton:checked { color: white; background: #3b1830; border-color: #e3008c; }
            #ChipButton { color: #b9bbc1; background: #1a1b20; border: 1px solid #30333b; border-radius: 4px; padding: 0 6px; }
            #ChipButton:hover { color: #eeeef0; background: #24262b; }
            #ChipButton:pressed { background: #2d3037; }
            #ChipButton:checked { color: white; background: #3b1830; border-color: #e3008c; }
            #BarSliderEditor { color: white; background: #24262b; border: 1px solid #e3008c; border-radius: 3px; padding: 0 8px; selection-background-color: #7a0f55; }
            QScrollArea { background: transparent; border: 0; }
            QScrollArea > QWidget > QWidget { background: #111215; }
            QComboBox { color: #e8e8ea; background: #24262b; border: 1px solid #343740; border-radius: 3px; padding: 4px 7px; }
            QMenu { color: #e8e8ea; background: #17181b; border: 1px solid #343740; padding: 4px; }
            QMenu::item { padding: 6px 28px 6px 10px; border-radius: 3px; }
            QMenu::item:selected { background: #3b1830; color: white; }
            QMenu::separator { height: 1px; background: #343740; margin: 4px 7px; }
            QPushButton { color: #e8e8ea; background: #24262b; border: 1px solid #343740; border-radius: 4px; padding: 5px 10px; }
            QPushButton:hover { background: #2d3037; border-color: #454955; }
            QPushButton:checked { color: white; background: #3b1830; border-color: #e3008c; }
            QPushButton:disabled { color: #666970; background: #1c1d21; border-color: #282a2f; }
            QLabel { color: #b9bbc1; }
        """

    def _connect_document(self) -> None:
        self.document.history_changed.connect(self._history_changed)
        self.document.history_changed.connect(self._schedule_recovery)
        self.document.layers_changed.connect(self._sync_document_size)
        self.document.canvas_bounds_changed.connect(lambda _rect: self._sync_document_size())
        self.document.brush_changed.connect(
            lambda brush, _color: self.command_actions["eraser_mode"].setChecked(bool(brush.eraser))
        )
        if self._settings_enabled:
            self.document.brush_changed.connect(lambda _brush, _color: self.brush_settings_timer.start())
            self.document.stamp_tip_changed.connect(self._save_stamp_tip)
        self.canvas.brush_size_changed.connect(lambda value: self.document.set_brush(size=float(value)))
        self.canvas.zoom_changed.connect(lambda value: self.zoom_label.setText(f"{value}%"))
        self.brush_panel.picker.preset_selected.connect(lambda _preset_id: self.canvas.activate_brush_tool())
        self.canvas.color_used.connect(self.brush_panel.add_recent_color)
        if self._settings_enabled:
            self._restore_recent_colors()
            self.brush_panel.recent_colors_changed.connect(self._save_recent_colors)
        self._sync_document_size()

    def toggle_visible(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
            return
        self.show_and_activate()

    def show_and_activate(self) -> None:
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def quit_application(self) -> None:
        self._allow_quit = True
        self.close()
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)

    def add_layer(self) -> None:
        self.document.add_paint_layer()

    def group_selected_layers(self) -> None:
        self.document.group_layers(self.layers_panel.selected_layer_ids())

    def stamp_visible(self) -> None:
        self.document.stamp_visible()

    def toggle_active_layer_visibility(self) -> None:
        layer = self.document.find_layer(self.document.active_layer_id)
        if layer is not None:
            self.document.set_layer_property(layer.layer_id, "visible", not layer.visible)

    def toggle_checkerboard(self, enabled: bool) -> None:
        if not hasattr(self, "canvas"):
            return
        self.canvas.set_checkerboard_background(enabled)
        QSettings("PaintStudio", "PaintStudio").setValue("checkerboardBackground", bool(enabled))

    def toggle_smart_shape(self, enabled: bool) -> None:
        if not hasattr(self, "canvas"):
            return
        self.canvas.set_smart_shape_enabled(enabled)
        QSettings("PaintStudio", "PaintStudio").setValue("smartShapeEnabled", bool(enabled))

    def show_quick_color(self) -> None:
        self.canvas.release_native_cursor()
        self.quick_color_popup.show_at_cursor()

    def _quick_color_dismissed(self) -> None:
        QTimer.singleShot(0, self._restore_canvas_after_quick_color)

    def _restore_canvas_after_quick_color(self) -> None:
        if not self.isVisible():
            return
        self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)
        self.canvas.refresh_native_cursor()

    def activate_brush_tool(self) -> None:
        self.canvas.activate_brush_tool()

    def select_brush(self, preset_id: str) -> None:
        self.document.select_brush_preset(preset_id)
        self.canvas.activate_brush_tool()

    def toggle_eraser_mode(self) -> None:
        self.document.toggle_eraser()
        self.canvas.activate_brush_tool()

    def activate_freehand_selection(self) -> None:
        self.canvas.activate_freehand_selection()

    def begin_transform(self) -> None:
        self.canvas.begin_transform(self.layers_panel.selected_layer_ids())

    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self._sync_window_state()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_window_state()

    def _sync_window_state(self) -> None:
        maximized = self.isMaximized()
        if hasattr(self, "window_frame"):
            self.window_frame.set_resizing_enabled(not maximized)
        if hasattr(self, "title_bar"):
            self.title_bar.maximize_button.setText("<>" if maximized else "[]")

    def new_document(self) -> None:
        self._finish_canvas_interactions()
        try:
            # Synchronous so a failed save keeps the canvas instead of clearing it.
            self._archive_current(wait=True)
        except Exception:
            logging.getLogger("paintstudio").exception("project save failed; document preserved")
            QMessageBox.warning(self, "Canvas kept", "The project could not be saved. Your canvas has not been cleared.")
            return
        self._start_blank_document()

    def show_recent_projects(self) -> None:
        projects = []
        if self.project_library is not None:
            self.project_writer.flush(5.0)
            projects = self.project_library.list_projects()
        self.recent_projects_popup.show_projects(projects, self.title_bar.menu_button, self._project_id)

    def open_project(self, project_id: str) -> None:
        if self.project_library is None:
            return
        if project_id == self._project_id and self.document.revision == self._project_saved_revision:
            return
        self._finish_canvas_interactions()
        try:
            self._archive_current(wait=False)
            self.project_writer.flush(30.0)
            snapshot, undo, redo = self.project_library.load(project_id)
        except Exception:
            logging.getLogger("paintstudio").exception("project open failed id=%s", project_id)
            QMessageBox.warning(self, "Project not opened", "That project could not be read.")
            return
        self.document.restore_layers(
            list(snapshot.roots),
            snapshot.active_layer_id,
            snapshot.revision,
            origin=snapshot.origin,
            canvas_rect=QRect(*snapshot.canvas_rect) if snapshot.canvas_rect is not None else None,
            selection=snapshot.selection,
        )
        self.document.restore_history(undo, redo)
        self._project_id = project_id
        self._project_saved_revision = self.document.revision
        self._set_document_path(None)
        self._last_recovery_revision = -1
        self.recovery_timer.start()
        self.canvas.fit_document()
        logging.getLogger("paintstudio").info(
            "project opened id=%s revision=%s undo=%s redo=%s", project_id, self.document.revision, len(undo), len(redo)
        )

    def _finish_canvas_interactions(self) -> None:
        self.canvas._finish_active_brush_stroke()
        if self.canvas._interaction:
            self.canvas._end_pointer(Qt.MouseButton.LeftButton)
        if self.canvas.transform_session is not None:
            self.canvas.commit_transform()

    def _document_is_blank(self) -> bool:
        undo, redo = self.document.history_state()
        if undo or redo:
            return False
        blank = QImage(self.document.image.size(), self.document.image.format())
        blank.fill(0)
        return self.document.image == blank

    def _archive_current(self, *, wait: bool) -> None:
        if self.project_library is None:
            return
        if self._project_id is not None and self.document.revision == self._project_saved_revision:
            return
        if self._project_id is None and self._document_is_blank():
            return
        project_id = self._project_id or self.project_library.new_project_id()
        undo, redo = self.document.history_state()
        snapshot = self._document_snapshot()
        if wait:
            self.project_writer.flush(30.0)
            self.project_library.save(project_id, snapshot, undo, redo)
        else:
            self.project_writer.queue(project_id, snapshot, undo, redo)
        self._project_id = project_id
        self._project_saved_revision = self.document.revision
        logging.getLogger("paintstudio").info(
            "project archived id=%s revision=%s undo=%s redo=%s wait=%s",
            project_id,
            self.document.revision,
            len(undo),
            len(redo),
            wait,
        )

    def _start_blank_document(self) -> None:
        self.document.reset()
        self._project_id = None
        self._project_saved_revision = -1
        self._set_document_path(None)
        self._last_recovery_revision = -1
        self.recovery_timer.start()
        self.canvas.fit_document()

    def toggle_mirror_horizontal(self, enabled: bool) -> None:
        self.canvas.set_mirroring(horizontal=enabled)

    def toggle_mirror_vertical(self, enabled: bool) -> None:
        self.canvas.set_mirroring(vertical=enabled)

    def open_document(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open",
            "",
            "Paint Studio (*.paintstudio);;Images (*.png *.jpg *.jpeg *.webp *.bmp)",
        )
        if not path:
            return
        if path.lower().endswith(".paintstudio"):
            snapshot = self.document_store.load(Path(path))
            self.document.restore_layers(
                list(snapshot.roots),
                snapshot.active_layer_id,
                snapshot.revision,
                origin=snapshot.origin,
                canvas_rect=QRect(*snapshot.canvas_rect) if snapshot.canvas_rect is not None else None,
                selection=snapshot.selection,
            )
            self._set_document_path(Path(path))
            self._last_recovery_revision = -1
            self.recovery_timer.start()
            self.canvas.fit_document()
            logging.getLogger("paintstudio").info(
                "document opened revision=%s size=%sx%s layers=%s path=%s",
                snapshot.revision,
                snapshot.image.width(),
                snapshot.image.height(),
                len(tuple(self.document.iter_layers())),
                path,
            )
            return
        image = QImage(path)
        if image.isNull():
            return
        self.document.replace_image(image)
        self._set_document_path(None)
        self._last_recovery_revision = -1
        self.recovery_timer.start()
        self.canvas.fit_document()
        logging.getLogger("paintstudio").info("image opened size=%sx%s path=%s", image.width(), image.height(), path)

    def save_document(self) -> None:
        path = str(self._document_path) if self._document_path else ""
        if not path:
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Save Paint Studio document",
                str(default_document_path()),
                "Paint Studio (*.paintstudio)",
            )
        if not path:
            return
        started_path = Path(path)
        self._set_document_path(self.document_store.write(started_path, self._document_snapshot()))
        logging.getLogger("paintstudio").info(
            "document saved revision=%s layers=%s path=%s",
            self.document.revision,
            len(tuple(self.document.iter_layers())),
            self._document_path,
        )

    def _document_snapshot(self) -> RecoverySnapshot:
        return RecoverySnapshot(
            self.document.revision,
            self.document.image.copy(),
            tuple(node.clone() for node in self.document.roots),
            self.document.active_layer_id,
            self.document.origin,
            self.document.canvas_rect.getRect(),
            QImage(self.document.selection_mask) if self.document.selection_mask is not None else None,
        )

    def export_image(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export image", "painting.png", "PNG image (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        temp = f"{path}.tmp"
        image = self.document.canvas_image()
        if not image.save(temp, "PNG"):
            logging.getLogger("paintstudio").error("image save failed path=%s", path)
            return
        Path(temp).replace(path)
        logging.getLogger("paintstudio").info(
            "image exported revision=%s size=%sx%s path=%s",
            self.document.revision,
            image.width(),
            image.height(),
            path,
        )

    def _history_changed(self, can_undo: bool, can_redo: bool) -> None:
        self.command_actions["undo"].setEnabled(can_undo)
        self.command_actions["redo"].setEnabled(can_redo)

    def _sync_document_size(self) -> None:
        self.size_label.setText(f"{self.document.width} x {self.document.height}")

    def _schedule_recovery(self, _can_undo: bool, _can_redo: bool) -> None:
        if self._recovery_enabled and not self._restoring:
            self.recovery_timer.start()

    def _queue_recovery_current(self) -> None:
        if not self._recovery_enabled:
            return
        revision = self.document.revision
        if revision <= self._last_recovery_revision:
            return
        self._queue_recovery(revision)

    def _queue_recovery(self, revision: int) -> None:
        self._last_recovery_revision = revision
        self.recovery_writer.queue(
            revision,
            self.document.image,
            self.document.roots,
            self.document.active_layer_id,
            self.document.origin,
            self.document.canvas_rect.getRect(),
            self.document.selection_mask,
        )

    def _set_document_path(self, path: Path | None) -> None:
        self._document_path = path
        settings = QSettings("PaintStudio", "PaintStudio")
        settings.setValue("lastDocumentPath", str(path) if path is not None else "")

    def _restore_document_path(self) -> None:
        value = str(QSettings("PaintStudio", "PaintStudio").value("lastDocumentPath", "") or "")
        path = Path(value) if value else None
        self._document_path = path if path is not None and path.is_file() else None

    def _restore_brush_profiles(self) -> None:
        raw = str(QSettings("PaintStudio", "PaintStudio").value("brushProfiles", "") or "")
        if not raw:
            return
        try:
            profiles = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if isinstance(profiles, dict):
            self.document.restore_brush_profiles(profiles)

    def _save_brush_profiles(self) -> None:
        QSettings("PaintStudio", "PaintStudio").setValue(
            "brushProfiles",
            json.dumps(self.document.brush_profiles(), separators=(",", ":")),
        )

    def _restore_stamp_tip(self) -> None:
        data = QSettings("PaintStudio", "PaintStudio").value("stampTipPng", QByteArray())
        if isinstance(data, QByteArray) and not data.isEmpty():
            image = QImage.fromData(data, "PNG")
            if not image.isNull():
                self.document.set_stamp_tip_image(image)

    def _save_stamp_tip(self, image: QImage) -> None:
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if image.save(buffer, "PNG"):
            QSettings("PaintStudio", "PaintStudio").setValue("stampTipPng", QByteArray(buffer.data()))

    def _restore_recent_colors(self) -> None:
        raw = str(QSettings("PaintStudio", "PaintStudio").value("recentColors", "") or "")
        try:
            names = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            names = []
        if isinstance(names, list):
            self.brush_panel.set_recent_colors(names)

    def _save_recent_colors(self, names: list) -> None:
        QSettings("PaintStudio", "PaintStudio").setValue("recentColors", json.dumps(list(names)))

    def _restore_recovery(self) -> None:
        snapshot = self.recovery_store.load_snapshot()
        if snapshot is None:
            return
        if snapshot.roots:
            self.document.restore_layers(
                list(snapshot.roots),
                snapshot.active_layer_id,
                snapshot.revision,
                origin=snapshot.origin,
                canvas_rect=QRect(*snapshot.canvas_rect) if snapshot.canvas_rect is not None else None,
                selection=snapshot.selection,
            )
        else:
            self.document.restore_flat_image(snapshot.image, snapshot.revision)
        self._last_recovery_revision = snapshot.revision
        self.recovery_writer.mark_saved(snapshot.revision)
        logging.getLogger("paintstudio").info(
            "recovery loaded revision=%s size=%sx%s layers=%s",
            snapshot.revision,
            snapshot.image.width(),
            snapshot.image.height(),
            len(tuple(self.document.iter_layers())),
        )

    def _restore_geometry(self) -> None:
        settings = QSettings("PaintStudio", "PaintStudio")
        geometry = settings.value("windowGeometry", QByteArray())
        if isinstance(geometry, QByteArray) and not geometry.isEmpty() and self.restoreGeometry(geometry):
            return
        screen = self.screen()
        available = screen.availableGeometry()
        width = min(1600, int(available.width() * 0.86))
        height = min(1050, int(available.height() * 0.86))
        self.resize(width, height)
        self.move(available.center() - self.rect().center())

    def closeEvent(self, event: QCloseEvent) -> None:
        if hasattr(self, "quick_color_popup"):
            self.quick_color_popup.hide()
        settings = QSettings("PaintStudio", "PaintStudio")
        settings.setValue("windowGeometry", self.saveGeometry())
        if not self._allow_quit:
            self.hide()
            self._close_to_blank(wait=False)
            logging.getLogger("paintstudio").info("application closed to blank revision=%s", self.document.revision)
            event.ignore()
            return
        self._shutdown()
        event.accept()

    def _close_to_blank(self, *, wait: bool) -> None:
        if self.project_library is None:
            return
        self._finish_canvas_interactions()
        try:
            self._archive_current(wait=wait)
        except Exception:
            # Save failed, keep the painting and its recovery bundle instead of clearing it.
            logging.getLogger("paintstudio").exception("project save on close failed; document kept")
            return
        if not self._document_is_blank() or self.document.selection_mask is not None:
            self._start_blank_document()

    def hideEvent(self, event: QEvent) -> None:
        if hasattr(self, "quick_color_popup"):
            self.quick_color_popup.hide()
        super().hideEvent(event)

    def _shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        self._close_to_blank(wait=True)
        self.recovery_timer.stop()
        self._queue_recovery_current()
        if self._last_recovery_revision > 0:
            self.recovery_writer.flush(self._last_recovery_revision)
        self.recovery_writer.close()
        if self.project_writer is not None:
            self.project_writer.close()
        logging.getLogger("paintstudio").info("application closed revision=%s", self.document.revision)
