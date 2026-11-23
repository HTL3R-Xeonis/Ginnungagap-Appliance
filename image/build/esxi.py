"""Pinned ESXi adapter and the VM/import/export phases of the image build."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import quote, urlencode

from . import names, pipeline
from .ports import (
    POWERED_OFF,
    POWERED_ON,
    AlreadyExists,
    AuthFailed,
    BuildError,
    Clock,
    ExportResult,
    NotFound,
    NotOurs,
    PermanentError,
    SystemClock,
    Timeout,
    ToolsUnavailable,
    TransientError,
    Unreachable,
    VmInfo,
)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


class EsxiAdapter:
    """One lazy ESXi connection. The password is never included in an argv or error."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        *,
        port: int = 443,
        clock: Clock | None = None,
        connection_factory=None,
        importer_factory=None,
        runner=None,
    ) -> None:
        self._host, self.user, self._password, self.port = host, user, password, port
        self.clock = clock or SystemClock()
        self._factory = connection_factory
        self._importer_factory = importer_factory
        self._runner = runner or subprocess.run
        self._conn = None

    @property
    def host(self) -> str:
        return self._host

    def _fail(self, exc: Exception, operation: str) -> None:
        if isinstance(exc, BuildError):
            raise exc
        kind = type(exc).__name__.lower()
        detail = str(exc).replace(self._password, "***") if self._password else str(exc)
        lowered = detail.lower()
        if "invalidlogin" in kind or "notauthenticated" in kind or "401" in lowered:
            raise AuthFailed(f"ESXi authentication failed during {operation}") from None
        if "notfound" in kind or "404" in lowered:
            raise NotFound(f"ESXi object not found during {operation}") from None
        if isinstance(exc, TimeoutError) or "timeout" in kind or "timed out" in lowered:
            raise Timeout(f"ESXi timed out during {operation}") from None
        if isinstance(exc, (ConnectionError, OSError)) or any(
            word in lowered
            for word in ("lease", "403", "connection reset", "connection refused")
        ):
            raise Unreachable(f"ESXi connection failed during {operation}") from None
        raise PermanentError(
            f"ESXi {operation} failed ({type(exc).__name__})", detail=detail
        ) from None

    def _call(self, operation: str, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classify every external pyvmomi/importer fault at the port boundary
            self._fail(exc, operation)

    @property
    def connection(self):
        if self._conn is None:
            factory = self._factory
            if factory is None:
                from src.connections.esxi_connection import ESXiConnection

                factory = ESXiConnection
            self._conn = self._call(
                "connect",
                lambda: factory(self.host, self.port, self.user, self._password),
            )
        return self._conn

    def check_login(self) -> None:
        self._call("login", lambda: self.connection.connection.CurrentTime())

    def _vms(self):
        return self.connection.get_all_vms()

    def _vm(self, name: str):
        # Deliberately exact: a numbered alias must never select another build's VM.
        vm = next((vm for vm in self._vms() if vm.name == name), None)
        if vm is None:
            raise NotFound(f"no ESXi VM named {name}")
        return vm

    def _owned(self, name: str):
        if not names.is_ours_vm(name):
            raise NotOurs(
                f"refusing to change a VM outside the image-build namespace: {name}"
            )
        vm = self._vm(name)
        annotation = getattr(getattr(vm, "config", None), "annotation", "") or ""
        if annotation and names.parse_annotation(annotation) is None:
            raise NotOurs(f"VM {name} has an annotation belonging to another tool")
        return vm

    @staticmethod
    def _info(vm) -> VmInfo:
        config, guest = getattr(vm, "config", None), getattr(vm, "guest", None)
        path = getattr(getattr(config, "files", None), "vmPathName", "") or ""
        match = re.match(r"\[[^]]+\]\s*([^/]+)/", path)
        return VmInfo(
            vm.name,
            str(vm.runtime.powerState),
            annotation=getattr(config, "annotation", "") or "",
            folder=match.group(1) if match else "",
            created=getattr(config, "createDate", None),
            ip=getattr(guest, "ipAddress", None),
            tools_running=str(getattr(guest, "toolsRunningStatus", ""))
            == "guestToolsRunning",
        )

    def list_vms(self) -> list[VmInfo]:
        return self._call("list VMs", lambda: [self._info(vm) for vm in self._vms()])

    def get_vm(self, name: str) -> VmInfo | None:
        return next((vm for vm in self.list_vms() if vm.name == name), None)

    def list_datastores(self) -> list[str]:
        return self._call(
            "list datastores",
            lambda: [ds.name for ds in self.connection.get_all_datastores()],
        )

    def _view(self, cls):
        content = self.connection.content
        view = content.viewManager.CreateContainerView(content.rootFolder, [cls], True)
        try:
            return list(view.view)
        finally:
            view.Destroy()

    def list_networks(self) -> list[str]:
        from pyVmomi import vim

        return self._call(
            "list networks",
            lambda: [network.name for network in self._view(vim.Network)],
        )

    def datastore_free_bytes(self, datastore: str) -> int:
        return self._call(
            "datastore space",
            lambda: int(self.connection.find_datastore(datastore).summary.freeSpace),
        )

    def _wait_task(self, task, timeout: float = 1800):
        deadline = self.clock.monotonic() + timeout
        while True:
            info = task.info
            if str(info.state) == "success":
                return getattr(info, "result", None)
            if str(info.state) == "error":
                error = getattr(info, "error", None)
                if isinstance(error, Exception):
                    self._fail(error, "task")
                raise PermanentError("ESXi task failed")
            if self.clock.monotonic() >= deadline:
                raise Timeout("ESXi task did not finish before its deadline")
            self.clock.sleep(1)

    def list_datastore_folders(self, datastore: str) -> list[str]:
        from pyVmomi import vim

        def query():
            ds = self.connection.find_datastore(datastore)
            spec = vim.host.DatastoreBrowser.SearchSpec(
                query=[vim.host.DatastoreBrowser.FolderQuery()]
            )
            result = self._wait_task(
                ds.browser.SearchDatastore_Task(
                    datastorePath=f"[{datastore}]", searchSpec=spec
                )
            )
            return [f.path.rstrip("/") for f in result.file or []]

        return self._call("list datastore folders", query)

    def get_extra_config(self, name: str) -> dict[str, str]:
        return self._call(
            "read guestinfo",
            lambda: {
                item.key: str(item.value)
                for item in self._vm(name).config.extraConfig or []
            },
        )

    def wait_powered_off(self, name: str, timeout: float) -> bool:
        deadline = self.clock.monotonic() + timeout
        while True:
            vm = self.get_vm(name)
            if vm is None:
                raise NotFound(f"no ESXi VM named {name}")
            if vm.power_state == POWERED_OFF:
                return True
            if self.clock.monotonic() >= deadline:
                return False
            self.clock.sleep(min(2, max(0, deadline - self.clock.monotonic())))

    def wait_for_ip(self, name: str, timeout: float) -> str:
        deadline = self.clock.monotonic() + timeout
        while True:
            ip = self._call("wait for IP", lambda: self._hardware_ipv4(self._vm(name)))
            if ip:
                return ip
            if self.clock.monotonic() >= deadline:
                raise Timeout(
                    f"VMware Tools did not report an unambiguous hardware-NIC IPv4 for {name}"
                )
            self.clock.sleep(min(2, max(0, deadline - self.clock.monotonic())))

    @staticmethod
    def _hardware_ipv4(vm) -> str | None:
        return EsxiAdapter._hardware_address(vm, 4, False)

    def wait_for_link_local(self, name: str, timeout: float) -> str:
        deadline = self.clock.monotonic() + timeout
        while True:
            address = self._call(
                "wait for link-local IP",
                lambda: self._hardware_address(self._vm(name), 6, True),
            )
            if address:
                return address
            if self.clock.monotonic() >= deadline:
                raise Timeout(
                    f"VMware Tools did not report an unambiguous hardware-NIC link-local IPv6 for {name}"
                )
            self.clock.sleep(min(2, max(0, deadline - self.clock.monotonic())))

    @staticmethod
    def _hardware_address(vm, version: int, link_local: bool) -> str | None:
        """Only use Tools addresses matched to an actual virtual Ethernet card.

        guest.ipAddress may be Docker's bridge address. No primary-IP fallback
        is safe when Tools has not yet reported the management NIC. Build VMs
        have one NIC; ambiguous multi-NIC/multi-address reports keep waiting.
        """
        from pyVmomi import vim

        def mac(value):
            compact = str(value or "").replace(":", "").replace("-", "").lower()
            return compact if re.fullmatch(r"[0-9a-f]{12}", compact) else None

        hardware = getattr(getattr(vm, "config", None), "hardware", None)
        cards = {
            mac(device.macAddress)
            for device in (getattr(hardware, "device", None) or [])
            if isinstance(device, vim.vm.device.VirtualEthernetCard)
            and getattr(getattr(device, "connectable", None), "connected", None)
            is not False
        } - {None}
        candidates = set()
        for nic in getattr(getattr(vm, "guest", None), "net", None) or []:
            if (
                mac(getattr(nic, "macAddress", None)) not in cards
                or getattr(nic, "connected", None) is False
            ):
                continue
            addresses = list(getattr(nic, "ipAddress", None) or [])
            addresses.extend(
                getattr(entry, "ipAddress", None)
                for entry in (
                    getattr(getattr(nic, "ipConfig", None), "ipAddress", None) or []
                )
            )
            for value in addresses:
                try:
                    address = ipaddress.ip_address(value)
                except ValueError:
                    continue
                if (
                    address.version == version
                    and not address.is_loopback
                    and address.is_link_local == link_local
                    and not address.is_multicast
                    and not address.is_unspecified
                    and (link_local or not address.is_reserved)
                ):
                    candidates.add(str(address))
        return next(iter(candidates)) if len(candidates) == 1 else None

    def import_ova(
        self,
        url: str,
        vm_name: str,
        datastore: str,
        networks: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> VmInfo:
        if not names.is_ours_vm(vm_name):
            raise NotOurs("an image build may import only imgbuild-/imgaccept- VMs")
        if self.get_vm(vm_name) is not None:
            raise AlreadyExists(f"VM {vm_name} already exists")

        def deploy():
            factory = self._importer_factory
            if factory is None:
                from src.pull_importer import PullImporter

                factory = PullImporter
            vm = factory(self.connection).import_ova(
                url, vm_name, datastore, list(networks), timeout=timeout
            )
            return self._info(vm)

        return self._call("pull import", deploy)

    def set_annotation(self, name: str, text: str) -> None:
        if names.parse_annotation(text) is None:
            raise NotOurs("image-build VM annotation must identify a valid run")
        self._call(
            "set annotation",
            lambda: self.connection.set_vm_annotation(self._owned(name), text),
        )

    def set_extra_config(self, name: str, options: Mapping[str, str]) -> None:
        from pyVmomi import vim

        if any(not key.startswith("guestinfo.ginnungagap.") for key in options):
            raise PermanentError("only appliance guestinfo keys may be changed")

        def change():
            vm = self._owned(name)
            if str(vm.runtime.powerState) != POWERED_OFF:
                raise PermanentError("guestinfo must be set before the first power-on")
            spec = vim.vm.ConfigSpec(
                extraConfig=[
                    vim.option.OptionValue(key=k, value=v) for k, v in options.items()
                ]
            )
            self._wait_task(vm.ReconfigVM_Task(spec=spec))

        self._call("set guestinfo", change)

    def clear_guestinfo(self, name: str) -> list[str]:
        keys = sorted(
            k
            for k in self.get_extra_config(name)
            if k.startswith("guestinfo.ginnungagap.")
        )
        self.set_extra_config(name, dict.fromkeys(keys, ""))
        return keys

    def power_on(self, name: str) -> None:
        def power():
            vm = self._owned(name)
            if str(vm.runtime.powerState) != POWERED_ON:
                self._wait_task(vm.PowerOnVM_Task())

        self._call("power on", power)

    def power_off(self, name: str) -> None:
        def power():
            vm = self._owned(name)
            if str(vm.runtime.powerState) != POWERED_OFF:
                self._wait_task(vm.PowerOffVM_Task())

        self._call("power off", power)

    def shutdown_guest(self, name: str) -> None:
        def shutdown():
            vm = self._owned(name)
            if str(vm.runtime.powerState) == POWERED_OFF:
                return
            if str(vm.guest.toolsRunningStatus) != "guestToolsRunning":
                raise ToolsUnavailable(f"VMware Tools is not running on {name}")
            vm.ShutdownGuest()

        self._call("guest shutdown", shutdown)

    def screenshot(self, name: str) -> bytes:
        import requests

        def capture():
            vm = self._owned(name)
            if str(vm.runtime.powerState) != POWERED_ON:
                raise PermanentError("a screenshot requires a powered-on VM")
            path = self._wait_task(vm.CreateScreenshot_Task())
            match = re.fullmatch(r"\[([^]]+)\]\s*(.+)", path or "")
            if match is None:
                raise PermanentError("ESXi returned an invalid screenshot path")
            ds, item = match.groups()
            url = f"https://{self.host}:{self.port}/folder/{quote(item, safe='/')}?{urlencode({'dcPath': 'ha-datacenter', 'dsName': ds})}"
            cookie = self.connection.connection._stub.cookie
            with requests.get(
                url,
                headers={"Cookie": cookie},
                verify=False,
                timeout=60,
                allow_redirects=False,
            ) as response:
                response.raise_for_status()
                result = response.content
            if not result.startswith(b"\x89PNG\r\n\x1a\n"):
                raise PermanentError("ESXi screenshot is not PNG")
            return result

        return self._call("screenshot", capture)

    def delete_vm(self, name: str) -> bool:
        if not names.is_ours_vm(name):
            raise NotOurs("refusing to delete a VM outside the build namespace")
        if self.get_vm(name) is None:
            return False

        def delete():
            vm = self._owned(name)
            if str(vm.runtime.powerState) != POWERED_OFF:
                self._wait_task(vm.PowerOffVM_Task())
            self._wait_task(vm.Destroy_Task())

        self._call("delete VM", delete)
        return True

    def delete_datastore_folder(self, datastore: str, folder: str) -> None:
        if not names.is_ours_folder(folder) or "/" in folder or "\\" in folder:
            raise NotOurs(
                "refusing to delete a datastore folder outside the build namespace"
            )
        if any(vm.folder == folder for vm in self.list_vms()):
            raise PermanentError("a registered VM still uses the datastore folder")
        if folder not in self.list_datastore_folders(datastore):
            return
        self._call(
            "delete datastore folder",
            lambda: self._wait_task(
                self.connection.content.fileManager.DeleteDatastoreFile_Task(
                    name=f"[{datastore}] {folder}", datacenter=None
                )
            ),
        )

    def export_ova(
        self,
        name: str,
        out_dir: Path,
        file_stem: str,
        *,
        os_type: str = "debian12_64Guest",
    ) -> ExportResult:
        if (
            Path(file_stem).name != file_stem
            or "\\" in file_stem
            or file_stem in ("", ".", "..")
        ):
            raise PermanentError("OVA file stem must be a plain name")
        vm = self.get_vm(name)
        self._owned(name)
        if vm.power_state != POWERED_OFF:
            raise PermanentError("the base VM must be powered off before export")
        path = Path(out_dir) / f"{file_stem}.ova"
        if path.exists():
            raise AlreadyExists("an exported OVA already exists at the output path")
        argv = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "export_ova.py"),
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--user",
            self.user,
            "--vm",
            name,
            "--out",
            str(out_dir),
            "--name",
            file_stem,
            "--os-type",
            os_type,
        ]
        import os

        try:
            result = self._runner(
                argv,
                env={**os.environ, "ESXI_PASSWORD": self._password},
                capture_output=True,
                timeout=7200,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise Timeout("OVA export exceeded two hours") from None
        if result.returncode:
            raise TransientError(
                "OVA export failed; see the ESXi task and export diagnostics"
            )
        if not path.is_file() or path.stat().st_size == 0:
            raise PermanentError("OVA export produced no archive")
        return ExportResult(path, path.stat().st_size, _digest(path))

    def close(self) -> None:
        if self._conn is not None:
            from pyVim.connect import Disconnect

            self._call("disconnect", lambda: Disconnect(self._conn.connection))
            self._conn = None


@pipeline.phase(
    "P4", describe=lambda ctx: f"pull import base as {names.build_vm_name(ctx.run_id)}"
)
def deploy_base(ctx):
    vm = names.build_vm_name(ctx.run_id)
    ctx.track_datastore_folder(ctx.config.esxi.datastore, vm)
    ctx.track_vm(vm)
    info = ctx.esxi.import_ova(
        ctx.result_of("P3")["url"],
        vm,
        ctx.config.esxi.datastore,
        [ctx.config.esxi.network],
    )
    if info.folder and info.folder != vm:
        ctx.track_datastore_folder(ctx.config.esxi.datastore, info.folder)
        # Reverse-order cleanup must destroy the VM before deleting the actual
        # datastore folder ESXi chose. The earlier entry covers partial imports.
        ctx.track_vm(vm)
    ctx.esxi.set_annotation(vm, names.annotation(ctx.run_id))
    return {"vm": vm, "datastore": ctx.config.esxi.datastore}


@pipeline.phase(
    "P9",
    describe=lambda ctx: (
        "shut down the prepared base and clear all appliance guestinfo"
    ),
)
def shutdown_base(ctx):
    vm = ctx.result_of("P4")["vm"]
    try:
        ctx.esxi.shutdown_guest(vm)
    except ToolsUnavailable:
        ctx.logger.warning(
            "VMware Tools unavailable after preparation; forcing power-off"
        )
        ctx.esxi.power_off(vm)
    if not ctx.esxi.wait_powered_off(vm, 300):
        ctx.logger.warning(
            "Graceful shutdown timed out after preparation; forcing power-off"
        )
        ctx.esxi.power_off(vm)
        if not ctx.esxi.wait_powered_off(vm, 30):
            raise Timeout("the prepared base VM did not power off")
    cleared = ctx.esxi.clear_guestinfo(vm)
    if any(
        v
        for k, v in ctx.esxi.get_extra_config(vm).items()
        if k.startswith("guestinfo.ginnungagap.")
    ):
        raise PermanentError("appliance guestinfo remains on the base VM")
    return {"powered_off": True, "cleared_keys": cleared}


@pipeline.phase(
    "P10", describe=lambda ctx: f"export the powered-off base as {ctx.names.stem}.ova"
)
def export_base(ctx):
    from .phases_install import verify_ova

    vm = ctx.result_of("P4")["vm"]
    directory = ctx.run_dir / "export"
    ctx.on_cleanup(
        "export_artifact",
        str(directory),
        lambda: cleanup_export_artifacts(
            ctx.state_dir, directory, published=ctx.summary.published
        ),
    )
    directory.mkdir(mode=0o700, exist_ok=True)
    exported = ctx.esxi.export_ova(
        vm, directory, ctx.names.stem, os_type=ctx.config.image.os_type
    )
    verify_ova(exported.path)
    if (
        _digest(exported.path) != exported.sha256
        or exported.path.stat().st_size != exported.size
    ):
        raise PermanentError("exported OVA changed while validating it")
    ctx.esxi.delete_vm(vm)
    return {
        "path": str(exported.path),
        "size": exported.size,
        "sha256": exported.sha256,
    }


def cleanup_export_artifacts(
    state_dir: Path, current: Path, *, published: bool = False
) -> None:
    """Keep one recorded, verified failed OVA; a partial export never evicts evidence."""
    state_dir = Path(state_dir)
    current = Path(current)
    state = pipeline.StateFile.load(current.parent)
    if (
        state is None
        or pipeline._owned_export_path(state_dir, state, str(current)) is None
    ):
        raise NotOurs("refusing to clean an export outside the run state")
    if not any(
        record.get("kind") == "export_artifact" and record.get("ref") == str(current)
        for record in state.data.get("resources", [])
    ):
        raise NotOurs("refusing to clean an unrecorded export")
    if published:
        if current.is_dir():
            shutil.rmtree(current)
        return
    report = pipeline.JanitorReport()
    pipeline.cleanup_recorded_exports(
        state_dir,
        {str(s.data.get("run_id")): s for s in pipeline.list_runs(state_dir)},
        report,
        now=SystemClock().now(),
        failed_run=str(state.data["run_id"]),
    )
    if report.errors:
        raise PermanentError("export cleanup failed: " + "; ".join(report.errors))


def register_phases(registry):
    for pid in ("P4", "P9", "P10"):
        registered = pipeline.DEFAULT_REGISTRY.get(pid)
        registry.register(
            pid, registered.fn, describe=registered.describe, replace=True
        )
