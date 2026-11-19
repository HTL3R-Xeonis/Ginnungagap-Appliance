"""
`ginnungagap update`: look for a new release of the appliance, install it, go back.

A release is a set of source tarballs plus a manifest (`components.json`, see `ginnungagap.manifest`), offered by GitHub
Releases (anonymously; a token is optional) or by a folder (`--offline-dir`, no network at all). An update downloads only
the components whose version changed, verifies checksums (and the signature when a key is trusted), unpacks them next to the
old ones, rebuilds only the services that use them, restarts them one after the other with a health check each, and rolls
back by itself when something is wrong. Everything it does is written down first in `transaction.json`, so a power loss in
the middle is cleaned up by the next boot (`update --resume`).

Every call to Docker goes through the same `Runner`/`Inspect` functions as `operate.py`, and everything that touches the
outside (network, clock, disk space, the signature tool, the questions) is a field of `Env`, so the whole thing runs in
tests without Docker. Standard library only (plus the PyYAML the rest of the program already needs).
"""

from __future__ import annotations

import contextlib
import json
import os
import posixpath
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ginnungagap import config as config_module
from ginnungagap import manifest as mf
from ginnungagap import operate, statelock
from ginnungagap import releases as rl
from ginnungagap import status as status_module
from ginnungagap.version import __version__ as PROGRAM_VERSION

EXIT_OK = 0
EXIT_AVAILABLE = 10
EXIT_ERROR = 1
EXIT_ROLLED_BACK = 2
EXIT_ROLLBACK_FAILED = 3
EXIT_REFUSED = 4

# the order services are restarted in: stateless first, the user-facing app last (its sessions are the only live ones)
RESTART_ORDER: tuple[tuple[str, ...], ...] = (
    ("generator",),
    ("collector-esxi", "collector-gns3"),
    ("template-esxi", "template-gns3"),
    ("setup",),
    ("ginnungagap",),
)
# a restarted service without a health check counts as up only when it stayed up this long without restarting again
STABLE_SECONDS = 6.0
MAX_TARBALL_BYTES = 500 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
IMAGE_HEADROOM_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 200_000
SIGNATURE_NAMESPACE = "ginnungagap-release"
NETWORK_HOSTS = ("pypi.org", "registry-1.docker.io")
ACTIONS = (
    "check",
    "apply",
    "rollback",
    "list",
    "versions",
    "history",
    "adopt",
    "set-token",
    "resume",
    "continue",
)
IGNORED_IN_ADOPTED_COPIES = shutil.ignore_patterns(
    ".git",
    "__pycache__",
    "*.pyc",
    ".venv",
    "venv",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
)


class UpdateError(Exception):
    """Stops the command with a plain message and an exit code (see the EXIT_ constants)."""

    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


class StepFailed(Exception):
    """A step of an update that is already under way failed: the update is rolled back."""


# ---- the places and the settings --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """Where everything lives (section 5.3 of the plan). `update_dir` holds the state, `root` its parent."""

    update_dir: Path
    root: Path
    config_dir: Path

    @classmethod
    def from_env(cls) -> Layout:
        update_dir = statelock.update_dir()
        return cls(
            update_dir=update_dir,
            root=update_dir.parent,
            config_dir=Path(
                os.environ.get("GINNUNGAGAP_CONFIG_DIR", "/etc/ginnungagap")
            ),
        )

    @classmethod
    def under(cls, root: Path, config_dir: Path) -> Layout:
        return cls(update_dir=root / "update", root=root, config_dir=config_dir)

    installed = property(lambda self: self.update_dir / "installed.json")
    available = property(lambda self: self.update_dir / "available.json")
    etag_cache = property(lambda self: self.update_dir / "etag-cache.json")
    history = property(lambda self: self.update_dir / "history")
    transaction = property(lambda self: self.update_dir / "transaction.json")
    audit_log = property(lambda self: self.update_dir / "audit.log")
    lock = property(lambda self: self.update_dir / statelock.LOCK_NAME)
    downloads = property(lambda self: self.update_dir / "downloads")
    releases = property(lambda self: self.root / "releases")
    components = property(lambda self: self.root / "components")
    backups = property(lambda self: self.root / "backups")
    trust_file = property(
        lambda self: self.config_dir / "update-trust" / "allowed_signers"
    )
    install_env = property(lambda self: self.config_dir / "install.env")

    def release_dir(self, component: str, folder: str) -> Path:
        return self.releases / mf.CHECKOUT_NAMES[component] / folder

    def link(self, component: str) -> Path:
        return self.components / mf.CHECKOUT_NAMES[component]


@dataclass(frozen=True)
class Settings:
    """The `update` section of ginnungagap.yml (section 10.1 of the plan)."""

    source: str = "github"
    repo: str = "HTL3R-Xeonis/Ginnungagap-Appliance"
    directory: str = ""
    channel: str = "stable"
    token_file: str = "/etc/ginnungagap/update-token"
    require_signature: str | bool = "auto"
    keep_releases: int = 2
    step_timeout_seconds: int = 90

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> Settings:
        given = (config or {}).get("update") or {}
        known = {k: v for k, v in given.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Options:
    """What was asked on the command line."""

    action: str = "check"
    offline_dir: str | None = None
    version: str | None = None
    channel: str | None = None
    components: list[str] = field(default_factory=list)
    yes: bool = False
    dry_run: bool = False
    no_backup: bool = False
    force: bool = False
    allow_downgrade: bool = False
    no_start: bool = False
    json: bool = False
    wait: float = 120.0
    restore_data: bool = False
    continue_id: str | None = None


# ---- the outside world ----------------------------------------------------------------------------------------------


def _default_confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _default_free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _default_reachable(host: str) -> bool:
    try:
        with socket.create_connection((host, 443), timeout=5):
            return True
    except OSError:
        return False


def _default_journal(text: str) -> None:
    try:
        import syslog

        syslog.openlog("ginnungagap-update")
        syslog.syslog(syslog.LOG_INFO, text)
    except (ImportError, OSError):
        pass


def verify_with_ssh_keygen(
    manifest_bytes: bytes, signature: Path, trust_file: Path
) -> tuple[bool, str]:
    """`ssh-keygen -Y verify`: (valid, the key's fingerprint or the reason it is not)."""
    command = [
        "ssh-keygen",
        "-Y",
        "verify",
        "-f",
        str(trust_file),
        "-I",
        SIGNATURE_NAMESPACE,
        "-n",
        SIGNATURE_NAMESPACE,
        "-s",
        str(signature),
    ]
    try:
        result = subprocess.run(
            command,
            input=manifest_bytes,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return False, "ssh-keygen is not installed (apt install openssh-client)"
    except subprocess.TimeoutExpired:
        return False, "ssh-keygen did not answer"
    text = (result.stdout + result.stderr).decode("utf-8", "replace").strip()
    if result.returncode == 0:
        found = re.search(r"(SHA256:[A-Za-z0-9+/=]+)", text)
        return True, found.group(1) if found else "key accepted"
    return False, text.splitlines()[-1] if text else "the signature does not match"


def _default_reexec(transaction_id: str, config_file: str | None) -> int:
    """Continues the update in a NEW process: the program files were just replaced, this process still runs the old ones."""
    wrapper = os.environ.get("GINNUNGAGAP_WRAPPER", "/usr/local/bin/ginnungagap")
    command = (
        [wrapper] if Path(wrapper).exists() else [sys.executable, "-m", "ginnungagap"]
    )
    extra = ["--config", config_file] if config_file else []
    return subprocess.run(
        [*command, "update", "--continue-tx", transaction_id, *extra], check=False
    ).returncode


@dataclass
class Env:
    """Everything an update needs from the outside. The defaults are the real thing; tests replace what they fake."""

    out: Callable[[str], None] = print
    err: Callable[[str], None] = lambda text: print(text, file=sys.stderr)
    run: operate.Runner | None = None
    inspect: operate.Inspect | None = None
    fetch: status_module.Fetch | None = None
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = mf.now_utc
    is_root: Callable[[], bool] = lambda: os.geteuid() == 0
    free_bytes: Callable[[Path], int] = _default_free_bytes
    confirm: Callable[[str], bool] = _default_confirm
    verify_signature: Callable[[bytes, Path, Path], tuple[bool, str]] = (
        verify_with_ssh_keygen
    )
    apply: Callable[..., int] = operate.apply
    backup: Callable[..., Path | None] = operate.backup
    restore: Callable[..., int] = operate.restore
    reexec: Callable[[str, str | None], int] = _default_reexec
    docker_root: Path | None = Path("/var/lib/docker")
    stop_builds: Callable[..., list[int]] | None = None  # None: stop_orphaned_builds
    reachable: Callable[[str], bool] = _default_reachable
    journal: Callable[[str], None] = _default_journal
    read_secret: Callable[[], str] | None = None
    opener: Any = None  # for the GitHub source; None = the real network
    api_base: str = rl.API_BASE
    requester: str | None = None


# ---- small helpers ----------------------------------------------------------------------------------------------------


def _short_date(stamp: str) -> str:
    return stamp[:10] if stamp else "?"


def _check_component_names(names: list[str]) -> list[str]:
    """The component keys for what was typed after --component: a key, an old key, a code name or a repository name."""
    unknown = [n for n in names if mf.resolve_component(n) is None]
    if unknown:
        raise UpdateError(
            f"unknown component {', '.join(unknown)}; the components are "
            f"{', '.join(mf.display_name(n) for n in mf.COMPONENT_NAMES)}"
        )
    return list(dict.fromkeys(mf.resolve_component(n) for n in names))


def read_version_py(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    found = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return found.group(1) if found else None


def safe_extract(
    tarball: Path, destination: Path, folder_name: str, limit: int
) -> Path:
    """
    Unpacks a source tarball (one top folder, `folder_name`) into `destination/<folder_name>`, refusing anything that could
    reach outside: parent or absolute paths, links that leave the folder, hard links, devices and pipes, more than `limit`
    bytes or a second top folder. Nothing is left behind when it is refused. Python's `data` filter is the second line.
    :raises UpdateError: (exit 4) the archive is hostile or damaged.
    """
    destination.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(dir=destination, prefix=".unpack-"))
    try:
        with tarfile.open(tarball, "r:gz") as tar:
            members = tar.getmembers()
            if len(members) > MAX_MEMBERS:
                raise UpdateError(f"{tarball.name} has too many files", EXIT_REFUSED)
            total = 0
            for member in members:
                name = member.name
                parts = posixpath.normpath(name).split("/")
                if name.startswith("/") or ".." in name.split("/") or ".." in parts:
                    raise UpdateError(
                        f"{tarball.name} contains the unsafe path {name!r} - refusing to unpack it",
                        EXIT_REFUSED,
                    )
                if parts[0] != folder_name:
                    raise UpdateError(
                        f"{tarball.name} must hold exactly one folder, {folder_name}; found {name!r}",
                        EXIT_REFUSED,
                    )
                if member.isdev() or member.isfifo() or member.islnk():
                    raise UpdateError(
                        f"{tarball.name} contains {name!r}, a device, pipe or hard link - refusing to unpack it",
                        EXIT_REFUSED,
                    )
                if member.issym():
                    target = posixpath.normpath(
                        posixpath.join(posixpath.dirname(name), member.linkname)
                    )
                    if (
                        member.linkname.startswith("/")
                        or target.split("/")[0] != folder_name
                        or target.startswith("..")
                    ):
                        raise UpdateError(
                            f"{tarball.name} contains the link {name!r} to {member.linkname!r}, which leaves the folder - refusing to unpack it",
                            EXIT_REFUSED,
                        )
                if member.isreg():
                    total += member.size
                    if total > limit:
                        raise UpdateError(
                            f"{tarball.name} unpacks to more than {limit // (1024 * 1024)} MB - refusing it",
                            EXIT_REFUSED,
                        )
            tar.extractall(scratch, members=members, filter="data")
    except tarfile.TarError as exc:
        shutil.rmtree(scratch, ignore_errors=True)
        raise UpdateError(
            f"{tarball.name} is not a usable tar.gz ({exc})", EXIT_REFUSED
        ) from exc
    except (OSError, UpdateError) as exc:
        shutil.rmtree(scratch, ignore_errors=True)
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(f"unpacking {tarball.name} failed ({exc})") from exc
    unpacked = scratch / folder_name
    if not unpacked.is_dir():
        shutil.rmtree(scratch, ignore_errors=True)
        raise UpdateError(f"{tarball.name} has no folder {folder_name}", EXIT_REFUSED)
    target = destination / folder_name
    if target.exists():
        shutil.rmtree(target)
    unpacked.rename(target)
    shutil.rmtree(scratch, ignore_errors=True)
    return target


def parse_sums(text: str) -> dict[str, str]:
    """`SHA256SUMS`: `<64 hex>  <name>` per line (a `*` before the name, as `sha256sum -b` writes, is accepted)."""
    sums = {}
    for line in text.splitlines():
        match = re.match(r"^([0-9a-f]{64}) [ *](\S.*)$", line.strip())
        if match:
            sums[match.group(2)] = match.group(1)
    return sums


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def rewrite_install_env(path: Path, repos_dir: Path) -> bool:
    """Points `REPOS_DIR` and `GINNUNGAGAP_GENERATOR_CONFIG` of install.env at `repos_dir`. False when there is no such file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    wanted = {
        "REPOS_DIR": str(repos_dir),
        "GINNUNGAGAP_GENERATOR_CONFIG": str(
            repos_dir / mf.CHECKOUT_NAMES["generator"] / "config"
        ),
    }
    seen = set()
    result = []
    for line in lines:
        key = line.split("=", 1)[0]
        if key in wanted and "=" in line:
            result.append(f"{key}={wanted[key]}")
            seen.add(key)
        else:
            result.append(line)
    result += [f"{key}={value}" for key, value in wanted.items() if key not in seen]
    mode = path.stat().st_mode & 0o777
    tmp = path.with_name(path.name + ".new")
    tmp.write_text("\n".join(result) + "\n", encoding="utf-8")
    tmp.chmod(mode)
    tmp.replace(path)
    return True


@dataclass(frozen=True)
class Probe:
    """What Docker says about one service's container right now."""

    container: str
    state: str  # running, restarting, exited, ...
    health: (
        str  # healthy, unhealthy, starting, or none (the service has no health check)
    )
    restarts: int


PROC = Path("/proc")


def _read_processes(proc: Path = PROC) -> dict[int, tuple[int, list[str]]]:
    """pid -> (parent pid, command line) of every process we may look at."""
    found: dict[int, tuple[int, list[str]]] = {}
    try:
        entries = list(proc.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            args = [
                a
                for a in (entry / "cmdline")
                .read_bytes()
                .decode(errors="replace")
                .split("\0")
                if a
            ]
            stat = (entry / "stat").read_text()
            fields = stat[stat.rindex(")") + 2 :].split()
            parent = int(fields[1])
        except (OSError, ValueError):
            continue
        if fields[0] in ("Z", "X"):
            continue  # already dead, only waiting to be reaped
        found[int(entry.name)] = (parent, args)
    return found


def find_orphaned_builds(
    bundle: Path, processes: dict[int, tuple[int, list[str]]] | None = None
) -> list[int]:
    """
    The `docker compose ... build` runs of this appliance that are still alive (and everything they started): after the update
    engine was killed (kill -9, an out-of-memory kill) its child keeps building and moves the `latest` tag of an image when it
    is done, which is exactly the tag a rollback wants to put the old image back on.
    """
    processes = _read_processes() if processes is None else processes
    mine = set()
    pid = os.getpid()
    while pid in processes and pid not in mine:
        mine.add(pid)
        pid = processes[pid][0]
    roots = set()
    for pid, (_, args) in processes.items():
        if pid in mine or "--project-directory" not in args:
            continue
        index = args.index("--project-directory")
        if (
            index + 1 < len(args)
            and args[index + 1] == str(bundle)
            and ("build" in args or "--build" in args)
        ):
            roots.add(pid)
    found = set(roots)
    grew = True
    while grew:
        grew = False
        for pid, (parent, _) in processes.items():
            if parent in found and pid not in found and pid not in mine:
                found.add(pid)
                grew = True
    return sorted(found)


def stop_orphaned_builds(
    bundle: Path,
    wait_seconds: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    processes: Callable[[], dict[int, tuple[int, list[str]]]] = _read_processes,
) -> list[int]:
    """Asks the leftover builds of this appliance to stop (SIGTERM), waits for them, kills what is still there. The pids found."""
    found = find_orphaned_builds(bundle, processes())
    if not found:
        return []

    def alive() -> list[int]:
        now = processes()
        return [p for p in found if p in now]

    for sig, patience in ((signal.SIGTERM, wait_seconds), (signal.SIGKILL, 10.0)):
        for pid in alive():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sig)
        deadline = monotonic() + patience
        while alive() and monotonic() < deadline:
            sleep(0.5)
        if not alive():
            break
    return found


@dataclass
class Signature:
    status: str  # valid | not verified | not checked
    detail: str
    required: bool = False

    def line(self) -> str:
        if self.status == "valid":
            return f"signature: valid (key {self.detail})"
        if self.status == "not checked":
            return f"signature: not checked ({self.detail})"
        return f"signature: NOT verified ({self.detail})"


@dataclass
class Target:
    """The release an update or a check is about: its manifest, where its files are staged, how it was signed."""

    release: rl.ReleaseInfo
    manifest: mf.Manifest
    folder: Path
    signature: Signature
    sums: dict[str, str]


# ---- the updater ----------------------------------------------------------------------------------------------------


class Updater:
    def __init__(
        self,
        layout: Layout,
        settings: Settings,
        options: Options,
        env: Env,
        config: dict[str, Any] | None = None,
        config_file: Path | None = None,
        bundle: Path | None = None,
        source: rl.ReleaseSource | None = None,
    ) -> None:
        self.layout = layout
        self.settings = settings
        self.opts = options
        self.env = env
        self.config = config
        self.config_file = config_file
        self.bundle = bundle or operate.bundle_dir()
        self._source = source
        self.out = env.out
        self.run = operate._runner(env.run, self.bundle)
        self.inspect = env.inspect or (
            lambda args: operate.default_inspect(args, cwd=self.bundle)
        )

    # -- small things --
    def say(self, text: str = "") -> None:
        self.out(text)

    @property
    def channel(self) -> str:
        return self.opts.channel or self.settings.channel

    def now(self) -> datetime:
        return self.env.now()

    def stamp(self) -> str:
        return mf.timestamp(self.now())

    def requester(self) -> str:
        if self.env.requester:
            return self.env.requester
        who = os.environ.get("GINNUNGAGAP_UPDATE_REQUESTER")
        if who:
            return who[:80]
        user = os.environ.get("SUDO_USER")
        return f"uid:{os.geteuid()}" + (f" (sudo {user})" if user else "")

    def audit(self, event: str, result: str, **fields: Any) -> None:
        """One JSON line in audit.log and one in the journal: who did what, from which version to which, how it ended."""
        record = {
            "ts": self.stamp(),
            "who": self.requester(),
            "action": event,
            "result": result,
            **{k: v for k, v in fields.items() if v is not None},
        }
        line = json.dumps(record, sort_keys=False)
        try:
            self.layout.update_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(
                self.layout.audit_log,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            self.out(f"warning: could not write the audit log ({exc.strerror})")
        self.env.journal(line)

    def require_root(self, what: str) -> None:
        if not self.env.is_root():
            raise UpdateError(
                f"{what} changes the system: run it as root (sudo ginnungagap update ...)",
                EXIT_REFUSED,
            )

    # -- state --
    def installed(self) -> mf.InstalledState | None:
        try:
            return mf.load_installed(self.layout.installed)
        except mf.ManifestError as exc:
            raise UpdateError(
                f"{self.layout.installed} is damaged ({'; '.join(exc.problems)}); "
                "describe the current files again with `ginnungagap update --adopt` after removing it",
                EXIT_ERROR,
            ) from exc

    def load_transaction(self) -> dict[str, Any] | None:
        try:
            data = mf.read_json(self.layout.transaction)
        except mf.ManifestError as exc:
            raise UpdateError(
                f"{self.layout.transaction} is damaged ({'; '.join(exc.problems)}); an update did not finish. "
                "Check the journal (journalctl -t ginnungagap-update), fix or remove the file and run `ginnungagap update --resume`"
            ) from exc
        return data if isinstance(data, dict) else None

    def save_transaction(self, tx: dict[str, Any], phase: str | None = None) -> None:
        if phase:
            tx["phase"] = phase
            tx["phase_at"] = self.stamp()
        mf.write_json_atomic(self.layout.transaction, tx, mode=0o600)

    def clear_transaction(self) -> None:
        self.layout.transaction.unlink(missing_ok=True)

    def project(self) -> str:
        return operate._project(self.bundle)

    def active_services(self) -> set[str] | None:
        """The services this machine runs (COMPOSE_PROFILES of the rendered environment); None when nothing is rendered."""
        try:
            text = (self.bundle / "rendered" / "compose.env").read_text(
                encoding="utf-8"
            )
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("COMPOSE_PROFILES="):
                return {p for p in line.split("=", 1)[1].strip().split(",") if p}
        return None

    # -- token and source --
    def make_source(self) -> rl.ReleaseSource:
        if self._source is not None:
            return self._source
        directory = self.opts.offline_dir or (
            self.settings.directory if self.settings.source == "directory" else None
        )
        if directory:
            return rl.DirectorySource(directory)
        return rl.GitHubSource(
            self.settings.repo,
            token_file=self.settings.token_file or None,
            opener=self.env.opener,
            api_base=self.env.api_base,
            cache_path=self.layout.etag_cache,
        )

    def token_state(self) -> str:
        path = self.settings.token_file
        if not path:
            return "none"
        try:
            return "set" if rl.read_token(path) else "none (anonymous)"
        except rl.SourceError as exc:
            return f"unusable: {exc.message}"

    # -- the release to look at --
    def pick(self, source: rl.ReleaseSource) -> rl.ReleaseInfo:
        """The release asked for with --version, else the newest of the channel."""
        releases = source.list_releases(self.channel)
        wanted = self.opts.version
        if wanted:
            for release in releases:
                if release.version == wanted:
                    return release
            hint = ""
            if self.channel == "stable" and mf.is_prerelease(wanted):
                hint = " (it is a pre-release: add --channel prerelease)"
            elif releases:
                hint = f"; it offers {', '.join(r.version for r in releases)}"
            raise UpdateError(
                f"{source.describe()} has no release {wanted} on the {self.channel} channel{hint}"
            )
        if releases:
            return releases[0]
        hint = ""
        if self.channel == "stable":
            with contextlib.suppress(rl.SourceError):
                if source.list_releases("prerelease"):
                    hint = " (it has pre-releases: add --channel prerelease)"
        raise UpdateError(
            f"{source.describe()} offers no release on the {self.channel} channel{hint}"
        )

    def signature_policy(self) -> bool | None:
        """True: a valid signature is required. False: not checked. None: only when a key is trusted (`auto`)."""
        wanted = self.settings.require_signature
        if wanted is True or wanted == "true":
            return True
        if wanted is False or wanted == "false":
            return False
        return None

    def trust_present(self) -> bool:
        try:
            lines = self.layout.trust_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        return any(line.strip() and not line.strip().startswith("#") for line in lines)

    def fetch_target(
        self, source: rl.ReleaseSource, release: rl.ReleaseInfo, folder: Path
    ) -> Target:
        """Downloads `components.json`, its signature and SHA256SUMS into `folder`, validates and verifies them."""
        manifest_path = source.fetch(
            release, "components.json", folder, 4 * 1024 * 1024
        )
        sums_path = source.fetch(release, "SHA256SUMS", folder, 4 * 1024 * 1024)
        manifest_bytes = manifest_path.read_bytes()
        try:
            manifest = mf.loads(manifest_bytes)
        except mf.ManifestError as exc:
            raise UpdateError(
                f"the manifest of {release.tag} is not valid: "
                + "; ".join(exc.problems)
            ) from exc
        if manifest.version != release.version:
            raise UpdateError(
                f"the manifest in {release.tag} says it is release {manifest.version} - refusing it",
                EXIT_REFUSED,
            )
        sums = parse_sums(sums_path.read_text(encoding="utf-8", errors="replace"))
        listed = sums.get("components.json")
        if listed is not None and listed != _sha256(manifest_path):
            raise UpdateError(
                "components.json does not match its checksum in SHA256SUMS - refusing the release",
                EXIT_REFUSED,
            )
        signature = self.check_signature(source, release, folder, manifest_bytes)
        return Target(release, manifest, folder, signature, sums)

    def check_signature(
        self,
        source: rl.ReleaseSource,
        release: rl.ReleaseInfo,
        folder: Path,
        manifest_bytes: bytes,
    ) -> Signature:
        policy = self.signature_policy()
        if policy is False:
            return Signature("not checked", "update.require_signature is false")
        trusted = self.trust_present()
        required = policy is True or trusted
        sig_path: Path | None = None
        try:
            sig_path = source.fetch(release, "components.json.sig", folder, 64 * 1024)
        except rl.SourceError as exc:
            if exc.kind != "not_found":
                raise
        if not required:
            note = (
                "this release is signed, but no key is trusted on this machine"
                if sig_path
                else "no key is trusted on this machine and the release is not signed"
            )
            return Signature("not verified", note)
        if not trusted:
            raise UpdateError(
                f"a signed release is required (update.require_signature) but no key is trusted: {self.layout.trust_file} is missing or has no key",
                EXIT_REFUSED,
            )
        if sig_path is None:
            raise UpdateError(
                "this release has no signature (components.json.sig) but a key is trusted here - refusing it",
                EXIT_REFUSED,
            )
        valid, detail = self.env.verify_signature(
            manifest_bytes, sig_path, self.layout.trust_file
        )
        if not valid:
            raise UpdateError(
                f"the signature of components.json is not valid ({detail}) - refusing the release",
                EXIT_REFUSED,
            )
        return Signature("valid", detail, required=True)

    # -- what would change --
    def selection(self) -> list[str] | None:
        names = _check_component_names(self.opts.components)
        return names or None

    def plan_changes(
        self, installed: mf.InstalledState | None, manifest: mf.Manifest
    ) -> list[mf.Change]:
        return mf.diff(installed, manifest, self.selection())

    def check_allowed(
        self, installed: mf.InstalledState | None, manifest: mf.Manifest
    ) -> None:
        """Refuses a release the installed one may not go to (older, or too old to jump straight)."""
        if installed is None:
            return
        have = installed.appliance_version
        if mf.is_semver(have):
            if manifest.min_installed and mf.semver_key(have) < mf.semver_key(
                manifest.min_installed
            ):
                raise UpdateError(
                    f"release {manifest.version} can only be installed from {manifest.min_installed} or newer, and this machine has {have}: "
                    f"update to a release between them first (for example `--version {manifest.min_installed}`)",
                    EXIT_REFUSED,
                )
            if (
                mf.semver_key(manifest.version) < mf.semver_key(have)
                and not self.opts.allow_downgrade
            ):
                raise UpdateError(
                    f"release {manifest.version} is older than the installed {have}; add --allow-downgrade if that is meant",
                    EXIT_REFUSED,
                )

    def render_table(
        self,
        installed: mf.InstalledState | None,
        target: Target,
        changes: list[mf.Change],
    ) -> str:
        manifest = target.manifest
        if installed is None:
            have = "not recorded (no `ginnungagap update` has run on this machine yet)"
        elif installed.adopted:
            have = f"adopted checkouts ({_short_date(installed.installed_at)})"
        else:
            have = f"appliance v{installed.appliance_version} ({_short_date(installed.installed_at)})"
        lines = [
            f"Installed: {have}        Available: v{manifest.version} ({manifest.channel}, {_short_date(manifest.released)})   source: {self.make_source().describe()}"
        ]
        rows = [("component", "installed", "available", "change", "restarts")]
        for change in changes:
            if change.kind in ("same", "skipped"):
                rows.append(
                    (
                        mf.display_name(change.component),
                        change.from_version or "-",
                        change.to_version,
                        "-" if change.kind == "same" else "not selected",
                        "-",
                    )
                )
                continue
            restarts = ", ".join(change.services)
            if "ginnungagap" in change.services:
                restarts += " (about 10-20 s of downtime)"
            rows.append(
                (
                    mf.display_name(change.component),
                    change.from_version or "-",
                    change.to_version,
                    {"replace": "replace"}.get(change.kind, change.kind),
                    restarts,
                )
            )
        widths = [max(len(row[i]) for row in rows) for i in range(4)]
        for row in rows:
            lines.append(
                "  ".join(row[i].ljust(widths[i]) for i in range(4)) + "  " + row[4]
            )
        lines.append(
            f"{target.signature.line()}   data migration: {'yes' if manifest.data_migration else 'no'}   "
            f"pre-update backup: {'no' if self.opts.no_backup else 'yes'}"
        )
        return "\n".join(lines)

    # ---- check, list, versions, history ----
    def check(self) -> int:
        source = self.make_source()
        installed = self.installed()
        release = self.pick(source)
        with tempfile.TemporaryDirectory(prefix="ginnungagap-check-") as scratch:
            target = self.fetch_target(source, release, Path(scratch))
        self.check_allowed_for_check(installed, target.manifest)
        changes = self.plan_changes(installed, target.manifest)
        available = any(c.changed and c.kind != "downgrade" for c in changes)
        self.say(self.render_table(installed, target, changes))
        self.write_available(installed, target, changes, available)
        if self.opts.json:
            self.emit(
                {
                    "ok": True,
                    "update_available": available,
                    "installed": installed.appliance_version if installed else None,
                    "available": target.manifest.version,
                    "channel": target.manifest.channel,
                    "signature": target.signature.status,
                    "data_migration": target.manifest.data_migration,
                    "changes": [_change_json(c) for c in changes],
                }
            )
        if not available:
            self.say(
                "Nothing to update: everything is at the version of this release or newer."
            )
            return EXIT_OK
        return EXIT_AVAILABLE

    def check_allowed_for_check(
        self, installed: mf.InstalledState | None, manifest: mf.Manifest
    ) -> None:
        """A check reports a release that could not be applied as it is (too old a jump) but does not refuse the look."""
        try:
            self.check_allowed(installed, manifest)
        except UpdateError as exc:
            if "--allow-downgrade" in exc.message:
                return  # an older release is simply "nothing to update"
            self.say(f"note: {exc.message}")

    def write_available(
        self,
        installed: mf.InstalledState | None,
        target: Target,
        changes: list[mf.Change],
        available: bool,
    ) -> None:
        record = {
            "checked_at": self.stamp(),
            "source": self.make_source().describe(),
            "channel": self.channel,
            "update_available": available,
            "installed": installed.appliance_version if installed else None,
            "available": {
                "version": target.manifest.version,
                "tag": target.manifest.tag,
                "channel": target.manifest.channel,
                "released": target.manifest.released,
                "notes": target.manifest.notes,
                "data_migration": target.manifest.data_migration,
            },
            "signature": target.signature.status,
            "changes": [_change_json(c) for c in changes],
            "error": None,
        }
        self._write_available(record)

    def _write_available(self, record: dict[str, Any]) -> None:
        try:
            mf.write_json_atomic(self.layout.available, record)
        except OSError as exc:
            self.say(
                f"(could not record the result in {self.layout.available}: {exc.strerror})"
            )

    def record_check_failure(self, message: str) -> None:
        self._write_available(
            {
                "checked_at": self.stamp(),
                "source": self.make_source().describe(),
                "channel": self.channel,
                "update_available": False,
                "error": message,
            }
        )

    def list_releases(self) -> int:
        source = self.make_source()
        releases = source.list_releases(self.channel)
        installed = self.installed()
        have = installed.appliance_version if installed else None
        if self.opts.json:
            self.emit(
                {
                    "ok": True,
                    "source": source.describe(),
                    "releases": [
                        {
                            "version": r.version,
                            "tag": r.tag,
                            "prerelease": r.prerelease,
                            "date": r.date,
                            "installed": r.version == have,
                        }
                        for r in releases
                    ],
                }
            )
            return EXIT_OK
        self.say(f"Releases on the {self.channel} channel, source: {source.describe()}")
        if not releases:
            self.say("  (none)")
        for release in releases:
            marker = "installed" if release.version == have else ""
            kind = "pre-release" if release.prerelease else "stable"
            self.say(
                f"  {release.version.ljust(14)} {kind.ljust(12)} {_short_date(release.date)}  {marker}".rstrip()
            )
        return EXIT_OK

    def versions(self) -> int:
        installed = self.installed()
        transaction = self.load_transaction()
        if self.opts.json:
            self.emit(
                {
                    "ok": True,
                    "program": PROGRAM_VERSION,
                    "installed": installed.to_dict() if installed else None,
                    "update_in_progress": bool(transaction),
                    "token": self.token_state(),
                }
            )
            return EXIT_OK
        if installed is None:
            self.say(
                f"Appliance release: not recorded (program version {PROGRAM_VERSION}). Run `ginnungagap update --adopt` to record the components installed now."
            )
        else:
            label = (
                "adopted checkouts"
                if installed.adopted
                else f"v{installed.appliance_version}"
            )
            self.say(
                f"Appliance release: {label} (installed {installed.installed_at or 'at an unknown time'}); program {PROGRAM_VERSION}"
            )
            width = max(len(mf.display_name(n)) for n in mf.COMPONENT_NAMES)
            for name in mf.COMPONENT_NAMES:
                label = mf.display_name(name)
                version = installed.version_of(name) or "-"
                where = installed.dirs.get(name, "")
                self.say(
                    f"  {label.ljust(width)}  {version.ljust(10)} {where}".rstrip()
                )
            if installed.partial:
                self.say(
                    "  (a partial update: not the tested combination of one release)"
                )
        if transaction:
            self.say(
                f"An update did not finish (phase {transaction.get('phase')}): run `ginnungagap update --resume`."
            )
        self.say(
            f"Update source: {self.make_source().describe()}; token: {self.token_state()}"
        )
        return EXIT_OK

    def history(self) -> int:
        entries = mf.list_history(self.layout.history)
        installed = self.installed()
        current = installed.entry if installed else None
        if self.opts.json:
            self.emit(
                {
                    "ok": True,
                    "history": [
                        {
                            "file": e.path.name,
                            "at": e.stamp,
                            "version": e.version,
                            "current": e.path.name == current,
                        }
                        for e in entries
                    ],
                }
            )
            return EXIT_OK
        if not entries:
            self.say("No release was recorded on this machine yet.")
        for entry in entries:
            self.say(
                f"  {entry.stamp}  {entry.version}{'  (current)' if entry.path.name == current else ''}"
            )
        return EXIT_OK

    def emit(self, data: dict[str, Any]) -> None:
        print(json.dumps(data, indent=2))

    # ---- the token ----
    def set_token(self) -> int:
        self.require_root("storing the token")
        path = self.settings.token_file
        if not path:
            raise UpdateError(
                "update.token_file is empty in the configuration, so there is no place for a token"
            )
        if self.env.read_secret is not None:
            raw = self.env.read_secret()
        elif sys.stdin.isatty():
            import getpass

            raw = getpass.getpass("GitHub token: ")
        else:
            raw = sys.stdin.read(4096)
        try:
            token = rl.check_token_text(raw)
        except ValueError as exc:
            raise UpdateError(f"{exc}; nothing was stored") from exc
        try:
            rl.write_token(path, token)
        except OSError as exc:
            raise UpdateError(f"could not write {path} ({exc.strerror})") from exc
        self.audit("set-token", "ok", file=path)
        self.say(
            f"Token stored in {path} (readable by root only). It is only used for requests to the GitHub API; delete the file to go without it."
        )
        return EXIT_OK

    # ---- adopt ----
    def current_repos_dir(self) -> Path | None:
        env_value = os.environ.get("REPOS_DIR")
        if env_value:
            return Path(env_value)
        try:
            for line in self.layout.install_env.read_text(
                encoding="utf-8"
            ).splitlines():
                if line.startswith("REPOS_DIR="):
                    return Path(line.split("=", 1)[1].strip())
        except OSError:
            pass
        return None

    def describe_checkouts(self) -> tuple[mf.InstalledState, dict[str, Path]] | None:
        """What the checkouts in REPOS_DIR are, as an installed state with the release name `adopted`. None: nothing to adopt."""
        repos = self.current_repos_dir()
        if repos is None or not repos.is_dir():
            return None
        try:
            if repos.resolve() == self.layout.components.resolve():
                return None
        except OSError:
            return None
        sources: dict[str, Path] = {}
        for name in mf.COMPONENT_NAMES:
            # the current folder name first, then the names of before the code names (the checkouts of an appliance image
            # up to v1.2.x, or a folder that was only partly renamed): they are copied under the current names either way
            candidate = repos / mf.CHECKOUT_NAMES[name]
            for old in mf.LEGACY_NAMES[name]:
                if not candidate.is_dir():
                    candidate = repos / old
            if name == "appliance" and not candidate.is_dir():
                candidate = self.bundle
            if not candidate.is_dir():
                return None
            sources[name] = candidate
        versions = {name: "unknown" for name in mf.COMPONENT_NAMES}
        operator_version = read_version_py(sources["operator"] / "app" / "version.py")
        if operator_version:
            versions["operator"] = operator_version
        appliance_version = read_version_py(
            sources["appliance"] / "ginnungagap" / "version.py"
        )
        if appliance_version:
            versions["appliance"] = appliance_version
        state = mf.InstalledState(
            appliance_version="adopted",
            components={
                name: {"repo": mf.CHECKOUT_NAMES[name], "version": versions[name]}
                for name in mf.COMPONENT_NAMES
            },
            installed_at=self.stamp(),
            adopted=True,
        )
        return state, sources

    def adopt(self) -> int:
        self.require_root("adopting the installed components")
        try:
            with statelock.hold(self.layout.lock, timeout=0, out=self.say):
                return self._adopt_locked()
        except statelock.LockTimeout as exc:
            raise UpdateError(str(exc), EXIT_REFUSED) from exc

    def _adopt_locked(self) -> int:
        self.migrate_legacy_layout()
        if self.installed() is not None:
            self.say(
                "The installed components are already recorded (see `ginnungagap update --versions`)."
            )
            return EXIT_OK
        if self.load_transaction():
            raise UpdateError(
                "an update did not finish; run `ginnungagap update --resume` first",
                EXIT_REFUSED,
            )
        found = self.describe_checkouts()
        if found is None:
            raise UpdateError(
                "nothing to adopt: REPOS_DIR does not hold the checkouts of "
                + ", ".join(
                    mf.CHECKOUT_NAMES[n] for n in mf.COMPONENT_NAMES if n != "appliance"
                )
                + " (or of the names they had before: "
                + ", ".join(
                    old for n in mf.COMPONENT_NAMES for old in mf.LEGACY_NAMES[n]
                )
                + "), or it already is the update's own components folder"
            )
        self.adopt_now(*found)
        return EXIT_OK

    def adopt_now(
        self, state: mf.InstalledState, sources: dict[str, Path]
    ) -> mf.InstalledState:
        self.layout.releases.mkdir(parents=True, exist_ok=True)
        self.layout.components.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        try:
            for name, source in sources.items():
                repo_dir = self.layout.releases / mf.CHECKOUT_NAMES[name]
                repo_dir.mkdir(parents=True, exist_ok=True)
                number = 1
                while (repo_dir / f"adopted-{number}").exists():
                    number += 1
                folder = f"adopted-{number}"
                shutil.copytree(
                    source,
                    repo_dir / folder,
                    symlinks=True,
                    ignore=IGNORED_IN_ADOPTED_COPIES,
                )
                copied.append(repo_dir / folder)
                state.dirs[name] = folder
        except OSError as exc:
            for folder_path in copied:
                shutil.rmtree(folder_path, ignore_errors=True)
            raise UpdateError(f"copying the checkouts failed ({exc})") from exc
        for name in mf.COMPONENT_NAMES:
            self.flip_link(name, state.dirs[name])
        os.environ["REPOS_DIR"] = str(self.layout.components)
        os.environ["GINNUNGAGAP_GENERATOR_CONFIG"] = str(
            self.layout.components / mf.CHECKOUT_NAMES["generator"] / "config"
        )
        if not rewrite_install_env(self.layout.install_env, self.layout.components):
            self.say(
                f"(no {self.layout.install_env} to rewrite; REPOS_DIR stays as it is for later runs)"
            )
        services = sorted(
            {s for n in mf.COMPONENT_NAMES for s in mf.DEFAULT_SERVICES[n]}
        )
        if self.active_services() is not None:
            done, _ = operate.retag_images(
                self.project(),
                services,
                "latest",
                "rollback-adopted",
                self.run,
                self.inspect,
            )
            if done:
                self.say(
                    f"kept the current images as the rollback target: {', '.join(done)}"
                )
        self.record_state(state, previous=None)
        self.audit("adopt", "ok", to=state.appliance_version)
        self.say(
            f"Recorded the components installed now as release 'adopted' (copies in {self.layout.releases}, "
            f"symlinks in {self.layout.components}). Nothing in the old folder was changed or deleted."
        )
        return state

    def _symlink(self, link: Path, target: str) -> None:
        """Makes `link` a symlink to `target`, atomically (a temporary link renamed over the old one)."""
        temporary = link.with_name(f".{link.name}.new")
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(target)
        os.replace(temporary, link)

    def migrate_legacy_layout(self) -> bool:
        """
        Brings a state that an earlier version of this program wrote (before the code names) to the current names, once, under
        the update lock: `releases/<old>` becomes `releases/<new>`, `components/<new>` points at the same release as
        `components/<old>` did, `install.env` and `installed.json` follow. The old name stays as a link to the new one, so
        the compose file of a release from before the rename (which asks for the old folder names) still finds its sources
        after a rollback. Every step is one `os.replace` and can be run again after a crash. Returns whether it changed anything.
        """
        releases, components = self.layout.releases, self.layout.components
        changes: list[str] = []
        try:
            for key in mf.COMPONENT_NAMES:
                new = mf.CHECKOUT_NAMES[key]
                for old in mf.LEGACY_NAMES[key]:
                    old_dir = releases / old
                    if old_dir.is_dir() and not old_dir.is_symlink():
                        new_dir = releases / new
                        if not new_dir.exists():
                            os.replace(old_dir, new_dir)
                        else:  # both exist: bring over what is not there yet
                            for child in sorted(old_dir.iterdir()):
                                if not (new_dir / child.name).exists():
                                    os.replace(child, new_dir / child.name)
                            with contextlib.suppress(OSError):
                                old_dir.rmdir()
                        changes.append(f"releases/{old} -> releases/{new}")
                    old_link, new_link = components / old, components / new
                    if old_link.is_symlink() and os.readlink(old_link) != new:
                        folder = posixpath.basename(os.readlink(old_link))
                        if not new_link.is_symlink() and not new_link.exists():
                            self._symlink(new_link, f"../releases/{new}/{folder}")
                        self._symlink(old_link, new)
                        changes.append(f"components/{old} -> components/{new}")
            raw = mf.read_json(self.layout.installed)
            if mf.has_legacy_names(raw):
                state = self.installed()
                if state is not None:
                    mf.save_installed(self.layout.installed, state)
                    changes.append("installed.json")
        except (OSError, mf.ManifestError) as exc:
            raise UpdateError(
                f"bringing the update state to the current component names failed ({exc}); nothing else was changed"
            ) from exc
        if not changes:
            return False
        with contextlib.suppress(OSError):
            if self.current_repos_dir() == components:
                rewrite_install_env(self.layout.install_env, components)
        os.environ["GINNUNGAGAP_GENERATOR_CONFIG"] = str(
            components / mf.CHECKOUT_NAMES["generator"] / "config"
        )
        self.audit("rename-components", "ok", changed=", ".join(changes))
        self.say(
            "Moved the update state to the current component names ("
            + ", ".join(changes)
            + ")."
        )
        return True

    def record_state(
        self,
        state: mf.InstalledState,
        previous: mf.InstalledState | None,
        installed_at: str | None = None,
    ) -> None:
        state.installed_at = installed_at or state.installed_at or self.stamp()
        state.previous = previous.appliance_version if previous else None
        state.previous_entry = previous.entry if previous else None
        self.layout.history.mkdir(parents=True, exist_ok=True)
        mf.add_history(self.layout.history, state, self.now())
        mf.save_installed(self.layout.installed, state)

    def flip_link(self, component: str, folder: str) -> None:
        """Points components/<Repo> at releases/<Repo>/<folder>, atomically (a temporary link renamed over the old one)."""
        link = self.layout.link(component)
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() and not link.is_symlink():
            raise UpdateError(
                f"{link} is a folder, not a symlink - not touching it (adopt or move it first)"
            )
        temporary = link.with_name(f".{link.name}.new")
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(
            os.path.join("..", "releases", mf.CHECKOUT_NAMES[component], folder)
        )
        os.replace(temporary, link)

    # ---- apply ----
    def apply(self) -> int:
        if not self.opts.dry_run:
            self.require_root("an update")
        if self.opts.dry_run:
            return self._apply_dry_run()
        outcome = self.locked(self._apply_locked)
        if isinstance(outcome, str):  # the new program takes over
            return self.env.reexec(
                outcome, str(self.config_file) if self.config_file else None
            )
        return outcome

    def locked(self, function: Callable[[], Any]) -> Any:
        try:
            with statelock.hold(self.layout.lock, timeout=0, out=self.say):
                self.migrate_legacy_layout()
                return function()
        except statelock.LockTimeout as exc:
            raise UpdateError(
                f"another update or apply is running ({exc}); try again when it has finished",
                EXIT_REFUSED,
            ) from exc

    def need_config(self) -> dict[str, Any] | None:
        if self.config is not None:
            return self.config
        if self.opts.no_start:
            return None
        raise UpdateError(
            f"the configuration {self.config_file} is missing or not valid, so the appliance can't be checked or restarted; fix it first",
            EXIT_REFUSED,
        )

    def docker_must_answer(self) -> None:
        if (
            self.inspect(["docker", "version", "--format", "{{.Server.Version}}"])
            is None
        ):
            raise UpdateError(
                "Docker does not answer (is the daemon running? `systemctl status docker`); nothing was changed"
            )

    def healthy_now(self, config: dict[str, Any]) -> tuple[bool, str]:
        rows = status_module.check(
            config,
            status_module.base_url(config),
            self.env.fetch or status_module.make_fetch(),
        )
        ok = status_module.healthy(rows) or status_module.healthy_behind_login(rows)
        return ok, status_module.render_table(rows)

    def resolve(
        self,
    ) -> tuple[
        mf.InstalledState | None,
        Target,
        list[mf.Change],
        tuple[mf.InstalledState, dict[str, Path]] | None,
        Path,
    ]:
        """The release to go to, staged in downloads/<version>/, and what it would change. Changes nothing but that folder."""
        installed = self.installed()
        source = self.make_source()
        release = self.pick(source)
        folder = self.layout.downloads / release.version
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            target = self.fetch_target(source, release, folder)
            adoptable = None
            effective = installed
            if installed is None:
                adoptable = self.describe_checkouts()
                effective = adoptable[0] if adoptable else None
            self.check_allowed(effective, target.manifest)
            changes = self.plan_changes(effective, target.manifest)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return effective, target, changes, adoptable, folder

    def _apply_dry_run(self) -> int:
        installed = self.installed()
        source = self.make_source()
        release = self.pick(source)
        with tempfile.TemporaryDirectory(prefix="ginnungagap-plan-") as scratch:
            target = self.fetch_target(source, release, Path(scratch))
        effective = installed
        if installed is None:
            found = self.describe_checkouts()
            effective = found[0] if found else None
        self.check_allowed(effective, target.manifest)
        changes = self.plan_changes(effective, target.manifest)
        self.say(self.render_table(effective, target, changes))
        if not any(c.changed for c in changes):
            self.say("Nothing to update.")
            return EXIT_OK
        self.explain_plan(changes, target.manifest)
        self.say("Dry run: nothing was downloaded or changed.")
        return EXIT_OK

    def explain_plan(self, changes: list[mf.Change], manifest: mf.Manifest) -> None:
        changed = [c for c in changes if c.changed]
        services = self.services_of(changed)
        self.say("Plan:")
        self.say(
            f"  download and verify: {', '.join(manifest.components[c.component].asset for c in changed)}"
        )
        if not self.opts.no_backup and not self.opts.no_start:
            self.say("  back up the configuration and the app's data")
        if any(c.component == "appliance" for c in changed):
            self.say(
                "  install the new tooling (install.sh of the release), then continue with the new program"
            )
        self.say(f"  rebuild: {', '.join(services)}")
        if self.opts.no_start:
            self.say("  restart nothing (--no-start)")
        else:
            self.say(
                f"  restart in this order: {', '.join(self.restart_sequence(services))}"
            )
        if self.opts.components:
            self.say(
                "  warning: only some components - this is not the combination that was tested together"
            )

    def services_of(self, changed: list[mf.Change]) -> list[str]:
        """The services that use the changed components, in restart order (services only the compose project knows come last)."""
        wanted = {s for c in changed for s in c.services}
        ordered = [s for group in RESTART_ORDER for s in group if s in wanted]
        return ordered + sorted(wanted - set(ordered))

    def restart_sequence(self, services: list[str]) -> list[str]:
        active = self.active_services()
        return [s for s in services if active is None or s in active]

    def new_transaction_id(self) -> str:
        return self.now().strftime("%Y%m%dT%H%M%SZ")

    def _apply_locked(self) -> int | str:
        opts = self.opts
        config = self.need_config()
        self.docker_must_answer()
        pending = self.load_transaction()
        if pending:
            raise UpdateError(
                f"an earlier update did not finish (id {pending.get('id')}, phase {pending.get('phase')}). "
                "Run `ginnungagap update --resume`: it discards what changed nothing yet and rolls back the rest.",
                EXIT_REFUSED,
            )
        installed = self.installed()
        if (
            installed is not None
            and config is not None
            and not opts.force
            and not opts.no_start
        ):
            ok, table = self.healthy_now(config)
            if not ok:
                self.say(table)
                raise UpdateError(
                    "the appliance is not healthy right now, so an update could make it worse. Fix that first, or add --force.",
                    EXIT_REFUSED,
                )
        effective, target, changes, adoptable, staging = self.resolve()
        manifest = target.manifest
        try:
            self.say(self.render_table(effective, target, changes))
            changed = [c for c in changes if c.changed]
            if not changed:
                self.say("Nothing to update.")
                shutil.rmtree(staging, ignore_errors=True)
                return EXIT_OK
            if opts.no_backup and any(
                manifest.components[c.component].data_migration for c in changed
            ):
                raise UpdateError(
                    "this release changes stored data, so the pre-update backup is required: drop --no-backup",
                    EXIT_REFUSED,
                )
            if effective is None and self.selection():
                raise UpdateError(
                    "nothing is installed yet, so --component can't pick a part of it: update everything first"
                )
            self.explain_plan(changes, manifest)
            needed = sum(manifest.components[c.component].size for c in changed)
            self.check_resources(needed, manifest, changed)
            if not opts.yes and not self.env.confirm("Apply this update?"):
                raise UpdateError(
                    "not confirmed - nothing was changed (run it again with --yes to skip the question)",
                    EXIT_REFUSED,
                )
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        tx: dict[str, Any] = {
            "id": self.new_transaction_id(),
            "phase": "started",
            "started": self.stamp(),
            "requester": self.requester(),
            "config_file": str(self.config_file) if self.config_file else None,
            "from": effective.to_dict() if effective else None,
            "from_adopted": adoptable is not None,
            "to": manifest.to_dict(),
            "changed": [c.component for c in changed],
            "partial": bool(self.selection()),
            "staging": str(staging),
            "services": self.services_of(changed),
            "backup": None,
            "data_migration": any(
                manifest.components[c.component].data_migration for c in changed
            ),
            "new_dirs": {},
            "tooling_done": False,
            "restarted": False,
            "options": {
                "no_start": opts.no_start,
                "wait": opts.wait,
                "force": opts.force,
                "restore_data": opts.restore_data,
                "json": opts.json,
                "channel": self.channel,
                "offline_dir": opts.offline_dir,
            },
        }
        self.save_transaction(tx)
        self.audit(
            "apply",
            "started",
            **{
                "from": effective.appliance_version if effective else None,
                "to": manifest.version,
            },
        )
        try:
            self.download(tx, target, changed)
            self.backup_step(tx, effective)
            if adoptable is not None:
                self.say(
                    "Recording the current components as the release to go back to ..."
                )
                effective = self.adopt_now(*adoptable)
                tx["from"] = effective.to_dict()
                self.save_transaction(tx)
            self.unpack(tx, manifest, changed)
        except UpdateError as exc:
            self.abandon(tx, exc.message)
            raise
        except (Exception, KeyboardInterrupt) as exc:
            self.abandon(tx, f"{type(exc).__name__}: {exc}")
            raise
        if "appliance" in tx["changed"]:
            try:
                self.install_tooling(tx, manifest)
            except StepFailed as exc:
                return self.roll_back_failed_update(tx, str(exc))
            return tx["id"]
        return self.continue_update(tx)

    def check_resources(
        self, needed: int, manifest: mf.Manifest, changed: list[mf.Change]
    ) -> None:
        for change in changed:
            size = manifest.components[change.component].size
            if size > MAX_TARBALL_BYTES:
                raise UpdateError(
                    f"{manifest.components[change.component].asset} is {size // (1024 * 1024)} MB; the limit is {MAX_TARBALL_BYTES // (1024 * 1024)} MB",
                    EXIT_REFUSED,
                )
        if needed > MAX_TOTAL_BYTES:
            raise UpdateError(
                f"the release is {needed // (1024 * 1024)} MB in total; the limit is {MAX_TOTAL_BYTES // (1024 * 1024)} MB",
                EXIT_REFUSED,
            )
        need = 3 * needed + IMAGE_HEADROOM_BYTES
        places = {self.layout.root: "the update state folder"}
        if self.env.docker_root is not None and self.env.docker_root.exists():
            places[self.env.docker_root] = "Docker's image folder"
        for place, label in places.items():
            free = self.env.free_bytes(place)
            if free < need:
                raise UpdateError(
                    f"not enough disk space on {label} ({place}): {free // (1024 * 1024)} MB free, about {need // (1024 * 1024)} MB needed",
                    EXIT_REFUSED,
                )
        for host in NETWORK_HOSTS:
            if not self.env.reachable(host):
                self.say(
                    f"warning: {host} can't be reached from this machine; the images are built here, so the build may fail (a cached build can still work)"
                )

    def abandon(self, tx: dict[str, Any], reason: str) -> None:
        """An update that failed before it changed anything that runs: forget it, delete what it staged."""
        self.discard_staged(tx)
        self.clear_transaction()
        self.audit(
            "apply", "failed", to=tx["to"]["appliance"]["version"], detail=reason
        )

    def discard_staged(self, tx: dict[str, Any]) -> None:
        for name, folder in (tx.get("new_dirs") or {}).items():
            shutil.rmtree(self.layout.release_dir(name, folder), ignore_errors=True)
        if tx.get("staging"):
            shutil.rmtree(tx["staging"], ignore_errors=True)

    # -- the steps --
    def download(
        self, tx: dict[str, Any], target: Target, changed: list[mf.Change]
    ) -> None:
        manifest = target.manifest
        source = self.make_source()
        staging = Path(tx["staging"])
        self.say("Downloading ...")
        for change in changed:
            component = manifest.components[change.component]
            self.say(f"  {component.asset} ({component.size // 1024} KB)")
            path = source.fetch(
                target.release, component.asset, staging, component.size
            )
            actual = path.stat().st_size
            if actual != component.size:
                raise UpdateError(
                    f"{component.asset} has {actual} bytes, the manifest says {component.size} - refusing it",
                    EXIT_REFUSED,
                )
            digest = _sha256(path)
            if digest != component.sha256:
                raise UpdateError(
                    f"{component.asset} does not match the checksum in components.json - refusing the release",
                    EXIT_REFUSED,
                )
            listed = target.sums.get(component.asset)
            if listed != component.sha256:
                raise UpdateError(
                    f"{component.asset} is not listed with the same checksum in SHA256SUMS - refusing the release",
                    EXIT_REFUSED,
                )
        self.save_transaction(tx, "downloaded")

    def backup_step(
        self, tx: dict[str, Any], effective: mf.InstalledState | None
    ) -> None:
        if (
            self.opts.no_backup
            or self.opts.no_start
            or effective is None
            or self.config_file is None
        ):
            if self.opts.no_backup:
                self.say("Skipping the pre-update backup (--no-backup).")
            return
        folder = (
            self.layout.backups / f"pre-update-{self.now().strftime('%Y%m%dT%H%M%SZ')}"
        )
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = self.env.backup(
            self.config_file,
            self.bundle,
            folder,
            run=self.run,
            out=self.say,
            inspect=self.inspect,
        )
        if result is None:
            shutil.rmtree(folder, ignore_errors=True)
            raise UpdateError("the pre-update backup failed, so nothing was changed")
        tx["backup"] = str(result)
        self.save_transaction(tx, "backed_up")

    def unpack(
        self, tx: dict[str, Any], manifest: mf.Manifest, changed: list[mf.Change]
    ) -> None:
        self.say("Unpacking ...")
        staging = Path(tx["staging"])
        for change in changed:
            component = manifest.components[change.component]
            repo_dir = self.layout.releases / component.checkout
            safe_extract(
                staging / component.asset,
                repo_dir,
                f"{component.repo_name}-{component.version}",
                MAX_TOTAL_BYTES,
            )
            final = repo_dir / component.version
            if final.exists():
                shutil.rmtree(final)
            (repo_dir / f"{component.repo_name}-{component.version}").rename(final)
            tx["new_dirs"][change.component] = component.version
        self.save_transaction(tx, "unpacked")

    def install_tooling(self, tx: dict[str, Any], manifest: mf.Manifest) -> None:
        """Replaces the program (`install.sh` of the new release); afterwards the NEW program continues the update."""
        self.say("Installing the new tooling ...")
        if tx["from"] is None:
            for name, folder in tx["new_dirs"].items():
                self.flip_link(name, folder)
            tx["flipped_early"] = True
        release_dir = self.layout.release_dir("appliance", tx["new_dirs"]["appliance"])
        command = self.install_command(
            release_dir, Path(tx["staging"]) / "components.json"
        )
        tx["tooling_done"] = True  # even a half-done install must be undone
        self.save_transaction(tx, "tooling")
        if self.run(command) != 0:
            raise StepFailed("the new release's install.sh failed")

    def install_command(
        self, release_dir: Path, manifest_path: Path | None
    ) -> list[str]:
        script = release_dir / "install.sh"
        if not script.is_file():
            raise StepFailed(f"{script} does not exist")
        command = [
            "bash",
            str(script),
            "--repos-dir",
            str(self.layout.components),
            "--config-dir",
            str(self.layout.config_dir),
            "--state-dir",
            str(self.layout.root),
            "--no-start",
        ]
        if self.bundle.name == "app":
            command += ["--prefix", str(self.bundle.parent)]
        if manifest_path is not None:
            command += ["--manifest", str(manifest_path)]
        return command + shlex.split(os.environ.get("GINNUNGAGAP_INSTALL_ARGS", ""))

    @staticmethod
    def knows_the_updater(script: Path) -> bool:
        """
        Whether this install.sh is one of a release made for the updater, i.e. takes --state-dir and --manifest. An older one
        (the checkout an adopted appliance came with) stops with `unknown option --state-dir`, and it also expects the old folder
        names, so it is not run at all: its program files are copied back by hand.
        """
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return "--state-dir" in text and "--manifest" in text

    def continue_update(self, tx: dict[str, Any]) -> int:
        """Steps 5 to 8: switch to the new sources, build, restart one service group after the other, wait for health."""
        manifest = mf.validate(tx["to"])
        try:
            self.switch_and_build(tx)
            if not tx["options"]["no_start"]:
                self.restart_and_check(tx, manifest)
        except StepFailed as exc:
            return self.roll_back_failed_update(tx, str(exc))
        except UpdateError as exc:
            return self.roll_back_failed_update(tx, exc.message)
        except Exception as exc:  # noqa: BLE001 - whatever goes wrong half-way must end in a rollback
            return self.roll_back_failed_update(
                tx, f"unexpected error: {type(exc).__name__}: {exc}"
            )
        return self.finish_update(tx, manifest)

    def project_services(self, tx: dict[str, Any]) -> tuple[list[str], list[str]]:
        """(the services to build, the services to restart): everything that uses a changed component, if it exists here."""
        services = tx["services"]
        no_start = tx["options"]["no_start"]
        active = self.active_services()
        project = self.project()
        build = []
        for service in services:
            if (
                no_start
                or active is None
                or service in active
                or operate.image_exists(
                    operate.image_reference(project, service), self.inspect
                )
            ):
                build.append(service)
        restart = [s for s in build if active is None or s in active]
        return build, restart

    def switch_and_build(self, tx: dict[str, Any]) -> None:
        build, _ = self.project_services(tx)
        tx["build"] = build
        old = mf.InstalledState.from_dict(tx["from"]) if tx["from"] else None
        project = self.project()
        has_env = (self.bundle / "rendered" / "compose.env").is_file()
        if old is not None and has_env:
            tag = f"rollback-{old.appliance_version}"
            # remember the ids too: a rollback puts the old image back by id, a tag may have been moved by then
            tx["image_ids"] = {
                service: found
                for service in build
                if (
                    found := operate.image_id(
                        operate.image_reference(project, service), self.inspect
                    )
                )
            }
            done, _ = operate.retag_images(
                project, build, "latest", tag, self.run, self.inspect
            )
            tx["tagged"] = done
            if done:
                self.say(f"Kept the current images as {tag}: {', '.join(done)}")
        self.save_transaction(tx)
        for name, folder in tx["new_dirs"].items():
            self.flip_link(name, folder)
        tx.setdefault("flipped", []).extend(tx["new_dirs"])
        os.environ["REPOS_DIR"] = str(self.layout.components)
        os.environ["GINNUNGAGAP_GENERATOR_CONFIG"] = str(
            self.layout.components / mf.CHECKOUT_NAMES["generator"] / "config"
        )
        self.save_transaction(tx, "switched")
        if not build:
            self.say("No service uses what changed; nothing to build.")
        elif has_env:
            if operate.build_services(self.bundle, build, self.run, self.say) != 0:
                raise StepFailed("building the images failed")
        else:
            generator = (
                self.layout.components / mf.CHECKOUT_NAMES["generator"] / "config"
            )
            if operate.build_images(self.bundle, generator, self.run, self.say) != 0:
                raise StepFailed("building the images failed")
        self.save_transaction(tx, "built")

    def probe_service(self, service: str) -> Probe | None:
        """The service's container as Docker sees it (None: there is none, or Docker did not answer)."""
        ids = (
            self.inspect(operate.compose_args(self.bundle, "ps", "-q", service)) or ""
        ).split()
        if not ids:
            return None
        answer = self.inspect(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.RestartCount}}",
                ids[0],
            ]
        )
        parts = (answer or "").strip().split("|")
        if len(parts) != 3:
            return None
        try:
            restarts = int(parts[2])
        except ValueError:
            restarts = 0
        return Probe(ids[0], parts[0], parts[1], restarts)

    def wait_service(
        self, service: str, timeout: float, before: Probe | None = None
    ) -> str | None:
        """
        Waits until the service is really up. None: it is, otherwise what is wrong. `running` alone proves nothing (a container
        that crashes on start is `running` for a moment after every restart), so: a service with a health check must be
        `healthy`, one without must stay up for STABLE_SECONDS, and a container that restarts by itself - or is waiting to -
        fails at once. `before`: the container as it was before the restart, to tell its old restarts from new ones.
        """
        deadline = self.env.monotonic() + timeout
        baseline: int | None = None
        stable_since: float | None = None
        problem = "did not answer"
        while True:
            probe = self.probe_service(service)
            if probe is not None:
                if baseline is None:
                    same = before is not None and before.container == probe.container
                    baseline = before.restarts if same and before else 0
                if probe.state in ("exited", "dead"):
                    return f"is not healthy: it stopped ({probe.state})"
                if probe.health == "unhealthy":
                    return "is not healthy: its health check fails"
                if probe.state == "restarting" or probe.restarts > baseline:
                    return f"is not healthy: it keeps restarting (restarted {max(probe.restarts - baseline, 1)} time(s) by itself)"
                problem = f"is {probe.state}, health {probe.health}"
                if probe.state == "running":
                    if probe.health == "healthy":
                        return None
                    if probe.health == "none":
                        now = self.env.monotonic()
                        stable_since = now if stable_since is None else stable_since
                        if now - stable_since >= STABLE_SECONDS:
                            return None
                    else:
                        stable_since = None
                else:
                    stable_since = None
            if self.env.monotonic() >= deadline:
                return f"is not healthy {int(timeout)} s after it was restarted ({problem})"
            self.env.sleep(2)

    def restart_groups(self, services: list[str]) -> None:
        timeout = float(self.settings.step_timeout_seconds)
        for group in RESTART_ORDER:
            members = [s for s in group if s in services]
            if not members:
                continue
            self.say(f"Restarting {', '.join(members)} ...")
            earlier = {m: self.probe_service(m) for m in members}
            command = operate.compose_args(
                self.bundle, "up", "-d", "--no-deps", *operate.OFFLINE_UP_ARGS, *members
            )
            if self.run(command) != 0:
                raise StepFailed(f"starting {', '.join(members)} failed")
            for service in members:
                problem = self.wait_service(service, timeout, earlier[service])
                if problem:
                    raise StepFailed(f"{service} {problem}")
        others = [s for s in services if not any(s in group for group in RESTART_ORDER)]
        if others:
            command = operate.compose_args(
                self.bundle, "up", "-d", "--no-deps", *operate.OFFLINE_UP_ARGS, *others
            )
            if self.run(command) != 0:
                raise StepFailed(f"starting {', '.join(others)} failed")

    def restart_and_check(self, tx: dict[str, Any], manifest: mf.Manifest) -> None:
        _, restart = self.project_services(tx)
        config = self.config
        if config is None:
            raise StepFailed(
                "the configuration is not available, so the restart can't be checked"
            )
        # the Operator container is recreated below: carry the state of one that predates the /data paths over first (nothing
        # has been restarted yet if this fails, so the rollback has nothing to restart either)
        if not operate.migrate_operator_state(
            self.bundle, self.run, self.inspect, self.say
        ):
            raise StepFailed(
                "the Operator's state (accounts, sessions) could not be carried over to the data volume"
            )
        tx["restarted"] = True
        self.save_transaction(tx, "restarting")
        self.restart_groups(restart)
        # everything: the gateway and the OVA server too if their files changed, and the whole health table
        code = self.env.apply(
            self.config_file,
            self.bundle,
            os.environ.get("GINNUNGAGAP_GENERATOR_CONFIG"),
            run=self.run,
            build=False,
            wait_seconds=tx["options"]["wait"],
            fetch=self.env.fetch,
            sleep=self.env.sleep,
            inspect=self.inspect,
            out=self.say,
        )
        if code != 0:
            raise StepFailed("the appliance is not healthy after the update")
        if "operator" in tx["changed"]:
            self.check_operator_version(config, manifest.components["operator"].version)

    def check_operator_version(
        self,
        config: dict[str, Any],
        expected: str,
        wrong: str = "the old container is still running",
    ) -> None:
        fetch = self.env.fetch or status_module.make_fetch()
        status, body, _ = fetch(status_module.base_url(config) + "/health")
        if status == 200:
            try:
                data = json.loads(body)
            except ValueError:
                data = {}
            reported = data.get("version") if isinstance(data, dict) else None
            if reported is None:
                self.say(
                    "(the Operator's /health does not report a version; the version could not be checked)"
                )
            elif reported != expected:
                raise StepFailed(
                    f"the Operator reports version {reported}, expected {expected}: {wrong}"
                )
        else:
            self.say("(the Operator is behind a login: its version was not checked)")

    def finish_update(self, tx: dict[str, Any], manifest: mf.Manifest) -> int:
        old = mf.InstalledState.from_dict(tx["from"]) if tx["from"] else None
        new_dirs = {**(old.dirs if old else {}), **tx["new_dirs"]}
        state = mf.state_from_manifest(
            manifest,
            new_dirs,
            self.stamp(),
            None,
            tx.get("backup"),
            partial=tx["partial"],
        )
        if tx["partial"] and old is not None:
            # only the selected components moved; the rest stay what they were, and so does the appliance release
            for name in mf.COMPONENT_NAMES:
                if name not in tx["changed"]:
                    state.components[name] = old.components.get(
                        name, state.components[name]
                    )
            if "appliance" not in tx["changed"]:
                state.appliance_version = old.appliance_version
                state.appliance_tag = old.appliance_tag
                state.channel = old.channel
                state.released = old.released
        state.data_migration = bool(tx["data_migration"])
        self.record_state(state, previous=old, installed_at=self.stamp())
        self.clear_transaction()
        if tx.get("staging"):
            shutil.rmtree(tx["staging"], ignore_errors=True)
        self.prune(state, old)
        self.audit(
            "apply",
            "ok",
            **{
                "from": old.appliance_version if old else None,
                "to": state.appliance_version,
            },
        )
        label = "Installed" if not tx["options"]["no_start"] else "Unpacked and built"
        self.say(
            f"{label}: appliance v{state.appliance_version}. Go back with `ginnungagap update --rollback`."
        )
        if self.opts.json:
            self.emit({"ok": True, "installed": state.appliance_version})
        return EXIT_OK

    def prune(
        self, state: mf.InstalledState, previous: mf.InstalledState | None
    ) -> None:
        """Keeps the folders of the installed and the previous release (and the newest ones up to keep_releases), the rest goes."""
        keep_count = max(2, int(self.settings.keep_releases))
        for name in mf.COMPONENT_NAMES:
            repo_dir = self.layout.releases / mf.CHECKOUT_NAMES[name]
            if not repo_dir.is_dir():
                continue
            keep = {state.dirs.get(name), previous.dirs.get(name) if previous else None}
            keep.discard(None)
            folders = sorted(
                (
                    p
                    for p in repo_dir.iterdir()
                    if p.is_dir() and not p.name.startswith(".")
                ),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for folder in folders:
                if folder.name in keep:
                    continue
                if len(keep) < keep_count:
                    keep.add(folder.name)
                    continue
                shutil.rmtree(folder, ignore_errors=True)
        wanted = f"rollback-{state.previous}" if state.previous else None
        listing = self.inspect(
            ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"]
        )
        project = self.project()
        for reference in (listing or "").split():
            repository, _, tag = reference.partition(":")
            if (
                repository.startswith(f"{project}-")
                and tag.startswith("rollback-")
                and tag != wanted
            ):
                self.run(["docker", "image", "rm", reference])

    # ---- rollback ----
    def roll_back_failed_update(self, tx: dict[str, Any], reason: str) -> int:
        """Puts everything back as it was before the update started. 2: it worked, 3: it did not."""
        self.say(f"The update failed: {reason}")
        old = mf.InstalledState.from_dict(tx["from"]) if tx["from"] else None
        to_version = tx["to"]["appliance"]["version"]
        if old is None:
            self.clear_transaction()
            if tx.get("staging"):
                shutil.rmtree(tx["staging"], ignore_errors=True)
            self.audit("apply", "failed", to=to_version, detail=reason)
            self.say(
                "Nothing was installed before, so there is nothing to go back to. The unpacked files stay in "
                + str(self.layout.releases)
            )
            return EXIT_ERROR
        self.say("Rolling back ...")
        try:
            self.save_transaction(tx, "rolling_back")
            self.restore_previous(
                old,
                tx["new_dirs"],
                tx["services"],
                restarted=tx.get("restarted", False),
                tooling=tx.get("tooling_done", False),
                backup=tx.get("backup")
                if tx.get("data_migration") or tx["options"].get("restore_data")
                else None,
                tagged=tx.get("tagged"),
                image_ids=tx.get("image_ids"),
            )
        except (StepFailed, UpdateError, OSError) as exc:
            message = exc.message if isinstance(exc, UpdateError) else str(exc)
            self.save_transaction(tx, "rollback_failed")
            self.audit(
                "apply",
                "rollback_failed",
                to=to_version,
                detail=f"{reason}; rollback: {message}",
            )
            self.say(
                f"The rollback failed too: {message}\n"
                f"The journal of this update is {self.layout.transaction} (phase rollback_failed). To try again: `ginnungagap update --resume`.\n"
                f"By hand: point the links in {self.layout.components} back at the old folders in {self.layout.releases}, "
                f"then `docker compose ... up -d` (the old images are tagged rollback-{old.appliance_version})."
            )
            return EXIT_ROLLBACK_FAILED
        self.clear_transaction()
        if tx.get("staging"):
            shutil.rmtree(tx["staging"], ignore_errors=True)
        for name, folder in tx["new_dirs"].items():
            if old.dirs.get(name) != folder:
                shutil.rmtree(self.layout.release_dir(name, folder), ignore_errors=True)
        self.audit(
            "apply",
            "rolled_back",
            to=to_version,
            **{"from": old.appliance_version},
            detail=reason,
        )
        self.say(
            f"Rolled back: appliance {old.appliance_version if not old.adopted else 'adopted'} is running again. The failed update changed nothing else."
        )
        if self.opts.json:
            self.emit({"ok": False, "rolled_back": True, "reason": reason})
        return EXIT_ROLLED_BACK

    def restore_previous(
        self,
        previous: mf.InstalledState,
        from_dirs: dict[str, str],
        services: list[str],
        restarted: bool,
        tooling: bool,
        backup: str | None,
        tagged: list[str] | None = None,
        image_ids: dict[str, str] | None = None,
    ) -> None:
        """
        Back to `previous`: links, images, tooling, (data), restarts, health. `from_dirs`: the folders being left (components
        whose folder equals the previous one are not touched). Needs no network when the rollback images are there.
        """
        project = self.project()
        names = [n for n, folder in from_dirs.items() if previous.dirs.get(n) != folder]
        for name in names:
            self.flip_link(name, previous.dirs[name])
        os.environ["REPOS_DIR"] = str(self.layout.components)
        affected = [
            s for s in services if any(s in mf.DEFAULT_SERVICES[n] for n in names)
        ] or services
        has_env = (self.bundle / "rendered" / "compose.env").is_file()
        tag = f"rollback-{previous.appliance_version}"
        if has_env:
            _, missing = operate.retag_images(
                project,
                affected,
                tag,
                "latest",
                self.run,
                self.inspect,
                by_id=image_ids,
            )
            active = self.active_services()
            # without a rollback image a service is rebuilt from the previous source, but only if it runs here and the image
            # was not simply never there (during a failed update `tagged` says which ones had one)
            wanted = [
                s for s in missing if tagged is None and (active is None or s in active)
            ]
            if wanted:
                self.say(
                    f"No rollback image for {', '.join(wanted)}: rebuilding them from the previous sources (this needs the network)."
                )
                if operate.build_services(self.bundle, wanted, self.run, self.say) != 0:
                    raise StepFailed("rebuilding the previous images failed")
        if tooling:
            appliance_dir = self.layout.release_dir(
                "appliance", previous.dirs["appliance"]
            )
            self.reinstall_tooling(appliance_dir)
        if backup and has_env:
            self.say(f"Restoring the data from {backup} ...")
            if (
                self.env.restore(
                    backup,
                    self.bundle,
                    run=self.run,
                    out=self.say,
                    inspect=self.inspect,
                )
                != 0
            ):
                raise StepFailed("restoring the data failed")
        if restarted and has_env and self.config is not None:
            _, restart = self.restart_targets(affected)
            self.restart_groups(restart)
            code = self.env.apply(
                self.config_file,
                self.bundle,
                os.environ.get("GINNUNGAGAP_GENERATOR_CONFIG"),
                run=self.run,
                build=False,
                wait_seconds=self.opts.wait,
                fetch=self.env.fetch,
                sleep=self.env.sleep,
                inspect=self.inspect,
                out=self.say,
            )
            if code != 0:
                raise StepFailed("the appliance is not healthy after the rollback")
            if "operator" in names:
                # healthy is not enough: a rollback that ends on the new Operator is not a rollback
                expected = (previous.components.get("operator") or {}).get("version")
                if expected and expected != "unknown":
                    self.check_operator_version(
                        self.config,
                        expected,
                        "the rollback did not bring the old Operator back",
                    )

    def restart_targets(self, services: list[str]) -> tuple[list[str], list[str]]:
        active = self.active_services()
        return services, [s for s in services if active is None or s in active]

    def reinstall_tooling(self, appliance_dir: Path) -> None:
        script = appliance_dir / "install.sh"
        if script.is_file() and self.knows_the_updater(script):
            if self.run(self.install_command(appliance_dir, None)) == 0:
                return
            self.say(
                "The previous release's install.sh failed; putting its program files back by hand instead ..."
            )
        elif script.is_file():
            # an adopted program from before the updater: its install.sh knows neither --state-dir nor --manifest
            self.say(
                "The previous program predates the updater (its install.sh does not know --state-dir and --manifest): "
                "putting its program files back by hand ..."
            )
        else:
            # an adopted program has no install.sh of its own: put its files back where the program lives
            self.say("Putting the previous program files back ...")
        for name in ("ginnungagap", "docker"):
            source = appliance_dir / name
            if source.is_dir():
                shutil.rmtree(self.bundle / name, ignore_errors=True)
                shutil.copytree(source, self.bundle / name)
        for file in appliance_dir.glob("compose*.yml"):
            shutil.copy2(file, self.bundle / file.name)

    def rollback(self) -> int:
        if self.opts.dry_run:
            return self._rollback_plan()
        self.require_root("a rollback")
        return self.locked(self._rollback_locked)

    def _rollback_plan(self) -> int:
        previous, installed = self.previous_state()
        names = [
            n
            for n in mf.COMPONENT_NAMES
            if installed.dirs.get(n) != previous.dirs.get(n)
        ]
        self.say(
            f"Rollback from {installed.appliance_version} to {previous.appliance_version}: components {', '.join(names) or 'none'}"
        )
        self.say("Dry run: nothing was changed.")
        return EXIT_OK

    def previous_state(self) -> tuple[mf.InstalledState, mf.InstalledState]:
        installed = self.installed()
        if installed is None or not installed.previous_entry:
            raise UpdateError("there is no earlier release recorded to go back to")
        path = self.layout.history / installed.previous_entry
        try:
            previous = mf.InstalledState.from_dict(mf.read_json(path))
        except mf.ManifestError as exc:
            raise UpdateError(
                f"the record of the earlier release ({path}) is missing or damaged"
            ) from exc
        for name in mf.COMPONENT_NAMES:
            if not self.layout.release_dir(name, previous.dirs.get(name, "?")).is_dir():
                raise UpdateError(
                    f"the sources of the earlier release are gone ({name} {previous.dirs.get(name)}); download that release and install it with --version ... --allow-downgrade"
                )
        return previous, installed

    def _rollback_locked(self) -> int:
        pending = self.load_transaction()
        if pending:
            return self.recover(pending)
        self.need_config()
        self.docker_must_answer()
        previous, installed = self.previous_state()
        names = [
            n
            for n in mf.COMPONENT_NAMES
            if installed.dirs.get(n) != previous.dirs.get(n)
        ]

        def label(state: mf.InstalledState) -> str:
            return "adopted" if state.adopted else state.appliance_version

        self.say(
            f"Rollback from {label(installed)} to {label(previous)}: components {', '.join(names) or 'none'}."
        )
        if not self.opts.yes and not self.env.confirm(
            "Go back to the earlier release?"
        ):
            raise UpdateError("not confirmed - nothing was changed", EXIT_REFUSED)
        services = sorted({s for n in names for s in mf.DEFAULT_SERVICES[n]})
        data = bool(installed.data_migration or self.opts.restore_data)
        backup = installed.backup if data else None
        if data and not backup:
            raise UpdateError(
                "the update being undone changed stored data, but no backup of it is recorded - restore one by hand with `ginnungagap restore`",
                EXIT_REFUSED,
            )
        self.audit(
            "rollback",
            "started",
            **{"from": installed.appliance_version, "to": previous.appliance_version},
        )
        try:
            self.restore_previous(
                previous,
                {n: installed.dirs[n] for n in names},
                services,
                restarted=not self.opts.no_start,
                tooling="appliance" in names,
                backup=backup,
                tagged=None,
            )
        except (StepFailed, UpdateError, OSError) as exc:
            message = exc.message if isinstance(exc, UpdateError) else str(exc)
            self.audit("rollback", "failed", detail=message)
            self.say(f"The rollback failed: {message}")
            return EXIT_ROLLBACK_FAILED
        restored = mf.InstalledState.from_dict(previous.to_dict())
        restored.entry = None
        self.record_state(restored, previous=None, installed_at=self.stamp())
        self.audit(
            "rollback",
            "ok",
            **{"from": installed.appliance_version, "to": previous.appliance_version},
        )
        self.say(f"Rolled back to {label(previous)}.")
        if self.opts.json:
            self.emit({"ok": True, "installed": previous.appliance_version})
        return EXIT_OK

    # ---- finishing or undoing an interrupted update ----
    def resume(self) -> int:
        """`--resume` (the boot-time unit, or by hand): what an interrupted update left is discarded or rolled back."""
        self.require_root("finishing an interrupted update")
        return self.locked(self._resume_locked)

    def _resume_locked(self) -> int:
        pending = self.load_transaction()
        if not pending:
            self.say("No update is waiting to be finished.")
            return EXIT_OK
        return self.recover(pending)

    def recover(self, tx: dict[str, Any]) -> int:
        """
        What an interrupted update left behind: before anything that runs was touched it is simply discarded, after that it is
        rolled back (the tooling counts as touched once its install.sh started).
        """
        phase = tx.get("phase")
        version = tx["to"]["appliance"]["version"]
        self.say(f"An update to {version} did not finish (phase {phase}).")
        early = ("started", "downloaded", "backed_up", "unpacked")
        if phase in early and not tx.get("tooling_done"):
            self.say("It had not changed anything that runs: discarding it.")
            self.discard_staged(tx)
            self.clear_transaction()
            self.audit("resume", "discarded", to=version, detail=f"phase {phase}")
            return EXIT_OK
        self.tx_options_into_self(tx)
        # an engine that was killed leaves its `docker compose build` running, and a build that finishes after the rollback moves
        # the image tag the rollback just set: stop it first
        stopped = (self.env.stop_builds or stop_orphaned_builds)(self.bundle)
        if stopped:
            self.say(
                f"Stopped {len(stopped)} process(es) of the interrupted update's build that were still running."
            )
        self.audit("resume", "rolling_back", to=version, detail=f"phase {phase}")
        code = self.roll_back_failed_update(
            tx, f"the update was interrupted in phase {phase}"
        )
        return EXIT_OK if code == EXIT_ROLLED_BACK else code

    def tx_options_into_self(self, tx: dict[str, Any]) -> None:
        self.opts.wait = float(tx.get("options", {}).get("wait", self.opts.wait))

    def continue_after_tooling(self) -> int:
        """Run by the NEW program after `install.sh` replaced the old one (`update --continue-tx ID`)."""
        self.require_root("an update")
        return self.locked(self._continue_locked)

    def _continue_locked(self) -> int:
        tx = self.load_transaction()
        if (
            not tx
            or tx.get("id") != self.opts.continue_id
            or tx.get("phase") != "tooling"
        ):
            raise UpdateError(
                "there is no update waiting for the new program to continue it",
                EXIT_REFUSED,
            )
        self.tx_options_into_self(tx)
        self.opts.no_start = bool(tx["options"].get("no_start"))
        self.opts.json = bool(tx["options"].get("json"))
        if self.opts.json:
            self.out = lambda text: print(text, file=sys.stderr)
        return self.continue_update(tx)

    # ---- dispatch ----
    def dispatch(self) -> int:
        action = self.opts.action
        if self.opts.json:
            self.out = lambda text: print(text, file=sys.stderr)
        try:
            if action == "check":
                try:
                    return self.check()
                except (rl.SourceError, UpdateError) as exc:
                    self.record_check_failure(exc.message)
                    raise
            if action == "apply":
                return self.apply()
            if action == "rollback":
                return self.rollback()
            if action == "list":
                return self.list_releases()
            if action == "versions":
                return self.versions()
            if action == "history":
                return self.history()
            if action == "adopt":
                return self.adopt()
            if action == "set-token":
                return self.set_token()
            if action == "resume":
                return self.resume()
            if action == "continue":
                return self.continue_after_tooling()
        except UpdateError as exc:
            self.env.err(exc.message)
            if self.opts.json:
                self.emit({"ok": False, "error": exc.message, "exit_code": exc.code})
            if exc.code == EXIT_REFUSED and action in ("apply", "rollback"):
                self.audit(action, "refused", detail=exc.message)
            return exc.code
        except rl.SourceError as exc:
            self.env.err(f"error: {exc.message}")
            if self.opts.json:
                self.emit({"ok": False, "error": exc.message, "exit_code": EXIT_ERROR})
            return EXIT_ERROR
        except mf.ManifestError as exc:
            self.env.err("error: " + "; ".join(exc.problems))
            return EXIT_ERROR
        raise UpdateError(f"unknown action {action}")


def _change_json(change: mf.Change) -> dict[str, Any]:
    return {
        "component": change.component,
        "installed": change.from_version,
        "available": change.to_version,
        "change": change.kind,
        "services": list(change.services),
    }


def run_update(
    options: Options,
    config_file: str | Path | None = None,
    env: Env | None = None,
    layout: Layout | None = None,
    bundle: Path | None = None,
    source: rl.ReleaseSource | None = None,
) -> int:
    """The command: loads the configuration (the `update` section and, for an apply, the whole file), then does the action."""
    env = env or Env()
    layout = layout or Layout.from_env()
    config_path = (
        Path(config_file) if config_file else layout.config_dir / "ginnungagap.yml"
    )
    config = None
    try:
        config = config_module.load(config_path)
    except config_module.ConfigError as exc:
        if (
            options.action in ("apply", "rollback", "resume", "continue")
            and not options.no_start
        ):
            env.err(f"{config_path} is not valid:")
            for problem in exc.problems:
                env.err(f"  - {problem}")
            if config_path.exists():
                return EXIT_REFUSED
    settings = Settings.from_config(config)
    updater = Updater(
        layout, settings, options, env, config, config_path, bundle, source
    )
    return updater.dispatch()
