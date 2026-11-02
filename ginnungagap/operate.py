"""
Operating the compose project: apply a configuration, back up and restore the app's data.

Every call to Docker goes through a `Runner` (a function taking an argument list and returning the exit code), so the
commands that would be run can be checked without Docker. Nothing here recreates a container that owns data: all state is in
named volumes, and only the services whose rendered file changed are recreated.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ginnungagap import config as config_module
from ginnungagap import render as render_module
from ginnungagap import status as status_module

Runner = Callable[[list[str]], int]

# A change to one of these files is not picked up by a running service on its own (compose only recreates a container when
# its own definition or env_file changed), so the owning service is recreated. The GNS3 collector re-reads its file on every
# run, and the env files recreate their services by themselves.
FILE_OWNERS = {
    "Caddyfile": "gateway",
    "generator/topology_config.yml": "generator",
    "generator/prompt_template.txt": "generator",
    "generator/example_topology.yml": "generator",
}

DATA_VOLUME = "data"
APP_SERVICES = (
    "ginnungagap",
    "collector-gns3",
)  # the services that use the data volume


def default_runner(args: list[str], cwd: Path | None = None) -> int:
    """
    Runs the command. Docker is run from the bundle folder: compose resolves the files named in COMPOSE_FILE (rendered/compose.env)
    relative to the working directory, not to --project-directory, so from anywhere else it looks for compose.yml in the wrong place.
    """
    return subprocess.run(args, check=False, cwd=cwd).returncode


def _runner(run: Runner | None, bundle: Path) -> Runner:
    return run or (lambda args: default_runner(args, cwd=bundle))


def _digests(folder: Path) -> dict[str, str]:
    if not folder.is_dir():
        return {}
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.name != render_module.MANIFEST
    }


def compose_args(bundle: Path, *more: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-directory",
        str(bundle),
        "--env-file",
        str(bundle / "rendered" / "compose.env"),
        *more,
    ]


def apply(
    config_file: str | Path,
    bundle: Path,
    generator_config: str | Path | None,
    run: Runner | None = None,
    start: bool = True,
    build: bool = True,
    wait_seconds: float = 90.0,
    fetch: status_module.Fetch | None = None,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> int:
    """Validate, render, start what is needed and wait until it is healthy. Returns the exit code (0 = all healthy)."""
    run = _runner(run, bundle)
    try:
        config = config_module.load(config_file)
    except config_module.ConfigError as exc:
        out(f"{config_file} is not valid:")
        for problem in exc.problems:
            out(f"  - {problem}")
        return 1
    rendered = bundle / "rendered"
    before = _digests(rendered)
    try:
        written = render_module.render(config, rendered, generator_config)
    except render_module.RenderError as exc:
        out(f"render failed: {exc}")
        return 1
    after = _digests(rendered)
    changed = sorted(
        name for name in after if before and before.get(name) != after[name]
    )
    out(
        f"Rendered {len(written)} file(s); changed since last time: {', '.join(changed) if changed else 'none'}"
    )
    if not start:
        return 0

    up = compose_args(bundle, "up", "-d", "--remove-orphans")
    if build:
        up.append("--build")
    if run(up) != 0:
        out("docker compose up failed")
        return 1
    recreate = sorted({FILE_OWNERS[name] for name in changed if name in FILE_OWNERS})
    if (
        recreate
        and run(
            compose_args(bundle, "up", "-d", "--force-recreate", "--no-deps", *recreate)
        )
        != 0
    ):
        out(f"recreating {', '.join(recreate)} failed")
        return 1
    if recreate:
        out(f"Recreated because their configuration changed: {', '.join(recreate)}")

    base = status_module.base_url(config)
    asking = fetch or status_module.make_fetch()
    deadline = time.monotonic() + wait_seconds
    while True:
        rows = status_module.check(config, base, asking)
        if status_module.healthy(rows) or time.monotonic() >= deadline:
            break
        sleep(3)
    out(status_module.render_table(rows))
    return 0 if status_module.healthy(rows) else 1


def _project(bundle: Path) -> str:
    """The compose project name the volumes are prefixed with (written by the renderer into compose.env)."""
    try:
        for line in (
            (bundle / "rendered" / "compose.env")
            .read_text(encoding="utf-8")
            .splitlines()
        ):
            if line.startswith("COMPOSE_PROJECT_NAME="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "ginnungagap"


def backup(
    config_file: str | Path,
    bundle: Path,
    destination: str | Path,
    run: Runner | None = None,
    now: Callable[[], datetime] = datetime.now,
    image: str = "python:3.12-slim",
    out: Callable[[str], None] = print,
) -> Path | None:
    """
    Copies the config file and the app's data volume (host profiles with their stored passwords, projects, designs, notes) into
    a new folder under `destination`. Everything in it is readable by the owner only. Returns the folder, None on failure.
    """
    run = _runner(run, bundle)
    folder = Path(destination) / f"ginnungagap-backup-{now():%Y%m%d-%H%M%S}"
    folder.mkdir(parents=True, mode=0o700)
    shutil.copyfile(config_file, folder / "ginnungagap.yml")
    (folder / "ginnungagap.yml").chmod(0o600)
    uid, gid = os.getuid(), os.getgid()
    code = run(
        [
            "docker", "run", "--rm", "-v", f"{_project(bundle)}_{DATA_VOLUME}:/data:ro", "-v", f"{folder}:/backup",
            "--entrypoint", "sh", image, "-c",
            f"tar czf /backup/data.tgz -C /data . && chown {uid}:{gid} /backup/data.tgz && chmod 600 /backup/data.tgz",
        ]
    )  # fmt: skip
    if code != 0:
        out("reading the data volume failed")
        return None
    out(
        f"Backup written to {folder} (config file and data.tgz, owner-readable only - it holds the stored host passwords)"
    )
    return folder


def restore(
    backup_folder: str | Path,
    bundle: Path,
    run: Runner | None = None,
    image: str = "python:3.12-slim",
    out: Callable[[str], None] = print,
) -> int:
    """Replaces the app's data volume by the backup's data.tgz. The services using it are stopped first and started again."""
    run = _runner(run, bundle)
    folder = Path(backup_folder)
    archive = folder / "data.tgz"
    if not archive.is_file():
        out(f"{folder} has no data.tgz - not a backup folder")
        return 1
    if run(compose_args(bundle, "stop", *APP_SERVICES)) != 0:
        out("stopping the services failed")
        return 1
    code = run(
        [
            "docker", "run", "--rm", "-v", f"{_project(bundle)}_{DATA_VOLUME}:/data", "-v", f"{folder}:/backup:ro",
            "--entrypoint", "sh", image, "-c",
            "find /data -mindepth 1 -delete && tar xzf /backup/data.tgz -C /data",
        ]
    )  # fmt: skip
    if code != 0:
        out("writing the data volume failed - the services are left stopped")
        return 1
    if run(compose_args(bundle, "up", "-d", *APP_SERVICES)) != 0:
        out("starting the services again failed")
        return 1
    out(
        "Data restored. (The config file in the backup is not applied: compare it with yours and run `apply` if you want it.)"
    )
    return 0


def bundle_dir() -> Path:
    """The folder with compose.yml: the parent of this package."""
    return Path(__file__).resolve().parent.parent
