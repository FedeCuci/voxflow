"""Desktop feedback.

GNOME's Wayland compositor does not let a client place an always-on-top
overlay or take focus politely, and a window that *did* appear would steal
focus from the app we are about to type into. Notifications are the only
focus-safe channel, so state changes go through notify-send with a replace
id so repeated updates land in one bubble instead of stacking.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger(__name__)

APP_NAME = "voxflow"


class Notifier:
    def __init__(self, enabled: bool = True, sounds: bool = False):
        self.enabled = enabled
        self.sounds = sounds
        self._last_id: str | None = None
        self._available = shutil.which("notify-send") is not None
        if enabled and not self._available:
            log.warning("notify-send not found; desktop notifications disabled")

    def notify(
        self,
        summary: str,
        body: str = "",
        icon: str = "audio-input-microphone-symbolic",
        timeout_ms: int = 2500,
        urgency: str = "low",
        replace: bool = True,
    ) -> None:
        if not (self.enabled and self._available):
            return
        cmd = [
            "notify-send",
            "--app-name",
            APP_NAME,
            "--icon",
            icon,
            "--expire-time",
            str(timeout_ms),
            "--urgency",
            urgency,
            "--print-id",
        ]
        if replace and self._last_id:
            cmd += ["--replace-id", self._last_id]
        cmd += [summary]
        if body:
            cmd += [body]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            new_id = result.stdout.strip()
            if new_id.isdigit():
                self._last_id = new_id
        except (subprocess.SubprocessError, OSError) as exc:
            log.debug("notify-send failed: %s", exc)

    def error(self, summary: str, body: str = "") -> None:
        self.notify(
            summary,
            body,
            icon="dialog-error-symbolic",
            timeout_ms=6000,
            urgency="normal",
        )

    def play(self, event: str) -> None:
        """Optional audio cue. Uses the freedesktop sound theme via canberra."""
        if not self.sounds:
            return
        sound = {"start": "message-new-instant", "stop": "complete"}.get(event)
        if not sound or not shutil.which("canberra-gtk-play"):
            return
        try:
            subprocess.Popen(
                ["canberra-gtk-play", "-i", sound],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass
