from __future__ import annotations

import argparse
import ctypes
import faulthandler
import json
import logging
import os
from pathlib import Path
import sys
import time

from PySide6.QtCore import QAbstractNativeEventFilter, qVersion
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from .backend import PaintingBackend, encode_response
from .window import MainWindow


INSTANCE_NAME = "paint-studio-v1"
_CRASH_STREAM = None


WM_TABLET_QUERYSYSTEMGESTURESTATUS = 0x02CC
TABLET_DISABLE_PRESSANDHOLD = 0x00000001
TABLET_DISABLE_PENTAPFEEDBACK = 0x00000008
TABLET_DISABLE_PENBARRELFEEDBACK = 0x00000010
PAINT_STUDIO_TABLET_FLAGS = (
    TABLET_DISABLE_PRESSANDHOLD
    | TABLET_DISABLE_PENTAPFEEDBACK
    | TABLET_DISABLE_PENBARRELFEEDBACK
)


class _WindowsMessage(ctypes.Structure):
    _fields_ = (("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint))


class NativePenFeedbackFilter(QAbstractNativeEventFilter):
    """Disable legacy Windows tablet feedback inside Paint Studio."""

    def nativeEventFilter(self, _event_type, message) -> tuple[bool, int]:
        if os.name != "nt":
            return False, 0
        try:
            native_message = ctypes.cast(int(message), ctypes.POINTER(_WindowsMessage)).contents
        except (TypeError, ValueError, OSError):
            return False, 0
        if native_message.message != WM_TABLET_QUERYSYSTEMGESTURESTATUS:
            return False, 0
        return True, PAINT_STUDIO_TABLET_FLAGS


def application_icon_path() -> Path:
    return Path(__file__).resolve().parent / "assets" / "paint-studio.ico"


def configure_windows_identity() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PaintStudio.App")
    except (AttributeError, OSError):
        logging.getLogger("paintstudio").exception("could not set Windows application identity")


def runtime_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "PaintStudio"


def configure_logging() -> Path:
    global _CRASH_STREAM
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    log_path = root / "paintstudio.log"
    crash_path = root / "crash.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s pid=%(process)d %(name)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
        force=True,
    )
    _CRASH_STREAM = crash_path.open("a", encoding="utf-8", buffering=1)
    faulthandler.enable(_CRASH_STREAM, all_threads=True)

    def report_unhandled(exc_type, exc_value, traceback) -> None:
        logging.getLogger("paintstudio").critical(
            "unhandled exception",
            exc_info=(exc_type, exc_value, traceback),
        )

    sys.excepthook = report_unhandled
    return log_path


def send_existing(command: str, timeout_ms: int = 350) -> bool:
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_NAME)
    if not socket.waitForConnected(timeout_ms):
        return False
    socket.write((command + "\n").encode("utf-8"))
    socket.flush()
    socket.waitForBytesWritten(timeout_ms)
    socket.disconnectFromServer()
    return True


class InstanceServer:
    def __init__(self, window: MainWindow, app: QApplication, instance_name: str = INSTANCE_NAME) -> None:
        self.window = window
        self.app = app
        self.backend = PaintingBackend(window.document)
        self.server = QLocalServer(window)
        self.server.newConnection.connect(self._read_connection)
        if not self.server.listen(instance_name):
            QLocalServer.removeServer(instance_name)
            if not self.server.listen(instance_name):
                raise RuntimeError(f"Could not listen on {instance_name}: {self.server.errorString()}")

    def _read_connection(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            socket.waitForReadyRead(150)
            payload = bytes(socket.readAll())
            while not payload.endswith(b"\n") and len(payload) < 16 * 1024 * 1024 and socket.waitForReadyRead(20):
                payload += bytes(socket.readAll())
            command = payload.decode("utf-8", errors="replace").strip()
            if command == "toggle":
                self.window.toggle_visible()
            elif command == "show":
                self.window.show_and_activate()
            elif command == "quit":
                self.window.quit_application()
            elif command.startswith("{"):
                try:
                    request = json.loads(command)
                    if not isinstance(request, dict):
                        raise ValueError("Request must be a JSON object")
                    response = self.backend.dispatch(request)
                except (json.JSONDecodeError, ValueError) as error:
                    response = {"ok": False, "error": str(error)}
                socket.write(encode_response(response))
                socket.flush()
                socket.waitForBytesWritten(500)
            socket.disconnectFromServer()


def write_runtime_state() -> None:
    path = runtime_root() / "runtime.json"
    temp = runtime_root() / ".runtime.json.tmp"
    temp.write_text(
        json.dumps({"pid": os.getpid(), "instance": INSTANCE_NAME}) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--toggle", action="store_true")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--quit", action="store_true")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--no-recover", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    started = time.perf_counter()
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    configure_windows_identity()
    app = QApplication([sys.argv[0]])
    app.setApplicationName("Paint Studio")
    app.setOrganizationName("PaintStudio")
    app.setWindowIcon(QIcon(str(application_icon_path())))
    app.setQuitOnLastWindowClosed(False)
    pen_feedback_filter = NativePenFeedbackFilter() if os.name == "nt" else None
    if pen_feedback_filter is not None:
        app.installNativeEventFilter(pen_feedback_filter)

    requested = (
        "quit"
        if args.quit
        else "show"
        if args.show
        else "toggle"
        if args.toggle
        else "background"
        if args.background
        else "show"
    )
    if requested and send_existing(requested):
        return 0

    if args.quit:
        return 0

    log_path = configure_logging()
    logger = logging.getLogger("paintstudio")
    window = MainWindow(recover=not args.no_recover, background=args.background)
    server = InstanceServer(window, app)
    write_runtime_state()
    logger.info(
        "application ready pid=%s qt=%s visible=%s startup_ms=%.1f log=%s",
        os.getpid(),
        qVersion(),
        int(window.isVisible()),
        (time.perf_counter() - started) * 1000.0,
        log_path,
    )
    _ = server, pen_feedback_filter
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
