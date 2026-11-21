{
  description = "voxflow - push-to-talk dictation for Linux (Wispr Flow for Wayland)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems =
        f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        voxflow = pkgs.callPackage ./package.nix { };
        default = voxflow;
      });

      apps = forAllSystems (pkgs: rec {
        voxflow = {
          type = "app";
          program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.voxflow}/bin/voxflow";
        };
        default = voxflow;
      });

      nixosModules.default =
        { pkgs, ... }:
        {
          imports = [ ./module.nix ];
          services.voxflow.package = nixpkgs.lib.mkDefault self.packages.${pkgs.stdenv.hostPlatform.system}.voxflow;
        };
      nixosModules.voxflow = self.nixosModules.default;

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          name = "voxflow-dev";

          packages = [
            (pkgs.python3.withPackages (ps: [
              ps.evdev
              ps.requests
              ps.anthropic
              ps.pygobject3
              ps.pygobject-stubs
            ]))
            pkgs.gtk4
            pkgs.libadwaita
            pkgs.gobject-introspection
            pkgs.pipewire
            pkgs.wl-clipboard
            pkgs.libnotify
            pkgs.ruff
          ];

          # Needed so PyGObject can find the Gtk/Adw typelibs in the shell.
          shellHook = ''
            export GI_TYPELIB_PATH="${
              pkgs.lib.makeSearchPath "lib/girepository-1.0" [
                pkgs.gtk4
                pkgs.libadwaita
                pkgs.glib.out
                pkgs.gobject-introspection
                pkgs.graphene
                pkgs.gdk-pixbuf
                pkgs.pango.out
                pkgs.harfbuzz
                pkgs.cairo
              ]
            }"
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
            export XDG_DATA_DIRS="${pkgs.gtk4}/share:${pkgs.adwaita-icon-theme}/share:$XDG_DATA_DIRS"
            echo "voxflow dev shell - run: python -m voxflow doctor"
          '';
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt-tree);
    };
}
