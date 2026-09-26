from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from PySide6.QtNetwork import QLocalSocket


INSTANCE_NAME = "paint-studio-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send one JSON command to a running Paint Studio instance.")
    parser.add_argument("request", nargs="?", help="JSON request object")
    parser.add_argument("--file", type=Path, help="Read the JSON request from a file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.file:
        request = json.loads(args.file.read_text(encoding="utf-8"))
    elif args.request:
        request = json.loads(args.request)
    else:
        request = {"command": "canvas_info"}
    if not isinstance(request, dict):
        raise SystemExit("Request must be a JSON object")
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_NAME)
    if not socket.waitForConnected(1000):
        raise SystemExit("Paint Studio is not running")
    socket.write((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
    socket.flush()
    if not socket.waitForReadyRead(30_000):
        raise SystemExit("Paint Studio did not respond")
    payload = bytes(socket.readAll())
    while not payload.endswith(b"\n") and socket.waitForReadyRead(50):
        payload += bytes(socket.readAll())
    response = json.loads(payload.decode("utf-8"))
    print(json.dumps(response, indent=2))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
