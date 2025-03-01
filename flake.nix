{
  description = "quaylib";

  inputs = {
    nixpkgs = {
      url = "github:NixOS/nixpkgs/nixos-unstable";
    };
  };

  outputs = {
    self,
    nixpkgs,
  }: let
    systems = [
      "aarch64-linux"
      "x86_64-linux"
    ];
    eachSystem = f:
      nixpkgs.lib.genAttrs systems (system:
        f {
          pkgs = nixpkgs.legacyPackages.${system};
        });
  in {
    formatter = eachSystem ({pkgs}: pkgs.alejandra);

    packages = eachSystem ({pkgs}: {
      default = pkgs.python3Packages.buildPythonApplication {
        pname = "quaylib";
        version = "0.0.1";
        pyproject = true;

        src = ./.;

        build-system = with pkgs.python3Packages; [
          setuptools
        ];

        # has no tests
        doCheck = false;

        dependencies = [
          pkgs.python3Packages.httpx
          pkgs.skopeo
        ];
      };
    });

    # https://wiki.nixos.org/wiki/NixOS_modules
    nixosModules = {
      default = {
        config,
        lib,
        pkgs,
        ...
      }: let
        cfg = config.services.quaylib;
      in {
        options.services.quaylib = {
          enable = lib.mkEnableOption "quaylib service";
          environmentFile = lib.mkOption {
            type = lib.types.path;
            description = ''
              Environment file as defined in {manpage}`systemd.exec(5)`.

              Must set QUAY_API_KEY and QUAY_REGISTRY_AUTH.
            '';
          };
        };

        config = lib.mkIf cfg.enable {
          systemd.services.quaylib = {
            serviceConfig = {
              Type = "oneshot";
              ExecStart = "${self.packages.${pkgs.system}.default}/bin/quaylib";
              DynamicUser = true;
              EnvironmentFile = cfg.environmentFile;
              Environment = [
                # Show print()s immediately
                "PYTHONUNBUFFERED=1"
              ];
            };
          };
          systemd.timers.quaylib = {
            wantedBy = ["timers.target"];
            timerConfig = {
              OnBootSec = "5m";
              OnUnitActiveSec = "1h";
              Unit = "quaylib.service";
            };
          };
        };
      };
    };
  };
}
