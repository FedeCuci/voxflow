"""Global hotkey capture by reading evdev directly.

On GNOME/Wayland an application cannot register a global hotkey, and it
cannot see key-release events at all through the toolkit - so real
push-to-talk is impossible via the desktop portal. Reading /dev/input/event*
works regardless of compositor and gives us both press and release, and it
works for mouse buttons as well as keys.

Two modes of operation:

* **Monitoring** (the default for keyboard keys). We read the device without
  claiming it, so the keystroke also reaches the focused application. That is
  fine for a bare modifier like Right Ctrl, which inserts nothing on its own.

* **Grab and forward** (the default for mouse buttons). A side button is not
  inert - browsers navigate back on BTN_SIDE - so monitoring alone would fire
  "back" every time you dictate. Instead we EVIOCGRAB the device, clone it as
  a virtual input device, and forward every event except the hotkey button.
  The compositor sees the clone, so the pointer behaves normally while the
  chosen button is consumed by voxflow.

Grabbing is per-device and all-or-nothing, which is why the forwarding clone
is necessary: the hotkey button lives on the same event node as pointer
motion and the other mouse buttons.
"""

from __future__ import annotations

import logging
import os
import selectors
import threading
import time
from collections.abc import Callable

try:
    import evdev
    from evdev import ecodes
except ImportError:  # pragma: no cover - surfaced by `voxflow doctor`
    evdev = None  # type: ignore[assignment]
    ecodes = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# Buttons that must stay usable while picking a hotkey, or capture would
# trigger on the very click that started it.
CAPTURE_IGNORED = ("BTN_LEFT", "BTN_RIGHT")

FRIENDLY_NAMES = {
    "KEY_RIGHTCTRL": "Right Ctrl",
    "KEY_LEFTCTRL": "Left Ctrl",
    "KEY_RIGHTALT": "Right Alt",
    "KEY_LEFTALT": "Left Alt",
    "KEY_RIGHTSHIFT": "Right Shift",
    "KEY_LEFTSHIFT": "Left Shift",
    "KEY_RIGHTMETA": "Right Super",
    "KEY_LEFTMETA": "Left Super",
    "KEY_CAPSLOCK": "Caps Lock",
    "KEY_SCROLLLOCK": "Scroll Lock",
    "BTN_SIDE": "side mouse button (back)",
    "BTN_EXTRA": "forward mouse button",
    "BTN_MIDDLE": "middle mouse button",
    "BTN_TASK": "task mouse button",
    "BTN_FORWARD": "forward mouse button",
    "BTN_BACK": "back mouse button",
}


class HotkeyError(RuntimeError):
    pass


def available() -> bool:
    return evdev is not None


def key_code(name: str) -> int:
    """Translate a KEY_* or BTN_* name into its evdev code.

    A bare name is accepted too: "side" resolves to BTN_SIDE, "f13" to
    KEY_F13.
    """
    if ecodes is None:
        raise HotkeyError("python-evdev is not installed")
    wanted = name.strip().upper()
    candidates = (
        [wanted]
        if wanted.startswith(("KEY_", "BTN_"))
        else [f"KEY_{wanted}", f"BTN_{wanted}"]
    )
    for candidate in candidates:
        code = ecodes.ecodes.get(candidate)
        if code is not None:
            return int(code)
    raise HotkeyError(f"unknown key or button name: {name}")


def key_name(code: int) -> str:
    """Canonical name for an EV_KEY code.

    ecodes.KEY only covers KEY_*; mouse buttons live in ecodes.BTN, so look
    both up through bytype, which also returns a tuple when a code has
    aliases (272 is both BTN_LEFT and BTN_MOUSE).
    """
    if ecodes is None:
        return str(code)
    name = ecodes.bytype.get(ecodes.EV_KEY, {}).get(code)
    if isinstance(name, (list, tuple)):
        name = name[0]
    return str(name or code)


def is_button(code: int) -> bool:
    """True for pointer/mouse buttons, which are not inert when passed through."""
    return key_name(code).startswith("BTN_")


def pretty_name(name: str) -> str:
    """Human-readable label for a key or button name."""
    upper = name.strip().upper()
    if upper in FRIENDLY_NAMES:
        return FRIENDLY_NAMES[upper]
    stripped = upper.removeprefix("KEY_").removeprefix("BTN_")
    label = stripped.replace("_", " ").title()
    return f"{label} button" if upper.startswith("BTN_") else label


def can_read_devices() -> bool:
    try:
        paths = evdev.list_devices() if evdev else []
    except OSError:
        return False
    return any(os.access(path, os.R_OK) for path in paths)


def _device_keys(device: "evdev.InputDevice") -> set[int]:
    try:
        return set(device.capabilities().get(ecodes.EV_KEY, []))
    except OSError:
        return set()


def _open_matching(wanted: set[int]) -> list["evdev.InputDevice"]:
    """Open every readable device that can emit at least one wanted code."""
    devices: list[evdev.InputDevice] = []
    if evdev is None:
        return devices
    try:
        paths = evdev.list_devices()
    except OSError as exc:
        log.warning("cannot enumerate input devices: %s", exc)
        return devices
    for path in paths:
        try:
            device = evdev.InputDevice(path)
        except (OSError, PermissionError):
            continue
        if wanted & _device_keys(device):
            devices.append(device)
        else:
            device.close()
    return devices


class HotkeyListener(threading.Thread):
    """Watches input devices for the configured keys and reports press/release.

    `suppress` holds codes that must not reach other applications. Any device
    carrying one of them is grabbed and mirrored through a virtual clone, so
    everything except those codes is passed through untouched.
    """

    def __init__(
        self,
        keys: set[int],
        callback: Callable[[int, bool], None],
        suppress: set[int] | frozenset[int] = frozenset(),
    ):
        super().__init__(daemon=True, name="voxflow-hotkey")
        self.keys = set(keys)
        self.suppress = set(suppress)
        self.callback = callback
        self._stop = threading.Event()
        self._devices: list["evdev.InputDevice"] = []
        self._clones: dict[int, "evdev.UInput"] = {}
        self._known_paths: set[str] = set()

    def update(self, keys: set[int], suppress: set[int] | frozenset[int] = frozenset()) -> None:
        self.keys = set(keys)
        self.suppress = set(suppress)
        # Force a rescan so a newly-configured key is picked up on devices
        # that were previously filtered out, and so stale grabs are released.
        self._known_paths = set()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if evdev is None:
            log.error("python-evdev unavailable; hotkey listener not started")
            return
        while not self._stop.is_set():
            try:
                self._rescan()
                if not self._devices:
                    log.warning(
                        "no readable input devices for the configured hotkey - "
                        "is your user in the 'input' group?"
                    )
                    self._stop.wait(5)
                    continue
                self._pump()
            except Exception:  # keep the daemon alive through device churn
                log.exception("hotkey listener error; retrying")
                self._close()
                self._stop.wait(2)
        self._close()

    def _rescan(self) -> None:
        try:
            paths = set(evdev.list_devices())
        except OSError:
            paths = set()
        if paths == self._known_paths and self._devices:
            return
        self._close()
        self._devices = _open_matching(self.keys)
        self._known_paths = paths
        for device in self._devices:
            if self.suppress & _device_keys(device):
                self._grab(device)
        log.info(
            "watching %d input device(s) for %s%s",
            len(self._devices),
            ", ".join(sorted(key_name(k) for k in self.keys)),
            f" ({len(self._clones)} grabbed)" if self._clones else "",
        )

    def _grab(self, device: "evdev.InputDevice") -> None:
        """Claim a device and mirror it, so the hotkey never reaches other apps."""
        try:
            clone = evdev.UInput.from_device(device, name=f"voxflow-filtered-{device.fd}")
        except Exception as exc:
            log.warning(
                "cannot mirror %s (%s); the hotkey will also reach other apps",
                device.path,
                exc,
            )
            return
        try:
            device.grab()
        except OSError as exc:
            log.warning(
                "cannot grab %s (%s); the hotkey will also reach other apps",
                device.path,
                exc,
            )
            clone.close()
            return
        self._clones[device.fd] = clone
        log.info("grabbed %s (%s) and mirroring its other events", device.path, device.name)

    def _pump(self) -> None:
        selector = selectors.DefaultSelector()
        for device in self._devices:
            selector.register(device, selectors.EVENT_READ)
        try:
            while not self._stop.is_set():
                for key, _ in selector.select(timeout=1.0):
                    device = key.fileobj
                    clone = self._clones.get(device.fd)  # type: ignore[union-attr]
                    try:
                        for event in device.read():  # type: ignore[union-attr]
                            self._dispatch(event, clone)
                    except OSError:
                        log.info("input device went away; rescanning")
                        return
                # Cheap hot-plug detection: compare the device list each second.
                try:
                    if set(evdev.list_devices()) != self._known_paths:
                        return
                except OSError:
                    return
        finally:
            selector.close()

    def _dispatch(self, event, clone: "evdev.UInput | None") -> None:
        consumed = False
        if event.type == ecodes.EV_KEY and event.code in self.keys:
            if event.value != 2:  # 2 is autorepeat, not a new press
                try:
                    self.callback(int(event.code), event.value == 1)
                except Exception:
                    log.exception("hotkey callback failed")
            consumed = event.code in self.suppress
        if clone is not None and not consumed:
            # Everything the hotkey did not claim is replayed verbatim, SYN
            # events included, so the pointer behaves exactly as before.
            try:
                clone.write_event(event)
            except OSError as exc:
                log.debug("could not forward event: %s", exc)

    def _close(self) -> None:
        for device in self._devices:
            if device.fd in self._clones:
                try:
                    device.ungrab()
                except OSError:
                    pass
            try:
                device.close()
            except OSError:
                pass
        for clone in self._clones.values():
            try:
                clone.close()
            except OSError:
                pass
        self._clones = {}
        self._devices = []


def capture_key(timeout: float = 10.0) -> str | None:
    """Block until a key or button is pressed and return its name.

    Used by `voxflow capture-key` and the settings UI so a hotkey can be
    chosen by pressing it. Left and right click are ignored, since they are
    needed to operate the dialog that starts the capture.
    """
    if evdev is None:
        raise HotkeyError("python-evdev is not installed")
    devices = _open_matching(set(range(ecodes.KEY_MAX)))
    if not devices:
        raise HotkeyError(
            "no readable input devices; add your user to the 'input' group and re-login"
        )
    ignored = {key_code(name) for name in CAPTURE_IGNORED}
    escape = key_code("KEY_ESC")

    selector = selectors.DefaultSelector()
    for device in devices:
        selector.register(device, selectors.EVENT_READ)
    try:
        # Drain whatever is already queued, so the click or keypress that
        # started the capture is not mistaken for the chosen hotkey.
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            for key, _ in selector.select(timeout=0.05):
                try:
                    list(key.fileobj.read())  # type: ignore[union-attr]
                except OSError:
                    continue

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for key, _ in selector.select(timeout=0.5):
                try:
                    events = list(key.fileobj.read())  # type: ignore[union-attr]
                except OSError:
                    continue
                for event in events:
                    if event.type != ecodes.EV_KEY or event.value != 1:
                        continue
                    if event.code in ignored:
                        continue
                    if event.code == escape:
                        return None
                    return key_name(event.code)
        return None
    finally:
        selector.close()
        for device in devices:
            try:
                device.close()
            except OSError:
                pass
