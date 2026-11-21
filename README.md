# voxflow

Push-to-talk dictation for Linux. Hold a key, speak, and finished text appears
at your cursor in whatever app has focus — the thing Wispr Flow does, for
Wayland desktops that it doesn't ship on.

Hold the key for as long as you're talking, or tap it once to lock recording on
and tap again to finish. Audio goes to a hosted Whisper endpoint; the raw
transcript is optionally rewritten by Claude Haiku into text you'd actually
have typed (filler words gone, punctuation and capitalisation fixed, your own
jargon spelled correctly); the result is inserted at the cursor.

## Why it works this way

GNOME on Wayland blocks the three things a dictation tool needs, so each one
is solved below the compositor:

| Need | Why the normal route fails | What voxflow does |
| --- | --- | --- |
| Global push-to-talk | Apps can't register global hotkeys, and the toolkit never reports key *releases* — so "hold to talk" is impossible through the desktop API | Reads `/dev/input/event*` directly via evdev |
| Typing into other apps | Mutter doesn't implement the virtual-keyboard Wayland protocol, so `wtype` can't work | Creates a kernel-level virtual keyboard via `/dev/uinput` |
| Visible status | A window would steal focus from the app you're dictating into | Notifications only, replaced in place |

A keyboard hotkey is *monitored*, not grabbed: grabbing is all-or-nothing and
would swallow every keystroke on that device. The key therefore still reaches
the focused app, which is why the default is a bare modifier (`Right Ctrl`) —
pressing it alone inserts nothing.

A **mouse button** is different, because it is not inert: `BTN_SIDE` is
"back" in most browsers. For mouse buttons voxflow grabs the device, clones it
as a virtual pointer, and forwards every event *except* the chosen button — so
the pointer behaves exactly as before while that one button is consumed. See
[Using a mouse button](#using-a-mouse-button).

Text is inserted by clipboard-and-paste rather than synthesised keystrokes.
One `Ctrl+V` beats typing hundreds of keycodes, and it's layout-independent:
typing by keycode assumes the compositor's keymap matches ours. Your previous
clipboard contents are saved and restored.

## Try it without installing

Clipboard dictation needs **no system changes at all** — no `uinput`, no
`input` group, no rebuild. Build it and add a launcher to your own
applications directory:

```sh
nix build .#voxflow
mkdir -p ~/.local/share/applications
sed "s|^Exec=voxflow|Exec=$PWD/result/bin/voxflow|" \
  share/applications/dev.voxflow.App.desktop \
  > ~/.local/share/applications/dev.voxflow.App.desktop
```

**voxflow** now appears in your app grid. Open it, click **Start** in the
banner, add an API key from the menu, and the **Dictate to clipboard** button
works. Typing at the cursor needs the full install below.

Remove the launcher with
`rm ~/.local/share/applications/dev.voxflow.App.desktop`.

## Install

Add this to `/etc/nixos/local.nix`, or wherever your machine-local config lives.

### On a channels-based system (no flakes)

```nix
{ config, pkgs, ... }:
let
  voxflow = /home/youruser/Projects/voxflow;
in
{
  imports = [ "${voxflow}/module.nix" ];

  services.voxflow = {
    enable = true;
    users = [ "youruser" ];
    package = pkgs.callPackage "${voxflow}/package.nix" { };
  };
}
```

### On a flake-based system

```nix
{
  inputs.voxflow.url = "path:/home/youruser/Projects/voxflow";

  # ...then in your nixosSystem modules:
  imports = [ inputs.voxflow.nixosModules.default ];
  services.voxflow = {
    enable = true;
    users = [ "youruser" ];
  };
}
```

Then, either way:

```sh
sudo nixos-rebuild switch
```

**Log out and back in.** The rebuild adds you to the `input` and `uinput`
groups, and group membership is only picked up by a fresh login session.

Then confirm everything is wired up:

```sh
voxflow doctor
```

Set your API key and preferences:

```sh
voxflow settings
```

### What the module changes

- `hardware.uinput.enable = true` — loads the `uinput` kernel module, creates
  the `uinput` group, and installs a udev rule making `/dev/uinput`
  group-writable.
- Adds the listed users to `input` (read key presses) and `uinput` (create the
  virtual keyboard).
- Installs a systemd **user** service bound to `graphical-session.target`.

Nothing runs as root, and no system service is added.

## Use

| Action | How |
| --- | --- |
| Open the app | Click **voxflow** in the app grid, or `voxflow app` |
| Dictate | Hold `Right Ctrl` (or a side mouse button), speak, release |
| Dictate hands-free | Tap `Right Ctrl`, speak, tap again |
| Cancel a recording | Press `Esc` while recording |
| Dictate without evdev | `voxflow toggle` — bind it to a shortcut (see below) |
| Dictate to the clipboard | The button in the app window, or right-click the launcher → **Dictate to clipboard** |

```sh
voxflow status            # what the daemon is doing
voxflow history -n 5      # recover text that went to the wrong window
voxflow history --raw     # compare the raw transcript with the cleaned text
voxflow devices           # list microphones
voxflow capture-key       # find the evdev name of a key
voxflow doctor            # diagnose a broken setup
```

### The app window

The launcher opens a small window showing state, a live input level, your
recent dictations (click one to copy it back), and a link to settings.
Right-clicking the launcher gives **Dictate to clipboard** and **Settings**
directly.

Its button copies rather than pastes, deliberately: while that window has
focus, a synthesised Ctrl+V would paste into voxflow itself and the text
would be lost. Pasting at the cursor is what the hotkey is for.

### Using a mouse button

Side buttons work as dictation keys and are often nicer than a modifier —
your hand is already there. Open `voxflow settings`, click **Press a key or
button…**, and press the button you want. Left and right click are ignored
during capture so you can still operate the dialog.

Typical codes on a 5-button mouse:

| Button | Code | Normally does |
| --- | --- | --- |
| Rear side button | `BTN_SIDE` | Back (in browsers) |
| Front side button | `BTN_EXTRA` | Forward |
| Wheel click | `BTN_MIDDLE` | Paste / open in new tab |

Because these aren't inert, `[hotkey] suppress` controls whether the button
still reaches your applications:

| Value | Behaviour |
| --- | --- |
| `auto` (default) | Hide it only when the hotkey is a mouse button |
| `always` | Hide it even for keyboard keys |
| `never` | Never hide it — your browser will also navigate back |

Hiding has two visible costs, both because it works by claiming the device
and mirroring it:

- A second pointer named `voxflow-filtered-…` appears in the input device
  list. If you have per-device pointer settings (acceleration, scroll
  direction), re-apply them to it.
- Every mouse event is relayed through voxflow while it runs. At a normal
  1 kHz polling rate this is negligible; if you run your mouse at 8 kHz,
  expect some constant CPU use.

If neither appeals, remap a side button to an unused key such as `F13` in the
mouse's own onboard memory, then use that key as the hotkey — zero overhead,
nothing grabbed, and `F13` does nothing in any application. `voxflow
capture-key` will tell you what the remapped button actually sends.

If voxflow exits or crashes, the grab is released by the kernel and the mouse
returns to normal immediately.

### Without the `input` group

If you'd rather not grant read access to your keyboard, skip the group and
drive it from a GNOME shortcut instead. You lose push-to-talk (hold-to-talk
needs key-release events) but keep tap-to-toggle:

*Settings → Keyboard → Keyboard Shortcuts → Custom Shortcuts*, command
`voxflow toggle`.

`/dev/uinput` access is still required to insert text.

## Configuration

`~/.config/voxflow/config.toml` is written by the settings window and is
hand-editable. API keys live separately in `~/.config/voxflow/secrets.toml`
(mode 0600) so the config can be shared or version-controlled.

Notable settings:

- `[hotkey] key` — a key (`KEY_RIGHTCTRL`) or mouse button (`BTN_SIDE`)
- `[hotkey] mode` (`hold`/`toggle`), `tap_to_lock`, `suppress` (see above)
- `[engine] provider` — `groq` (default, `whisper-large-v3-turbo`) or `openai`
- `[cleanup] enabled`, `style` — `default`, `email`, `message`, `notes`, `code`
- `[inject] method` — `paste` (default) or `type`
- `[dictionary] terms` — names and jargon, biased into the transcriber *and*
  spelled exactly this way by the cleanup pass
- `[replacements]` — literal substitutions, e.g. `"my email" = "me@example.com"`

Cleanup degrades rather than fails: with no Anthropic key, or on any API
error, voxflow falls back to local regex rules (spoken punctuation, sentence
capitalisation) and still inserts your text. A dictation is never lost to a
failed network call — and anything that couldn't be inserted is still in
`voxflow history`.

## Privacy and cost

Audio is uploaded to the configured transcription provider, and the raw
transcript to Anthropic if cleanup is on. Nothing is sent anywhere if you
don't configure a key. Transcripts are stored locally in
`~/.local/share/voxflow/history.db`.

Rough cost per dictation: transcription is a fraction of a cent per minute of
audio; cleanup with Claude Haiku ($1/$5 per million tokens) is a few hundred
tokens, so well under a cent. The transcription backend is behind an
interface — a local faster-whisper or whisper.cpp engine can be added in
`src/voxflow/engines/` without touching the daemon.

## Development

```sh
nix develop
python -m voxflow doctor
python -m voxflow daemon -v

# tests need no hardware, network, or test framework
PYTHONPATH=src python3 tests/test_core.py
```

## Layout

```
src/voxflow/
  cli.py        command-line interface, including `doctor`
  daemon.py     state machine: hotkey -> record -> transcribe -> clean -> insert
  hotkey.py     evdev monitoring, press/release, key capture
  audio.py      PipeWire capture via pw-record
  engines/      transcription backends (groq, openai)
  cleanup.py    Claude Haiku rewrite + offline regex fallback
  inject.py     uinput virtual keyboard and clipboard paste
  control.py    unix-socket control channel (`voxflow toggle`)
  history.py    local sqlite dictation log
  notify.py     focus-safe desktop feedback
  ui/window.py    the app window: status, one-click dictation, history
  ui/settings.py  GTK4/libadwaita settings window
```
