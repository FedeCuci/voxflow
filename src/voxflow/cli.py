"""Command-line entry point."""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .config import CONFIG_PATH, SECRETS_PATH, Config, Secrets, socket_path

OK = "\033[32m  ok \033[0m"
WARN = "\033[33mwarn \033[0m"
FAIL = "\033[31mfail \033[0m"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="voxflow",
        description="Push-to-talk dictation: hold a key, speak, get clean text at your cursor.",
    )
    parser.add_argument("--version", action="version", version=f"voxflow {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug logging"
    )

    # Accept -v on either side of the subcommand. SUPPRESS keeps the
    # subparser's default from overwriting a -v given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="enable debug logging",
    )

    sub = parser.add_subparsers(dest="command")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=[common])

    add("daemon", "run the dictation daemon (usually via systemd)")
    add("toggle", "start or stop dictating")
    add("start", "start dictating")
    add("stop", "stop dictating and insert the text")
    add("cancel", "discard the current recording")
    add("reload", "reload the configuration")
    add("app", "open the voxflow window")
    add("settings", "open the settings window")
    add("dictate", "start or stop dictating to the clipboard")
    add("doctor", "check that everything this needs is in place")
    add("devices", "list available microphones")
    add("config", "show config file paths and effective settings")

    p_status = add("status", "show daemon status")
    p_status.add_argument("--json", action="store_true", help="machine-readable output")

    p_capture = add("capture-key", "print the next key or mouse button you press")
    p_capture.add_argument("-t", "--timeout", type=float, default=10.0)

    p_history = add("history", "show recent dictations")
    p_history.add_argument("-n", "--limit", type=int, default=10)
    p_history.add_argument("--full", action="store_true", help="print the full text")
    p_history.add_argument("--raw", action="store_true", help="show the raw transcript")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    command = args.command or "status"
    handlers = {
        "daemon": _cmd_daemon,
        "app": _cmd_app,
        "settings": _cmd_settings,
        "doctor": _cmd_doctor,
        "devices": _cmd_devices,
        "status": _cmd_status,
        "capture-key": _cmd_capture_key,
        "history": _cmd_history,
        "config": _cmd_config,
    }
    if command in handlers:
        return handlers[command](args)
    if command in {"toggle", "dictate", "start", "stop", "cancel", "reload"}:
        return _cmd_send(command)
    parser.print_help()
    return 2


def _cmd_daemon(_args) -> int:
    from .daemon import main as daemon_main

    return daemon_main()


def _cmd_app(_args) -> int:
    from .ui.window import main as window_main

    return window_main()


def _cmd_settings(_args) -> int:
    from .ui.settings import main as settings_main

    return settings_main()


def _cmd_send(command: str) -> int:
    from .control import send_command

    try:
        reply = send_command(command)
    except ConnectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not reply.get("ok", False):
        print(f"error: {reply.get('error', 'unknown error')}", file=sys.stderr)
        return 1
    state = reply.get("state")
    if state:
        print(state)
    return 0


def _cmd_status(args) -> int:
    from .control import send_command

    try:
        reply = send_command("status")
    except ConnectionError as exc:
        if args.json:
            print(json.dumps({"state": "stopped"}))
        else:
            print(f"voxflow: not running\n  {exc}")
        return 1
    if args.json:
        print(json.dumps(reply, indent=2))
        return 0
    print(f"state       {reply.get('state')}" + ("  (locked)" if reply.get("latched") else ""))
    print(f"hotkey      {reply.get('hotkey')} ({reply.get('mode')})")
    print(f"provider    {reply.get('provider')}")
    print(f"cleanup     {'on' if reply.get('cleanup') else 'off'}")
    if reply.get("state") == "recording":
        print(f"elapsed     {reply.get('elapsed')}s  level {reply.get('level')}")
    print(f"dictations  {reply.get('dictations')} this session")
    if reply.get("last"):
        print(f"last        {reply.get('last')}")
    return 0


def _cmd_devices(_args) -> int:
    from .audio import list_sources

    sources = list_sources()
    if not sources:
        print("no audio sources found")
        return 1
    cfg = Config.load()
    for name, label in sources:
        marker = "*" if cfg.audio.target == name else " "
        print(f"{marker} {label}\n    {name}")
    if not cfg.audio.target:
        print("\n* no device configured; using the system default source")
    return 0


def _cmd_capture_key(args) -> int:
    from .hotkey import HotkeyError, capture_key

    print(
        f"press a key or mouse button within {args.timeout:.0f}s "
        "(left/right click and Esc are ignored)…",
        file=sys.stderr,
    )
    try:
        name = capture_key(args.timeout)
    except HotkeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not name:
        print("timed out", file=sys.stderr)
        return 1
    print(name)
    return 0


def _cmd_history(args) -> int:
    from .history import History

    history = History()
    entries = history.recent(args.limit)
    if not entries:
        print("no dictations recorded yet")
        return 0
    for entry in entries:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.ts))
        tag = "cleaned" if entry.cleaned else "raw"
        print(f"\033[2m{when}  {entry.duration:5.1f}s  {entry.engine}/{tag}\033[0m")
        text = entry.raw_text if args.raw else entry.final_text
        if not args.full:
            flat = " ".join(text.split())
            text = flat if len(flat) <= 100 else flat[:99] + "…"
        print(f"  {text}\n")
    stats = history.stats()
    print(
        f"\033[2m{int(stats['count'])} dictations, "
        f"{stats['seconds'] / 60:.0f} min spoken, "
        f"{int(stats['characters'])} characters written\033[0m"
    )
    history.close()
    return 0


def _cmd_config(_args) -> int:
    cfg = Config.load()
    secrets = Secrets.load()
    print(f"config   {CONFIG_PATH}{'' if CONFIG_PATH.exists() else '  (not created yet)'}")
    print(f"secrets  {SECRETS_PATH}{'' if SECRETS_PATH.exists() else '  (not created yet)'}")
    print(f"socket   {socket_path()}")
    print()
    print(cfg.to_toml())
    for field_name in ("groq_api_key", "openai_api_key", "anthropic_api_key"):
        value = getattr(secrets, field_name)
        print(f"# {field_name}: {'set' if value else 'not set'}")
    return 0


def _cmd_doctor(_args) -> int:
    cfg = Config.load()
    secrets = Secrets.load()
    failures = 0
    warnings = 0

    def report(status: str, label: str, detail: str = "") -> None:
        nonlocal failures, warnings
        if status is FAIL:
            failures += 1
        elif status is WARN:
            warnings += 1
        print(f"[{status}] {label}")
        if detail:
            print(f"         {detail}")

    print("Python packages")
    for module, why in (
        ("evdev", "global hotkeys and the virtual keyboard"),
        ("requests", "talking to the transcription API"),
        ("anthropic", "LLM transcript cleanup"),
        ("gi", "the settings window"),
    ):
        if importlib.util.find_spec(module) is not None:
            report(OK, module)
        else:
            report(FAIL if module != "gi" else WARN, module, f"needed for {why}")

    print("\nExternal programs")
    for binary, why, required in (
        ("pw-record", "recording from the microphone", True),
        ("pw-dump", "listing microphones", False),
        ("wl-copy", "inserting text via the clipboard", True),
        ("wl-paste", "restoring your clipboard afterwards", False),
        ("notify-send", "on-screen feedback", False),
    ):
        if shutil.which(binary):
            report(OK, binary)
        else:
            report(FAIL if required else WARN, binary, f"needed for {why}")

    print("\nSession")
    session = os.environ.get("XDG_SESSION_TYPE", "unknown")
    if os.environ.get("WAYLAND_DISPLAY"):
        report(OK, f"Wayland session ({session})")
    elif os.environ.get("DISPLAY"):
        report(WARN, "X11 session", "built and tested against Wayland")
    else:
        report(
            FAIL,
            "no graphical session detected",
            "WAYLAND_DISPLAY is unset; the clipboard will not work",
        )

    print("\nPermissions")
    uinput = Path("/dev/uinput")
    if not uinput.exists():
        report(
            FAIL,
            "/dev/uinput is missing",
            "add `hardware.uinput.enable = true;` to your NixOS config and rebuild",
        )
    elif os.access(uinput, os.W_OK):
        report(OK, "/dev/uinput is writable")
    else:
        report(
            FAIL,
            "/dev/uinput is not writable",
            "add your user to the 'uinput' group, then log out and back in",
        )

    try:
        import evdev

        paths = evdev.list_devices()
        readable = [p for p in paths if os.access(p, os.R_OK)]
        if readable:
            report(OK, f"{len(readable)} readable input device(s)")
        else:
            report(
                WARN,
                "no readable input devices",
                "push-to-talk needs the 'input' group; "
                "without it, use `voxflow toggle` from a desktop shortcut",
            )
    except ImportError:
        report(FAIL, "cannot check input devices", "python-evdev is not installed")

    groups = _groups()
    for group in ("input", "uinput"):
        if group in groups:
            report(OK, f"member of group '{group}'")
        else:
            report(WARN, f"not in group '{group}'", "re-login after adding it")

    print("\nAudio")
    try:
        from .audio import list_sources

        sources = list_sources()
        if sources:
            report(OK, f"{len(sources)} capture device(s)")
            if cfg.audio.target and cfg.audio.target not in {n for n, _ in sources}:
                report(
                    WARN,
                    f"configured device not present: {cfg.audio.target}",
                    "run `voxflow devices` and pick another",
                )
        else:
            report(WARN, "no capture devices found", "is a microphone connected?")
    except Exception as exc:
        report(WARN, "could not query PipeWire", str(exc))

    print("\nCredentials")
    provider = cfg.engine.provider
    field = {"groq": "groq_api_key", "openai": "openai_api_key"}.get(provider, "")
    if field and getattr(secrets, field, ""):
        report(OK, f"{provider} API key configured")
    else:
        report(
            FAIL,
            f"no {provider} API key",
            "set one with `voxflow settings`",
        )
    if cfg.cleanup.enabled:
        if secrets.anthropic_api_key:
            report(OK, f"Anthropic API key configured ({cfg.cleanup.model})")
        else:
            report(
                WARN,
                "cleanup is on but no Anthropic API key is set",
                "dictations will fall back to local punctuation rules",
            )
    else:
        report(OK, "cleanup disabled (local punctuation rules only)")

    print("\nDaemon")
    from .control import send_command

    try:
        reply = send_command("status")
        report(OK, f"running, state={reply.get('state')}")
    except ConnectionError:
        report(
            WARN,
            "not running",
            "start it with `systemctl --user start voxflow`",
        )

    print()
    if failures:
        print(f"{failures} blocking problem(s), {warnings} warning(s).")
        return 1
    print(f"No blocking problems. {warnings} warning(s).")
    return 0


def _groups() -> set[str]:
    try:
        out = subprocess.run(
            ["id", "-Gn"], capture_output=True, text=True, timeout=5, check=True
        ).stdout
        return set(out.split())
    except (subprocess.SubprocessError, OSError):
        return set()


if __name__ == "__main__":
    sys.exit(main())
