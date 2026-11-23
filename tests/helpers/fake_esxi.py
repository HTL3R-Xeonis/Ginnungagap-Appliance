"""
An in-memory ESXi host for the tests of the image build: `FakeEsxi` implements `image.build.ports.EsxiPort`, `FakeClock`
implements `Clock`. Nothing here touches a network or a disk except `export_ova` (it writes a small file you name).

What it models: VMs (power state, notes, extraConfig/guestinfo, folder, creation time, IP, VMware Tools), pull import by URL
(the URL is recorded), shutdown through the tools, screenshots, IP wait, delete, and the datastore's folders (an import makes
the folder `<vm>`, or `<vm>_<n>` when the name is taken, like ESXi; deleting a VM removes its folder; an orphan folder can be
seeded or left behind by a failing import).

Fault injection, per call:
    fake.inject("import_ova", TransientError("lease"), times=1)             the first call fails with that error
    fake.inject("power_on", PermanentError("no"), skip=2, times=1)          the third call fails
    fake.inject("delete_vm", Unreachable("x"), times=2, match=lambda c: c.args["name"] == "imgbuild-v1.0.0-...")
    fake.inject("import_ova", TransientError("lease"), side_effect=lambda: fake.add_folder("imgbuild-x"))   leave an orphan
    `exc` may also be a callable returning the exception. A fault with `times=None` never runs out.

Call log: `fake.calls` is a list of `Call(name, args, outcome)` in order; `fake.calls_named("import_ova")`, `fake.names_called()`.
The log NEVER holds guestinfo values (set_extra_config logs the key names only), so a test can scan it for a secret.
`fake.imports` records every import (url, vm_name, datastore, networks).

Time: pass a `FakeClock` so waits cost nothing: `wait_for_ip` / `wait_powered_off` advance it by their timeout when they give up.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from image.build import ports
from image.build.ports import (
    POWERED_OFF,
    POWERED_ON,
    AlreadyExists,
    AuthFailed,
    ExportResult,
    NotFound,
    PermanentError,
    Timeout,
    ToolsUnavailable,
    VmInfo,
)

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


class FakeClock:
    """Time that only moves when something sleeps or the test advances it."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 11, 20, 10, 0, 0, tzinfo=UTC)
        self._mono = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds


@dataclass
class Call:
    name: str
    args: dict[str, Any]
    outcome: str = "ok"  # "ok" or the class name of the error it raised


@dataclass
class Fault:
    method: str
    exc: BaseException | Callable[[], BaseException]
    skip: int = 0
    times: int | None = 1
    match: Callable[[Call], bool] | None = None
    side_effect: Callable[[], None] | None = None
    seen: int = 0

    def applies(self, call: Call) -> bool:
        if self.match is not None and not self.match(call):
            return False
        self.seen += 1
        if self.seen <= self.skip:
            return False
        return self.times is None or self.seen <= self.skip + self.times

    def make(self) -> BaseException:
        return self.exc() if callable(self.exc) else self.exc


@dataclass
class FakeVm:
    name: str
    power_state: str = POWERED_OFF
    annotation: str = ""
    folder: str = ""
    created: datetime | None = None
    ip: str | None = None
    tools_running: bool = False
    extra_config: dict[str, str] = field(default_factory=dict)
    networks: list[str] = field(default_factory=list)
    shutdown_pending: bool = False
    never_gets_ip: bool = False
    shutdown_hangs: bool = False  # the guest ignores the shutdown request
    disk: dict[str, str] = field(
        default_factory=dict
    )  # free space for a test to write marker files into the "guest"


@dataclass(frozen=True)
class ImportRecord:
    url: str
    vm_name: str
    datastore: str
    networks: tuple[str, ...]


class FakeEsxi:
    """See the module docstring. Constructor arguments are the host's world: its address, datastore, port groups and free space."""

    def __init__(
        self,
        host: str = "10.20.20.205",
        *,
        datastore: str = "Datastore",
        networks: Sequence[str] = ("VM Network",),
        clock: FakeClock | None = None,
        free_bytes: int = 200 * 2**30,
    ) -> None:
        self._host = host
        self.datastore = datastore
        self.networks = list(networks)
        self.clock = clock or FakeClock()
        self.free_bytes = {datastore: free_bytes}
        self.vms: dict[str, FakeVm] = {}
        self.folders: dict[str, set[str]] = {datastore: set()}
        self.calls: list[Call] = []
        self.imports: list[ImportRecord] = []
        self.faults: list[Fault] = []
        self.login_ok = True
        self.known_urls: set[str] | None = (
            None  # None: any URL can be imported; a set: only these, else NotFound
        )
        self.ova_bytes = b"fake-ova-content"
        self.closed = False
        self._ip_counter = 100

    # ---------- the test's side ----------
    def inject(
        self,
        method: str,
        exc: BaseException | Callable[[], BaseException],
        *,
        skip: int = 0,
        times: int | None = 1,
        match: Callable[[Call], bool] | None = None,
        side_effect: Callable[[], None] | None = None,
    ) -> Fault:
        fault = Fault(method, exc, skip, times, match, side_effect)
        self.faults.append(fault)
        return fault

    def add_vm(
        self,
        name: str,
        *,
        power_state: str = POWERED_OFF,
        annotation: str = "",
        created: datetime | None = None,
        folder: str | None = None,
        ip: str | None = None,
        tools_running: bool | None = None,
    ) -> FakeVm:
        """Seeds a VM that was there before the build (GNS3VM5, a stale imgbuild-... VM, ...)."""
        folder = folder if folder is not None else self._free_folder(name)
        self.folders[self.datastore].add(folder)
        vm = FakeVm(
            name=name,
            power_state=power_state,
            annotation=annotation,
            folder=folder,
            created=created or self.clock.now(),
            ip=ip,
            tools_running=power_state == POWERED_ON
            if tools_running is None
            else tools_running,
        )
        self.vms[name] = vm
        return vm

    def add_folder(self, folder: str, datastore: str | None = None) -> None:
        """An orphan folder (what a failed import or a crash leaves)."""
        self.folders.setdefault(datastore or self.datastore, set()).add(folder)

    def vm(self, name: str) -> FakeVm:
        return self.vms[name]

    def vm_names(self) -> list[str]:
        return sorted(self.vms)

    def calls_named(self, name: str) -> list[Call]:
        return [c for c in self.calls if c.name == name]

    def names_called(self) -> list[str]:
        return [c.name for c in self.calls]

    def mutating_calls(self) -> list[Call]:
        return [c for c in self.calls if c.name in ports.ESXI_MUTATING]

    # ---------- internals ----------
    def _enter(self, method: str, /, **args: Any) -> Call:
        call = Call(method, args)
        self.calls.append(call)
        for fault in self.faults:
            if fault.method == method and fault.applies(call):
                if fault.side_effect is not None:
                    fault.side_effect()
                exc = fault.make()
                call.outcome = type(exc).__name__
                raise exc
        return call

    def _need(self, name: str) -> FakeVm:
        vm = self.vms.get(name)
        if vm is None:
            raise NotFound(f"no VM named {name}")
        return vm

    def _free_folder(self, name: str) -> str:
        taken = self.folders.get(self.datastore, set())
        if name not in taken:
            return name
        counter = 1
        while f"{name}_{counter}" in taken:
            counter += 1
        return f"{name}_{counter}"

    def _info(self, vm: FakeVm) -> VmInfo:
        return VmInfo(
            name=vm.name,
            power_state=vm.power_state,
            annotation=vm.annotation,
            folder=vm.folder,
            created=vm.created,
            ip=vm.ip if vm.power_state == POWERED_ON else None,
            tools_running=vm.tools_running and vm.power_state == POWERED_ON,
        )

    # ---------- EsxiPort ----------
    @property
    def host(self) -> str:
        return self._host

    def check_login(self) -> None:
        self._enter("check_login")
        if not self.login_ok:
            raise AuthFailed("the login was refused")

    def list_vms(self) -> list[VmInfo]:
        self._enter("list_vms")
        return [self._info(vm) for vm in self.vms.values()]

    def get_vm(self, name: str) -> VmInfo | None:
        self._enter("get_vm", name=name)
        vm = self.vms.get(name)
        return self._info(vm) if vm else None

    def list_datastores(self) -> list[str]:
        self._enter("list_datastores")
        return sorted(self.folders)

    def list_networks(self) -> list[str]:
        self._enter("list_networks")
        return list(self.networks)

    def datastore_free_bytes(self, datastore: str) -> int:
        self._enter("datastore_free_bytes", datastore=datastore)
        if datastore not in self.free_bytes:
            raise NotFound(f"no datastore {datastore}")
        return self.free_bytes[datastore]

    def list_datastore_folders(self, datastore: str) -> list[str]:
        self._enter("list_datastore_folders", datastore=datastore)
        if datastore not in self.folders:
            raise NotFound(f"no datastore {datastore}")
        return sorted(self.folders[datastore])

    def get_extra_config(self, name: str) -> dict[str, str]:
        self._enter("get_extra_config", name=name)
        return dict(self._need(name).extra_config)

    def wait_powered_off(self, name: str, timeout: float) -> bool:
        self._enter("wait_powered_off", name=name, timeout=timeout)
        vm = self._need(name)
        if vm.power_state == POWERED_OFF:
            return True
        if vm.shutdown_pending and not vm.shutdown_hangs:
            vm.power_state = POWERED_OFF
            vm.shutdown_pending = False
            vm.ip = None
            vm.tools_running = False
            self.clock.advance(20)
            return True
        self.clock.advance(timeout)
        return False

    def wait_for_link_local(self, name: str, timeout: float) -> str:
        self._enter("wait_for_link_local", name=name, timeout=timeout)
        vm = self._need(name)
        if vm.power_state != POWERED_ON or vm.never_gets_ip:
            self.clock.advance(timeout)
            raise Timeout(f"{name} reported no hardware link-local IPv6")
        return "fe80::250:56ff:feaa:bbcc"

    def wait_for_ip(self, name: str, timeout: float) -> str:
        self._enter("wait_for_ip", name=name, timeout=timeout)
        vm = self._need(name)
        if vm.power_state != POWERED_ON or vm.never_gets_ip or vm.ip is None:
            self.clock.advance(timeout)
            raise Timeout(f"{name} reported no IP address within {timeout:g} s")
        self.clock.advance(5)
        return vm.ip

    def import_ova(
        self,
        url: str,
        vm_name: str,
        datastore: str,
        networks: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> VmInfo:
        self._enter(
            "import_ova",
            url=url,
            name=vm_name,
            datastore=datastore,
            networks=list(networks),
        )
        if datastore not in self.folders:
            raise NotFound(f"no datastore {datastore}")
        for network in networks:
            if network not in self.networks:
                raise NotFound(f"no port group {network}")
        if self.known_urls is not None and url not in self.known_urls:
            raise NotFound(f"the OVA at {url} cannot be read")
        if vm_name in self.vms:
            raise AlreadyExists(f"a VM named {vm_name} exists")
        self.imports.append(ImportRecord(url, vm_name, datastore, tuple(networks)))
        folder = self._free_folder(vm_name)
        self.folders[datastore].add(folder)
        vm = FakeVm(
            name=vm_name,
            folder=folder,
            created=self.clock.now(),
            networks=list(networks),
        )
        self.vms[vm_name] = vm
        self.clock.advance(100)
        return self._info(vm)

    def set_annotation(self, name: str, text: str) -> None:
        self._enter("set_annotation", name=name, text=text)
        self._need(name).annotation = text

    def set_extra_config(self, name: str, options: Mapping[str, str]) -> None:
        self._enter(
            "set_extra_config", name=name, keys=sorted(options)
        )  # keys only: the values may be secrets
        self._need(name).extra_config.update(options)

    def clear_guestinfo(self, name: str) -> list[str]:
        self._enter("clear_guestinfo", name=name)
        vm = self._need(name)
        removed = sorted(
            k for k in vm.extra_config if k.startswith(ports.GUESTINFO_PREFIX)
        )
        for key in removed:
            del vm.extra_config[key]
        return removed

    def power_on(self, name: str) -> None:
        self._enter("power_on", name=name)
        vm = self._need(name)
        if vm.power_state == POWERED_ON:
            return
        vm.power_state = POWERED_ON
        vm.tools_running = True
        if not vm.never_gets_ip:
            self._ip_counter += 1
            vm.ip = f"10.20.20.{self._ip_counter}"

    def power_off(self, name: str) -> None:
        self._enter("power_off", name=name)
        vm = self._need(name)
        vm.power_state = POWERED_OFF
        vm.ip = None
        vm.tools_running = False
        vm.shutdown_pending = False

    def shutdown_guest(self, name: str) -> None:
        self._enter("shutdown_guest", name=name)
        vm = self._need(name)
        if vm.power_state != POWERED_ON or not vm.tools_running:
            raise ToolsUnavailable(f"VMware Tools of {name} do not answer")
        vm.shutdown_pending = True

    def screenshot(self, name: str) -> bytes:
        self._enter("screenshot", name=name)
        vm = self._need(name)
        if vm.power_state != POWERED_ON:
            raise PermanentError(
                f"{name} is not powered on: there is no console to capture"
            )
        return PNG_HEADER + f"console of {name}".encode()

    def delete_vm(self, name: str) -> bool:
        self._enter("delete_vm", name=name)
        vm = self.vms.pop(name, None)
        if vm is None:
            return False
        for folders in self.folders.values():
            folders.discard(vm.folder)
        return True

    def delete_datastore_folder(self, datastore: str, folder: str) -> None:
        self._enter("delete_datastore_folder", datastore=datastore, folder=folder)
        if datastore not in self.folders:
            raise NotFound(f"no datastore {datastore}")
        if any(vm.folder == folder for vm in self.vms.values()):
            raise PermanentError(f"the folder {folder} holds a registered VM")
        self.folders[datastore].discard(folder)

    def export_ova(
        self,
        name: str,
        out_dir: Path,
        file_stem: str,
        *,
        os_type: str = "debian12_64Guest",
    ) -> ExportResult:
        self._enter(
            "export_ova",
            name=name,
            out_dir=str(out_dir),
            stem=file_stem,
            os_type=os_type,
        )
        vm = self._need(name)
        if vm.power_state != POWERED_OFF:
            raise PermanentError(f"{name} must be powered off to be exported")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{file_stem}.ova"
        target.write_bytes(self.ova_bytes)
        return ExportResult(
            target, len(self.ova_bytes), hashlib.sha256(self.ova_bytes).hexdigest()
        )

    def close(self) -> None:
        self._enter("close")
        self.closed = True
