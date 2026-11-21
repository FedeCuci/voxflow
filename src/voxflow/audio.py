"""Microphone capture via PipeWire's pw-record.

Shelling out to pw-record avoids a PortAudio/ALSA dependency and gets us
PipeWire-native device selection for free. We ask for raw mono 16 kHz s16,
which is exactly what every Whisper-family endpoint wants, and wrap it in a
WAV container ourselves when it is time to upload.
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import threading
import time
import wave

log = logging.getLogger(__name__)


class AudioError(RuntimeError):
    pass


def have_recorder() -> bool:
    return shutil.which("pw-record") is not None


def list_sources() -> list[tuple[str, str]]:
    """Return (node_name, description) for each audio capture device."""
    if not shutil.which("pw-dump"):
        return []
    import json

    try:
        raw = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=10, check=True
        ).stdout
        objects = json.loads(raw)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        log.warning("could not list audio sources: %s", exc)
        return []

    sources: list[tuple[str, str]] = []
    for obj in objects:
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Audio/Source":
            continue
        name = props.get("node.name")
        if not name:
            continue
        label = props.get("node.description") or props.get("node.nick") or name
        sources.append((name, label))
    return sources


class Recorder:
    """A single push-to-talk recording.

    start() spawns pw-record and drains its stdout on a reader thread so the
    pipe never fills; stop() returns the captured PCM.
    """

    def __init__(self, sample_rate: int = 16000, target: str = "", max_seconds: int = 300):
        self.sample_rate = sample_rate
        self.target = target
        self.max_seconds = max_seconds
        self._proc: subprocess.Popen[bytes] | None = None
        self._chunks: list[bytes] = []
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._started_at = 0.0
        self._level = 0.0
        self._stderr = b""

    @property
    def level(self) -> float:
        """Most recent peak amplitude, 0.0 - 1.0. Useful for a meter."""
        return self._level

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started_at if self._started_at else 0.0

    def start(self) -> None:
        if self._proc is not None:
            raise AudioError("recorder already running")
        if not have_recorder():
            raise AudioError("pw-record not found; is PipeWire installed?")

        cmd = [
            "pw-record",
            "--raw",
            f"--rate={self.sample_rate}",
            "--channels=1",
            "--format=s16",
            "--latency=20ms",
        ]
        if self.target:
            cmd.append(f"--target={self.target}")
        cmd.append("-")

        self._chunks = []
        self._started_at = time.monotonic()
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except OSError as exc:
            raise AudioError(f"could not start pw-record: {exc}") from exc

        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        frame_bytes = 2
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            with self._lock:
                self._chunks.append(chunk)
            self._level = _peak(chunk)
            if self.max_seconds and self.elapsed > self.max_seconds:
                log.warning("max_seconds reached, stopping capture")
                proc.terminate()
                break
        if proc.stderr is not None:
            try:
                self._stderr = proc.stderr.read() or b""
            except OSError:
                pass

    def stop(self) -> bytes:
        """Stop capture and return the raw PCM collected so far."""
        proc = self._proc
        if proc is None:
            return b""
        self._proc = None
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        if self._reader is not None:
            self._reader.join(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        with self._lock:
            pcm = b"".join(self._chunks)
            self._chunks = []
        if not pcm and self._stderr:
            raise AudioError(self._stderr.decode("utf-8", "replace").strip())
        return pcm

    def duration(self, pcm: bytes) -> float:
        return len(pcm) / (2 * self.sample_rate) if self.sample_rate else 0.0

    def to_wav(self, pcm: bytes) -> bytes:
        return pcm_to_wav(pcm, self.sample_rate)


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def _peak(chunk: bytes) -> float:
    """Peak amplitude of a little-endian s16 buffer, normalised to 0.0-1.0."""
    if len(chunk) < 2:
        return 0.0
    import array

    samples = array.array("h")
    samples.frombytes(chunk[: len(chunk) - (len(chunk) % 2)])
    if not samples:
        return 0.0
    return min(1.0, max(abs(min(samples)), abs(max(samples))) / 32768.0)
