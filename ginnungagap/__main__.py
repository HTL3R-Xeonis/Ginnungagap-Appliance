"""Command line: python -m ginnungagap validate|show|render|apply|status|backup|restore|build-images|firstboot|issue|update|versions|apply-update-request."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

import yaml

from ginnungagap import bootstrap as bootstrap_module
from ginnungagap import config as config_module
from ginnungagap import console, firstboot, operate, setup_watcher, statelock
from ginnungagap import manifest as manifest_module
from ginnungagap import render as render_module
from ginnungagap import status as status_module
from ginnungagap import update as update_module
from ginnungagap import update_request as update_request_module

DEFAULT_GENERATOR_CONFIG = os.environ.get("GINNUNGAGAP_GENERATOR_CONFIG") or str(
    Path(__file__).resolve().parent.parent.parent
    / manifest_module.CHECKOUT_NAMES["generator"]
    / "config"
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
        help="config folder of the generator repository (Mimyr)",
    )

    apply = with_file(
        "apply",
        "validate, render, start what is needed and wait until everything is healthy",
    )
    apply.add_argument("--generator-config", default=DEFAULT_GENERATOR_CONFIG)
    apply.add_argument("--no-start", action="store_true", help="only render")
    apply.add_argument(
        "--no-build", action="store_true", help="use the images already on disk"
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

    build = commands.add_parser(
        "build-images",
        help="build and pull the images of every service (for an appliance image)",
    )
    build.add_argument("--generator-config", default=DEFAULT_GENERATOR_CONFIG)
    build.add_argument(
        "--remove-other-images",
        action="store_true",
        help="afterwards remove every tagged Docker image that the compose project does not use "
        "(only for the build machine of an appliance image; remove the containers first)",
    )
    first = commands.add_parser(
        "firstboot",
        help="first boot of an appliance image: the configuration from the hypervisor's guestinfo",
    )
    first.add_argument(
        "--config-dir",
        default=os.environ.get("GINNUNGAGAP_CONFIG_DIR", "/etc/ginnungagap"),
    )

    issue = commands.add_parser(
        "issue",
        help="rewrite the text of the console login (SSH address and port, web interface) for the machine as it is now",
    )
    issue.add_argument(
        "--config-dir",
        default=os.environ.get("GINNUNGAGAP_CONFIG_DIR", "/etc/ginnungagap"),
    )
    issue.add_argument(
        "--clear-screens",
        action="store_true",
        help="also wipe the idle login screens (after a password change, so the initial password does not stay on them)",
    )

    restore = commands.add_parser(
        "restore",
        help="replace the app's data by a backup (stops the app for a moment)",
    )
    restore.add_argument("backup", help="a backup folder made by `backup`")
    restore.add_argument(
        "--yes", action="store_true", help="confirm that the current data is replaced"
    )

    boot = commands.add_parser(
        "bootstrap",
        help="bring up only the gateway and the setup wizard, for a machine with no configuration yet",
    )
    boot.add_argument(
        "--no-build", action="store_true", help="use the images already on disk"
    )

    staged = commands.add_parser(
        "apply-staged",
        help="apply the configuration the setup wizard staged, if any (run by the systemd path unit)",
    )
    staged.add_argument(
        "--staging-dir",
        default=os.environ.get(
            "GINNUNGAGAP_SETUP_STAGING_DIR", "/var/lib/ginnungagap/setup"
        ),
    )
    staged.add_argument(
        "--config-dir",
        default=os.environ.get("GINNUNGAGAP_CONFIG_DIR", "/etc/ginnungagap"),
    )
    staged.add_argument("--generator-config", default=DEFAULT_GENERATOR_CONFIG)
    staged.add_argument(
        "--no-build", action="store_true", help="use the images already on disk"
    )
    web = commands.add_parser(
        "apply-update-request",
        help="act on the update request the web panel left, if any (run by the systemd path unit)",
    )
    web.add_argument(
        "--requests-dir",
        default=os.environ.get(
            "GINNUNGAGAP_UPDATE_REQUEST_DIR", "/var/lib/ginnungagap/update-requests"
        ),
    )
    web.add_argument(
        "--config-dir",
        default=os.environ.get("GINNUNGAGAP_CONFIG_DIR", "/etc/ginnungagap"),
    )
    _update_parser(commands)
    return parser


def _add_update_options(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--config",
        default=os.path.join(
            os.environ.get("GINNUNGAGAP_CONFIG_DIR", "/etc/ginnungagap"),
            "ginnungagap.yml",
        ),
        help="the configuration file (its update section is read)",
    )
    command.add_argument("--json", action="store_true", help="machine-readable output")


def _update_parser(commands) -> None:
    update = commands.add_parser(
        "update",
        help="look for a new release of the appliance components, install it, go back (see the README, Updating components)",
        description=(
            "Idynoun, the updater of the appliance: looks for a new release, installs it component by component and goes "
            "back when something is wrong. Components: "
            + ", ".join(
                manifest_module.display_name(n) for n in manifest_module.COMPONENT_NAMES
            )
            + "."
        ),
    )
    actions = update.add_mutually_exclusive_group()
    for flag, text in (
        (
            "check",
            "ask the source, compare with what is installed (the default; exit 10 when an update is available)",
        ),
        (
            "apply",
            "download, verify, build, restart, check health; rolls back by itself when it fails",
        ),
        ("rollback", "go back to the previous release (needs no internet)"),
        ("list", "the releases the source offers"),
        ("versions", "the installed release and every component's version"),
        ("history", "the releases installed on this machine so far"),
        (
            "adopt",
            "record the components installed now as release 'adopted' (the first --apply does it too)",
        ),
        (
            "set-token",
            "read an optional GitHub token from stdin into the token file (0600)",
        ),
        (
            "resume",
            "finish or roll back an update that was interrupted (run by the boot unit)",
        ),
    ):
        actions.add_argument(
            f"--{flag}", dest="action", action="store_const", const=flag, help=text
        )
    update.add_argument("--continue-tx", help=argparse.SUPPRESS)
    update.add_argument(
        "--offline-dir",
        help="a folder with the files of ONE release (or of several, one per sub-folder) instead of GitHub",
    )
    update.add_argument(
        "--version", help="the release to go to (default: the newest of the channel)"
    )
    update.add_argument(
        "--channel",
        choices=("stable", "prerelease"),
        help="default: update.channel of the configuration",
    )
    update.add_argument(
        "--component",
        action="append",
        default=[],
        help=(
            "only this component (repeatable; not the tested combination); a key, a code name such as igdrasyl, or "
            "a key of an earlier version (see the README)"
        ),
    )
    update.add_argument(
        "--yes", action="store_true", help="do not ask for confirmation"
    )
    update.add_argument(
        "--dry-run", action="store_true", help="show the plan, change nothing"
    )
    update.add_argument(
        "--no-backup",
        action="store_true",
        help="skip the pre-update backup (not recommended)",
    )
    update.add_argument(
        "--force",
        action="store_true",
        help="update although the appliance is not healthy before",
    )
    update.add_argument("--allow-downgrade", action="store_true")
    update.add_argument(
        "--no-start",
        action="store_true",
        help="unpack, verify and build only; restart nothing (for an appliance image)",
    )
    update.add_argument(
        "--wait",
        type=float,
        default=120.0,
        help="seconds to wait for health after the restarts",
    )
    update.add_argument(
        "--restore-data",
        action="store_true",
        help="a rollback also restores the data from the pre-update backup",
    )
    _add_update_options(update)
    versions = commands.add_parser(
        "versions",
        help="the installed release and every component's version (same as update --versions)",
    )
    _add_update_options(versions)


def _run_update(args: argparse.Namespace) -> int:
    if args.command == "versions":
        options = update_module.Options(action="versions", json=args.json)
    else:
        action = args.action or "check"
        if args.continue_tx:
            action = "continue"
        options = update_module.Options(
            action=action,
            offline_dir=args.offline_dir,
            version=args.version,
            channel=args.channel,
            components=args.component,
            yes=args.yes,
            dry_run=args.dry_run,
            no_backup=args.no_backup,
            force=args.force,
            allow_downgrade=args.allow_downgrade,
            no_start=args.no_start,
            json=args.json,
            wait=args.wait,
            restore_data=args.restore_data,
            continue_id=args.continue_tx,
        )
    return update_module.run_update(options, args.config, bundle=operate.bundle_dir())


def _load(path: str):
    try:
        return config_module.load(path)
    except config_module.ConfigError as exc:
        print(f"{path} is not valid:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return None


def _refresh_console(config_dir: str | Path, clear_screens: bool = False) -> None:
    """The console login text always describes the machine as it is now (SSH address and port, web interface)."""
    console.refresh(config_dir)
    console.reload_login_prompt(clear_screens=clear_screens)


def _line_buffered() -> None:
    """
    Print each line when it is written, also when stdout is a pipe or a file (the web panel's update log, a systemd unit): the
    default there is block buffering, so the steps of an update appeared in one lump at the end, after the output of Docker.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _line_buffered()
    args = _parser().parse_args(argv)
    bundle = operate.bundle_dir()
    if args.command in ("update", "versions"):
        return _run_update(args)
    if args.command == "restore":
        if not args.yes:
            print(
                "restore replaces the app's current data (host profiles, projects, designs, notes). Run it again with --yes.",
                file=sys.stderr,
            )
            return 1
        return operate.restore(args.backup, bundle)
    if args.command == "build-images":
        if args.remove_other_images:
            return operate.build_images(
                bundle, args.generator_config, remove_other=True
            )
        return operate.build_images(bundle, args.generator_config)
    if args.command == "firstboot":
        return firstboot.run(args.config_dir)
    if args.command == "issue":
        _refresh_console(args.config_dir, clear_screens=args.clear_screens)
        return 0
    if args.command == "bootstrap":
        result = bootstrap_module.run(bundle, build=not args.no_build)
        if result == 0:
            _refresh_console(render_module.host_paths()["GINNUNGAGAP_CONFIG_DIR"])
        return result
    if args.command == "apply-staged":
        result = setup_watcher.apply_staged(
            args.staging_dir,
            Path(args.config_dir) / "ginnungagap.yml",
            bundle,
            args.generator_config,
            build=not args.no_build,
        )
        _refresh_console(args.config_dir)
        return result
    if args.command == "apply-update-request":
        return update_request_module.process(
            args.requests_dir,
            statelock.update_dir(),
            Path(args.config_dir) / "ginnungagap.yml",
            bundle,
        )
    if args.command == "apply":
        result = operate.apply(
            args.file,
            bundle,
            args.generator_config,
            start=not args.no_start,
            build=not args.no_build,
            wait_seconds=args.wait,
        )
        if result == 0 and not args.no_start:
            _refresh_console(
                os.environ.get("GINNUNGAGAP_CONFIG_DIR", console.CONFIG_DIR)
            )
        return result

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
        print(status_module.title_line())
        print(status_module.render_table(rows))
        return 0 if status_module.healthy(rows) else 1
    elif args.command == "backup":
        return 0 if operate.backup(args.file, bundle, args.to) else 1
    return 0


def _run() -> int:
    """`main`, quiet when the reader of the output went away (`ginnungagap ... | head`): no traceback, the usual exit code."""
    try:
        return main()
    except BrokenPipeError:
        os.dup2(
            os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno()
        )  # so the flush at exit does not complain again
        return 141


if __name__ == "__main__":
    sys.exit(_run())
