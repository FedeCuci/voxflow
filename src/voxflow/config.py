"""Configuration, secrets, and filesystem paths.

Config lives in TOML so it stays hand-editable; secrets live in a separate
0600 file so the main config can be shared or version-controlled safely.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "voxflow"
CONFIG_PATH = CONFIG_DIR / "config.toml"
SECRETS_PATH = CONFIG_DIR / "secrets.toml"
STATE_DIR = _xdg("XDG_STATE_HOME", ".local/share") / "voxflow"
HISTORY_PATH = STATE_DIR / "history.db"

SOCKET_NAME = "control.sock"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    path = Path(base) / "voxflow" if base else Path(f"/tmp/voxflow-{os.getuid()}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def socket_path() -> Path:
    return runtime_dir() / SOCKET_NAME


@dataclass
class HotkeyConfig:
    # Bare modifiers are the safest default: we monitor the keyboard without
    # grabbing it, so the keypress still reaches the focused app. A modifier
    # inserts nothing on its own, so nothing is corrupted.
    #
    # Mouse buttons are not inert - browsers navigate back on BTN_SIDE - so
    # for those the device is grabbed and mirrored instead, which hides just
    # that one button from other applications.
    key: str = "KEY_RIGHTCTRL"
    mode: str = "hold"  # hold | toggle
    tap_to_lock: bool = True
    tap_ms: int = 250
    cancel_key: str = "KEY_ESC"
    # auto   - hide the hotkey from other apps only when it is a mouse button
    # always - always hide it (grabs the device that carries it)
    # never  - never hide it; the hotkey also reaches the focused app
    suppress: str = "auto"


@dataclass
class AudioConfig:
    target: str = ""  # PipeWire node name; empty means the default source
    sample_rate: int = 16000
    max_seconds: int = 300
    min_seconds: float = 0.3


@dataclass
class EngineConfig:
    provider: str = "groq"  # groq | openai
    model: str = ""  # empty means the provider default
    language: str = ""  # empty means autodetect
    prompt: str = ""  # biasing hint sent to the ASR model
    timeout: float = 60.0


@dataclass
class CleanupConfig:
    enabled: bool = False
    model: str = "claude-haiku-4-5"
    max_tokens: int = 2000
    timeout: float = 20.0
    style: str = "default"  # default | email | message | notes | code
    spoken_punctuation: bool = True
    instructions: str = ""


@dataclass
class InjectConfig:
    method: str = "paste"  # paste | type
    restore_clipboard: bool = True
    paste_delay_ms: int = 120
    trailing_space: bool = False


@dataclass
class FeedbackConfig:
    notifications: bool = True
    sounds: bool = False


@dataclass
class Config:
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    inject: InjectConfig = field(default_factory=InjectConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    terms: list[str] = field(default_factory=list)
    replacements: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or CONFIG_PATH
        cfg = cls()
        if not path.exists():
            return cfg
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        for f in fields(cls):
            if is_dataclass(getattr(cfg, f.name)):
                section = data.get(f.name)
                if isinstance(section, dict):
                    _apply(getattr(cfg, f.name), section)
        dictionary = data.get("dictionary")
        if isinstance(dictionary, dict):
            terms = dictionary.get("terms")
            if isinstance(terms, list):
                cfg.terms = [str(t) for t in terms if str(t).strip()]
        replacements = data.get("replacements")
        if isinstance(replacements, dict):
            cfg.replacements = {
                str(k): str(v) for k, v in replacements.items() if str(k).strip()
            }
        return cfg

    def save(self, path: Path | None = None) -> None:
        path = path or CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(self.to_toml(), encoding="utf-8")
        tmp.replace(path)

    def to_toml(self) -> str:
        out: list[str] = [
            "# voxflow configuration. API keys live in secrets.toml, not here.",
            "",
        ]
        for f in fields(self):
            section = getattr(self, f.name)
            if not is_dataclass(section):
                continue
            out.append(f"[{f.name}]")
            for sf in fields(section):
                out.append(f"{sf.name} = {_toml_value(getattr(section, sf.name))}")
            out.append("")
        out.append("[dictionary]")
        out.append("# Proper nouns, jargon, and names the transcriber should get right.")
        out.append(f"terms = {_toml_value(self.terms)}")
        out.append("")
        out.append("[replacements]")
        out.append('# Literal substitutions applied last, e.g. "my email" = "me@example.com"')
        for key, value in self.replacements.items():
            out.append(f"{_toml_str(key)} = {_toml_str(value)}")
        out.append("")
        return "\n".join(out)


@dataclass
class Secrets:
    groq_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # The settings UI writes this file, so a value stored here wins. An
    # environment variable is only consulted when the stored value is empty.
    _ENV = {
        "groq_api_key": "GROQ_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
    }

    @classmethod
    def load(cls, path: Path | None = None) -> Secrets:
        path = path or SECRETS_PATH
        secrets = cls()
        if path.exists():
            with path.open("rb") as handle:
                data = tomllib.load(handle)
            _apply(secrets, data)
        for name, env in cls._ENV.items():
            if not getattr(secrets, name):
                setattr(secrets, name, os.environ.get(env, ""))
        return secrets

    def save(self, path: Path | None = None) -> None:
        path = path or SECRETS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".toml.tmp")
        lines = ["# voxflow secrets. Keep mode 0600.", ""]
        for f in fields(self):
            lines.append(f"{f.name} = {_toml_str(getattr(self, f.name))}")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
        path.chmod(0o600)


def _apply(obj: Any, data: dict[str, Any]) -> None:
    """Copy known keys onto a dataclass, coercing to the declared type."""
    for f in fields(obj):
        if f.name not in data:
            continue
        raw = data[f.name]
        try:
            if f.type in ("bool", bool):
                value = bool(raw)
            elif f.type in ("int", int):
                value = int(raw)
            elif f.type in ("float", float):
                value = float(raw)
            else:
                value = str(raw)
        except (TypeError, ValueError):
            continue
        setattr(obj, f.name, value)


def _toml_str(value: str) -> str:
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_str(v) for v in value) + "]"
    return _toml_str(value)
