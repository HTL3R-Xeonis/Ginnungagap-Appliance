"""Deterministic RemotePort fake. Unknown commands fail instead of silently succeeding."""

from __future__ import annotations

import json
from pathlib import Path

from image.build.ports import CommandFailed, CommandResult, KeyPair


class FakeRemote:
    def __init__(self):
        self.calls = []
        self.files = {}
        self.responses = []
        self.faults = {}
        self.prepared = CommandResult(
            0, "Ready. Shut this machine down now\nIMGBUILD_PREPARE_VERIFIED\n"
        )

    def inject(self, method, exc, *, times=1):
        self.faults[method] = [exc, times]

    def _call(self, method, **facts):
        self.calls.append({"method": method, **facts})
        fault = self.faults.get(method)
        if fault and fault[1]:
            fault[1] -= 1
            raise fault[0]

    def respond(self, prefix, result=None, *, times=None):
        self.responses.append(
            [tuple(prefix), result if result is not None else CommandResult(0), times]
        )

    def generate_key(self, directory: Path, comment: str) -> KeyPair:
        self._call("generate_key", directory=str(directory), comment=comment)
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        key = directory / "id_ed25519"
        key.write_text("fake-private-key-material")
        key.chmod(0o600)
        public = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeImageBuildTestKey " + comment
        key.with_suffix(".pub").write_text(public)
        return KeyPair(key, public)

    def wait_ssh(self, target, timeout):
        self._call("wait_ssh", host=target.host, timeout=timeout)

    def run(self, target, argv, *, timeout=300.0, check=True, stdin=None, env=None):
        self._call(
            "run",
            host=target.host,
            argv=list(argv),
            timeout=timeout,
            stdin_present=stdin is not None,
            env_keys=sorted(env or {}),
        )
        for rule in self.responses:
            prefix, result, count = rule
            if tuple(argv[: len(prefix)]) == prefix and count != 0:
                if count is not None:
                    rule[2] -= 1
                value = result(target, argv, stdin, env) if callable(result) else result
                if check and not value.ok:
                    raise CommandFailed(argv, value.exit_code)
                return value
        if argv[:2] == ["test", "-f"]:
            return CommandResult(0)
        if argv[:2] == ["mkdir", "-p"]:
            return CommandResult(0)
        if argv[:2] == ["python3", "-c"] and "p.stat().st_size" in argv[2]:
            prefix = argv[3].rstrip("/") + "/"
            values = {
                name[len(prefix) :]: len(data)
                for (host, name), data in self.files.items()
                if host == target.host and name.startswith(prefix)
            }
            return CommandResult(0, json.dumps(values))
        if check:
            raise CommandFailed(argv, 127)
        return CommandResult(127)

    def run_detached(
        self,
        target,
        unit,
        argv,
        *,
        log_path,
        timeout,
        success_marker=None,
        poll_seconds=15.0,
    ):
        self._call(
            "run_detached",
            host=target.host,
            unit=unit,
            argv=list(argv),
            log_path=log_path,
            timeout=timeout,
        )
        return self.prepared

    def put(self, target, local, remote, *, recursive=False):
        self._call(
            "put",
            host=target.host,
            local=str(local),
            remote=remote,
            recursive=recursive,
        )
        local = Path(local)
        if recursive:
            for p in local.rglob("*"):
                if p.is_file():
                    self.files[
                        target.host,
                        remote.rstrip("/") + "/" + p.relative_to(local).as_posix(),
                    ] = p.read_bytes()
        else:
            self.files[target.host, remote] = local.read_bytes()

    def get(self, target, remote, local, *, recursive=False):
        self._call(
            "get",
            host=target.host,
            remote=remote,
            local=str(local),
            recursive=recursive,
        )
        local = Path(local)
        if recursive:
            local.mkdir(parents=True, exist_ok=True)
            prefix = remote.rstrip("/") + "/"
            for (host, name), data in self.files.items():
                if host == target.host and name.startswith(prefix):
                    path = local / name[len(prefix) :]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
        else:
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(self.files[target.host, remote])

    def close_master(self, target):
        self._call("close_master", host=target.host)

    def forget(self, target):
        self._call("forget", host=target.host)
        for p in (
            target.key_file,
            target.key_file.with_suffix(".pub"),
            target.known_hosts,
        ):
            p.unlink(missing_ok=True)
