"""The voxflow daemon: hotkey -> record -> transcribe -> clean -> insert."""

from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from dataclasses import dataclass

from . import cleanup as cleanup_mod
from .audio import AudioError, Recorder, pcm_to_wav
from .cleanup import Cleaner
from .config import Config, Secrets
from .control import ControlServer
from .engines import EngineError, get_engine
from .history import History
from .hotkey import (
    HotkeyError,
    HotkeyListener,
    is_button,
    key_code,
    pretty_name,
)
from .inject import InjectError, Injector, clipboard_set
from .notify import Notifier

log = logging.getLogger(__name__)

IDLE = "idle"
RECORDING = "recording"
PROCESSING = "processing"


@dataclass
class Job:
    pcm: bytes
    duration: float
    sample_rate: int
    # Dictations started from the app window go to the clipboard instead of
    # being pasted: that window has focus, so a synthesised Ctrl+V would
    # paste into voxflow itself and the text would be lost.
    to_clipboard: bool = False


class Daemon:
    def __init__(self) -> None:
        self.cfg = Config.load()
        self.secrets = Secrets.load()
        self.notifier = Notifier(
            self.cfg.feedback.notifications, self.cfg.feedback.sounds
        )
        self.injector = Injector()
        self.history = History()
        self.state = IDLE
        self._lock = threading.RLock()
        self._recorder: Recorder | None = None
        self._queue: queue.Queue[Job | None] = queue.Queue()
        self._stop = threading.Event()
        self._keys_down: set[int] = set()
        self._latched = False
        self._press_at = 0.0
        self._listener: HotkeyListener | None = None
        self._control: ControlServer | None = None
        self._last_text = ""
        self._counter = 0
        self._to_clipboard = False

    # ---------------------------------------------------------------- lifecycle

    def run(self) -> int:
        log.info("voxflow starting")
        self._control = ControlServer(self._on_command)
        try:
            self._control.start()
        except (OSError, RuntimeError) as exc:
            log.error("cannot start control socket: %s", exc)
            return 1

        worker = threading.Thread(target=self._worker, daemon=True, name="voxflow-worker")
        worker.start()

        self._start_listener()

        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        try:
            signal.signal(signal.SIGHUP, self._on_sighup)
        except (AttributeError, ValueError):
            pass

        hotkey = self.cfg.hotkey
        log.info(
            "ready - %s %s to dictate (provider=%s, cleanup=%s)",
            "hold" if hotkey.mode == "hold" else "press",
            hotkey.key,
            self.cfg.engine.provider,
            "on" if self._cleanup_active() else "off",
        )
        self.notifier.notify(
            "voxflow ready",
            f"{'Hold' if hotkey.mode == 'hold' else 'Press'} "
            f"{pretty_name(hotkey.key)} to dictate",
            timeout_ms=2000,
        )

        try:
            while not self._stop.wait(0.5):
                pass
        finally:
            self.shutdown()
        return 0

    def _start_listener(self) -> None:
        try:
            hotkey = key_code(self.cfg.hotkey.key)
            keys = {hotkey}
            if self.cfg.hotkey.cancel_key:
                keys.add(key_code(self.cfg.hotkey.cancel_key))
        except HotkeyError as exc:
            log.error("bad hotkey configuration: %s", exc)
            return

        suppress = self._suppressed_codes(hotkey)
        if suppress:
            log.info(
                "%s will be hidden from other applications",
                pretty_name(self.cfg.hotkey.key),
            )
        if self._listener is not None:
            self._listener.update(keys, suppress)
            return
        self._listener = HotkeyListener(keys, self._on_key, suppress)
        self._listener.start()

    def _suppressed_codes(self, hotkey: int) -> set[int]:
        """Which codes to hide from other apps.

        A mouse button is not inert - BTN_SIDE navigates back in a browser -
        so by default it is swallowed. A bare modifier does nothing on its own
        and is cheaper to leave alone, since suppressing means grabbing and
        mirroring the whole device.
        """
        mode = (self.cfg.hotkey.suppress or "auto").strip().lower()
        if mode == "always":
            return {hotkey}
        if mode == "never":
            return set()
        if mode != "auto":
            log.warning("unknown hotkey.suppress value %r; treating it as 'auto'", mode)
        return {hotkey} if is_button(hotkey) else set()

    def shutdown(self) -> None:
        log.info("shutting down")
        self._stop.set()
        with self._lock:
            if self._recorder is not None:
                try:
                    self._recorder.stop()
                except AudioError:
                    pass
                self._recorder = None
        self._queue.put(None)
        if self._listener is not None:
            self._listener.stop()
        if self._control is not None:
            self._control.stop()
        self.injector.close()
        self.history.close()

    def _on_signal(self, *_args) -> None:
        self._stop.set()

    def _on_sighup(self, *_args) -> None:
        self.reload()

    def reload(self) -> None:
        log.info("reloading configuration")
        self.cfg = Config.load()
        self.secrets = Secrets.load()
        self.notifier.enabled = self.cfg.feedback.notifications
        self.notifier.sounds = self.cfg.feedback.sounds
        self._start_listener()

    # ------------------------------------------------------------------ hotkeys

    def _on_key(self, code: int, down: bool) -> None:
        cancel = self.cfg.hotkey.cancel_key
        if down:
            self._keys_down.add(code)
        else:
            self._keys_down.discard(code)

        if cancel and code == key_code(cancel):
            if down and self.state == RECORDING:
                self.cancel()
            return

        if code != key_code(self.cfg.hotkey.key):
            return

        if self.cfg.hotkey.mode == "toggle":
            if down:
                self.toggle()
            return

        # Hold mode, with an optional tap-to-lock gesture: holding the key
        # records for as long as it is held, while a quick tap latches
        # recording on until the next tap.
        if down:
            if self._latched:
                self._latched = False
                self.stop_and_process()
            else:
                self._press_at = time.monotonic()
                self.start_recording()
            return

        if self._latched:
            return
        held_ms = (time.monotonic() - self._press_at) * 1000
        if self.cfg.hotkey.tap_to_lock and held_ms < self.cfg.hotkey.tap_ms:
            self._latched = True
            self.notifier.notify(
                "Listening (locked)",
                f"Tap {pretty_name(self.cfg.hotkey.key)} again to finish",
                timeout_ms=0,
            )
            return
        self.stop_and_process()

    # -------------------------------------------------------------------- states

    def start_recording(self, to_clipboard: bool = False) -> None:
        with self._lock:
            if self.state == RECORDING:
                return
            self._to_clipboard = to_clipboard
            recorder = Recorder(
                sample_rate=self.cfg.audio.sample_rate,
                target=self.cfg.audio.target,
                max_seconds=self.cfg.audio.max_seconds,
            )
            try:
                recorder.start()
            except AudioError as exc:
                log.error("cannot record: %s", exc)
                self.notifier.error("Microphone unavailable", str(exc))
                return
            self._recorder = recorder
            self.state = RECORDING
        self.notifier.notify("Listening…", timeout_ms=0)
        self.notifier.play("start")

    def stop_and_process(self) -> None:
        with self._lock:
            if self.state != RECORDING or self._recorder is None:
                return
            recorder = self._recorder
            self._recorder = None
            self._latched = False
            to_clipboard = self._to_clipboard
            self._to_clipboard = False
            try:
                pcm = recorder.stop()
            except AudioError as exc:
                self.state = IDLE
                log.error("recording failed: %s", exc)
                self.notifier.error("Recording failed", str(exc))
                return
            duration = recorder.duration(pcm)
            self.state = PROCESSING if pcm else IDLE

        self.notifier.play("stop")
        if duration < self.cfg.audio.min_seconds:
            with self._lock:
                self.state = IDLE
            self.notifier.notify("Too short", "Nothing was dictated", timeout_ms=1500)
            return
        self._queue.put(
            Job(
                pcm=pcm,
                duration=duration,
                sample_rate=self.cfg.audio.sample_rate,
                to_clipboard=to_clipboard,
            )
        )

    def cancel(self) -> None:
        with self._lock:
            if self.state != RECORDING or self._recorder is None:
                return
            recorder = self._recorder
            self._recorder = None
            self._latched = False
            self._to_clipboard = False
            self.state = IDLE
        try:
            recorder.stop()
        except AudioError:
            pass
        self.notifier.notify("Cancelled", timeout_ms=1200)

    def toggle(self, to_clipboard: bool = False) -> None:
        if self.state == RECORDING:
            self.stop_and_process()
        elif self.state == IDLE:
            self.start_recording(to_clipboard)

    # ------------------------------------------------------------------- worker

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                self._handle(job)
            except Exception:
                log.exception("unhandled error while processing dictation")
                self.notifier.error("Dictation failed", "See `journalctl --user -u voxflow`")
            finally:
                with self._lock:
                    if self._recorder is None:
                        self.state = IDLE
                self._queue.task_done()

    def _handle(self, job: Job) -> None:
        self.notifier.notify(
            "Transcribing…", f"{job.duration:.1f}s of audio", timeout_ms=0
        )
        wav = pcm_to_wav(job.pcm, job.sample_rate)

        engine = get_engine(self.cfg.engine.provider)
        api_key = getattr(self.secrets, engine.secret_field, "")
        started = time.monotonic()
        try:
            raw = engine.transcribe(
                wav,
                api_key=api_key,
                model=self.cfg.engine.model,
                language=self.cfg.engine.language,
                prompt=self._asr_prompt(),
                timeout=self.cfg.engine.timeout,
            )
        except EngineError as exc:
            log.error("transcription failed: %s", exc)
            self.notifier.error("Transcription failed", str(exc))
            return

        if not raw.strip():
            self.notifier.notify("Nothing heard", "No speech detected", timeout_ms=1800)
            return

        text, cleaned = self._refine(raw)
        if not text.strip():
            self.notifier.notify("Nothing to insert", timeout_ms=1500)
            return
        if self.cfg.inject.trailing_space:
            text = text + " "

        if job.to_clipboard:
            try:
                clipboard_set(text)
            except InjectError as exc:
                log.error("could not copy text: %s", exc)
                self.notifier.error("Could not copy text", str(exc))
                self.history.add(raw, text, job.duration, engine.name, cleaned)
                return
            self._counter += 1
            self._last_text = text
            self.history.add(raw, text, job.duration, engine.name, cleaned)
            log.info("copied %d chars to the clipboard (cleanup=%s)", len(text), cleaned)
            self.notifier.notify(
                "Copied to clipboard",
                _preview(text),
                icon="edit-copy-symbolic",
                timeout_ms=2500,
            )
            return

        self._wait_for_key_release()
        try:
            self.injector.insert(
                text,
                method=self.cfg.inject.method,
                restore_clipboard=self.cfg.inject.restore_clipboard,
                paste_delay_ms=self.cfg.inject.paste_delay_ms,
            )
        except InjectError as exc:
            log.error("could not insert text: %s", exc)
            # The text is still recoverable from history, so say so.
            self.notifier.error(
                "Could not insert text",
                f"{exc}\nRecover it with: voxflow history -n 1",
            )
            self.history.add(raw, text, job.duration, engine.name, cleaned)
            return

        elapsed = time.monotonic() - started
        self._counter += 1
        self._last_text = text
        self.history.add(raw, text, job.duration, engine.name, cleaned)
        log.info(
            "inserted %d chars in %.1fs (cleanup=%s)", len(text), elapsed, cleaned
        )
        self.notifier.notify(
            "Inserted",
            _preview(text),
            icon="emblem-ok-symbolic",
            timeout_ms=1800,
        )

    def _refine(self, raw: str) -> tuple[str, bool]:
        """Apply LLM cleanup if configured, otherwise local rules."""
        cleaned = False
        text = raw
        if self._cleanup_active():
            cleaner = Cleaner(
                api_key=self.secrets.anthropic_api_key,
                model=self.cfg.cleanup.model,
                timeout=self.cfg.cleanup.timeout,
            )
            result = cleaner.clean(
                raw,
                style=self.cfg.cleanup.style,
                terms=self.cfg.terms,
                instructions=self.cfg.cleanup.instructions,
                max_tokens=self.cfg.cleanup.max_tokens,
            )
            text, cleaned = result.text, result.cleaned
            if result.error:
                log.info("cleanup unavailable (%s); using raw transcript", result.error)
        if not cleaned:
            text = cleanup_mod.apply_local_rules(
                text, self.cfg.cleanup.spoken_punctuation
            )
        if self.cfg.replacements:
            text = cleanup_mod.apply_replacements(text, self.cfg.replacements)
        return text, cleaned

    def _cleanup_active(self) -> bool:
        return bool(self.cfg.cleanup.enabled and self.secrets.anthropic_api_key)

    def _asr_prompt(self) -> str:
        """Bias the transcriber toward the user's own vocabulary."""
        parts = []
        if self.cfg.engine.prompt.strip():
            parts.append(self.cfg.engine.prompt.strip())
        if self.cfg.terms:
            parts.append(", ".join(self.cfg.terms))
        return " ".join(parts)[:900]

    def _wait_for_key_release(self, limit: float = 1.5) -> None:
        """Avoid synthesising Ctrl+V while the user still holds a modifier."""
        deadline = time.monotonic() + limit
        while self._keys_down and time.monotonic() < deadline:
            time.sleep(0.03)

    # ------------------------------------------------------------------ control

    def _on_command(self, raw: str) -> dict:
        command = raw.strip().lower()
        if command == "toggle":
            self.toggle()
        elif command == "start":
            self.start_recording()
        elif command == "stop":
            self.stop_and_process()
        elif command == "dictate":
            # Started from the app window, which holds focus - deliver to the
            # clipboard rather than pasting into ourselves.
            self.toggle(to_clipboard=True)
        elif command == "cancel":
            self.cancel()
        elif command == "reload":
            self.reload()
        elif command == "quit":
            self._stop.set()
        elif command == "status":
            pass
        else:
            return {"ok": False, "error": f"unknown command: {command}"}
        return {"ok": True, **self.status()}

    def status(self) -> dict:
        recorder = self._recorder
        return {
            "state": self.state,
            "latched": self._latched,
            "clipboard": self._to_clipboard,
            "pending": self._queue.qsize(),
            "hotkey": self.cfg.hotkey.key,
            "mode": self.cfg.hotkey.mode,
            "provider": self.cfg.engine.provider,
            "cleanup": self._cleanup_active(),
            "elapsed": round(recorder.elapsed, 1) if recorder else 0.0,
            "level": round(recorder.level, 3) if recorder else 0.0,
            "dictations": self._counter,
            "last": _preview(self._last_text, 80),
        }


def _preview(text: str, limit: int = 120) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def main() -> int:
    return Daemon().run()
