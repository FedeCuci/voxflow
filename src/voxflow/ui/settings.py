"""GTK4/libadwaita settings window.

The daemon itself is deliberately windowless: on Wayland a window that
appeared would take focus away from the application we are about to type
into. Settings therefore live in a separate process, and changes are handed
to the running daemon over its control socket.
"""

from __future__ import annotations

import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..audio import list_sources  # noqa: E402
from ..cleanup import CLEANUP_MODEL  # noqa: E402
from ..config import Config, Secrets  # noqa: E402
from ..control import send_command  # noqa: E402
from ..hotkey import can_read_devices  # noqa: E402

APP_ID = "dev.voxflow.Settings"

MODES = [("hold", "Hold to talk"), ("toggle", "Press to start and stop")]
PROVIDERS = [("groq", "Groq (whisper-large-v3-turbo)"), ("openai", "OpenAI")]
STYLES = [
    ("default", "Prose"),
    ("email", "Email"),
    ("message", "Chat message"),
    ("notes", "Notes"),
    ("code", "Code and technical terms"),
]
SUPPRESS = [
    ("auto", "Automatic — mouse buttons only"),
    ("always", "Always"),
    ("never", "Never"),
]
METHODS = [
    ("paste", "Paste (fast, any layout)"),
    ("type", "Type character by character"),
]


def _combo(title: str, options: list[tuple[str, str]], current: str) -> Adw.ComboRow:
    row = Adw.ComboRow(title=title)
    row.set_model(Gtk.StringList.new([label for _, label in options]))
    values = [value for value, _ in options]
    row.set_selected(values.index(current) if current in values else 0)
    row._values = values  # keep the value mapping with the widget
    return row


def _combo_value(row: Adw.ComboRow) -> str:
    index = row.get_selected()
    values = getattr(row, "_values", [])
    return values[index] if 0 <= index < len(values) else ""


def _spin(title: str, low: float, high: float, step: float, value: float, digits: int = 0):
    row = Adw.SpinRow.new_with_range(low, high, step)
    row.set_title(title)
    row.set_digits(digits)
    row.set_value(value)
    return row


def _textview(text: str, placeholder: str) -> tuple[Gtk.Frame, Gtk.TextView]:
    view = Gtk.TextView(
        wrap_mode=Gtk.WrapMode.WORD_CHAR,
        top_margin=8,
        bottom_margin=8,
        left_margin=8,
        right_margin=8,
    )
    view.get_buffer().set_text(text)
    view.set_tooltip_text(placeholder)
    scroller = Gtk.ScrolledWindow(min_content_height=110, vexpand=False)
    scroller.set_child(view)
    frame = Gtk.Frame()
    frame.set_child(scroller)
    return frame, view


def _buffer_text(view: Gtk.TextView) -> str:
    buf = view.get_buffer()
    return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)


class SettingsWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="voxflow", default_width=640, default_height=760)
        self.cfg = Config.load()
        self.secrets = Secrets.load()
        self._can_capture = can_read_devices()

        page = Adw.PreferencesPage()
        page.add(self._group_dictation())
        page.add(self._group_transcription())
        page.add(self._group_cleanup())
        page.add(self._group_insertion())
        page.add(self._group_vocabulary())
        page.add(self._group_feedback())

        header = Adw.HeaderBar()
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save)
        header.pack_end(save)

        dictate = Gtk.Button(icon_name="audio-input-microphone-symbolic")
        dictate.set_tooltip_text("Start or stop dictating now")
        dictate.connect("clicked", lambda *_: self._send("toggle"))
        header.pack_start(dictate)

        # A persistent banner, not a toast: without the 'input' group neither
        # key capture nor the hotkey itself can work, and that is a state the
        # user stays in until they re-login - so it must not scroll past.
        self.banner = Adw.Banner(
            title="Can't read input devices — add your user to the 'input' "
            "group and log back in",
            revealed=not self._can_capture,
        )

        page.set_vexpand(True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.banner)
        content.append(page)

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(content)
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

    # ------------------------------------------------------------------ groups

    def _group_dictation(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Dictation key",
            description=(
                "A bare modifier or a spare mouse button works best. Click "
                "Capture and then press the key or mouse button you want."
            ),
        )
        self.row_key = Adw.EntryRow(title="Key or mouse button")
        self.row_key.set_text(self.cfg.hotkey.key)
        self.capture_button = Gtk.Button(label="Capture")
        self.capture_button.set_valign(Gtk.Align.CENTER)
        self.capture_button.connect("clicked", self._on_capture)
        # Capture reads /dev/input directly, so it is simply impossible
        # without the 'input' group. Disable it rather than letting the click
        # look like it did nothing.
        self.capture_button.set_sensitive(self._can_capture)
        if not self._can_capture:
            self.row_key.set_tooltip_text(
                "Capture needs read access to /dev/input. You can still type a "
                "name such as BTN_SIDE here; it takes effect once you are in "
                "the 'input' group."
            )
            self.capture_button.set_tooltip_text(
                "Unavailable until your user is in the 'input' group"
            )
        self.row_key.add_suffix(self.capture_button)
        group.add(self.row_key)

        self.row_mode = _combo("Behaviour", MODES, self.cfg.hotkey.mode)
        group.add(self.row_mode)

        self.row_suppress = _combo(
            "Hide the hotkey from other apps", SUPPRESS, self.cfg.hotkey.suppress
        )
        self.row_suppress.set_subtitle(
            "Mouse buttons need this, or your browser navigates back while you "
            "dictate. It claims the device and mirrors it, so it appears as a "
            "second pointer in Settings."
        )
        group.add(self.row_suppress)

        self.row_lock = Adw.SwitchRow(
            title="Tap to lock",
            subtitle="A quick tap keeps recording until you tap again",
            active=self.cfg.hotkey.tap_to_lock,
        )
        group.add(self.row_lock)

        self.row_cancel = Adw.EntryRow(title="Cancel key")
        self.row_cancel.set_text(self.cfg.hotkey.cancel_key)
        group.add(self.row_cancel)
        return group

    def _group_transcription(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Transcription",
            description="Audio is uploaded to this provider for transcription.",
        )
        self.row_provider = _combo("Provider", PROVIDERS, self.cfg.engine.provider)
        group.add(self.row_provider)

        self.row_groq_key = Adw.PasswordEntryRow(title="Groq API key")
        self.row_groq_key.set_text(self.secrets.groq_api_key)
        group.add(self.row_groq_key)

        self.row_openai_key = Adw.PasswordEntryRow(title="OpenAI API key")
        self.row_openai_key.set_text(self.secrets.openai_api_key)
        group.add(self.row_openai_key)

        self.row_model = Adw.EntryRow(title="Model override")
        self.row_model.set_text(self.cfg.engine.model)
        group.add(self.row_model)

        self.row_language = Adw.EntryRow(title="Language code")
        self.row_language.set_text(self.cfg.engine.language)
        self.row_language.set_tooltip_text("e.g. en, es, de. Leave empty to autodetect.")
        group.add(self.row_language)

        sources = [("", "System default")] + list_sources()
        self.row_device = _combo(
            "Microphone",
            [(name, label) for name, label in sources],
            self.cfg.audio.target,
        )
        group.add(self.row_device)
        return group

    def _group_cleanup(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Cleanup",
            description=(
                "Rewrites the raw transcript into finished text: removes filler, "
                "fixes punctuation, applies your vocabulary. Without a key, local "
                "punctuation rules are used instead."
            ),
        )
        self.row_cleanup = Adw.SwitchRow(
            title="Clean up with Claude", active=self.cfg.cleanup.enabled
        )
        group.add(self.row_cleanup)

        self.row_anthropic_key = Adw.PasswordEntryRow(title="Anthropic API key")
        self.row_anthropic_key.set_text(self.secrets.anthropic_api_key)
        group.add(self.row_anthropic_key)

        self.row_cleanup_model = Adw.EntryRow(title="Model")
        self.row_cleanup_model.set_text(self.cfg.cleanup.model or CLEANUP_MODEL)
        group.add(self.row_cleanup_model)

        self.row_style = _combo("Style", STYLES, self.cfg.cleanup.style)
        group.add(self.row_style)

        self.row_spoken = Adw.SwitchRow(
            title="Spoken punctuation offline",
            subtitle='Turn "period" and "new line" into punctuation when cleanup is off',
            active=self.cfg.cleanup.spoken_punctuation,
        )
        group.add(self.row_spoken)

        frame, self.view_instructions = _textview(
            self.cfg.cleanup.instructions, "Extra instructions for the cleanup model"
        )
        group.add(Gtk.Label(label="Extra cleanup instructions", xalign=0, margin_top=12))
        group.add(frame)
        return group

    def _group_insertion(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Insertion")
        self.row_method = _combo("Method", METHODS, self.cfg.inject.method)
        group.add(self.row_method)

        self.row_restore = Adw.SwitchRow(
            title="Restore clipboard",
            subtitle="Put your previous clipboard contents back after pasting",
            active=self.cfg.inject.restore_clipboard,
        )
        group.add(self.row_restore)

        self.row_trailing = Adw.SwitchRow(
            title="Add a trailing space", active=self.cfg.inject.trailing_space
        )
        group.add(self.row_trailing)

        self.row_delay = _spin(
            "Paste delay (ms)", 0, 1000, 10, self.cfg.inject.paste_delay_ms
        )
        self.row_delay.set_subtitle("Raise this if the first paste sometimes misses")
        group.add(self.row_delay)

        self.row_max_seconds = _spin(
            "Maximum recording (s)", 10, 1800, 10, self.cfg.audio.max_seconds
        )
        group.add(self.row_max_seconds)
        return group

    def _group_vocabulary(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Vocabulary",
            description=(
                "Names and jargon, one per line. These bias the transcriber and are "
                "spelled exactly as written here."
            ),
        )
        frame, self.view_terms = _textview(
            "\n".join(self.cfg.terms), "One term per line"
        )
        group.add(frame)

        replacements = "\n".join(f"{k} = {v}" for k, v in self.cfg.replacements.items())
        frame2, self.view_replacements = _textview(
            replacements, "spoken phrase = replacement, one per line"
        )
        group.add(Gtk.Label(label="Replacements", xalign=0, margin_top=12))
        group.add(frame2)
        return group

    def _group_feedback(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Feedback")
        self.row_notify = Adw.SwitchRow(
            title="Show notifications", active=self.cfg.feedback.notifications
        )
        group.add(self.row_notify)
        self.row_sounds = Adw.SwitchRow(
            title="Play a sound when recording starts and stops",
            active=self.cfg.feedback.sounds,
        )
        group.add(self.row_sounds)

        status = Adw.ActionRow(title="Daemon")
        self.status_label = Gtk.Label(label="checking…")
        self.status_label.add_css_class("dim-label")
        status.add_suffix(self.status_label)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER)
        refresh.connect("clicked", lambda *_: self._refresh_status())
        status.add_suffix(refresh)
        group.add(status)
        self._refresh_status()
        return group

    # ----------------------------------------------------------------- actions

    def _refresh_status(self) -> None:
        try:
            reply = send_command("status")
            self.status_label.set_label(f"running — {reply.get('state', '?')}")
        except ConnectionError:
            self.status_label.set_label("not running")

    def _on_capture(self, _button) -> None:
        # The label has to change visibly, or an armed capture is
        # indistinguishable from a click that did nothing.
        self.capture_button.set_sensitive(False)
        self.capture_button.set_label("Listening…")
        self._toast("Press the key or mouse button you want to use")

        def worker() -> None:
            from ..hotkey import HotkeyError, capture_key

            try:
                name = capture_key(10.0)
                error = None
            except HotkeyError as exc:
                name, error = None, str(exc)
            GLib.idle_add(self._capture_done, name, error)

        threading.Thread(target=worker, daemon=True).start()

    def _capture_done(self, name: str | None, error: str | None) -> bool:
        self.capture_button.set_sensitive(self._can_capture)
        self.capture_button.set_label("Capture")
        if error:
            self._toast(f"Capture unavailable: {error}")
        elif name:
            self.row_key.set_text(name)
            self._toast(f"Hotkey set to {name}")
        else:
            self._toast("No key detected")
        return False

    def _on_save(self, _button) -> None:
        cfg, secrets = self.cfg, self.secrets

        cfg.hotkey.key = self.row_key.get_text().strip().upper() or "KEY_RIGHTCTRL"
        cfg.hotkey.mode = _combo_value(self.row_mode)
        cfg.hotkey.tap_to_lock = self.row_lock.get_active()
        cfg.hotkey.cancel_key = self.row_cancel.get_text().strip().upper()
        cfg.hotkey.suppress = _combo_value(self.row_suppress)

        cfg.engine.provider = _combo_value(self.row_provider)
        cfg.engine.model = self.row_model.get_text().strip()
        cfg.engine.language = self.row_language.get_text().strip()
        cfg.audio.target = _combo_value(self.row_device)
        cfg.audio.max_seconds = int(self.row_max_seconds.get_value())

        cfg.cleanup.enabled = self.row_cleanup.get_active()
        cfg.cleanup.model = self.row_cleanup_model.get_text().strip() or CLEANUP_MODEL
        cfg.cleanup.style = _combo_value(self.row_style)
        cfg.cleanup.spoken_punctuation = self.row_spoken.get_active()
        cfg.cleanup.instructions = _buffer_text(self.view_instructions).strip()

        cfg.inject.method = _combo_value(self.row_method)
        cfg.inject.restore_clipboard = self.row_restore.get_active()
        cfg.inject.trailing_space = self.row_trailing.get_active()
        cfg.inject.paste_delay_ms = int(self.row_delay.get_value())

        cfg.feedback.notifications = self.row_notify.get_active()
        cfg.feedback.sounds = self.row_sounds.get_active()

        cfg.terms = [
            line.strip()
            for line in _buffer_text(self.view_terms).splitlines()
            if line.strip()
        ]
        replacements: dict[str, str] = {}
        for line in _buffer_text(self.view_replacements).splitlines():
            if "=" not in line:
                continue
            spoken, written = line.split("=", 1)
            if spoken.strip():
                replacements[spoken.strip()] = written.strip()
        cfg.replacements = replacements

        secrets.groq_api_key = self.row_groq_key.get_text().strip()
        secrets.openai_api_key = self.row_openai_key.get_text().strip()
        secrets.anthropic_api_key = self.row_anthropic_key.get_text().strip()

        try:
            cfg.save()
            secrets.save()
        except OSError as exc:
            self._toast(f"Could not save: {exc}")
            return

        try:
            send_command("reload")
            self._toast("Saved and reloaded")
        except ConnectionError:
            self._toast("Saved. Start the daemon to apply it.")
        self._refresh_status()

    def _send(self, command: str) -> None:
        try:
            send_command(command)
        except ConnectionError as exc:
            self._toast(str(exc))

    def _toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=message, timeout=3))


class SettingsApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)

    def do_activate(self) -> None:
        window = self.props.active_window or SettingsWindow(self)
        window.present()


def main() -> int:
    return SettingsApp().run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
