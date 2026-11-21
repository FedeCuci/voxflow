"""Getting text into whatever window has focus.

Two layers, because Wayland gives us no sanctioned way to do this:

* A virtual keyboard created through /dev/uinput. This is a kernel-level
  input device, so the compositor treats it exactly like real hardware -
  unlike the virtual-keyboard Wayland protocol, which Mutter does not
  implement at all.
* The clipboard. Synthesising one Ctrl+V is far faster and more reliable
  than typing hundreds of keystrokes, and it is layout-independent: typing
  via keycodes assumes the compositor's keymap matches ours, pasting does
  not. The previous clipboard contents are saved and restored.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time

try:
    import evdev
    from evdev import ecodes
except ImportError:  # pragma: no cover
    evdev = None  # type: ignore[assignment]
    ecodes = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

UINPUT_PATH = "/dev/uinput"


class InjectError(RuntimeError):
    pass


def _build_keymap() -> dict[str, tuple[int, bool]]:
    """char -> (keycode, needs_shift) for a US layout."""
    if ecodes is None:
        return {}
    keymap: dict[str, tuple[int, bool]] = {}
    for char in "abcdefghijklmnopqrstuvwxyz":
        code = ecodes.ecodes[f"KEY_{char.upper()}"]
        keymap[char] = (code, False)
        keymap[char.upper()] = (code, True)
    digits = {
        "1": "!", "2": "@", "3": "#", "4": "$", "5": "%",
        "6": "^", "7": "&", "8": "*", "9": "(", "0": ")",
    }
    for digit, shifted in digits.items():
        code = ecodes.ecodes[f"KEY_{digit}"]
        keymap[digit] = (code, False)
        keymap[shifted] = (code, True)
    pairs = {
        "KEY_MINUS": ("-", "_"),
        "KEY_EQUAL": ("=", "+"),
        "KEY_LEFTBRACE": ("[", "{"),
        "KEY_RIGHTBRACE": ("]", "}"),
        "KEY_BACKSLASH": ("\\", "|"),
        "KEY_SEMICOLON": (";", ":"),
        "KEY_APOSTROPHE": ("'", '"'),
        "KEY_GRAVE": ("`", "~"),
        "KEY_COMMA": (",", "<"),
        "KEY_DOT": (".", ">"),
        "KEY_SLASH": ("/", "?"),
    }
    for name, (plain, shifted) in pairs.items():
        code = ecodes.ecodes[name]
        keymap[plain] = (code, False)
        keymap[shifted] = (code, True)
    keymap[" "] = (ecodes.KEY_SPACE, False)
    keymap["\n"] = (ecodes.KEY_ENTER, False)
    keymap["\t"] = (ecodes.KEY_TAB, False)
    return keymap


KEYMAP = _build_keymap()


class VirtualKeyboard:
    """Lazily-opened uinput device. Opening it requires access to /dev/uinput."""

    def __init__(self) -> None:
        self._device: "evdev.UInput | None" = None
        self._lock = threading.Lock()

    def _ensure(self) -> "evdev.UInput":
        if evdev is None:
            raise InjectError("python-evdev is not installed")
        if self._device is not None:
            return self._device
        codes = sorted({code for code, _ in KEYMAP.values()} | {
            ecodes.KEY_LEFTCTRL,
            ecodes.KEY_LEFTSHIFT,
            ecodes.KEY_V,
        })
        try:
            self._device = evdev.UInput(
                events={ecodes.EV_KEY: codes}, name="voxflow-virtual-keyboard"
            )
        except (OSError, PermissionError) as exc:
            raise InjectError(
                f"cannot open {UINPUT_PATH} ({exc}). Enable hardware.uinput and add "
                "your user to the 'uinput' group, then re-login."
            ) from exc
        # Give the compositor a moment to notice the new device, otherwise the
        # first synthesised keystroke after startup can be dropped.
        time.sleep(0.2)
        return self._device

    def close(self) -> None:
        with self._lock:
            if self._device is not None:
                try:
                    self._device.close()
                except OSError:
                    pass
                self._device = None

    def tap(self, code: int, *, ctrl: bool = False, shift: bool = False) -> None:
        with self._lock:
            device = self._ensure()
            if ctrl:
                device.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 1)
            if shift:
                device.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1)
            device.write(ecodes.EV_KEY, code, 1)
            device.write(ecodes.EV_KEY, code, 0)
            if shift:
                device.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0)
            if ctrl:
                device.write(ecodes.EV_KEY, ecodes.KEY_LEFTCTRL, 0)
            device.syn()

    def type_text(self, text: str, delay: float = 0.004) -> list[str]:
        """Type ASCII via keycodes. Returns characters it could not represent."""
        unsupported: list[str] = []
        with self._lock:
            device = self._ensure()
            for char in text:
                mapping = KEYMAP.get(char)
                if mapping is None:
                    unsupported.append(char)
                    continue
                code, shift = mapping
                if shift:
                    device.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1)
                device.write(ecodes.EV_KEY, code, 1)
                device.write(ecodes.EV_KEY, code, 0)
                if shift:
                    device.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0)
                device.syn()
                time.sleep(delay)
        return unsupported


def have_clipboard() -> bool:
    return shutil.which("wl-copy") is not None and shutil.which("wl-paste") is not None


def clipboard_get() -> str | None:
    """Current clipboard text, or None if it is empty or not text."""
    if not shutil.which("wl-paste"):
        return None
    try:
        result = subprocess.run(
            ["wl-paste", "--no-newline", "--type", "text/plain"],
            capture_output=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace")


def clipboard_set(text: str) -> None:
    if not shutil.which("wl-copy"):
        raise InjectError("wl-copy not found; install wl-clipboard")
    try:
        # wl-copy forks into the background to serve the selection, so this
        # returns as soon as it owns the clipboard.
        subprocess.run(
            ["wl-copy", "--type", "text/plain", "--", text],
            check=True,
            timeout=5,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()
        raise InjectError(f"wl-copy failed: {stderr or exc}") from exc
    except (subprocess.SubprocessError, OSError) as exc:
        raise InjectError(f"wl-copy failed: {exc}") from exc


class Injector:
    def __init__(self, keyboard: VirtualKeyboard | None = None):
        self.keyboard = keyboard or VirtualKeyboard()

    def insert(
        self,
        text: str,
        method: str = "paste",
        restore_clipboard: bool = True,
        paste_delay_ms: int = 120,
    ) -> None:
        if not text:
            return
        if method == "type":
            leftover = self.keyboard.type_text(text)
            if leftover:
                # Characters outside the US ASCII keymap cannot be typed by
                # keycode; fall back to the clipboard for the whole string.
                log.info(
                    "typing could not represent %d character(s); pasting instead",
                    len(leftover),
                )
                self._paste(text, restore_clipboard, paste_delay_ms)
            return
        self._paste(text, restore_clipboard, paste_delay_ms)

    def _paste(self, text: str, restore: bool, paste_delay_ms: int) -> None:
        if ecodes is None:
            raise InjectError("python-evdev is not installed")
        previous = clipboard_get() if restore else None
        clipboard_set(text)
        time.sleep(max(paste_delay_ms, 0) / 1000.0)
        self.keyboard.tap(ecodes.KEY_V, ctrl=True)
        if restore and previous is not None:
            # Restore after the paste has certainly been read. Doing this on a
            # timer keeps the hotkey loop responsive.
            threading.Timer(1.0, self._restore, args=(previous,)).start()

    @staticmethod
    def _restore(previous: str) -> None:
        try:
            if previous:
                clipboard_set(previous)
        except InjectError as exc:
            log.debug("could not restore clipboard: %s", exc)

    def close(self) -> None:
        self.keyboard.close()
