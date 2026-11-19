"""
The Operator's state (accounts, session secret, AI switch, push audit, snippet topics, app log) was in the container's writable
layer on appliances installed before it was rendered onto the data volume, and every recreate of the container erased it.
`operate.migrate_operator_state` carries it over once, before the container is recreated. The fake Docker here has a real
folder for the container's layer and one for the volume and really runs the shell script the migration hands to Docker.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from ginnungagap import operate

WORKDIR = "/srv/ginnungagap"


class FakeDocker:
    def __init__(self, root: Path) -> None:
        self.layer = root / "layer"
        self.volume = root / "volume"
        self.layer.mkdir()
        self.volume.mkdir()
        self.container: str | None = "oldcid"
        self.calls: list[list[str]] = []
        self.fail: list[str] = []
        self.copied: list[str] = []

    def text(self, args):
        return " ".join(args)

    def run(self, args):
        self.calls.append(list(args))
        line = self.text(args)
        if any(pattern in line for pattern in self.fail):
            return 1
        if args[:2] == ["docker", "cp"]:
            _, _, source = args[2].partition(":")
            relative = Path(source).relative_to(WORKDIR)
            self.copied.append(str(relative))
            target = Path(args[3])
            origin = self.layer / relative
            if origin.is_dir():
                shutil.copytree(origin, target)
            else:
                shutil.copy2(origin, target)
            return 0
        if args[:2] == ["docker", "run"] and "/migrate:ro" in line:
            script = args[-1]
            migrate = args[args.index("-v", args.index("-v") + 1) + 1].split(":")[0]
            script = script.replace('"/data/', f'"{self.volume}/').replace(
                '"/migrate/', f'"{migrate}/'
            )
            return subprocess.run(["sh", "-c", script], check=False).returncode
        return 0

    def inspect(self, args):
        if "ps" in args and "-q" in args:
            return f"{self.container}\n" if self.container else ""
        if args[:2] == ["docker", "diff"]:
            lines = []
            for path in sorted(self.layer.rglob("*")):
                lines.append(f"A {WORKDIR}/{path.relative_to(self.layer)}")
            return "\n".join(["C /srv", f"C {WORKDIR}", *lines]) + "\n"
        if args[:2] == ["docker", "inspect"] and "{{.Image}}" in args:
            return "sha256:operator-image\n"
        if args[:2] == ["docker", "run"] and args[-1] == "ls -A /data":
            return "\n".join(sorted(os.listdir(self.volume))) + "\n"
        return ""


@pytest.fixture
def docker(tmp_path):
    return FakeDocker(tmp_path)


@pytest.fixture
def bundle(tmp_path):
    folder = tmp_path / "bundle"
    (folder / "rendered").mkdir(parents=True)
    (folder / "rendered" / "compose.env").write_text(
        "COMPOSE_PROJECT_NAME=ginnungagap\n"
    )
    return folder


def old_container_state(docker, secret=b"S" * 64):
    (docker.layer / "users.json").write_text(
        '{"admin": {"role": "admin"}, "leon": {"role": "user"}}'
    )
    (docker.layer / "users.json").chmod(0o600)
    (docker.layer / "session_secret.key").write_bytes(secret)
    (docker.layer / "ai_config.json").write_text("true")
    (docker.layer / "push_audit.jsonl").write_text('{"who": "leon"}\n')
    (docker.layer / "config_snippets").mkdir()
    (docker.layer / "config_snippets" / "vlan.json").write_text('{"label": "vlan"}')
    (docker.layer / "logs").mkdir()
    (docker.layer / "logs" / "app.log").write_text("line\n")


def migrate(docker, bundle, out=None):
    return operate.migrate_operator_state(
        bundle, docker.run, docker.inspect, (out if out is not None else []).append
    )


def test_an_old_containers_state_is_carried_over_to_the_volume(docker, bundle):
    old_container_state(docker)
    out: list[str] = []
    assert migrate(docker, bundle, out) is True
    assert (docker.volume / "users.json").read_text().count("leon") == 1
    assert (docker.volume / "session_secret.key").read_bytes() == b"S" * 64
    assert (docker.volume / "ai_config.json").read_text() == "true"
    assert (docker.volume / "push_audit.jsonl").read_text() == '{"who": "leon"}\n'
    assert (docker.volume / "config_snippets" / "vlan.json").is_file()
    assert (docker.volume / "logs" / "app.log").read_text() == "line\n"
    assert oct((docker.volume / "users.json").stat().st_mode & 0o777) == "0o600"
    assert any("Carried the Operator's state over" in line for line in out)
    # copied with docker cp out of the old container, written with a throw-away container of the Operator's own image
    run = next(c for c in docker.calls if c[:2] == ["docker", "run"])
    assert "sha256:operator-image" in run and "ginnungagap_data:/data" in run
    assert not any("python:3.12-slim" in " ".join(c) for c in docker.calls)


def test_nothing_on_the_volume_is_ever_overwritten(docker, bundle):
    old_container_state(docker)
    (docker.volume / "users.json").write_text("NEWER")
    (docker.volume / "config_snippets").mkdir()
    (docker.volume / "config_snippets" / "mine.json").write_text("mine")
    assert migrate(docker, bundle) is True
    assert (docker.volume / "users.json").read_text() == "NEWER"
    assert not (docker.volume / "config_snippets" / "vlan.json").exists()
    assert (docker.volume / "config_snippets" / "mine.json").read_text() == "mine"
    # the files that were missing on the volume still came over
    assert (docker.volume / "session_secret.key").read_bytes() == b"S" * 64


def test_running_it_again_does_nothing(docker, bundle):
    old_container_state(docker)
    assert migrate(docker, bundle) is True
    docker.calls.clear()
    docker.copied.clear()
    out: list[str] = []
    assert migrate(docker, bundle, out) is True
    assert docker.copied == [] and out == []
    assert not any(c[:2] == ["docker", "cp"] for c in docker.calls)
    # a later change of the file on the volume survives a third run too
    (docker.volume / "users.json").write_text("changed on the volume")
    assert migrate(docker, bundle) is True
    assert (docker.volume / "users.json").read_text() == "changed on the volume"


def test_without_an_operator_container_or_without_state_in_it_nothing_happens(
    docker, bundle
):
    docker.container = None
    assert migrate(docker, bundle) is True
    assert docker.calls == []
    docker.container = "oldcid"  # a container that never wrote any state (the files are on the volume already)
    assert migrate(docker, bundle) is True
    assert not any(c[:2] == ["docker", "cp"] for c in docker.calls)


def test_a_failure_is_reported_so_that_the_container_is_not_recreated(docker, bundle):
    old_container_state(docker)
    docker.fail = ["docker cp"]
    out: list[str] = []
    assert migrate(docker, bundle, out) is False
    assert any("failed" in line for line in out)
    assert not (docker.volume / "users.json").exists()
    docker.fail = ["/migrate:ro"]
    assert migrate(docker, bundle) is False


def test_the_temporary_copy_is_removed(docker, bundle, tmp_path, monkeypatch):
    seen = []
    real = operate.tempfile.mkdtemp

    def tracking(*args, **kwargs):
        seen.append(real(*args, **kwargs))
        return seen[-1]

    monkeypatch.setattr(operate.tempfile, "mkdtemp", tracking)
    old_container_state(docker)
    migrate(docker, bundle)
    docker.fail = []
    assert seen and not Path(seen[0]).exists()


# ---- in `apply`: before anything is recreated, and a failure stops it ----


@pytest.fixture
def config_file(tmp_path, example):
    example["templates"]["store"]["local"]["path"] = str(tmp_path / "store")
    path = tmp_path / "ginnungagap.yml"
    path.write_text(yaml.safe_dump(example))
    return path


def apply(config_file, bundle, generator_sources, docker):
    lines: list[str] = []
    code = operate.apply(
        config_file,
        bundle,
        generator_sources,
        run=docker.run,
        out=lines.append,
        fetch=lambda url: (
            200,
            '{"warning": null, "templates": 1, "servers": 1}',
            None,
        ),
        sleep=lambda s: None,
        inspect=docker.inspect,
    )
    return code, lines


def test_apply_carries_the_state_over_before_it_recreates_anything(
    docker, bundle, config_file, generator_sources
):
    old_container_state(docker)
    apply(config_file, bundle, generator_sources, docker)
    commands = [" ".join(c) for c in docker.calls]
    migrated = next(i for i, c in enumerate(commands) if "/migrate:ro" in c)
    first_up = next(i for i, c in enumerate(commands) if " up -d" in c)
    assert migrated < first_up
    assert (docker.volume / "users.json").is_file()


def test_apply_does_not_recreate_when_the_state_could_not_be_carried_over(
    docker, bundle, config_file, generator_sources
):
    old_container_state(docker)
    docker.fail = ["docker cp"]
    code, lines = apply(config_file, bundle, generator_sources, docker)
    assert code == 1
    assert any("not recreating anything" in line for line in lines)
    assert not any(" up -d" in " ".join(c) for c in docker.calls)
    assert not any(" rm -sf" in " ".join(c) for c in docker.calls)


def test_the_template_store_recreation_does_not_get_ahead_of_the_migration(
    docker, bundle, config_file, generator_sources
):
    """`rm -sf ... ginnungagap` (a changed template store) removes the Operator container, so the state goes first."""
    old_container_state(docker)
    stale = (
        '{"device":":/old","o":"addr=1.2.3.4,ro,nfsvers=4,soft,timeo=100","type":"nfs"}'
    )
    original = docker.inspect

    def inspect(args):
        if args[:3] == ["docker", "volume", "inspect"]:
            return stale
        return original(args)

    docker.inspect = inspect
    apply(config_file, bundle, generator_sources, docker)
    commands = [" ".join(c) for c in docker.calls]
    migrated = next(i for i, c in enumerate(commands) if "/migrate:ro" in c)
    removed = next(i for i, c in enumerate(commands) if " rm -sf" in c)
    assert migrated < removed


# ---- backup and restore use an image that is always there ----


def test_backup_and_restore_use_the_operators_own_image_when_it_exists(
    tmp_path, bundle
):
    config = tmp_path / "ginnungagap.yml"
    config.write_text("x: 1\n")
    calls: list[list[str]] = []

    def run(args):
        calls.append(args)
        return 0

    def inspect(args):
        return "sha256:1" if args[-1] == "ginnungagap-ginnungagap:latest" else None

    folder = operate.backup(config, bundle, tmp_path, run=run, inspect=inspect)
    assert folder is not None
    assert "ginnungagap-ginnungagap:latest" in calls[0]
    assert not any("python:3.12-slim" in " ".join(c) for c in calls)
    (folder / "data.tgz").write_text("x")
    calls.clear()
    assert operate.restore(folder, bundle, run=run, inspect=inspect) == 0
    assert any("ginnungagap-ginnungagap:latest" in c for c in calls)
    assert not any("python:3.12-slim" in " ".join(c) for c in calls)


def test_backup_falls_back_to_the_base_image_when_nothing_was_built_yet(
    tmp_path, bundle
):
    config = tmp_path / "ginnungagap.yml"
    config.write_text("x: 1\n")
    calls: list[list[str]] = []
    folder = operate.backup(
        config,
        bundle,
        tmp_path,
        run=lambda a: calls.append(a) or 0,
        inspect=lambda args: None,
    )
    assert folder is not None and "python:3.12-slim" in calls[0]
