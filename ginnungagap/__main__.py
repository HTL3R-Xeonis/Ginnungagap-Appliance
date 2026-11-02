"""Command line: python -m ginnungagap validate|show|render FILE."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from ginnungagap import config as config_module
from ginnungagap import render as render_module


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ginnungagap",
        description="The one configuration file of the Ginnungagap appliance.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("validate", "check the file and report every problem"),
        ("show", "print the effective configuration (keys hidden)"),
        ("render", "write the configuration of every service"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("file", help="path of ginnungagap.yml")
        if name == "render":
            command.add_argument(
                "--out",
                default="rendered",
                help="folder to write into (default: rendered)",
            )
            command.add_argument(
                "--generator-config",
                default=str(
                    Path(__file__).resolve().parent.parent.parent
                    / "topology-generator"
                    / "config"
                ),
                help="config folder of the topology-generator repository (prompt and example files)",
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = config_module.load(args.file)
    except config_module.ConfigError as exc:
        print(f"{args.file} is not valid:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    if args.command == "validate":
        print(f"{args.file} is valid.")
    elif args.command == "show":
        print(yaml.safe_dump(config_module.redacted(config), sort_keys=False), end="")
    else:
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
