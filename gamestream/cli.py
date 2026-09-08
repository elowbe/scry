"""Game Stream command-line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import os

from .auth import AuthManager
from .catalog import GameCatalog
from .certificates import ensure_certificate
from .config import DEFAULT_CONFIG, load_config
from .doctor import ready, run_checks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scry-server", description="Scry - Server")
    parser.add_argument("--config", default=os.environ.get("SCRY_CONFIG", os.environ.get("GAMESTREAM_CONFIG", str(DEFAULT_CONFIG))), help="TOML configuration file")
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create the TLS certificate and access token")
    init.add_argument("--force-certificate", action="store_true")
    doctor = commands.add_parser("doctor", help="Check host streaming requirements")
    doctor.add_argument("--probe-capture", action="store_true")
    doctor.add_argument("--json", action="store_true")
    setup = commands.add_parser("setup", help="Prepare Scry and optionally install your DLSS runtime")
    setup.add_argument("--dlss-dll", help="Path to your nvngx_dlssnr.dll")
    commands.add_parser("tray", help="Run Scry - Server with a status menu")
    commands.add_parser("list-games", help="List discovered Steam and Ubisoft games")
    serve = commands.add_parser("serve", help="Run the HTTPS/WebRTC server")
    serve.add_argument("--resume-steam", help="Attach to an already running installed Steam app without relaunching it")
    serve.add_argument("--resume-dlss", action="store_true", help="Keep DLSS enabled for the resumed session")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config(args.config)
        if args.command == "setup":
            from .onboarding import setup
            setup(config, args.dlss_dll)
            return 0
        if args.command == "tray":
            from .tray import run_tray
            run_tray(config)
            return 0
        if args.command == "init":
            cert, key = ensure_certificate(config, force=args.force_certificate)
            token = AuthManager(config.server.token_file).token
            host = config.server.public_host or "localhost"
            print(f"Certificate: {cert}")
            print(f"Private key: {key}")
            print(f"Pairing URL: https://{host}:{config.server.port}/?token={token}")
            return 0
        if args.command == "doctor":
            checks = run_checks(config, probe_capture=args.probe_capture)
            if args.json:
                print(json.dumps({"ready": ready(checks), "checks": [item.public() for item in checks]}, indent=2))
            else:
                symbols = {"ok": "OK", "warning": "WARN", "error": "FAIL"}
                for check in checks:
                    print(f"{symbols[check.status]:4}  {check.label}: {check.detail}")
            return 0 if ready(checks) else 1
        if args.command == "list-games":
            for game in GameCatalog(config).all():
                proton = "proton-ready" if game.proton_ready else "proton-on-first-launch"
                print(f"{game.provider:7} {game.id:>10}  {game.name} [{proton}]")
            return 0
        if args.command == "serve":
            from .server import run_server

            run_server(config, resume_steam=args.resume_steam, resume_dlss=args.resume_dlss)
            return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())

