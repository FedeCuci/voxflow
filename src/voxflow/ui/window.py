"""The voxflow app window: status, one-click dictation, and recent history.

Note what this window deliberately does *not* do: paste at the cursor. While
it has focus, a synthesised Ctrl+V would paste into voxflow itself, so the
button here dictates to the **clipboard** instead. Inserting at the cursor is
what the global hotkey is for - this window is for checking state, dictating
without a hotkey, and getting back text that went astray.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from ..config import Config  # noqa: E402
from ..control import send_command  # noqa: E402
from ..history import History  # noqa: E402
from ..hotkey import pretty_name  # noqa: E402
from ..inject import clipboard_set  # noqa: E402

APP_ID = "dev.voxflow.App"
POLL_MS = 700

STATES = {
    "stopped": ("Not running", "process-stop-symbolic"),
    "idle": ("Ready", "audio-input-microphone-symbolic"),
    "recording": ("Listening…", "audio-input-microphone-symbolic"),
    "processing": ("Transcribing…", "content-loading-symbolic"),
}


class VoxflowWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="voxflow", default_width=420, default_height=640)
        self.cfg = Config.load()
        self._history = History()
        self._state = "stopped"
        self._shown_count = -1
        self._recent_rows: list[Adw.ActionRow] = []

        header = Adw.HeaderBar()
        menu = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu.set_menu_model(self._menu_model())
        header.pack_end(menu)

        self.icon = Gtk.Image(
            icon_name="audio-input-microphone-symbolic", pixel_size=72, margin_top=24
        )
        self.state_label = Gtk.Label(label="Checking…", margin_top=12)
        self.state_label.add_css_class("title-1")
        self.detail_label = Gtk.Label(
            label="",
            margin_top=4,
            wrap=True,
            max_width_chars=34,
            justify=Gtk.Justification.CENTER,
        )
        self.detail_label.add_css_class("dim-label")

        self.level = Gtk.LevelBar(
            min_value=0.0,
            max_value=1.0,
            margin_start=48,
            margin_end=48,
            margin_top=16,
            visible=False,
        )

        self.dictate_button = Gtk.Button(
            label="Dictate to clipboard", margin_top=24, halign=Gtk.Align.CENTER
        )
        self.dictate_button.add_css_class("pill")
        self.dictate_button.add_css_class("suggested-action")
        self.dictate_button.connect("clicked", self._on_dictate)

        self.cancel_button = Gtk.Button(
            label="Cancel", margin_top=8, halign=Gtk.Align.CENTER, visible=False
        )
        self.cancel_button.add_css_class("flat")
        self.cancel_button.connect("clicked", lambda *_: self._send("cancel"))

        self.hint_label = Gtk.Label(
            label="",
            margin_top=20,
            wrap=True,
            max_width_chars=38,
            justify=Gtk.Justification.CENTER,
        )
        self.hint_label.add_css_class("dim-label")
        self.hint_label.add_css_class("caption")

        self.recent_group = Adw.PreferencesGroup(
            title="Recent", margin_top=28, margin_start=12, margin_end=12, margin_bottom=12
        )

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for widget in (
            self.icon,
            self.state_label,
            self.detail_label,
            self.level,
            self.dictate_button,
            self.cancel_button,
            self.hint_label,
            self.recent_group,
        ):
            column.append(widget)

        clamp = Adw.Clamp(maximum_size=460, tightening_threshold=360)
        clamp.set_child(column)

        scroller = Gtk.ScrolledWindow(
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_width=True,
        )
        scroller.set_child(clamp)

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(scroller)
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        self._refresh_hint()
        self._tick()
        self._timer = GLib.timeout_add(POLL_MS, self._tick)
        self.connect("close-request", self._on_close)

    # -------------------------------------------------------------------- menu

    def _menu_model(self) -> Gtk.MenuModel:
        menu = Gtk.Builder.new_from_string(
            """
            <interface>
              <menu id="menu">
                <section>
                  <item>
                    <attribute name="label">Settings…</attribute>
                    <attribute name="action">win.settings</attribute>
                  </item>
                  <item>
                    <attribute name="label">Reload configuration</attribute>
                    <attribute name="action">win.reload</attribute>
                  </item>
                </section>
              </menu>
            </interface>
            """,
            -1,
        ).get_object("menu")

        for name, handler in (
            ("settings", self._on_settings),
            ("reload", lambda *_: self._send("reload", toast="Configuration reloaded")),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
        return menu

    # ------------------------------------------------------------------ polling

    def _tick(self) -> bool:
        """Poll the daemon off the main loop so a wedged socket cannot stall the UI."""

        def worker() -> None:
            try:
                reply = send_command("status", timeout=1.0)
            except ConnectionError:
                reply = {"state": "stopped"}
            GLib.idle_add(self._apply, reply)

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _apply(self, reply: dict) -> bool:
        state = reply.get("state", "stopped")
        self._state = state
        label, icon = STATES.get(state, STATES["idle"])

        self.state_label.set_label(label)
        self.icon.set_from_icon_name(icon)

        recording = state == "recording"
        self.level.set_visible(recording)
        self.cancel_button.set_visible(recording)
        if recording:
            self.level.set_value(min(1.0, float(reply.get("level", 0.0)) * 4))
            elapsed = reply.get("elapsed", 0.0)
            target = "clipboard" if reply.get("clipboard") else "your cursor"
            self.detail_label.set_label(f"{elapsed:.0f}s — will go to {target}")
            self.dictate_button.set_label("Stop and copy")
        elif state == "processing":
            self.detail_label.set_label("Transcribing and cleaning up…")
            self.dictate_button.set_label("Dictate to clipboard")
        elif state == "stopped":
            self.detail_label.set_label("The dictation daemon is not running")
            self.dictate_button.set_label("Start voxflow")
        else:
            count = reply.get("dictations", 0)
            self.detail_label.set_label(
                f"{count} dictation{'s' if count != 1 else ''} this session"
                if count
                else "Ready when you are"
            )
            self.dictate_button.set_label("Dictate to clipboard")

        self.dictate_button.set_sensitive(state in {"idle", "recording", "stopped"})

        # Reload the list only when something new was actually produced.
        count = reply.get("dictations", 0)
        if state != "stopped" and count != self._shown_count:
            self._shown_count = count
            self._load_recent()
        return False

    def _refresh_hint(self) -> None:
        self.cfg = Config.load()
        verb = "Hold" if self.cfg.hotkey.mode == "hold" else "Press"
        key = pretty_name(self.cfg.hotkey.key)
        self.hint_label.set_label(
            f"To type straight into another app, {verb.lower()} {key} there instead.\n"
            "The button above copies instead of pasting, because this window has focus."
        )

    # ------------------------------------------------------------------ history

    def _load_recent(self) -> None:
        for row in self._recent_rows:
            self.recent_group.remove(row)
        self._recent_rows = []

        entries = self._history.recent(8)
        if not entries:
            placeholder = Adw.ActionRow(
                title="Nothing dictated yet",
                subtitle="Your last few dictations will appear here",
            )
            placeholder.set_sensitive(False)
            self.recent_group.add(placeholder)
            self._recent_rows.append(placeholder)
            return

        for entry in entries:
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(_preview(entry.final_text)),
                subtitle=f"{_ago(entry.ts)} · {entry.duration:.0f}s"
                + ("" if entry.cleaned else " · raw"),
            )
            row.set_activatable(True)
            row.connect("activated", self._on_copy, entry.final_text)
            copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER)
            copy.add_css_class("flat")
            copy.set_tooltip_text("Copy")
            copy.connect("clicked", self._on_copy, entry.final_text)
            row.add_suffix(copy)
            self.recent_group.add(row)
            self._recent_rows.append(row)

    # ------------------------------------------------------------------ actions

    def _on_dictate(self, _button) -> None:
        if self._state == "stopped":
            self._on_start_daemon()
        elif self._state == "recording":
            self._send("stop")
        else:
            self._send("dictate")

    def _on_copy(self, _widget, text: str) -> None:
        try:
            clipboard_set(text)
            self._toast("Copied")
        except Exception as exc:
            self._toast(f"Could not copy: {exc}")

    def _on_settings(self, *_args) -> None:
        # Prefer the binary sitting next to this one, so a build run from the
        # source tree opens its own settings rather than an installed copy.
        sibling = Path(sys.argv[0]).resolve().parent / "voxflow"
        for command in ([str(sibling), "settings"], ["voxflow", "settings"]):
            try:
                subprocess.Popen(command)
                return
            except OSError:
                continue
        self._toast("Could not find the voxflow binary to open settings")

    def _on_start_daemon(self, *_args) -> None:
        # Preferred once installed through the NixOS module, which provides
        # the user service.
        try:
            subprocess.run(
                ["systemctl", "--user", "start", "voxflow"],
                check=True,
                capture_output=True,
                timeout=10,
            )
            self._toast("Starting voxflow…")
            return
        except (subprocess.SubprocessError, OSError):
            pass

        # No service unit yet - running straight from a build. Launch the
        # daemon directly so the app works before being installed.
        sibling = Path(sys.argv[0]).resolve().parent / "voxflow"
        for command in ([str(sibling), "daemon"], ["voxflow", "daemon"]):
            try:
                subprocess.Popen(
                    command,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._toast("Started the daemon")
                return
            except OSError:
                continue
        self._toast("Could not start the daemon")

    def _send(self, command: str, toast: str = "") -> None:
        try:
            send_command(command, timeout=2.0)
            if toast:
                self._toast(toast)
        except ConnectionError as exc:
            self._toast(str(exc))
        self._tick()

    def _toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=message, timeout=3))

    def _on_close(self, *_args) -> bool:
        if getattr(self, "_timer", None):
            GLib.source_remove(self._timer)
            self._timer = None
        self._history.close()
        return False


def _preview(text: str, limit: int = 52) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _ago(ts: float) -> str:
    seconds = max(0, int(time.time() - ts))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return time.strftime("%d %b", time.localtime(ts))


class VoxflowApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)

    def do_activate(self) -> None:
        window = self.props.active_window or VoxflowWindow(self)
        window.present()


def main() -> int:
    return VoxflowApp().run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
