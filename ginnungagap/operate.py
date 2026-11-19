"""
Operating the compose project: apply a configuration, back up and restore the app's data.

Every call to Docker goes through a `Runner` (a function taking an argument list and returning the exit code), so the
commands that would be run can be checked without Docker. Nothing here recreates a container that owns data: all state is in
named volumes, and only the services whose rendered file changed are recreated.
"""

from __future__ import annotations

import hashlib
import json
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
Inspect = Callable[[list[str]], str | None]

# A change to one of these files is not picked up by a running service on its own (compose only recreates a container when
# its own definition or env_file changed), so the owning service is recreated. The GNS3 collector re-reads its file on every
# run, and the env files recreate their services by themselves.
FILE_OWNERS = {
    "Caddyfile": "gateway",
    "generator/topology_config.yml": "generator",
    "generator/prompt_template.txt": "generator",
    "generator/example_topology.yml": "generator",
    "ova-store/Caddyfile": "ova-store",
}

DATA_VOLUME = "data"
OFFLINE_UP_ARGS = ("--no-build", "--pull", "never")
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


def default_inspect(args: list[str], cwd: Path | None = None) -> str | None:
    """The output of a read-only command, None when it fails (for example a volume that does not exist)."""
    try:
        result = subprocess.run(
            args, check=False, cwd=cwd, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


# The services that can mount the template store (a bind mount or an NFS export).
STORE_VOLUME = "template-store"
STORE_USERS = ("template-esxi", "collector-esxi", "ova-store", "ginnungagap")


def _wanted_store_options(rendered: Path) -> dict[str, str] | None:
    """The driver options the rendered compose.env asks for, None when it does not say."""
    try:
        lines = (rendered / "compose.env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    values = dict(line.split("=", 1) for line in lines if "=" in line)
    keys = {
        "type": "TEMPLATE_STORE_VOLUME_TYPE",
        "o": "TEMPLATE_STORE_VOLUME_OPTS",
        "device": "TEMPLATE_STORE_VOLUME_DEVICE",
    }
    if any(key not in values for key in keys.values()):
        return None
    return {name: values[key].strip().strip("\"'") for name, key in keys.items()}


def _store_volume_is_stale(project: str, rendered: Path, inspect: Inspect) -> bool:
    """
    Docker never changes an existing volume's driver options, and compose does not recreate it when they change in the
    file: after switching the store from NFS to a local folder (or to another export) the services would keep reading the
    old one. True when the volume exists and its options differ from the rendered ones.
    """
    wanted = _wanted_store_options(rendered)
    answer = inspect(
        [
            "docker",
            "volume",
            "inspect",
            "--format",
            "{{json .Options}}",
            f"{project}_{STORE_VOLUME}",
        ]
    )
    if wanted is None or answer is None:
        return False
    try:
        actual = json.loads(answer.strip() or "null") or {}
    except ValueError:
        return False
    return {key: str(actual.get(key, "")) for key in wanted} != wanted


def _runner(run: Runner | None, bundle: Path) -> Runner:
    return run or (lambda args: default_runner(args, cwd=bundle))


# What the running containers were last started with: written only after an apply went through, so a run that failed
# half-way (e.g. `up` aborted) cannot make the next run believe "nothing changed" and skip recreating a service.
APPLIED = ".applied-digests.json"


def _digests(folder: Path) -> dict[str, str]:
    if not folder.is_dir():
        return {}
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.name not in (render_module.MANIFEST, APPLIED)
    }


def _applied_digests(folder: Path) -> dict[str, str]:
    try:
        data = json.loads((folder / APPLIED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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


def _make_store_dir(path: Path) -> None:
    path.mkdir(parents=True, mode=0o755, exist_ok=True)


def _ensure_local_store(
    config: dict,
    make_dir: Callable[[Path], None],
    out: Callable[[str], None],
) -> None:
    """
    A local template store is bind-mounted from a folder on this machine; Docker refuses to mount one that does not exist,
    which is exactly the state of a fresh VM (the wizard's default is /srv/templates). The folder is created empty - the
    templates in it are the admin's to put there.
    """
    store = config["templates"]["store"]
    if store["type"] != "local":
        return
    path = Path(store["local"]["path"])
    if path.exists():
        return
    try:
        make_dir(path)
    except OSError as exc:
        out(f"could not create the template store folder {path}: {exc}")
        return
    out(f"created the (empty) template store folder {path}")


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
    inspect: Inspect | None = None,
    make_store_dir: Callable[[Path], None] | None = None,
) -> int:
    """Validate, render, start what is needed and wait until it is healthy. Returns the exit code (0 = all healthy)."""
    run = _runner(run, bundle)
    inspect = inspect or (lambda args: default_inspect(args, cwd=bundle))
    try:
        config = config_module.load(config_file)
    except config_module.ConfigError as exc:
        out(f"{config_file} is not valid:")
        for problem in exc.problems:
            out(f"  - {problem}")
        return 1
    rendered = bundle / "rendered"
    before = _applied_digests(rendered)
    if not before:
        # nothing recorded yet: what is in the folder now is what the running containers use - remember it BEFORE
        # rendering over it, so even a first run that fails half-way leaves a reference for the next one
        before = _digests(rendered)
        if before:
            (rendered / APPLIED).write_text(json.dumps(before), encoding="utf-8")
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

    _ensure_local_store(config, make_store_dir or _make_store_dir, out)
    project = _project(bundle)
    if _store_volume_is_stale(project, rendered, inspect):
        out("The template store changed: recreating its Docker mount")
        if run(compose_args(bundle, "rm", "-sf", *STORE_USERS)) != 0 or (
            run(["docker", "volume", "rm", f"{project}_{STORE_VOLUME}"]) != 0
        ):
            out("recreating the template store volume failed")
            return 1

    offline = () if build else OFFLINE_UP_ARGS
    up = compose_args(
        bundle, "up", "-d", "--remove-orphans", *(("--build",) if build else offline)
    )
    if run(up) != 0:
        out("docker compose up failed")
        return 1
    recreate = sorted({FILE_OWNERS[name] for name in changed if name in FILE_OWNERS})
    if (
        recreate
        and run(
            compose_args(
                bundle, "up", "-d", "--force-recreate", "--no-deps", *offline, *recreate
            )
        )
        != 0
    ):
        out(f"recreating {', '.join(recreate)} failed")
        return 1
    if recreate:
        out(f"Recreated because their configuration changed: {', '.join(recreate)}")
    (rendered / APPLIED).write_text(json.dumps(after), encoding="utf-8")

    base = status_module.base_url(config)
    asking = fetch or status_module.make_fetch()
    deadline = time.monotonic() + wait_seconds
    while True:
        rows = status_module.check(config, base, asking)
        if (
            status_module.healthy(rows)
            or status_module.healthy_behind_login(rows)
            or time.monotonic() >= deadline
        ):
            break
        sleep(3)
    out(status_module.render_table(rows))
    if status_module.healthy_behind_login(rows):
        out(
            "The gateway is up and asks for a login, so the services behind it were not checked here - "
            "run `ginnungagap status --user <name>` for the full check."
        )
        return 0
    return 0 if status_module.healthy(rows) else 1


def _image_reference(name: str) -> str:
    """`name` the way `docker image ls` writes it: with a tag (`latest` when there is none)."""
    last = name.rsplit("/", 1)[-1]
    return name if ":" in last else f"{name}:latest"


def remove_other_images(
    bundle: Path,
    run: Runner,
    inspect: Inspect,
    out: Callable[[str], None] = print,
) -> int:
    """
    Removes every tagged Docker image that the compose project does not use (the rendered environment must exist): leftovers of
    an older bundle, for example the image of a service that was dropped, which `docker image prune` keeps because it is tagged.
    Nothing is removed when the list of the needed images cannot be read or is empty. Containers that still use an image keep
    it, which is reported as a failure, so remove the containers first.
    """
    wanted_text = inspect(compose_args(bundle, "--profile", "*", "config", "--images"))
    wanted = {
        _image_reference(line.strip())
        for line in (wanted_text or "").splitlines()
        if line.strip()
    }
    if not wanted:
        out("could not read the images the compose project needs; no image was removed")
        return 1
    present_text = inspect(
        ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"]
    )
    if present_text is None:
        out("could not list the Docker images; no image was removed")
        return 1
    present = {line.strip() for line in present_text.splitlines() if line.strip()}
    others = sorted(ref for ref in present if "<none>" not in ref and ref not in wanted)
    for ref in others:
        out(f"removing the image {ref}, which the compose project does not use")
        if run(["docker", "image", "rm", ref]) != 0:
            out(f"removing the image {ref} failed")
            return 1
    return 0


def build_images(
    bundle: Path,
    generator_config: str | Path | None,
    run: Runner | None = None,
    out: Callable[[str], None] = print,
    remove_other: bool = False,
    inspect: Inspect | None = None,
) -> int:
    """
    Builds and pulls the images of EVERY service, without starting anything - for an appliance image, which must come up
    without internet and without a build. A configuration with everything switched on is rendered only to give compose its
    environment, and removed again afterwards, so no file of it ends up in the image. With `remove_other` every tagged image
    that does not belong to the compose project is removed afterwards (only for the build machine of an image).
    """
    run = _runner(run, bundle)
    inspect = inspect or (lambda args: default_inspect(args, cwd=bundle))
    config = config_module.normalise(
        {
            "services": {"ova_store": True},
            "generator": {
                "llm_servers": [
                    {
                        "name": "placeholder",
                        "url": "http://127.0.0.1:11434",
                        "model": "none",
                    }
                ]
            },
        }
    )
    rendered = bundle / "rendered"
    try:
        render_module.render(config, rendered, generator_config)
    except render_module.RenderError as exc:
        out(f"render failed: {exc}")
        return 1
    try:
        for step, command in (
            ("building", ("--profile", "*", "build")),
            ("pulling", ("--profile", "*", "pull", "--ignore-buildable")),
        ):
            out(f"{step} the images of every service")
            if run(compose_args(bundle, *command)) != 0:
                out(f"{step} the images failed")
                return 1
        if remove_other and remove_other_images(bundle, run, inspect, out) != 0:
            return 1
    finally:
        shutil.rmtree(rendered, ignore_errors=True)
    out("All images are in the local image store.")
    return 0


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
