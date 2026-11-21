# NixOS module. Import it and set:
#
#   services.voxflow = {
#     enable = true;
#     users = [ "yourname" ];
#   };
#
# The group changes below need a re-login (not just a rebuild) to take effect.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.voxflow;
in
{
  options.services.voxflow = {
    enable = lib.mkEnableOption "the voxflow dictation daemon";

    package = lib.mkOption {
      type = lib.types.package;
      description = "The voxflow package to use.";
    };

    users = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "alice" ];
      description = ''
        Users who may dictate. They are added to the `input` group (so
        voxflow can see key presses and releases for push-to-talk) and the
        `uinput` group (so it can create the virtual keyboard it types
        with). Both require a re-login.
      '';
    };

    autoStart = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Start the daemon automatically with the graphical session.";
    };
  };

  config = lib.mkIf cfg.enable {
    # Loads the uinput kernel module, creates the `uinput` group, and installs
    # a udev rule making /dev/uinput group-writable.
    hardware.uinput.enable = true;

    users.users = lib.genAttrs cfg.users (_: {
      extraGroups = [
        "input"
        "uinput"
      ];
    });

    environment.systemPackages = [ cfg.package ];

    systemd.user.services.voxflow = {
      description = "voxflow dictation daemon";
      documentation = [ "man:voxflow(1)" ];
      partOf = [ "graphical-session.target" ];
      after = [ "graphical-session.target" ];
      wantedBy = lib.optionals cfg.autoStart [ "graphical-session.target" ];

      serviceConfig = {
        ExecStart = "${lib.getExe cfg.package} daemon";
        ExecReload = "${pkgs.coreutils}/bin/kill -HUP $MAINPID";
        Restart = "on-failure";
        RestartSec = 2;
        # The daemon needs /dev/uinput and /dev/input, so the usual device
        # sandboxing is deliberately not applied here.
        Slice = "session.slice";
      };
    };
  };
}
