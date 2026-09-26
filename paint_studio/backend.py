from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import time

from PySide6.QtGui import QColor, QImage

from .core import PaintDocument, StrokeBundle
from .storage import default_data_root


class PaintingBackend:
    """Local bridge commands against the live document."""

    def __init__(self, document: PaintDocument, output_root: Path | None = None) -> None:
        self.document = document
        self.output_root = output_root or default_data_root() / "Bridge"

    @property
    def canvas_path(self) -> Path:
        return self.output_root / "current-canvas.png"

    @property
    def check_path(self) -> Path:
        return self.output_root / "current-canvas.json"

    @property
    def pending_path(self) -> Path:
        return self.output_root / "current-canvas.pending.json"

    def dispatch(self, request: dict[str, object]) -> dict[str, object]:
        started = time.perf_counter()
        command = str(request.get("command", ""))
        try:
            result = self._dispatch(command, request)
            result.update(
                {
                    "ok": True,
                    "command": command,
                    "revision": self.document.revision,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
                }
            )
            return result
        except (KeyError, OSError, TypeError, ValueError) as error:
            logging.getLogger("paintstudio.backend").warning(
                "bridge command failed command=%s revision=%s error_type=%s error=%s",
                command or "<empty>",
                self.document.revision,
                type(error).__name__,
                str(error)[:240],
            )
            return {
                "ok": False,
                "command": command,
                "error": str(error),
                "revision": self.document.revision,
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
            }

    def _dispatch(self, command: str, request: dict[str, object]) -> dict[str, object]:
        if command == "canvas_info":
            return self._canvas_info()
        if command == "canvas_png":
            return self._canvas_png()
        if command == "canvas_check":
            return self._canvas_check()
        if command == "layers":
            return {"layers": [self._layer_data(node) for node in reversed(self.document.roots)]}
        if command == "set_brush":
            self._set_brush(request)
            return {"brush": self.document.brush_settings.to_dict(), "color": self.document.brush_color.name()}
        if command == "stroke_bundle":
            raw_bundle = request.get("bundle")
            if not isinstance(raw_bundle, dict):
                raise ValueError("stroke_bundle requires a bundle object")
            result = self.document.execute_bundle(StrokeBundle.from_dict(raw_bundle))
            return {
                "stroke_count": result.stroke_count,
                "sample_count": result.sample_count,
                "dab_count": result.dab_count,
                "render_ms": round(result.elapsed_ms, 2),
                "dirty": [result.dirty.x(), result.dirty.y(), result.dirty.width(), result.dirty.height()],
            }
        if command == "add_layer":
            return {"layer_id": self.document.add_paint_layer(str(request.get("name") or "Paint Layer"))}
        if command == "add_group":
            return {"layer_id": self.document.add_group(str(request.get("name") or "Group"))}
        if command == "set_active_layer":
            layer_id = str(request["layer_id"])
            if not self.document.set_active_layer(layer_id):
                raise ValueError(f"Unknown or empty layer: {layer_id}")
            return {"layer_id": self.document.active_layer_id}
        if command == "set_layer":
            layer_id = str(request["layer_id"])
            property_name = str(request["property"])
            if not self.document.set_layer_property(layer_id, property_name, request.get("value")):
                raise ValueError(f"Invalid layer property: {property_name}")
            return {"layer_id": layer_id, "property": property_name, "value": request.get("value")}
        if command == "undo":
            return {"changed": self.document.undo()}
        if command == "redo":
            return {"changed": self.document.redo()}
        raise ValueError(f"Unknown command: {command or '<empty>'}")

    def _canvas_info(self) -> dict[str, object]:
        return {
            "width": self.document.width,
            "height": self.document.height,
            "active_layer_id": self.document.active_layer_id,
            "layer_count": sum(1 for _ in self.document.iter_layers()),
            "pixel_hash": self.document.pixel_hash(),
        }

    def _canvas_png(self) -> dict[str, object]:
        self.output_root.mkdir(parents=True, exist_ok=True)
        # Snapshot once so the PNG and JSON match even with another command queued.
        image = self.document.canvas_image()
        revision = self.document.revision
        width, height = image.width(), image.height()
        pixel_hash = self._pixel_hash(image)
        transaction_id = os.urandom(16).hex()
        pending = {
            "schema": 1,
            "transaction_id": transaction_id,
            "revision": revision,
            "width": width,
            "height": height,
            "pixel_hash": pixel_hash,
            "started_unix_ns": time.time_ns(),
        }
        self._write_json_atomic(self.pending_path, pending)

        temp = self.output_root / ".current-canvas.png.tmp"
        if not image.save(str(temp), "PNG"):
            raise OSError("Could not encode the current canvas")
        os.replace(temp, self.canvas_path)
        facts = self._png_facts(self.canvas_path)
        if facts["width"] != width or facts["height"] != height or facts["pixel_hash"] != pixel_hash:
            raise OSError("Bridge PNG verification did not match the exported document snapshot")

        check = {
            "schema": 2,
            "transaction_id": transaction_id,
            "path": str(self.canvas_path),
            "revision": revision,
            "width": width,
            "height": height,
            "pixel_hash": pixel_hash,
            "png_pixel_hash": facts["pixel_hash"],
            "png_sha256": facts["sha256"],
            "png_bytes": facts["bytes"],
            "png_mtime_ns": facts["mtime_ns"],
            "updated_unix_ns": time.time_ns(),
        }
        # JSON goes last, a crash before the pending file is removed reads as invalid.
        self._write_json_atomic(self.check_path, check)
        self.pending_path.unlink()
        result = self._canvas_check()
        if not bool(result["check_valid"]):
            raise OSError(f"Bridge check failed: {result.get('check_error', 'unknown error')}")
        logging.getLogger("paintstudio.backend").info(
            "bridge canvas exported revision=%s size=%sx%s png_bytes=%s sha256=%s",
            revision,
            width,
            height,
            facts["bytes"],
            str(facts["sha256"])[:12],
        )
        return result

    def _canvas_check(self) -> dict[str, object]:
        if self.pending_path.exists():
            return self._invalid_check("Bridge export is incomplete or was interrupted", None, None)
        if not self.canvas_path.is_file() or not self.check_path.is_file():
            raise OSError("Current canvas PNG or check file is missing")
        try:
            check = json.loads(self.check_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise OSError(f"Current canvas check file is unreadable: {error}") from error
        if not isinstance(check, dict) or check.get("schema") != 2:
            return self._invalid_check("Current canvas check schema is invalid", check if isinstance(check, dict) else None, None)
        try:
            facts = self._png_facts(self.canvas_path)
        except OSError as error:
            return self._invalid_check(f"Current canvas PNG is unreadable: {error}", check, None)

        current = self._canvas_info()
        check_revision = self._as_int(check.get("revision"))
        check_width = self._as_int(check.get("width"))
        check_height = self._as_int(check.get("height"))
        failures: list[str] = []
        if str(check.get("path") or "") != str(self.canvas_path):
            failures.append("check path")
        if not str(check.get("transaction_id") or ""):
            failures.append("check transaction")
        if check_revision != self.document.revision:
            failures.append("document revision")
        if check_width != self.document.width or check_height != self.document.height:
            failures.append("document dimensions")
        if check_width != facts["width"] or check_height != facts["height"]:
            failures.append("PNG dimensions")
        if str(check.get("pixel_hash") or "") != str(current["pixel_hash"]):
            failures.append("document pixel hash")
        if str(check.get("png_pixel_hash") or "") != str(facts["pixel_hash"]):
            failures.append("PNG pixel hash")
        if str(check.get("pixel_hash") or "") != str(facts["pixel_hash"]):
            failures.append("PNG/document pixel match")
        if self._as_int(check.get("png_bytes")) != facts["bytes"]:
            failures.append("PNG byte count")
        if self._as_int(check.get("png_mtime_ns")) != facts["mtime_ns"]:
            failures.append("PNG mtime")
        if str(check.get("png_sha256") or "") != str(facts["sha256"]):
            failures.append("PNG SHA-256")
        return {
            "path": str(self.canvas_path),
            "check_path": str(self.check_path),
            "check_valid": not failures,
            "check_error": "; ".join(failures) if failures else None,
            "check_revision": check_revision,
            "check_width": check_width,
            "check_height": check_height,
            "check_pixel_hash": check.get("pixel_hash"),
            "png_pixel_hash": facts["pixel_hash"],
            "png_sha256": facts["sha256"],
            "png_bytes": facts["bytes"],
            "png_width": facts["width"],
            "png_height": facts["height"],
        }

    def _invalid_check(
        self,
        error: str,
        check: dict[str, object] | None,
        facts: dict[str, object] | None,
    ) -> dict[str, object]:
        return {
            "path": str(self.canvas_path),
            "check_path": str(self.check_path),
            "check_valid": False,
            "check_error": error,
            "check_revision": self._as_int(check.get("revision")) if check else None,
            "check_width": self._as_int(check.get("width")) if check else None,
            "check_height": self._as_int(check.get("height")) if check else None,
            "check_pixel_hash": check.get("pixel_hash") if check else None,
            "png_pixel_hash": facts.get("pixel_hash") if facts else None,
            "png_sha256": facts.get("sha256") if facts else None,
            "png_bytes": facts.get("bytes") if facts else None,
            "png_width": facts.get("width") if facts else None,
            "png_height": facts.get("height") if facts else None,
        }

    def _write_json_atomic(self, path: Path, data: dict[str, object]) -> None:
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)

    def _png_facts(self, path: Path) -> dict[str, object]:
        image = QImage(str(path))
        if image.isNull():
            raise OSError("PNG could not be decoded")
        stat = path.stat()
        content = path.read_bytes()
        return {
            "width": image.width(),
            "height": image.height(),
            "pixel_hash": self._pixel_hash(image),
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }

    @staticmethod
    def _pixel_hash(image: QImage) -> str:
        normalized = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        bits = normalized.constBits()
        return hashlib.sha256(bytes(bits[: normalized.sizeInBytes()])).hexdigest()

    @staticmethod
    def _as_int(value: object) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _set_brush(self, request: dict[str, object]) -> None:
        preset_id = request.get("preset_id")
        if preset_id:
            self.document.select_brush_preset(str(preset_id))
        color = QColor(str(request["color"])) if request.get("color") else None
        self.document.set_brush(
            color=color,
            size=float(request["size"]) if request.get("size") is not None else None,
            eraser=bool(request["eraser"]) if request.get("eraser") is not None else None,
            opacity=float(request["opacity"]) if request.get("opacity") is not None else None,
            flow=float(request["flow"]) if request.get("flow") is not None else None,
            spacing=float(request["spacing"]) if request.get("spacing") is not None else None,
            hardness=float(request["hardness"]) if request.get("hardness") is not None else None,
        )

    def _layer_data(self, node) -> dict[str, object]:
        return {
            "id": node.layer_id,
            "name": node.name,
            "kind": node.kind,
            "visible": node.visible,
            "opacity": node.opacity,
            "blend_mode": node.blend_mode,
            "alpha_locked": node.alpha_locked,
            "clipping": node.clipping,
            "children": [self._layer_data(child) for child in reversed(node.children)],
        }


def encode_response(response: dict[str, object]) -> bytes:
    return (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
