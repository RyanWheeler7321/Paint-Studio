"""Autosaved projects, each saved with its full undo/redo history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid
import zipfile

from PySide6.QtCore import QBuffer, QIODevice, QRect, Qt
from PySide6.QtGui import QImage

from .core.document import HistoryEntry, TilePatch
from .core.layers import LayerNode
from .storage import RecoverySnapshot, default_data_root

PROJECT_FORMAT = 1
THUMBNAIL_EDGE = 192
# Qt maps PNG quality to zlib level, 80 is still lossless and quick to write.
PNG_QUALITY = 80


def default_projects_root() -> Path:
    return default_data_root() / "Projects"


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    project_id: str
    path: Path
    modified: float
    revision: int
    width: int
    height: int
    thumbnail_path: Path | None


class _ImageWriter:
    """Deduplicates identical images inside one project archive."""

    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.archive = archive
        self.formats: dict[str, int] = {}

    def add(self, image: QImage | None) -> str | None:
        if image is None or image.isNull():
            return None
        digest = hashlib.blake2b(digest_size=16)
        digest.update(f"{image.width()}x{image.height()}:{image.format().value}:".encode())
        digest.update(image.constBits()[: image.sizeInBytes()])
        key = digest.hexdigest()
        if key not in self.formats:
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not image.save(buffer, "PNG", PNG_QUALITY):
                raise OSError("Could not encode project image")
            self.archive.writestr(f"images/{key}.png", bytes(buffer.data()), compress_type=zipfile.ZIP_STORED)
            self.formats[key] = image.format().value
        return key


class _ImageReader:
    def __init__(self, archive: zipfile.ZipFile, formats: dict[str, int]) -> None:
        self.archive = archive
        self.formats = formats
        self._cache: dict[str, QImage] = {}

    def get(self, key: str | None) -> QImage | None:
        if not key:
            return None
        image = self._cache.get(key)
        if image is None:
            image = QImage.fromData(self.archive.read(f"images/{key}.png"), "PNG")
            if image.isNull():
                raise ValueError(f"Invalid project image: {key}")
            image = image.convertToFormat(QImage.Format(self.formats.get(key, QImage.Format.Format_ARGB32_Premultiplied.value)))
            self._cache[key] = image
        # Callers get their own copy, shared data detaches on first write.
        return QImage(image)


def _layer_json(node: LayerNode, images: _ImageWriter) -> dict[str, object]:
    return {
        "id": node.layer_id,
        "name": node.name,
        "kind": node.kind,
        "visible": node.visible,
        "opacity": node.opacity,
        "blend_mode": node.blend_mode,
        "alpha_locked": node.alpha_locked,
        "clipping": node.clipping,
        "image": images.add(node.image),
        "children": [_layer_json(child, images) for child in node.children],
    }


def _layer_from_json(data: dict[str, object], images: _ImageReader) -> LayerNode:
    return LayerNode(
        name=str(data.get("name", "Layer")),
        kind=str(data.get("kind", "paint")),
        layer_id=str(data["id"]),
        visible=bool(data.get("visible", True)),
        opacity=float(data.get("opacity", 1.0)),
        blend_mode=str(data.get("blend_mode", "Normal")),
        alpha_locked=bool(data.get("alpha_locked", False)),
        clipping=bool(data.get("clipping", False)),
        image=images.get(data.get("image")),
        children=[_layer_from_json(child, images) for child in data.get("children", [])],
    )


def _tree_json(roots: list[LayerNode] | None, images: _ImageWriter) -> list[dict[str, object]] | None:
    return None if roots is None else [_layer_json(node, images) for node in roots]


def _tree_from_json(data, images: _ImageReader) -> list[LayerNode] | None:
    return None if data is None else [_layer_from_json(node, images) for node in data]


def _rect_json(rect: QRect | None) -> list[int] | None:
    return None if rect is None else [rect.x(), rect.y(), rect.width(), rect.height()]


def _rect_from_json(data) -> QRect | None:
    return None if data is None else QRect(*(int(value) for value in data))


def _point_json(point: tuple[int, int] | None) -> list[int] | None:
    return None if point is None else [int(point[0]), int(point[1])]


def _point_from_json(data) -> tuple[int, int] | None:
    return None if data is None else (int(data[0]), int(data[1]))


def _entry_json(entry: HistoryEntry, images: _ImageWriter) -> dict[str, object]:
    patches = None
    if entry.patches is not None:
        patches = [
            {
                "layer_id": patch.layer_id,
                "x": patch.x,
                "y": patch.y,
                "before": images.add(patch.before),
                "after": images.add(patch.after),
            }
            for patch in entry.patches
        ]
    return {
        "patches": patches,
        "roots_before": _tree_json(entry.roots_before, images),
        "roots_after": _tree_json(entry.roots_after, images),
        "active_before": entry.active_before,
        "active_after": entry.active_after,
        "origin_before": _point_json(entry.origin_before),
        "origin_after": _point_json(entry.origin_after),
        "canvas_before": _rect_json(entry.canvas_before),
        "canvas_after": _rect_json(entry.canvas_after),
        "selection_before": images.add(entry.selection_before),
        "selection_after": images.add(entry.selection_after),
    }


def _entry_from_json(data: dict[str, object], images: _ImageReader) -> HistoryEntry:
    patches = None
    if data.get("patches") is not None:
        patches = [
            TilePatch(
                str(patch["layer_id"]),
                int(patch["x"]),
                int(patch["y"]),
                images.get(patch["before"]),
                images.get(patch["after"]),
            )
            for patch in data["patches"]
        ]
    return HistoryEntry(
        patches=patches,
        roots_before=_tree_from_json(data.get("roots_before"), images),
        roots_after=_tree_from_json(data.get("roots_after"), images),
        active_before=data.get("active_before"),
        active_after=data.get("active_after"),
        origin_before=_point_from_json(data.get("origin_before")),
        origin_after=_point_from_json(data.get("origin_after")),
        canvas_before=_rect_from_json(data.get("canvas_before")),
        canvas_after=_rect_from_json(data.get("canvas_after")),
        selection_before=images.get(data.get("selection_before")),
        selection_after=images.get(data.get("selection_after")),
    )


class ProjectLibrary:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_projects_root()
        self.root.mkdir(parents=True, exist_ok=True)

    def new_project_id(self) -> str:
        return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def save(
        self,
        project_id: str,
        snapshot: RecoverySnapshot,
        undo: list[HistoryEntry],
        redo: list[HistoryEntry],
    ) -> ProjectInfo:
        started = time.perf_counter()
        folder = self.root / project_id
        folder.mkdir(parents=True, exist_ok=True)
        archive_path = folder / "project.zip"
        temp = folder / ".project.zip.tmp"
        canvas = snapshot.canvas_rect or (snapshot.origin[0], snapshot.origin[1], snapshot.image.width(), snapshot.image.height())
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            images = _ImageWriter(archive)
            manifest = {
                "format": PROJECT_FORMAT,
                "revision": snapshot.revision,
                "origin": list(snapshot.origin),
                "canvas_rect": list(canvas),
                "active_layer_id": snapshot.active_layer_id,
                "composite": images.add(snapshot.image),
                "selection": images.add(snapshot.selection),
                "roots": [_layer_json(node, images) for node in snapshot.roots],
                "undo": [_entry_json(entry, images) for entry in undo],
                "redo": [_entry_json(entry, images) for entry in redo],
            }
            manifest["image_formats"] = images.formats
            archive.writestr("project.json", json.dumps(manifest, separators=(",", ":")))
        os.replace(temp, archive_path)

        thumbnail_path = folder / "thumbnail.png"
        image_rect = QRect(canvas[0] - snapshot.origin[0], canvas[1] - snapshot.origin[1], canvas[2], canvas[3])
        thumbnail = snapshot.image.copy(image_rect.intersected(snapshot.image.rect()))
        if not thumbnail.isNull():
            thumbnail = thumbnail.scaled(
                THUMBNAIL_EDGE,
                THUMBNAIL_EDGE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            thumbnail.save(str(thumbnail_path), "PNG")

        modified = time.time()
        info = {
            "format": PROJECT_FORMAT,
            "revision": snapshot.revision,
            "width": int(canvas[2]),
            "height": int(canvas[3]),
            "modified": modified,
            "undo": len(undo),
            "redo": len(redo),
        }
        info_temp = folder / ".info.json.tmp"
        info_temp.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
        os.replace(info_temp, folder / "info.json")
        logging.getLogger("paintstudio.projects").info(
            "project saved id=%s revision=%s undo=%s redo=%s images=%s elapsed_ms=%.1f",
            project_id,
            snapshot.revision,
            len(undo),
            len(redo),
            len(images.formats),
            (time.perf_counter() - started) * 1000.0,
        )
        return self._info(folder, info)

    def list_projects(self) -> list[ProjectInfo]:
        projects = []
        for folder in self.root.iterdir() if self.root.is_dir() else ():
            if not folder.is_dir() or not (folder / "project.zip").is_file():
                continue
            try:
                info = json.loads((folder / "info.json").read_text(encoding="utf-8"))
                projects.append(self._info(folder, info))
            except (OSError, ValueError, TypeError, KeyError):
                continue
        projects.sort(key=lambda project: project.modified, reverse=True)
        return projects

    def load(self, project_id: str) -> tuple[RecoverySnapshot, list[HistoryEntry], list[HistoryEntry]]:
        with zipfile.ZipFile(self.root / project_id / "project.zip", "r") as archive:
            manifest = json.loads(archive.read("project.json"))
            if manifest.get("format") != PROJECT_FORMAT:
                raise ValueError(f"Unsupported Paint Studio project format: {manifest.get('format')}")
            images = _ImageReader(archive, {key: int(value) for key, value in manifest.get("image_formats", {}).items()})
            composite = images.get(manifest["composite"])
            if composite is None:
                raise ValueError("The project has no composite image")
            snapshot = RecoverySnapshot(
                int(manifest.get("revision", 0)),
                composite,
                tuple(_layer_from_json(node, images) for node in manifest.get("roots", [])),
                str(manifest.get("active_layer_id", "")),
                _point_from_json(manifest.get("origin")) or (0, 0),
                tuple(int(value) for value in manifest["canvas_rect"]),
                images.get(manifest.get("selection")),
            )
            undo = [_entry_from_json(entry, images) for entry in manifest.get("undo", [])]
            redo = [_entry_from_json(entry, images) for entry in manifest.get("redo", [])]
        return snapshot, undo, redo

    def _info(self, folder: Path, info: dict[str, object]) -> ProjectInfo:
        thumbnail = folder / "thumbnail.png"
        return ProjectInfo(
            project_id=folder.name,
            path=folder,
            modified=float(info["modified"]),
            revision=int(info.get("revision", 0)),
            width=int(info.get("width", 0)),
            height=int(info.get("height", 0)),
            thumbnail_path=thumbnail if thumbnail.is_file() else None,
        )


class ProjectWriter:
    """Background saves so closing the window never waits on PNG encoding."""

    def __init__(self, library: ProjectLibrary) -> None:
        self.library = library
        self._condition = threading.Condition()
        self._pending: dict[str, tuple[RecoverySnapshot, list[HistoryEntry], list[HistoryEntry]]] = {}
        self._busy = False
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="PaintProjects", daemon=True)
        self._thread.start()

    def queue(
        self,
        project_id: str,
        snapshot: RecoverySnapshot,
        undo: list[HistoryEntry],
        redo: list[HistoryEntry],
    ) -> None:
        with self._condition:
            self._pending[project_id] = (snapshot, list(undo), list(redo))
            self._condition.notify_all()

    def flush(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while (self._pending or self._busy) and time.monotonic() < deadline:
                self._condition.wait(max(0.01, deadline - time.monotonic()))
            return not self._pending and not self._busy

    def close(self, timeout: float = 30.0) -> None:
        self.flush(timeout)
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._thread.join(timeout)

    def _run(self) -> None:
        logger = logging.getLogger("paintstudio.projects")
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if not self._pending and self._stopping:
                    return
                project_id = next(iter(self._pending))
                snapshot, undo, redo = self._pending.pop(project_id)
                self._busy = True
            try:
                self.library.save(project_id, snapshot, undo, redo)
            except Exception:
                logger.exception("project save failed id=%s revision=%s", project_id, snapshot.revision)
                (self.library.root / project_id / ".project.zip.tmp").unlink(missing_ok=True)
            finally:
                with self._condition:
                    self._busy = False
                    self._condition.notify_all()
