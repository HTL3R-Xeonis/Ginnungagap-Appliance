"""Per-run OpenSSH transport and guest bootstrap/upload phases."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import shlex
import stat
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from . import pipeline
from .ports import (
    CommandFailed,
    CommandResult,
    KeyPair,
    PermanentError,
    RemoteTarget,
    SystemClock,
    Timeout,
    Unreachable,
)

_BOOTSTRAP = """import base64,json,os,subprocess,sys
p=json.load(sys.stdin)
r=subprocess.run(p['argv'],input=base64.b64decode(p['stdin']) if p['stdin'] is not None else None,env={**os.environ,**p['env']},capture_output=True)
sys.stdout.buffer.write(r.stdout);sys.stderr.buffer.write(r.stderr);sys.exit(r.returncode)
"""

_DETACHED_WRAPPER = """import json,os,pathlib,subprocess,sys,tempfile
destination,nonce=sys.argv[1:3]
try:
    code=subprocess.run(sys.argv[3:]).returncode
except OSError:
    code=127
if code<0: code=128-code
fd,temporary=tempfile.mkstemp(prefix='.imgbuild-exit-',dir='/run')
try:
    with os.fdopen(fd,'w') as stream:
        json.dump({'nonce':nonce,'returncode':code},stream)
        stream.flush();os.fsync(stream.fileno());os.fchmod(stream.fileno(),0o644)
    os.replace(temporary,destination)
finally:
    pathlib.Path(temporary).unlink(missing_ok=True)
sys.exit(code)
"""


class SshRemote:
    """Secrets travel in stdin, never in the ssh command line.

    The persistent per-run master connection deliberately survives removal of
    authorized_keys during prepare-image. Losing it is a failure, not success.
    """

    def __init__(self, *, clock=None, runner=None):
        self.clock = clock or SystemClock()
        self.runner = runner or subprocess.run

    @staticmethod
    def _control_path(target: RemoteTarget) -> Path:
        # OpenSSH appends a random suffix before binding the Unix socket, whose
        # path has a 108-byte Linux limit. Production run/key paths exceed that.
        # Keep sockets in a short private directory; the identity includes the
        # full key path so different runs/attempts never share a master.
        directory = Path("/tmp") / f"imgbuild-ssh-{os.getuid()}"
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise PermanentError(
                "the SSH control directory is not private and owned by this user"
            )
        identity = f"{target.key_file.absolute()}:{target.user}@{target.host}:{target.port}".encode()
        return directory / ("c-" + hashlib.sha256(identity).hexdigest()[:32])

    @staticmethod
    def _options(target: RemoteTarget) -> list[str]:
        return [
            "-i",
            str(target.key_file),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"UserKnownHostsFile={target.known_hosts}",
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={SshRemote._control_path(target)}",
            "-o",
            "ControlPersist=1800",
        ]

    def _execute(self, argv, *, timeout, input=None):
        try:
            return self.runner(
                argv, input=input, capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            raise Timeout("SSH operation timed out") from None
        except OSError:
            raise Unreachable("could not start the SSH transport") from None

    def generate_key(self, directory: Path, comment: str) -> KeyPair:
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        private = directory / "id_ed25519"
        if private.exists() or private.with_suffix(".pub").exists():
            raise PermanentError("the run's SSH key already exists")
        if any(c in comment for c in "\r\n\x00"):
            raise PermanentError("invalid SSH key comment")
        result = self._execute(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                comment,
                "-f",
                str(private),
            ],
            timeout=30,
        )
        if result.returncode or not private.is_file():
            raise PermanentError("could not generate the run's SSH key")
        private.chmod(0o600)
        return KeyPair(private, private.with_suffix(".pub").read_text().strip())

    def run(
        self,
        target: RemoteTarget,
        argv: Sequence[str],
        *,
        timeout: float = 300,
        check: bool = True,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        payload = json.dumps(
            {
                "argv": list(argv),
                "stdin": base64.b64encode(stdin).decode()
                if stdin is not None
                else None,
                "env": dict(env or {}),
            }
        ).encode()
        command = "python3 -c " + shlex.quote(_BOOTSTRAP)
        result = self._execute(
            [
                "ssh",
                *self._options(target),
                "-p",
                str(target.port),
                f"{target.user}@{target.host}",
                command,
            ],
            timeout=timeout,
            input=payload,
        )
        stdout = (
            result.stdout.decode(errors="replace")
            if isinstance(result.stdout, bytes)
            else result.stdout or ""
        )
        stderr = (
            result.stderr.decode(errors="replace")
            if isinstance(result.stderr, bytes)
            else result.stderr or ""
        )
        if result.returncode == 255:
            # ssh diagnostics may contain arbitrary server text. Do not echo it.
            raise Unreachable("the SSH session was lost or refused")
        if check and result.returncode:
            # Command argv contains only public arguments. Output is returned to
            # the phase on success, but never inserted into an exception.
            raise CommandFailed(list(argv), result.returncode)
        return CommandResult(result.returncode, stdout, stderr)

    def wait_ssh(self, target: RemoteTarget, timeout: float) -> None:
        deadline = self.clock.monotonic() + timeout
        while True:
            try:
                self.run(
                    target,
                    ["true"],
                    timeout=min(15, max(1, deadline - self.clock.monotonic())),
                )
                return
            except (Unreachable, Timeout):
                if self.clock.monotonic() >= deadline:
                    raise Timeout(
                        "the new VM did not accept its run key in time"
                    ) from None
                self.clock.sleep(min(2, max(0, deadline - self.clock.monotonic())))

    def run_detached(
        self,
        target: RemoteTarget,
        unit: str,
        argv: Sequence[str],
        *,
        log_path: str,
        timeout: float,
        success_marker: str | None = None,
        poll_seconds: float = 15,
    ) -> CommandResult:
        if re.fullmatch(r"img[a-z0-9-]{1,100}", unit) is None:
            raise PermanentError("transient image-build unit names must start with img")
        if not log_path.startswith("/run/") or ".." in Path(log_path).parts:
            raise PermanentError("the preparation log must be a fresh file below /run")
        # Successful transient services can be garbage-collected before the next
        # poll. A root wrapper records the real exit code atomically outside the
        # unit; the fresh nonce prevents stale completion files proving success.
        completion = f"/run/{unit}.exit.json"
        nonce = uuid.uuid4().hex
        self.run(target, ["sudo", "-n", "rm", "-f", "--", log_path, completion])
        self.run(
            target,
            [
                "sudo",
                "-n",
                "systemd-run",
                "--collect",
                "--unit",
                unit,
                "--property=Type=exec",
                "--property=UMask=0022",
                f"--property=StandardOutput=append:{log_path}",
                f"--property=StandardError=append:{log_path}",
                "--",
                "python3",
                "-c",
                _DETACHED_WRAPPER,
                completion,
                nonce,
                *argv,
            ],
        )
        probe = """import json,pathlib,stat,subprocess,sys
r=subprocess.run(['systemctl','show',sys.argv[1],'--property=ActiveState,Result,ExecMainStatus'],capture_output=True,text=True)
fields=dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
p=subprocess.run(['tail','-c','12000','--',sys.argv[2]],capture_output=True,text=True)
completion=None
path=pathlib.Path(sys.argv[3])
try: info=path.lstat()
except FileNotFoundError: pass
else:
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o022 or info.st_size>4096:
        raise ValueError('unsafe preparation completion metadata')
    completion=json.loads(path.read_text())
print(json.dumps({'fields':fields,'log':p.stdout,'status':r.returncode,'completion':completion}))
"""
        deadline = self.clock.monotonic() + timeout
        while True:
            try:
                result = self.run(
                    target,
                    ["python3", "-c", probe, unit, log_path, completion],
                    timeout=30,
                )
                data = json.loads(result.stdout)
                fields = data["fields"]
                finished = data.get("completion")
                if finished is not None:
                    if (
                        not isinstance(finished, dict)
                        or finished.get("nonce") != nonce
                        or type(finished.get("returncode")) is not int
                    ):
                        raise PermanentError("invalid preparation completion metadata")
                    if finished["returncode"] != 0:
                        raise CommandFailed(argv, finished["returncode"])
                    if success_marker and success_marker not in data.get("log", ""):
                        raise PermanentError(
                            "the preparation unit ended without its success marker"
                        )
                    return CommandResult(0, data.get("log", ""))
                if fields.get("ActiveState") == "failed":
                    raise CommandFailed(argv, int(fields.get("ExecMainStatus") or 1))
            except (Unreachable, Timeout):
                # A temporary transport loss can recover through the master.
                # Never infer success from disappearance after key cleanup.
                pass
            except (ValueError, KeyError):
                raise PermanentError(
                    "invalid response while monitoring the preparation unit"
                ) from None
            if self.clock.monotonic() >= deadline:
                raise Timeout(
                    "the preparation unit could not be verified before its deadline"
                )
            self.clock.sleep(
                min(poll_seconds, max(0, deadline - self.clock.monotonic()))
            )

    def put(
        self, target: RemoteTarget, local: Path, remote: str, *, recursive: bool = False
    ) -> None:
        flags = ["-r"] if recursive else []
        # -O selects the classic protocol whose quoted remote path works on all
        # OpenSSH versions shipped in the Debian base images.
        result = self._execute(
            [
                "scp",
                "-O",
                *flags,
                *self._options(target),
                "-P",
                str(target.port),
                str(local),
                f"{target.user}@{target.host}:{shlex.quote(remote)}",
            ],
            timeout=600,
        )
        if result.returncode:
            raise Unreachable("upload to the build VM failed")

    def get(
        self, target: RemoteTarget, remote: str, local: Path, *, recursive: bool = False
    ) -> None:
        flags = ["-r"] if recursive else []
        result = self._execute(
            [
                "scp",
                "-O",
                *flags,
                *self._options(target),
                "-P",
                str(target.port),
                f"{target.user}@{target.host}:{shlex.quote(remote)}",
                str(local),
            ],
            timeout=600,
        )
        if result.returncode:
            raise Unreachable("download from the build VM failed")

    def forget(self, target: RemoteTarget) -> None:
        try:
            self.close_master(target)
        finally:
            for path in (
                target.known_hosts,
                target.key_file,
                target.key_file.with_suffix(".pub"),
            ):
                path.unlink(missing_ok=True)

    def close_master(self, target: RemoteTarget) -> None:
        try:
            self._execute(
                [
                    "ssh",
                    *self._options(target),
                    "-p",
                    str(target.port),
                    "-O",
                    "exit",
                    f"{target.user}@{target.host}",
                ],
                timeout=15,
            )
        finally:
            self._control_path(target).unlink(missing_ok=True)


def target_from_result(result: Mapping) -> RemoteTarget:
    data = result.get("target", result)
    return RemoteTarget(
        str(data["host"]),
        str(data["user"]),
        Path(data["key_file"]),
        Path(data["known_hosts"]),
        int(data.get("port", 22)),
    )


def config_marker(ctx) -> str:
    # A generator server name only allows letters, digits, '-' and '_'. Run ids
    # include dotted semantic versions; hash them instead of embedding them.
    digest = hashlib.sha256(ctx.run_id.encode()).hexdigest()[:24]
    return f"imgbuild-{digest}-a{ctx.attempt}"


def minimal_config(ctx) -> bytes:
    from ginnungagap.config import ConfigError, normalise

    data = yaml.safe_load((Path(__file__).with_name("minimal-config.yml")).read_text())
    data["generator"]["llm_servers"][0]["name"] = config_marker(ctx)
    if ctx.options.allow_unsigned:
        data.setdefault("update", {})["require_signature"] = False
    try:
        normalise(data)
    except ConfigError:
        raise PermanentError("the bundled bootstrap configuration is invalid") from None
    return yaml.safe_dump(data, sort_keys=False).encode()


def network_payload(network, address: str) -> dict:
    return {
        "address": address,
        "prefix": network.prefix,
        "gateway": network.gateway,
        "dns": list(network.dns),
    }


def _worker_interface(network) -> str:
    subnet = ipaddress.IPv4Network(
        f"{network.build_address}/{network.prefix}", strict=False
    )
    try:
        result = subprocess.run(
            ["ip", "-j", "address", "show"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        candidates = set()
        for interface in json.loads(result.stdout):
            for info in interface.get("addr_info", []):
                if info.get("family") != "inet":
                    continue
                address = ipaddress.IPv4Address(info["local"])
                if str(address) in (network.build_address, network.acceptance_address):
                    raise PermanentError(
                        "a reserved guest management address is assigned to the worker"
                    )
                if address in subnet and "UP" in interface.get("flags", []):
                    candidates.add(interface["ifname"])
        if len(candidates) != 1:
            raise PermanentError(
                "the worker needs one active interface on the guest management subnet"
            )
        name = candidates.pop()
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", name) is None:
            raise PermanentError(
                "the worker management interface has an unsupported name"
            )
        return name
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        raise PermanentError(
            "could not determine the worker management interface"
        ) from None


def configure_static_guest(
    ctx, vm: str, user: str, key_file: Path, known_hosts: Path, address: str
) -> RemoteTarget:
    """Bootstrap through hardware link-local IPv6, then prove fresh IPv4 SSH and assignment."""
    from ginnungagap.image_network import bootstrap_script

    network = ctx.config.image.management_network
    if network is None or address not in (
        network.build_address,
        network.acceptance_address,
    ):
        raise PermanentError(
            "the guest address was not reserved in image.management_network"
        )
    interface = _worker_interface(network)
    link_local = ctx.esxi.wait_for_link_local(vm, 180)
    try:
        if not ipaddress.IPv6Address(link_local).is_link_local:
            raise ValueError()
    except ValueError:
        raise PermanentError(
            "the VM did not report a hardware link-local IPv6 address"
        ) from None
    target = RemoteTarget(
        f"{link_local}%{interface}", user, Path(key_file), Path(known_hosts)
    )
    final = RemoteTarget(address, user, Path(key_file), Path(known_hosts))
    identity = hashlib.sha256(
        f"{ctx.run_id}:{ctx.attempt}:{vm}:{address}".encode()
    ).hexdigest()[:24]
    unit = f"imgnetwork-{identity}"
    completed = False
    try:
        ctx.remote.wait_ssh(target, 180)
        script = (
            bootstrap_script() + "\nprint('IMGBUILD_NETWORK_APPLIED', flush=True)\n"
        )
        applied = ctx.remote.run_detached(
            target,
            unit,
            ["python3", "-c", script, json.dumps(network_payload(network, address))],
            log_path=f"/run/{unit}.log",
            timeout=180,
            success_marker="IMGBUILD_NETWORK_APPLIED",
            poll_seconds=2,
        )
        if not applied.ok:
            raise CommandFailed(
                ["configure reserved management network"], applied.exit_code
            )
        ctx.remote.wait_ssh(final, 180)
        probe = "import json,subprocess; print(subprocess.check_output(['ip','-j','address','show'],text=True))"
        data = json.loads(ctx.remote.run(final, ["python3", "-c", probe]).stdout)
        assigned = any(
            info.get("family") == "inet"
            and info.get("local") == address
            and info.get("prefixlen") == network.prefix
            for nic in data
            for info in nic.get("addr_info", [])
        )
        if not assigned:
            raise PermanentError(
                "the guest did not confirm its reserved management address"
            )
        completed = True
        return final
    finally:
        for transport in [target] if completed else [target, final]:
            try:
                ctx.remote.close_master(transport)
            except (Unreachable, Timeout):
                pass


@pipeline.phase(
    "P5",
    describe=lambda ctx: (
        "set guestinfo and the run key before the first boot, wait for SSH"
    ),
)
def bootstrap_base(ctx):
    from image import set_guestinfo

    vm = ctx.result_of("P4")["vm"]
    user = getattr(ctx.config.image, "ssh_user", None) or ctx.result_of("P3").get(
        "username"
    )
    if (
        not isinstance(user, str)
        or re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]{0,31}", user) is None
    ):
        raise PermanentError(
            "set image.ssh_user: the base template does not identify its login account"
        )
    directory = ctx.run_dir / f"key-attempt-{ctx.attempt}"
    ctx.track_path(directory)
    key = ctx.remote.generate_key(directory, ctx.run_id)
    ctx.secret(key.private.read_text())
    ctx.secret(key.public)
    # Public audit material must survive removal of the run's private-key
    # directory, so a failed exported OVA can still be inspected on resume.
    evidence = ctx.run_dir / "evidence"
    if evidence.is_symlink():
        raise PermanentError("the run's public-key evidence directory is linked")
    evidence.mkdir(mode=0o700, exist_ok=True)
    evidence.chmod(0o700)
    public_key_file = evidence / f"public-attempt-{ctx.attempt}.pub"
    descriptor = os.open(public_key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(key.public.strip() + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    config_path = directory / "minimal-config.yml"
    config_path.write_bytes(minimal_config(ctx))
    config_path.chmod(0o600)
    options = set_guestinfo.guestinfo_options(
        config_path.read_bytes(), key.public, "imgbuild", True
    )
    network = ctx.config.image.management_network
    if network is not None:
        options["guestinfo.ginnungagap.network"] = json.dumps(
            network_payload(network, network.build_address)
        )
    ctx.esxi.set_extra_config(vm, options)
    ctx.esxi.power_on(vm)
    if network is not None:
        target = configure_static_guest(
            ctx, vm, user, key.private, directory / "known_hosts", network.build_address
        )
        host = target.host
    else:
        host = ctx.esxi.wait_for_ip(vm, 180)
        target = RemoteTarget(host, user, key.private, directory / "known_hosts")
    ctx.on_cleanup("path", str(directory), lambda: ctx.remote.forget(target))
    ctx.remote.wait_ssh(target, 180)
    deadline = ctx.clock.monotonic() + 180
    while not ctx.remote.run(
        target,
        ["sudo", "-n", "test", "-f", "/var/lib/ginnungagap/firstboot.done"],
        check=False,
    ).ok:
        if ctx.remote.run(
            target,
            ["sudo", "-n", "test", "-f", "/var/lib/ginnungagap/firstboot.problems"],
            check=False,
        ).ok:
            raise PermanentError("the guest rejected its first-boot configuration")
        if ctx.clock.monotonic() >= deadline:
            raise Timeout("first boot did not complete")
        ctx.clock.sleep(2)
    return {
        "vm": vm,
        "target": {
            "host": host,
            "user": user,
            "key_file": str(key.private),
            "known_hosts": str(target.known_hosts),
            "port": target.port,
        },
        "key_file": str(key.private),
        "public_key_file": str(public_key_file),
        "config_path": str(config_path),
        "config_marker": config_marker(ctx),
        "remote_release_dir": f"/home/{user}/rel",
    }


@pipeline.phase(
    "P6",
    describe=lambda ctx: "upload verified release assets and verify their remote sizes",
)
def upload_release(ctx):
    target = target_from_result(ctx.result_of("P5"))
    directory = Path(ctx.result_of("P2")["directory"])
    remote = ctx.result_of("P5")["remote_release_dir"]
    files = sorted(
        path for path in directory.iterdir() if path.is_file() and not path.is_symlink()
    )
    if not files or not (directory / "components.json").is_file():
        raise PermanentError("the verified release directory has no manifest")
    ctx.remote.run(target, ["mkdir", "-p", remote])
    for path in files:
        ctx.remote.put(target, path, f"{remote}/{path.name}")
    script = "import json,pathlib,sys; print(json.dumps({p.name:p.stat().st_size for p in pathlib.Path(sys.argv[1]).iterdir() if p.is_file()}))"
    received = json.loads(
        ctx.remote.run(target, ["python3", "-c", script, remote]).stdout
    )
    expected = {path.name: path.stat().st_size for path in files}
    if received != expected:
        raise PermanentError(
            "release asset sizes on the build VM do not match the verified files"
        )
    return {"release_dir": remote, "files": len(files)}


def register_phases(registry):
    for pid in ("P5", "P6"):
        registered = pipeline.DEFAULT_REGISTRY.get(pid)
        registry.register(
            pid, registered.fn, describe=registered.describe, replace=True
        )
