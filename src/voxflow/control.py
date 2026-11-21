"""Unix-socket control channel.

This is what makes voxflow usable even without evdev access: a GNOME custom
keyboard shortcut can run `voxflow toggle`, which talks to the running
daemon over this socket. Push-to-talk needs raw key events, but
toggle-to-dictate does not.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Callable

from .config import socket_path

log = logging.getLogger(__name__)

Handler = Callable[[str], dict]


class ControlServer(threading.Thread):
    def __init__(self, handler: Handler):
        super().__init__(daemon=True, name="voxflow-control")
        self.handler = handler
        self.path = socket_path()
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        # A leftover socket from a crashed daemon would make bind() fail.
        if self.path.exists():
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.5)
                probe.connect(str(self.path))
                probe.close()
                raise RuntimeError(f"another voxflow daemon owns {self.path}")
            except (ConnectionRefusedError, socket.timeout, OSError):
                self.path.unlink(missing_ok=True)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self.path.chmod(0o600)
        self._sock.listen(8)
        self._sock.settimeout(1.0)
        super().start()

    def run(self) -> None:
        while not self._stop.is_set():
            if self._sock is None:
                return
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(5.0)
                    raw = conn.recv(4096).decode("utf-8", "replace").strip()
                    reply = self.handler(raw) if raw else {"ok": False, "error": "empty command"}
                    conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
                except Exception as exc:
                    log.debug("control connection failed: %s", exc)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self.path.unlink(missing_ok=True)


def send_command(command: str, timeout: float = 5.0) -> dict:
    """Send a command to a running daemon. Raises ConnectionError if none."""
    path = socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise ConnectionError(
            "voxflow daemon is not running (try: systemctl --user start voxflow)"
        ) from exc
    try:
        sock.sendall(command.encode("utf-8"))
        data = sock.recv(65536).decode("utf-8", "replace").strip()
    finally:
        sock.close()
    if not data:
        return {"ok": True}
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return {"ok": False, "error": data}
