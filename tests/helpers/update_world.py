"""
A fake appliance for the tests of `ginnungagap update`: state folders, an old installation (checkouts in a repos folder), a
bundle with a rendered compose.env, a Docker that only records the commands, and an `Env` with every outside thing faked.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from ginnungagap import update as up
from tests.helpers import fake_release

SERVICES = [
    "gateway",
    "ginnungagap",
    "template-esxi",
    "collector-esxi",
    "template-gns3",
    "collector-gns3",
    "generator",
    "ova-store",
    "setup",
]
PROFILES = ",".join(SERVICES)


class Crash(BaseException):
    """A power loss: nothing in the update catches it, so the journal stays as it was."""


class Docker:
    """Runner and inspect in one: records every command, answers what a healthy host would."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.images = {f"ginnungagap-{s}:latest" for s in SERVICES}
        self.fail: list[tuple[str, int]] = []  # (text in the command, exit code)
        self.crash_on: str | None = None
        self.health: dict[str, str] = {}
        self.docker_up = True
        self.on_run: list[Callable[[list[str]], None]] = []

    def text(self, args: list[str]) -> str:
        return " ".join(args)

    def run(self, args: list[str]) -> int:
        self.calls.append(list(args))
        line = self.text(args)
        if self.crash_on and self.crash_on in line:
            raise Crash(line)
        for hook in self.on_run:
            hook(args)
        for pattern, code in self.fail:
            if pattern in line:
                return code
        if args[:2] == ["docker", "tag"]:
            self.images.add(args[3])
        if args[:3] == ["docker", "image", "rm"]:
            self.images.discard(args[3])
        return 0

    def inspect(self, args: list[str]) -> str | None:
        if args[:2] == ["docker", "version"]:
            return "27.0" if self.docker_up else None
        if args[:3] == ["docker", "image", "inspect"]:
            return "sha256:abc" if args[-1] in self.images else None
        if "ps" in args and "--format" in args:
            return self.health.get(args[-1], "running healthy")
        if args[:3] == ["docker", "image", "ls"]:
            return "\n".join(sorted(self.images))
        return ""

    def compose(self) -> list[str]:
        """The compose commands, without the `docker compose --project-directory ... --env-file ...` prefix."""
        found = []
        for args in self.calls:
            if args[:2] == ["docker", "compose"] and "--env-file" in args:
                found.append(" ".join(args[args.index("--env-file") + 2 :]))
        return found

    def commands(self) -> list[str]:
        return [self.text(a) for a in self.calls]


class FakeApply:
    """Stands for operate.apply (the final health gate)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.codes: list[int] = []

    def __call__(self, config_file, bundle, generator_config, **kwargs) -> int:
        self.calls.append(
            {"config": config_file, "generator": generator_config, **kwargs}
        )
        return self.codes.pop(0) if self.codes else 0


class World:
    def __init__(self, tmp_path: Path, example: dict, monkeypatch) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "state"
        self.config_dir = tmp_path / "etc"
        self.bundle = tmp_path / "opt" / "app"
        self.repos = tmp_path / "srv" / "repos"
        self.layout = up.Layout.under(self.root, self.config_dir)
        self.docker = Docker()
        self.apply = FakeApply()
        self.out: list[str] = []
        self.err: list[str] = []
        self.journal: list[str] = []
        self.backups: list[Path] = []
        self.restores: list[str] = []
        self.backup_ok = True
        self.free = 100 * 1024**3
        self.confirm = True
        self.root_user = True
        self.reexecs: list[tuple[str, str | None]] = []
        self.operator_reports: str | None = None
        self.fetch_code = 200
        self.clock = datetime(2026, 12, 1, 10, 0, 0, tzinfo=UTC)
        self.install_log = tmp_path / "install.log"
        monkeypatch.setenv("GINNUNGAGAP_FAKE_INSTALL_LOG", str(self.install_log))
        monkeypatch.setenv("REPOS_DIR", str(self.repos))
        monkeypatch.delenv("GINNUNGAGAP_INSTALL_ARGS", raising=False)
        monkeypatch.delenv("GINNUNGAGAP_UPDATE_REQUESTER", raising=False)
        monkeypatch.delenv("SUDO_USER", raising=False)
        self.config_file = self.config_dir / "ginnungagap.yml"
        self.config_dir.mkdir(parents=True)
        example = dict(example)
        example["templates"] = {
            **example["templates"],
            "store": {"type": "local", "local": {"path": str(tmp_path / "store")}},
        }
        example["update"] = {"token_file": str(self.config_dir / "update-token")}
        self.config_file.write_text(yaml.safe_dump(example))
        self.token_file = self.config_dir / "update-token"
        (self.config_dir / "install.env").write_text(
            f"PYTHONPATH={self.bundle}\nREPOS_DIR={self.repos}\n"
            f"GINNUNGAGAP_GENERATOR_CONFIG={self.repos}/topology-generator/config\nGINNUNGAGAP_CONFIG_DIR={self.config_dir}\n"
        )
        (self.bundle / "rendered").mkdir(parents=True)
        (self.bundle / "rendered" / "compose.env").write_text(
            f"COMPOSE_PROJECT_NAME=ginnungagap\nCOMPOSE_PROFILES={PROFILES}\n"
        )
        (self.bundle / "ginnungagap").mkdir()
        (self.bundle / "ginnungagap" / "marker.txt").write_text("old program\n")
        self.make_old_installation()
        self.env = up.Env(
            out=self.out.append,
            err=self.err.append,
            run=self.docker.run,
            inspect=self.docker.inspect,
            fetch=self.fetch,
            sleep=lambda seconds: None,
            monotonic=self.monotonic,
            now=self.now,
            is_root=lambda: self.root_user,
            free_bytes=lambda path: self.free,
            confirm=lambda question: self.confirm,
            apply=self.apply,
            backup=self.fake_backup,
            restore=self.fake_restore,
            reexec=self.reexec,
            reachable=lambda host: True,
            journal=self.journal.append,
            docker_root=None,
        )
        self._ticks = 0.0

    # -- fakes --
    def now(self) -> datetime:
        self.clock += timedelta(minutes=1)
        return self.clock

    def monotonic(self) -> float:
        self._ticks += 1.0
        return self._ticks

    def fetch(self, url: str):
        body = {"warning": None, "status": "ok"}
        if self.operator_reports:
            body["version"] = self.operator_reports
        return self.fetch_code, json.dumps(body), None

    def fake_backup(self, config_file, bundle, destination, run=None, out=print, **kw):
        if not self.backup_ok:
            return None
        folder = Path(destination) / "ginnungagap-backup-x"
        folder.mkdir(parents=True)
        (folder / "data.tgz").write_text("data")
        self.backups.append(folder)
        return folder

    def fake_restore(self, folder, bundle, run=None, out=print, **kw):
        self.restores.append(str(folder))
        return 0

    def reexec(self, transaction_id: str, config_file: str | None) -> int:
        """What a new process would do: run `update --continue-tx` with the same environment."""
        self.reexecs.append((transaction_id, config_file))
        options = up.Options(action="continue", continue_id=transaction_id)
        return up.run_update(options, config_file, self.env, self.layout, self.bundle)

    # -- the old installation: what `install.sh --repos-dir /srv/repos` left --
    # `layout`: "old" is an appliance image up to v1.2.x (the folder names of before the code names), "new" the current names,
    # "mixed" has the generator under its old name only
    def make_old_installation(
        self, operator: str = "1.2.1", layout: str = "old"
    ) -> None:
        names = {
            "operator": "Igdrasyl",
            "builder": "Mjonyr",
            "template-services": "Hugyn-and-Munyn",
            "generator": "Mimyr",
        }
        if layout in ("old", "mixed"):
            old = fake_release.OLD_REPOS | {"generator": "topology-generator"}
            names = old if layout == "old" else names | {"generator": old["generator"]}
        for repo in (*names.values(), "Ginnungagap-Appliance"):
            (self.repos / repo).mkdir(parents=True, exist_ok=True)
            (self.repos / repo / "README.md").write_text(f"old {repo}\n")
        (self.repos / names["operator"] / "app").mkdir()
        (self.repos / names["operator"] / "app" / "version.py").write_text(
            f'__version__ = "{operator}"\n'
        )
        (self.repos / "Ginnungagap-Appliance" / "install.sh").write_text(
            '#!/bin/sh\necho "old install.sh $*" >> "$GINNUNGAGAP_FAKE_INSTALL_LOG"\n'
        )
        (self.repos / names["generator"] / "config").mkdir()
        (self.repos / "Ginnungagap-Appliance" / ".git").mkdir()

    def release(self, name: str, **kwargs) -> Path:
        return fake_release.make_release(self.tmp / "releases" / name, **kwargs)

    # -- running the command --
    def update(
        self, action: str = "apply", offline: Path | None = None, **flags
    ) -> int:
        flags.setdefault("yes", True)
        options = up.Options(
            action=action,
            offline_dir=str(offline) if offline else None,
            **flags,
        )
        return up.run_update(
            options, self.config_file, self.env, self.layout, self.bundle
        )

    # -- looking at the result --
    def installed(self) -> dict:
        return json.loads(self.layout.installed.read_text())

    def link_target(self, repo: str) -> str:
        return os.readlink(self.layout.components / repo)

    def text(self) -> str:
        return "\n".join(self.out + self.err)

    def audit(self) -> list[dict]:
        if not self.layout.audit_log.exists():
            return []
        return [json.loads(x) for x in self.layout.audit_log.read_text().splitlines()]
