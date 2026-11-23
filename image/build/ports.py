"""
The contracts of the image build: every outside world the pipeline touches is a Protocol here, and every way something can go
wrong is an exception class here. Nothing in this module talks to a network, a disk or a host.

Who implements what (see README.md of this package):
    EsxiPort         real: image/build/esxi.py (WP3),      fake: tests/helpers/fake_esxi.py  (FakeEsxi)
    RemotePort       real: image/build/remote.py (WP3),    fake: tests/helpers/fake_remote.py
    ShareApiPort     real: image/build/publish.py (WP5),   the template share (NFS or ssh) AND the web server that serves it
    TemplateApiPort  real: image/build/publish.py (WP5),   the template API on .171 (`/api/templates`, `/api/refresh`)
    Clock            real: SystemClock below,               fake: FakeClock in tests/helpers/fake_esxi.py

THE RULES OF EVERY PORT
    * A port is addressed by NAME (a VM name, a folder name), never by a handle of the outside library.
    * A method either returns what its signature says or raises a BuildError (below). An adapter must wrap the errors of its
      library (pyvmomi faults, `subprocess` failures, `urllib` errors, `OSError`) into a TransientError or a PermanentError;
      the pipeline treats an exception that is neither as a bug and does NOT retry it (a few builtin network errors are
      classified as transient as a last line of defence, see pipeline.classify).
    * A secret never crosses a port as an argument that gets logged: the ESXi password belongs to the adapter's constructor,
      guestinfo values and command output are never logged by the pipeline, a command's argv is (so no secret in argv: use
      `stdin` or `env`).
    * Mutating methods are idempotent where it is cheap (delete of something that is gone is not an error), so that cleanup can
      always be run twice.
    * The `*_READ_ONLY` sets list the methods that change nothing anywhere. A dry run hands P0-P3 a view of the ports that
      refuses every other method (`read_only`), and a test checks that every method of a Protocol is in exactly one of
      the two sets (a new method forces a decision).

ERRORS AND THE RETRY RULES (plan section 4/6/16, A11)
    BuildError
      TransientError   (`transient = True`)  a second try may work: network, lease, importer HTTP 403, ssh drop, timeouts.
                       The pipeline retries ONCE (RetryPolicy.max_attempts = 2 attempts per run) from the base-VM phase.
        Unreachable, Timeout, ToolsUnavailable
      PermanentError   a second try cannot work or must not be made: never retried.
        AuthFailed, NotFound, AlreadyExists, NotOurs, CommandFailed, DryRunViolation
        Blocked        no retry, LOUD: signature missing/invalid, version mismatch, base template missing. Result `blocked`.
      Deferred         not a failure: not the right time. Result `deferred`, nothing was created, try again at the next tick.
        AlreadyRunning another build holds the lock
    Interrupted(BaseException) is raised by the signal handler (SIGTERM/SIGINT/SIGHUP); only the pipeline catches it.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# ---------- errors ----------


class BuildError(Exception):
    """Base of every error the build code raises on purpose. `transient` tells the pipeline whether a retry makes sense."""

    transient: bool = False

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        self.message = message
        self.detail = detail  # longer text for the log (never shown in the summary line); may be a command's stderr tail
        super().__init__(message)


class TransientError(BuildError):
    """A second try may work (network, lease, HTTP 403 after an aborted pull, ssh drop, a timeout)."""

    transient = True


class PermanentError(BuildError):
    """A second try cannot work (or must not be made): the pipeline never retries it."""


class Unreachable(TransientError):
    """A host did not answer: connection refused/reset, DNS, 5xx, ssh exit 255."""


class Timeout(TransientError):
    """A wait ran out: SSH not up, no IP, a lease, a command that took too long."""


class ToolsUnavailable(TransientError):
    """VMware Tools do not answer (yet): no IP, no graceful shutdown possible. The phase may fall back to a hard power-off."""


class AuthFailed(PermanentError):
    """The login was refused (wrong ESXi password, 401 from an API)."""


class NotFound(PermanentError):
    """A VM, folder, datastore, network, file or template that must exist does not."""


class AlreadyExists(PermanentError):
    """The never-overwrite rule: a name, folder or file that is about to be created is already there."""


class NotOurs(PermanentError):
    """A delete was refused because the object does not carry this run's marker (it belongs to somebody else)."""


class CommandFailed(PermanentError):
    """A command on a VM or helper exited non-zero. A phase that knows the failure is a network drop re-raises TransientError."""

    def __init__(
        self,
        command: Sequence[str] | str,
        exit_code: int,
        stderr: str = "",
        stdout: str = "",
    ) -> None:
        text = command if isinstance(command, str) else " ".join(command)
        self.command = text
        self.exit_code = exit_code
        self.stderr = stderr
        self.stdout = stdout
        super().__init__(
            f"command failed with exit code {exit_code}: {text[:200]}",
            detail=(stderr or stdout)[-2000:] or None,
        )


class DryRunViolation(PermanentError):
    """A dry run reached for a mutating method: a bug of the phase, never a retry."""


class Blocked(PermanentError):
    """No retry, loud: signature missing or invalid, version mismatch, base template missing. The result is `blocked`."""


class Deferred(BuildError):
    """Not a failure: lab busy, share or datastore too full, API down, PAUSE file. Nothing was created; try again later."""


class AlreadyRunning(Deferred):
    """Another build holds the lock ("build X is running")."""


class Interrupted(BaseException):
    """Raised by the pipeline's signal handler. Never catch it in a phase; the cleanup runs in the pipeline."""

    def __init__(self, signum: int | None = None) -> None:
        self.signum = signum
        super().__init__(f"interrupted by signal {signum}" if signum else "interrupted")


# ---------- data that crosses the ports ----------

POWERED_ON = "poweredOn"
POWERED_OFF = "poweredOff"
SUSPENDED = "suspended"
GUESTINFO_PREFIX = "guestinfo.ginnungagap."


@dataclass(frozen=True)
class VmInfo:
    """What the pipeline may know about a VM. A snapshot: it does not change after it was returned."""

    name: str
    power_state: str  # POWERED_ON | POWERED_OFF | SUSPENDED
    annotation: str = ""  # the notes field (the janitor reads the run id from it)
    folder: str = ""  # the datastore folder that holds its files (first part of vmPathName), "" when unknown
    created: datetime | None = (
        None  # timezone-aware UTC; None when the host does not know (the janitor then keeps the VM)
    )
    ip: str | None = None  # first IPv4 address VMware Tools report
    tools_running: bool = False

    @property
    def powered_on(self) -> bool:
        return self.power_state == POWERED_ON


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class RemoteTarget:
    """One VM reached over ssh with a per-run throw-away key and a per-run known_hosts file (the VM is new every time)."""

    host: str
    user: str
    key_file: Path
    known_hosts: Path
    port: int = 22


@dataclass(frozen=True)
class KeyPair:
    private: Path  # 0600, inside the run directory
    public: str  # one line, `ssh-ed25519 AAAA... comment`


@dataclass(frozen=True)
class ExportResult:
    path: Path  # the .ova
    size: int
    sha256: str  # lower-case hex


@dataclass(frozen=True)
class HeadResult:
    """The answer of a HEAD request to the OVA web server (.181): the three headers the importer needs."""

    status: int
    content_length: int | None = None
    accept_ranges: str | None = None

    @property
    def importable(self) -> bool:
        """200, a Content-Length and range support: what `PullImporter` requires of a URL."""
        return (
            self.status == 200
            and self.content_length is not None
            and (self.accept_ranges or "").lower() == "bytes"
        )


@dataclass(frozen=True)
class TemplateEntry:
    """One template of the template API (`GET /api/templates`)."""

    name: str
    id: str  # the SHA-256 in capitals
    file: str  # the .ova file name
    source_dir: str = ""  # `_source_dir`: the folder below the web server's root
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)


# ---------- the ports ----------


@runtime_checkable
class EsxiPort(Protocol):
    """
    One ESXi host (the build host, `.205`). Real adapter: `PullImporter` + `ESXiConnection` of TopologyBuilder (branch Mjonyr),
    pyvmomi for the rest, `export_ova.py` for the export. All methods take the VM by NAME.

    Classification: connection problems, lease problems, an importer HTTP 403, a task timeout -> TransientError;
    a wrong password -> AuthFailed; an unknown VM/datastore/network -> NotFound; a task that failed for a reason of the
    request itself -> PermanentError. Idempotent: power_on, power_off, delete_vm, delete_datastore_folder, clear_guestinfo.
    """

    @property
    def host(self) -> str:
        """The address of the host this port is connected to (the pipeline compares it with `esxi.allowed_hosts`)."""
        ...

    # -- reads (ESXI_READ_ONLY) --
    def check_login(self) -> None:
        """:raises AuthFailed: wrong credentials. :raises Unreachable: the host does not answer."""
        ...

    def list_vms(self) -> list[VmInfo]:
        """Every VM registered on the host (the lab-busy check, the janitor and the cleanup check use it)."""
        ...

    def get_vm(self, name: str) -> VmInfo | None:
        """The VM with exactly this name, or None."""
        ...

    def list_datastores(self) -> list[str]: ...

    def list_networks(self) -> list[str]:
        """Port group names."""
        ...

    def datastore_free_bytes(self, datastore: str) -> int:
        """:raises NotFound: no such datastore."""
        ...

    def list_datastore_folders(self, datastore: str) -> list[str]:
        """Names of the folders in the root of the datastore (the importer's orphan folders `<vm>` / `<vm>_<n>` are among them)."""
        ...

    def get_extra_config(self, name: str) -> dict[str, str]:
        """The VM's extraConfig. The values may be secrets: never log them. :raises NotFound:"""
        ...

    def wait_powered_off(self, name: str, timeout: float) -> bool:
        """Waits until the VM is powered off. True = it is, False = still on after `timeout` seconds (no exception: the caller decides)."""
        ...

    def wait_for_ip(self, name: str, timeout: float) -> str:
        """The first IPv4 address VMware Tools report. :raises Timeout: none within `timeout` seconds. :raises NotFound:"""
        ...

    def wait_for_link_local(self, name: str, timeout: float) -> str:
        """The hardware-MAC-matched link-local IPv6 address, without worker interface zone."""
        ...

    # -- changes --
    def import_ova(
        self,
        url: str,
        vm_name: str,
        datastore: str,
        networks: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> VmInfo:
        """
        Pull import (the host fetches `url` itself, `PullImporter.import_ova`): a thin VM `vm_name` on `datastore` with ONE
        vmxnet3 NIC per entry of `networks`, powered off. A failed import cleans up what it created (importer) but may leave
        an orphan folder; the janitor removes it. :raises AlreadyExists: a VM of that name exists.
        :raises NotFound: datastore/network/URL missing. :raises TransientError: lease, HTTP 403, timeout.
        """
        ...

    def set_annotation(self, name: str, text: str) -> None:
        """The notes field (`ESXiConnection.set_vm_annotation`). Always `names.annotation(run_id)` for build VMs."""
        ...

    def set_extra_config(self, name: str, options: Mapping[str, str]) -> None:
        """
        Sets extraConfig keys (guestinfo.ginnungagap.* from `set_guestinfo.guestinfo_options`) BEFORE the first power-on.
        The pipeline logs the key names only, never the values.
        """
        ...

    def clear_guestinfo(self, name: str) -> list[str]:
        """Empties every `guestinfo.ginnungagap.*` key (what `set_guestinfo.py --clear` does). Returns the key names removed."""
        ...

    def power_on(self, name: str) -> None: ...

    def power_off(self, name: str) -> None:
        """Hard power-off. A VM that is already off is not an error."""
        ...

    def shutdown_guest(self, name: str) -> None:
        """Asks the guest to shut down through VMware Tools (does not wait). :raises ToolsUnavailable:"""
        ...

    def screenshot(self, name: str) -> bytes:
        """The console as PNG, the raw bytes the host produced (never re-encode). :raises PermanentError: the VM is not powered on."""
        ...

    def delete_vm(self, name: str) -> bool:
        """Powers off if needed and destroys the VM with its files. True = it existed. Never raises for a VM that is gone."""
        ...

    def delete_datastore_folder(self, datastore: str, folder: str) -> None:
        """Deletes a folder in the root of the datastore. Gone already = fine. :raises PermanentError: a registered VM lives in it."""
        ...

    def export_ova(
        self,
        name: str,
        out_dir: Path,
        file_stem: str,
        *,
        os_type: str = "debian12_64Guest",
    ) -> ExportResult:
        """
        `export_ova.py --host H --vm NAME --out OUT_DIR --name FILE_STEM --os-type OS_TYPE` (the password goes through the
        environment of the child, never argv). The VM must be powered off. Writes `<out_dir>/<file_stem>.ova`.
        """
        ...

    def close(self) -> None: ...


@runtime_checkable
class RemotePort(Protocol):
    """
    ssh/scp to the throw-away VMs (base VM, acceptance VMs). Real adapter: `ssh`/`scp` subprocesses with the per-run key, a
    per-run known_hosts and `StrictHostKeyChecking=no` (the VM is new each time). Every call has a timeout; an ssh drop
    (exit 255) is a TransientError, a command that exits non-zero is CommandFailed (when `check`).
    """

    def generate_key(self, directory: Path, comment: str) -> KeyPair:
        """A new ed25519 key in `directory` (created 0700, key file 0600)."""
        ...

    def wait_ssh(self, target: RemoteTarget, timeout: float) -> None:
        """Waits until a login works. :raises Timeout:"""
        ...

    def run(
        self,
        target: RemoteTarget,
        argv: Sequence[str],
        *,
        timeout: float = 300.0,
        check: bool = True,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """
        Runs `argv` (quoted by the adapter) on the VM. The argv is logged, so a secret goes through `stdin` or `env`.
        :raises CommandFailed: non-zero exit and `check`. :raises Timeout: :raises Unreachable: ssh itself failed (255).
        """
        ...

    def run_detached(
        self,
        target: RemoteTarget,
        unit: str,
        argv: Sequence[str],
        *,
        log_path: str,
        timeout: float,
        success_marker: str | None = None,
        poll_seconds: float = 15.0,
    ) -> CommandResult:
        """
        Runs `argv` as a transient systemd unit (`systemd-run --unit UNIT`, output in `log_path` on the VM) so an ssh drop does
        not kill it (prepare-image wipes /tmp), polls until the unit ended (or `success_marker` is in the log), and returns
        its exit code and the log tail. :raises Timeout:
        """
        ...

    def put(
        self, target: RemoteTarget, local: Path, remote: str, *, recursive: bool = False
    ) -> None: ...

    def get(
        self, target: RemoteTarget, remote: str, local: Path, *, recursive: bool = False
    ) -> None: ...

    def forget(self, target: RemoteTarget) -> None:
        """Removes the per-run known_hosts and key files of this target. Idempotent."""
        ...

    def close_master(self, target: RemoteTarget) -> None:
        """Close one SSH multiplexed transport without deleting shared run keys or known hosts."""
        ...


@runtime_checkable
class ShareApiPort(Protocol):
    """
    The template share (`.180:/mnt/Image-Pool/Templates`, mounted at `/mnt/esxi_templates`, or reached through `ssh .172`)
    AND the web server `.181` that serves it. A folder is addressed by its name in the root of the share; a file by
    (folder, name). Never-overwrite is the port's job: `make_folder` is an atomic mkdir and `put_*` refuse an existing file
    unless `replace=True`; files are written through a `<name>.partial` and renamed, so a reader never sees half a file.
    """

    # -- reads (SHARE_READ_ONLY) --
    def free_bytes(self) -> int: ...

    def list_folders(self) -> list[str]: ...

    def folder_files(self, folder: str) -> list[str]:
        """:raises NotFound: no such folder."""
        ...

    def read_text(self, folder: str, name: str) -> str | None:
        """The text of a small file, None when it does not exist."""
        ...

    def size(self, folder: str, name: str) -> int:
        """:raises NotFound:"""
        ...

    def sha256(self, folder: str, name: str) -> str:
        """Lower-case hex, computed on the share (the compare after a copy). :raises NotFound:"""
        ...

    def url_for(self, folder: str, name: str) -> str:
        """The URL the ESXi host pulls the file from (`<ova_base_url>/<folder>/<name>`, parts percent-encoded)."""
        ...

    def head(self, url: str) -> HeadResult:
        """HEAD on the web server; a 404 is a result, not an error. :raises Unreachable: the server does not answer."""
        ...

    # -- changes --
    def make_folder(self, folder: str) -> None:
        """Atomic mkdir. :raises AlreadyExists: the folder exists (never overwrite)."""
        ...

    def put_file(
        self, folder: str, name: str, source: Path, *, replace: bool = False
    ) -> None:
        """Copies through `.partial` and renames. :raises AlreadyExists: the file exists and not `replace`."""
        ...

    def put_text(
        self, folder: str, name: str, text: str, *, replace: bool = False
    ) -> None:
        """Same for a small text (the `.imgbuild-run` marker, the `.yml` written last, BUILD-INFO.json)."""
        ...

    def remove_folder(self, folder: str, *, marker_name: str, marker_text: str) -> bool:
        """
        Removes the folder only when `marker_name` in it contains exactly `marker_text` (proof that this run made it).
        :raises NotOurs: the marker is missing or different. Returns False when the folder is gone already.
        """
        ...


@runtime_checkable
class TemplateApiPort(Protocol):
    """The template API on `.171:8000`. The `.yml` a template is listed by is written through the ShareApiPort."""

    def check(self) -> None:
        """:raises Unreachable: the API does not answer."""
        ...

    def list_templates(self) -> list[TemplateEntry]:
        """`GET /api/templates`."""
        ...

    def refresh(self) -> None:
        """`POST /api/refresh`."""
        ...


@runtime_checkable
class Clock(Protocol):
    """Time. Every wait of the pipeline and the adapters goes through it, so tests run in zero seconds."""

    def now(self) -> datetime:
        """Timezone-aware UTC."""
        ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# ---------- which methods change something ----------

ESXI_READ_ONLY = frozenset(
    {
        "host",
        "check_login",
        "list_vms",
        "get_vm",
        "list_datastores",
        "list_networks",
        "datastore_free_bytes",
        "list_datastore_folders",
        "get_extra_config",
        "wait_powered_off",
        "wait_for_ip",
        "wait_for_link_local",
    }
)
ESXI_MUTATING = frozenset(
    {
        "import_ova",
        "set_annotation",
        "set_extra_config",
        "clear_guestinfo",
        "power_on",
        "power_off",
        "shutdown_guest",
        "screenshot",
        "delete_vm",
        "delete_datastore_folder",
        "export_ova",
        "close",
    }
)
REMOTE_READ_ONLY: frozenset[str] = frozenset()
REMOTE_MUTATING = frozenset(
    {
        "generate_key",
        "wait_ssh",
        "run",
        "run_detached",
        "put",
        "get",
        "forget",
        "close_master",
    }
)
SHARE_READ_ONLY = frozenset(
    {
        "free_bytes",
        "list_folders",
        "folder_files",
        "read_text",
        "size",
        "sha256",
        "url_for",
        "head",
    }
)
SHARE_MUTATING = frozenset({"make_folder", "put_file", "put_text", "remove_folder"})
TEMPLATE_READ_ONLY = frozenset({"check", "list_templates"})
TEMPLATE_MUTATING = frozenset({"refresh"})

PORT_CLASSIFICATION = {
    EsxiPort: (ESXI_READ_ONLY, ESXI_MUTATING),
    RemotePort: (REMOTE_READ_ONLY, REMOTE_MUTATING),
    ShareApiPort: (SHARE_READ_ONLY, SHARE_MUTATING),
    TemplateApiPort: (TEMPLATE_READ_ONLY, TEMPLATE_MUTATING),
}


class _ReadOnlyView:
    """A port that answers only its read-only methods; anything else raises DryRunViolation."""

    def __init__(self, port: Any, allowed: frozenset[str], label: str) -> None:
        object.__setattr__(self, "_port", port)
        object.__setattr__(self, "_allowed", allowed)
        object.__setattr__(self, "_label", label)

    def __getattr__(self, name: str) -> Any:
        if name not in self._allowed:
            raise DryRunViolation(
                f"{self._label}.{name} changes something; a dry run may only read"
            )
        return getattr(self._port, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise DryRunViolation(f"{self._label} is a read-only view")


def read_only(port: Any, allowed: frozenset[str], label: str) -> Any:
    """The view of `port` a dry run hands to the phases that really run (P0-P3)."""
    return _ReadOnlyView(port, allowed, label)
