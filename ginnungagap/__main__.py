"""Command line: python -m ginnungagap validate|show|render|apply|status|backup|restore|build-images|firstboot."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

import yaml

from ginnungagap import config as config_module
from ginnungagap import firstboot, operate
from ginnungagap import render as render_module
from ginnungagap import status as status_module

DEFAULT_GENERATOR_CONFIG = os.environ.get("GINNUNGAGAP_GENERATOR_CONFIG") or str(
    Path(__file__).resolve().parent.parent.parent / "topology-generator" / "config"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ginnungagap",
        description="The one configuration file of the Ginnungagap appliance.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def with_file(name: str, text: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=text)
        command.add_argument("file", help="path of ginnungagap.yml")
        return command

    with_file("validate", "check the file and report every problem")
    with_file("show", "print the effective configuration (keys hidden)")
    render = with_file("render", "write the configuration of every service")
    render.add_argument(
        "--out", default="rendered", help="folder to write into (default: rendered)"
    )
    render.add_argument(
        "--generator-config",
        default=DEFAULT_GENERATOR_CONFIG,
        help="config folder of the topology-generator repository",
    )

    apply = with_file(
        "apply",
        "validate, render, start what is needed and wait until everything is healthy",
    )
    apply.add_argument("--generator-config", default=DEFAULT_GENERATOR_CONFIG)
    apply.add_argument("--no-start", action="store_true", help="only render")
    apply.add_argument(
        "--no-build", action="store_true", help="do not rebuild the images"
    )
    apply.add_argument(
        "--wait",
        type=float,
        default=90.0,
        help="seconds to wait for every service to be healthy",
    )

    status = with_file(
        "status", "one health view over every service, asked through the gateway"
    )
    status.add_argument(
        "--url", help="the gateway's address if it is not on this machine's own ports"
    )
    status.add_argument(
        "--user",
        help="user name when the gateway asks for a login (the password is asked for, or read from GINNUNGAGAP_PASSWORD)",
    )

    backup = with_file(
        "backup", "copy the config file and the app's data into a new folder"
    )
    backup.add_argument(
        "--to", required=True, help="folder to create the backup folder in"
    )

    commands.add_parser(
        "build-images",
        help="build and pull the images of every service (for an appliance image)",
    ).add_argument("--generator-config", default=DEFAULT_GENERATOR_CONFIG)
    first = commands.add_parser(
        "firstboot",
        help="first boot of an appliance image: the configuration from the hypervisor's guestinfo",
    )
    first.add_argument(
        "--config-dir",
        default=os.environ.get("GINNUNGAGAP_CONFIG_DIR", "/etc/ginnungagap"),
    )

    restore = commands.add_parser(
        "restore",
        help="replace the app's data by a backup (stops the app for a moment)",
    )
    restore.add_argument("backup", help="a backup folder made by `backup`")
    restore.add_argument(
        "--yes", action="store_true", help="confirm that the current data is replaced"
    )
    return parser


def _load(path: str):
    try:
        return config_module.load(path)
    except config_module.ConfigError as exc:
        print(f"{path} is not valid:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    bundle = operate.bundle_dir()
    if args.command == "restore":
        if not args.yes:
            print(
                "restore replaces the app's current data (host profiles, projects, designs, notes). Run it again with --yes.",
                file=sys.stderr,
            )
            return 1
        return operate.restore(args.backup, bundle)
    if args.command == "build-images":
        return operate.build_images(bundle, args.generator_config)
    if args.command == "firstboot":
        return firstboot.run(args.config_dir)
    if args.command == "apply":
        return operate.apply(
            args.file,
            bundle,
            args.generator_config,
            start=not args.no_start,
            build=not args.no_build,
            wait_seconds=args.wait,
        )

    config = _load(args.file)
    if config is None:
        return 1
    if args.command == "validate":
        print(f"{args.file} is valid.")
    elif args.command == "show":
        print(yaml.safe_dump(config_module.redacted(config), sort_keys=False), end="")
    elif args.command == "render":
        try:
            written = render_module.render(config, args.out, args.generator_config)
        except render_module.RenderError as exc:
            print(f"render failed: {exc}", file=sys.stderr)
            return 1
        print(f"Wrote {len(written)} file(s) into {args.out}/:")
        for name in written:
            print(f"  {name}")
        print(
            f"Start with: docker compose --env-file {args.out}/compose.env up -d --build"
        )
    elif args.command == "status":
        password = os.environ.get("GINNUNGAGAP_PASSWORD") or (
            getpass.getpass(f"Password for {args.user}: ") if args.user else None
        )
        fetch = status_module.make_fetch(args.user, password)
        rows = status_module.check(
            config, status_module.base_url(config, args.url), fetch
        )
        print(status_module.render_table(rows))
        return 0 if status_module.healthy(rows) else 1
    elif args.command == "backup":
        return 0 if operate.backup(args.file, bundle, args.to) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
