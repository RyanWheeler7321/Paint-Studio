from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil
import threading
import time
import zipfile

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QImage

from .core.layers import LayerNode


FORMAT_VERSION = 3
SUPPORTED_FORMATS = {2, 3}
APP_ROOT = Path(__file__).resolve().parents[1]


def default_data_root() -> Path:
    # A PaintStudioData folder beside the app wins (portable), otherwise Documents.
    portable = APP_ROOT / "PaintStudioData"
    if portable.is_dir():
        return portable
    documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    return Path(documents or Path.home() / "Documents") / "PaintStudioData"


def default_documents_root(data_root: Path | None = None) -> Path:
    """Default folder for editable .paintstudio documents."""
    return (data_root or default_data_root()) / "documents"


def default_document_path(data_root: Path | None = None) -> Path:
    return default_documents_root(data_root) / "painting.paintstudio"


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    revision: int
    image: QImage
    roots: tuple[LayerNode, ...] = ()
    active_layer_id: str = ""
    origin: tuple[int, int] = (0, 0)
    canvas_rect: tuple[int, int, int, int] | None = None
    selection: QImage | None = None


class RecoveryStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_data_root() / "Recovery"
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "current.json"

    def write(self, snapshot: RecoverySnapshot) -> Path:
        bundle_name = f"document-{snapshot.revision:08d}"
        bundle_path = self.root / bundle_name
        bundle_temp = self.root / f".{bundle_name}.tmp"
        shutil.rmtree(bundle_temp, ignore_errors=True)
        bundle_temp.mkdir(parents=True)

        composite_path = bundle_temp / "composite.png"
        if not snapshot.image.save(str(composite_path), "PNG"):
            raise OSError(f"Could not write recovery image: {composite_path}")
        if snapshot.selection is not None and not snapshot.selection.save(str(bundle_temp / "selection.png"), "PNG"):
            raise OSError("Could not write recovery selection")
        tree = [self._write_layer(node, bundle_temp) for node in snapshot.roots]
        (bundle_temp / "document.json").write_text(
            json.dumps(
                {
                    "format": FORMAT_VERSION,
                    "revision": snapshot.revision,
                    "width": snapshot.image.width(),
                    "height": snapshot.image.height(),
                    "origin": list(snapshot.origin),
                    "canvas_rect": list(snapshot.canvas_rect or (0, 0, snapshot.image.width(), snapshot.image.height())),
                    "selection": "selection.png" if snapshot.selection is not None else None,
                    "active_layer_id": snapshot.active_layer_id,
                    "roots": tree,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        shutil.rmtree(bundle_path, ignore_errors=True)
        os.replace(bundle_temp, bundle_path)

        manifest = {
            "format": FORMAT_VERSION,
            "revision": snapshot.revision,
            "bundle": bundle_name,
            "saved_at": time.time(),
        }
        manifest_temp = self.root / ".current.json.tmp"
        manifest_temp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(manifest_temp, self.manifest_path)
        self._trim(keep={bundle_name})
        return bundle_path

    def load_snapshot(self) -> RecoverySnapshot | None:
        if not self.manifest_path.exists():
            return None
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if manifest.get("format") == 1:
                image = QImage(str(self.root / str(manifest["image"])))
                if image.isNull():
                    return None
                return RecoverySnapshot(int(manifest.get("revision", 0)), image)
            if manifest.get("format") not in SUPPORTED_FORMATS:
                return None
            bundle = self.root / str(manifest["bundle"])
            document = json.loads((bundle / "document.json").read_text(encoding="utf-8"))
            image = QImage(str(bundle / "composite.png"))
            if image.isNull():
                return None
            roots = tuple(self._read_layer(data, bundle) for data in document.get("roots", []))
            selection_name = document.get("selection")
            selection = QImage(str(bundle / str(selection_name))) if selection_name else None
            if selection is not None and selection.isNull():
                raise OSError("Could not read recovery selection")
            return RecoverySnapshot(
                int(document.get("revision", 0)),
                image,
                roots,
                str(document.get("active_layer_id", "")),
                self._origin(document),
                self._canvas_rect(document, image),
                selection,
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            logging.getLogger("paintstudio.storage").exception("Recovery load failed")
            return None

    def load(self) -> tuple[QImage, int] | None:
        snapshot = self.load_snapshot()
        return (snapshot.image, snapshot.revision) if snapshot is not None else None

    def _write_layer(self, node: LayerNode, bundle: Path) -> dict[str, object]:
        data: dict[str, object] = {
            "id": node.layer_id,
            "name": node.name,
            "kind": node.kind,
            "visible": node.visible,
            "opacity": node.opacity,
            "blend_mode": node.blend_mode,
            "alpha_locked": node.alpha_locked,
            "clipping": node.clipping,
            "children": [self._write_layer(child, bundle) for child in node.children],
        }
        if node.image is not None:
            image_name = f"layer-{node.layer_id}.png"
            if not node.image.save(str(bundle / image_name), "PNG"):
                raise OSError(f"Could not write recovery layer: {node.layer_id}")
            data["image"] = image_name
        return data

    def _read_layer(self, data: dict[str, object], bundle: Path) -> LayerNode:
        image_name = data.get("image")
        image = QImage(str(bundle / str(image_name))) if image_name else None
        if image is not None and image.isNull():
            raise OSError(f"Could not read recovery layer: {image_name}")
        return LayerNode(
            name=str(data.get("name", "Layer")),
            kind=str(data.get("kind", "paint")),
            layer_id=str(data["id"]),
            visible=bool(data.get("visible", True)),
            opacity=float(data.get("opacity", 1.0)),
            blend_mode=str(data.get("blend_mode", "Normal")),
            alpha_locked=bool(data.get("alpha_locked", False)),
            clipping=bool(data.get("clipping", False)),
            image=image,
            children=[self._read_layer(child, bundle) for child in data.get("children", [])],
        )

    def _origin(self, document: dict[str, object]) -> tuple[int, int]:
        value = document.get("origin")
        if isinstance(value, list) and len(value) >= 2:
            return int(value[0]), int(value[1])
        return 0, 0

    def _canvas_rect(self, document: dict[str, object], image: QImage) -> tuple[int, int, int, int]:
        value = document.get("canvas_rect")
        if isinstance(value, list) and len(value) >= 4:
            return tuple(int(part) for part in value[:4])
        origin = self._origin(document)
        return origin[0], origin[1], image.width(), image.height()

    def _trim(self, *, keep: set[str]) -> None:
        candidates = sorted(
            self.root.glob("document-*") ,
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        retained = 0
        for path in candidates:
            if path.name in keep or retained < 3:
                retained += 1
                continue
            try:
                shutil.rmtree(path)
            except OSError:
                logging.getLogger("paintstudio.storage").warning("Could not trim recovery bundle", exc_info=True)
        for path in self.root.glob("canvas-*.png"):
            try:
                path.unlink()
            except OSError:
                pass


class RecoveryWriter:
    def __init__(self, store: RecoveryStore) -> None:
        self.store = store
        self._condition = threading.Condition()
        self._pending: RecoverySnapshot | None = None
        self._saved_revision = -1
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="PaintRecovery", daemon=True)
        self._thread.start()

    def queue(
        self,
        revision: int,
        image: QImage,
        roots: list[LayerNode] | None = None,
        active_layer_id: str = "",
        origin: tuple[int, int] = (0, 0),
        canvas_rect: tuple[int, int, int, int] | None = None,
        selection: QImage | None = None,
    ) -> None:
        snapshot = RecoverySnapshot(
            revision,
            image.copy(),
            tuple(node.clone() for node in roots or ()),
            active_layer_id,
            origin,
            canvas_rect,
            QImage(selection) if selection is not None else None,
        )
        with self._condition:
            self._pending = snapshot
            self._condition.notify_all()

    def mark_saved(self, revision: int) -> None:
        with self._condition:
            self._saved_revision = max(self._saved_revision, revision)

    def flush(self, revision: int, timeout: float = 4.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._saved_revision < revision and time.monotonic() < deadline:
                self._condition.wait(max(0.01, deadline - time.monotonic()))
            return self._saved_revision >= revision

    def close(self, timeout: float = 4.0) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._thread.join(timeout)

    def _run(self) -> None:
        logger = logging.getLogger("paintstudio.storage")
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._pending is None and self._stopping:
                    return
                snapshot = self._pending
                self._pending = None
            assert snapshot is not None
            started = time.perf_counter()
            try:
                path = self.store.write(snapshot)
                logger.info(
                    "recovery saved revision=%s size=%sx%s layers=%s elapsed_ms=%.1f path=%s",
                    snapshot.revision,
                    snapshot.image.width(),
                    snapshot.image.height(),
                    sum(1 for _ in self._walk(snapshot.roots)),
                    (time.perf_counter() - started) * 1000.0,
                    path,
                )
                with self._condition:
                    self._saved_revision = max(self._saved_revision, snapshot.revision)
                    self._condition.notify_all()
            except Exception:
                logger.exception("recovery save failed revision=%s", snapshot.revision)

    def _walk(self, nodes: tuple[LayerNode, ...]):
        for node in nodes:
            yield node
            yield from self._walk(tuple(node.children))


class DocumentBundleStore:
    """Single-file .paintstudio save and load, written atomically."""

    def write(self, path: Path, snapshot: RecoverySnapshot) -> Path:
        path = path.with_suffix(".paintstudio") if path.suffix.lower() != ".paintstudio" else path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.tmp")
        manifest = {
            "format": FORMAT_VERSION,
            "revision": snapshot.revision,
            "width": snapshot.image.width(),
            "height": snapshot.image.height(),
            "origin": list(snapshot.origin),
            "canvas_rect": list(snapshot.canvas_rect or (0, 0, snapshot.image.width(), snapshot.image.height())),
            "selection": "selection.png" if snapshot.selection is not None else None,
            "active_layer_id": snapshot.active_layer_id,
            "roots": [self._layer_metadata(node) for node in snapshot.roots],
        }
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            archive.writestr("document.json", json.dumps(manifest, indent=2) + "\n")
            archive.writestr("composite.png", self._png_bytes(snapshot.image))
            for node in self._walk(snapshot.roots):
                if node.image is not None:
                    archive.writestr(f"layers/{node.layer_id}.png", self._png_bytes(node.image))
            if snapshot.selection is not None:
                archive.writestr("selection.png", self._png_bytes(snapshot.selection))
        os.replace(temp, path)
        return path

    def load(self, path: Path) -> RecoverySnapshot:
        with zipfile.ZipFile(path, "r") as archive:
            manifest = json.loads(archive.read("document.json"))
            if manifest.get("format") not in SUPPORTED_FORMATS:
                raise ValueError(f"Unsupported Paint Studio document format: {manifest.get('format')}")
            composite = QImage.fromData(archive.read("composite.png"), "PNG")
            if composite.isNull():
                raise ValueError("The Paint Studio document has no valid composite")
            roots = tuple(self._layer_from_archive(data, archive) for data in manifest.get("roots", []))
            selection_name = manifest.get("selection")
            selection = QImage.fromData(archive.read(str(selection_name)), "PNG") if selection_name else None
            if selection is not None and selection.isNull():
                raise ValueError("Invalid Paint Studio selection")
            return RecoverySnapshot(
                int(manifest.get("revision", 0)),
                composite,
                roots,
                str(manifest.get("active_layer_id", "")),
                self._origin(manifest),
                self._canvas_rect(manifest, composite),
                selection,
            )

    def _origin(self, manifest: dict[str, object]) -> tuple[int, int]:
        value = manifest.get("origin")
        if isinstance(value, list) and len(value) >= 2:
            return int(value[0]), int(value[1])
        return 0, 0

    def _canvas_rect(self, manifest: dict[str, object], image: QImage) -> tuple[int, int, int, int]:
        value = manifest.get("canvas_rect")
        if isinstance(value, list) and len(value) >= 4:
            return tuple(int(part) for part in value[:4])
        origin = self._origin(manifest)
        return origin[0], origin[1], image.width(), image.height()

    def _layer_metadata(self, node: LayerNode) -> dict[str, object]:
        return {
            "id": node.layer_id,
            "name": node.name,
            "kind": node.kind,
            "visible": node.visible,
            "opacity": node.opacity,
            "blend_mode": node.blend_mode,
            "alpha_locked": node.alpha_locked,
            "clipping": node.clipping,
            "image": f"layers/{node.layer_id}.png" if node.image is not None else None,
            "children": [self._layer_metadata(child) for child in node.children],
        }

    def _layer_from_archive(self, data: dict[str, object], archive: zipfile.ZipFile) -> LayerNode:
        image_name = data.get("image")
        image = QImage.fromData(archive.read(str(image_name)), "PNG") if image_name else None
        if image is not None and image.isNull():
            raise ValueError(f"Invalid layer image: {image_name}")
        return LayerNode(
            name=str(data.get("name", "Layer")),
            kind=str(data.get("kind", "paint")),
            layer_id=str(data["id"]),
            visible=bool(data.get("visible", True)),
            opacity=float(data.get("opacity", 1.0)),
            blend_mode=str(data.get("blend_mode", "Normal")),
            alpha_locked=bool(data.get("alpha_locked", False)),
            clipping=bool(data.get("clipping", False)),
            image=image,
            children=[self._layer_from_archive(child, archive) for child in data.get("children", [])],
        )

    def _png_bytes(self, image: QImage) -> bytes:
        from PySide6.QtCore import QBuffer, QIODevice

        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise OSError("Could not encode document image")
        return bytes(buffer.data())

    def _walk(self, nodes: tuple[LayerNode, ...]):
        for node in nodes:
            yield node
            yield from self._walk(tuple(node.children))
