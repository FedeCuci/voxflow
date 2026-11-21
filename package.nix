{
  lib,
  python3Packages,
  wrapGAppsHook4,
  gobject-introspection,
  gtk4,
  libadwaita,
  pipewire,
  wl-clipboard,
  libnotify,
  libcanberra-gtk3,
}:

python3Packages.buildPythonApplication {
  pname = "voxflow";
  version = "0.1.0";
  pyproject = true;

  # Name the inputs explicitly rather than filtering ./. — this keeps build
  # artefacts (result symlinks, __pycache__, .git) out of the store path, so
  # the hash only changes when the sources actually change. It also means this
  # builds cleanly when called by path from /etc/nixos.
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./src
      ./share
    ];
  };

  build-system = [ python3Packages.setuptools ];

  dependencies = with python3Packages; [
    evdev
    requests
    anthropic
    pygobject3
  ];

  nativeBuildInputs = [
    wrapGAppsHook4
    gobject-introspection
  ];

  buildInputs = [
    gtk4
    libadwaita
  ];

  # wrapGAppsHook4 and buildPythonApplication both want to wrap the binaries.
  # Let the Python wrapper do it and splice the GApps arguments into it, so the
  # settings window still finds its typelibs and icon theme.
  dontWrapGApps = true;

  # These have to be appended to the shell array here rather than passed via
  # makeWrapperArgs: that is a Nix list handed to the C wrapper, which cannot
  # expand a shell array and will not split a single "--prefix PATH : ..."
  # string into separate arguments.
  preFixup = ''
    makeWrapperArgs+=(
      "''${gappsWrapperArgs[@]}"
      --prefix PATH : "${
        lib.makeBinPath [
          pipewire # pw-record, pw-dump
          wl-clipboard # wl-copy, wl-paste
          libnotify # notify-send
          libcanberra-gtk3 # canberra-gtk-play (optional sound cues)
        ]
      }"
    )
  '';

  postInstall = ''
    install -Dm644 share/applications/dev.voxflow.App.desktop \
      $out/share/applications/dev.voxflow.App.desktop
  '';

  pythonImportsCheck = [
    "voxflow"
    "voxflow.cli"
    "voxflow.daemon"
  ];

  # There is no test suite yet; the import check above is the smoke test.
  doCheck = false;

  meta = {
    description = "Push-to-talk dictation for Linux: hold a key, speak, get clean text at your cursor";
    longDescription = ''
      voxflow is a Wispr Flow-style dictation tool for Wayland desktops. It
      captures a global push-to-talk hotkey by reading evdev directly,
      records from PipeWire, transcribes via a hosted Whisper endpoint,
      optionally rewrites the transcript into finished prose with Claude, and
      inserts the result at the cursor through a uinput virtual keyboard.
    '';
    mainProgram = "voxflow";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
  };
}
