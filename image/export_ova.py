"""
Exports a powered-off VM from an ESXi host as an .ova (OVF + SHA-256 manifest + streamOptimized VMDKs in a ustar tar).

    ESXI_PASSWORD=... python export_ova.py --host 10.20.20.202 --user root --vm ginnungagap-test-debian --out /data/ova

The password is only read from ESXI_PASSWORD. The network name inside the OVF is replaced by --network-name (default
"Network") so whoever imports it can map it to any port group.

Everything that does not need an ESXi is a plain function (OVF rewrite, manifest, tar packing, space check, download loop,
lease keep-alive) so it can be tested without one.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Self
from urllib.parse import unquote, urlparse
from xml.sax.saxutils import escape, unescape

import appliance_common as common

CHUNK_SIZE = 1024 * 1024
KEEPALIVE_SECONDS = (
    10.0  # an NFC lease that gets no progress report for a few minutes expires
)
LEASE_READY_TIMEOUT = 120.0
SAFETY_MARGIN = 1 << 30
USTAR_MAX_MEMBER = (1 << 33) - 1  # a ustar size field holds 11 octal digits
USTAR_MAX_NAME = 100

_QUOTES = {'"': "&quot;"}
_NETWORK_ELEMENT = re.compile(r'(<Network\b[^>]*\bovf:name=")([^"]*)(")')
_CONNECTION_ELEMENT = re.compile(
    r"(<rasd:Connection\b[^>]*>)([^<]*)(</rasd:Connection>)"
)


class LeaseError(RuntimeError):
    """The export lease did not become ready or failed."""


# ---------- OVF ----------


def network_names(ovf: str) -> list[str]:
    """The networks the descriptor names, in order of appearance, without duplicates."""
    found: list[str] = []
    for pattern in (_NETWORK_ELEMENT, _CONNECTION_ELEMENT):
        for match in pattern.finditer(ovf):
            name = unescape(match.group(2), {"&quot;": '"'})
            if name and name not in found:
                found.append(name)
    return found


def rewrite_networks(ovf: str, network_name: str) -> str:
    """
    Replaces the network names of the descriptor: the first one becomes `network_name`, a second one (rare) `<name>-2`, and so
    on. Covers the <Network> declarations, every <rasd:Connection> and the quoted name in the adapters' descriptions.
    """
    names = network_names(ovf)
    mapping = {
        old: network_name if index == 0 else f"{network_name}-{index + 1}"
        for index, old in enumerate(names)
    }

    def swap(match: re.Match[str]) -> str:
        old = unescape(match.group(2), {"&quot;": '"'})
        return match.group(1) + escape(mapping.get(old, old), _QUOTES) + match.group(3)

    ovf = _NETWORK_ELEMENT.sub(swap, ovf)
    ovf = _CONNECTION_ELEMENT.sub(swap, ovf)
    for old, new in mapping.items():
        ovf = ovf.replace(
            f"&quot;{escape(old, _QUOTES)}&quot;", f"&quot;{escape(new, _QUOTES)}&quot;"
        )
        ovf = ovf.replace(
            f"<Description>The {escape(old)} network</Description>",
            f"<Description>The {escape(new)} network</Description>",
        )
    return ovf


_OS_TYPE = re.compile(r'(vmw:osType=")[^"]*(")')


def rewrite_os_type(ovf: str, os_type: str) -> str:
    """
    Sets the guest operating system the descriptor declares (vmw:osType, e.g. debian12_64Guest). A VM that was imported
    without it has the generic type; the host then does not know which virtual hardware suits the guest.
    """
    if not re.fullmatch(r"[A-Za-z0-9_]+", os_type):
        raise ValueError(f"{os_type!r} is not a guest operating system id")
    return _OS_TYPE.sub(lambda match: match.group(1) + os_type + match.group(2), ovf)


# Resource settings a host managed by a vCenter refuses to take over from a descriptor ("access to resource settings on the host
# is restricted to the server that is managing it"): CPU/memory shares, reservations and limits. Not needed for an appliance.
_RESOURCE_CONFIG = re.compile(
    r'[ \t]*<vmw:Config\b[^>]*vmw:key="(?:cpuAllocation|memoryAllocation|sched)\.[^"]*"[^>]*/>[ \t]*\r?\n?'
)


def strip_resource_settings(ovf: str) -> str:
    """Removes the CPU/memory allocation settings (shares, reservation, limit) from the descriptor."""
    return _RESOURCE_CONFIG.sub("", ovf)


def referenced_files(ovf: str, names: Sequence[str]) -> list[str]:
    """Those of the downloaded files the descriptor actually refers to (ovf:href), in the given order."""
    hrefs = {
        unescape(m, {"&quot;": '"'}) for m in re.findall(r'ovf:href="([^"]*)"', ovf)
    }
    return [name for name in names if name in hrefs]


# ---------- manifest and tar ----------


def manifest_text(files: Sequence[Path]) -> str:
    """The SHA-256 manifest (<name>.mf): one `SHA256(file)= hex` line per file."""
    return "".join(f"SHA256({path.name})= {common.sha256_of(path)}\n" for path in files)


def pack_ova(ova: Path, members: Sequence[Path]) -> None:
    """
    Writes the members into a ustar tar. The descriptor (.ovf) has to be the first member, as the OVF standard requires; names
    must be plain ASCII of at most 100 characters and no member may be 8 GiB or more (limits of the ustar format).
    """
    if not members or members[0].suffix != ".ovf":
        raise ValueError("the OVF descriptor must be the first member of an OVA")
    for path in members:
        if len(path.name) > USTAR_MAX_NAME or not path.name.isascii():
            raise ValueError(
                f"{path.name}: a ustar member name is ASCII and at most 100 characters"
            )
        if path.stat().st_size > USTAR_MAX_MEMBER:
            raise ValueError(
                f"{path.name} is 8 GiB or more, too big for a ustar member"
            )
    with tarfile.open(ova, "w", format=tarfile.USTAR_FORMAT) as archive:
        for path in members:
            info = archive.gettarinfo(str(path), arcname=path.name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            with path.open("rb") as handle:
                archive.addfile(info, handle)


# ---------- space ----------


def repack_ova(
    source: Path,
    target: Path,
    os_type: str | None = None,
    network_name: str | None = None,
) -> None:
    """
    Corrects an OVA that was exported earlier, without a host: takes the descriptor apart, strips the resource settings (and
    sets the guest type / network name when given), writes a new manifest and packs everything into `target`.
    """
    if source.resolve() == target.resolve():
        raise ValueError("the corrected OVA must be written to another file")
    with tempfile.TemporaryDirectory(dir=target.parent) as folder:
        work = Path(folder)
        with tarfile.open(source) as archive:
            names = [m.name for m in archive.getmembers() if m.isfile()]
            archive.extractall(work, filter="data")
        ovf_name = next((n for n in names if n.endswith(".ovf")), None)
        if ovf_name is None:
            raise ValueError(f"{source.name} has no .ovf descriptor")
        text = strip_resource_settings((work / ovf_name).read_text(encoding="utf-8"))
        if network_name:
            text = rewrite_networks(text, network_name)
        if os_type:
            text = rewrite_os_type(text, os_type)
        (work / ovf_name).write_text(text, encoding="utf-8")
        others = [n for n in names if not n.endswith((".ovf", ".mf"))]
        manifest = work / (Path(ovf_name).stem + ".mf")
        manifest.write_text(
            manifest_text([work / ovf_name, *[work / n for n in others]]),
            encoding="utf-8",
        )
        pack_ova(target, [work / ovf_name, manifest, *[work / n for n in others]])


def estimate_needed(committed_bytes: int) -> int:
    """Disk space the export needs: the VMDK files plus the OVA made from them (about twice what the VM occupies) and a margin."""
    return 2 * committed_bytes + SAFETY_MARGIN


def check_free_space(
    directory: Path,
    needed: int,
    usage: Callable[[Path], Any] = shutil.disk_usage,
) -> None:
    """:raises OSError: less than `needed` bytes are free where the export is written."""
    free = usage(directory).free
    if free < needed:
        raise OSError(
            f"not enough free space in {directory}: {free / 2**30:.1f} GiB free, about {needed / 2**30:.1f} GiB needed"
        )


# ---------- lease ----------


def wait_for_lease(
    lease: Any,
    timeout: float = LEASE_READY_TIMEOUT,
    poll_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Waits until the export lease is ready. :raises LeaseError: it failed, ended or did not become ready in time."""
    deadline = clock() + timeout
    while True:
        state = str(lease.state)
        if state == "ready":
            return
        if state == "error":
            fault = getattr(lease, "error", None)
            raise LeaseError(
                getattr(fault, "msg", None)
                or getattr(fault, "localizedMessage", None)
                or "the export lease failed"
            )
        if state != "initializing":
            raise LeaseError(f"the export lease is {state}")
        if clock() >= deadline:
            raise LeaseError("the export lease did not become ready in time")
        sleep(poll_seconds)


class LeaseKeeper:
    """Reports progress to the lease every few seconds from a thread, so a long download does not let it expire."""

    def __init__(self, lease: Any, interval: float = KEEPALIVE_SECONDS) -> None:
        self._lease = lease
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._percent = 0
        self.failure: Exception | None = None

    def set_percent(self, percent: int) -> None:
        self._percent = max(0, min(99, int(percent)))

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._lease.HttpNfcLeaseProgress(self._percent)
            except Exception as exc:  # noqa: BLE001 - the download goes on; the failure is reported at the end
                self.failure = exc

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


# ---------- download ----------


def target_url(url: str, host: str) -> str:
    """The lease URLs name the host as `*`: it stands for the ESXi host that was connected to."""
    return url.replace("*", host)


def local_name(name: str, device_url: Any, disk_index: int) -> str:
    """The file name a lease item is stored under: <name>-disk<N>.vmdk for a disk, the URL's own file name for anything else."""
    if getattr(device_url, "disk", False):
        return f"{name}-disk{disk_index}.vmdk"
    base = unquote(Path(urlparse(device_url.url).path).name)
    return re.sub(r"[^A-Za-z0-9._-]", "_", base) or f"{name}-file{disk_index}"


def download_stream(
    response: Any,
    destination: Path,
    chunk_size: int = CHUNK_SIZE,
    on_bytes: Callable[[int], None] | None = None,
    expected: int | None = None,
) -> int:
    """
    Copies a response (anything with .read(n)) to a file in chunks and returns the bytes written.
    :raises OSError: fewer or more bytes than `expected` arrived.
    """
    written = 0
    with destination.open("wb") as handle:
        while True:
            block = response.read(chunk_size)
            if not block:
                break
            handle.write(block)
            written += len(block)
            if on_bytes is not None:
                on_bytes(len(block))
    if expected is not None and written != expected:
        raise OSError(
            f"{destination.name}: {written} bytes received, {expected} expected"
        )
    return written


def open_unverified(url: str) -> Any:
    """Opens an https URL on the ESXi host (its certificate is not verified, as everywhere in this project)."""
    import ssl

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return urllib.request.urlopen(url, context=context, timeout=300)


# ---------- the export ----------


def percent_done(done: int, total: int) -> int:
    return 0 if total <= 0 else min(99, done * 100 // total)


def descriptor_for(
    ovf_manager: Any, vm: Any, name: str, files: Sequence[tuple[str, str, int]]
) -> str:
    """
    Asks the ESXi host for the OVF descriptor of the VM. `files` are (device key, file name, size in bytes).
    :raises RuntimeError: the host reports an error.
    """
    from pyVmomi import vim

    params = vim.OvfManager.CreateDescriptorParams(
        name=name,
        ovfFiles=[
            vim.OvfManager.OvfFile(deviceId=key, path=path, size=size)
            for key, path, size in files
        ],
    )
    result = ovf_manager.CreateDescriptor(obj=vm, cdp=params)
    errors = [
        getattr(e, "localizedMessage", None) or str(e) for e in (result.error or [])
    ]
    if errors:
        raise RuntimeError(
            "the OVF descriptor could not be created: " + "; ".join(errors)
        )
    return result.ovfDescriptor


def export_vm(
    instance: Any,
    vm: Any,
    host: str,
    out_dir: Path,
    name: str,
    network_name: str,
    chunk_size: int = CHUNK_SIZE,
    os_type: str | None = None,
    log: Callable[[str], None] = print,
    opener: Callable[[str], Any] = open_unverified,
) -> Path:
    """Exports the powered-off VM into out_dir/<name>.ova and returns that path. Temporary files are removed again."""
    if str(vm.runtime.powerState) != "poweredOff":
        raise RuntimeError(f"{vm.name} is {vm.runtime.powerState}; shut it down first")
    out_dir.mkdir(parents=True, exist_ok=True)
    committed = int(vm.summary.storage.committed)
    check_free_space(out_dir, estimate_needed(committed))

    work = out_dir / f".{name}.export"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    ova = out_dir / f"{name}.ova"
    lease = vm.ExportVm()
    try:
        wait_for_lease(lease)
        items = list(lease.info.deviceUrl)
        total = (
            sum(int(getattr(item, "fileSize", 0) or 0) for item in items) or committed
        )
        done = 0
        downloaded: list[tuple[str, str, int]] = []
        disk_index = 0
        with LeaseKeeper(lease) as keeper:

            def progress(count: int) -> None:
                nonlocal done
                done += count
                keeper.set_percent(percent_done(done, total))

            for item in items:
                file_name = local_name(name, item, disk_index)
                disk_index += 1 if getattr(item, "disk", False) else 0
                log(f"downloading {file_name}")
                with opener(target_url(item.url, host)) as response:
                    size = download_stream(
                        response, work / file_name, chunk_size, progress
                    )
                downloaded.append((item.key, file_name, size))
        if keeper.failure is not None:
            log(
                f"note: a lease keep-alive failed once ({keeper.failure}); the download itself went through"
            )

        descriptor = descriptor_for(
            instance.RetrieveContent().ovfManager, vm, name, downloaded
        )
        ovf_text = strip_resource_settings(rewrite_networks(descriptor, network_name))
        if os_type:
            ovf_text = rewrite_os_type(ovf_text, os_type)
        used = referenced_files(ovf_text, [path for _, path, _ in downloaded])
        for _, path, _ in downloaded:
            if path not in used:
                log(f"note: {path} is not part of the descriptor and is left out")
        ovf = work / f"{name}.ovf"
        ovf.write_text(ovf_text, encoding="utf-8")
        members = [ovf]
        manifest = work / f"{name}.mf"
        manifest.write_text(
            manifest_text([ovf, *[work / path for path in used]]), encoding="utf-8"
        )
        members += [manifest, *[work / path for path in used]]
        pack_ova(ova, members)
        lease.HttpNfcLeaseComplete()
    except BaseException:
        with contextlib.suppress(
            Exception
        ):  # the original error is the one worth reporting
            lease.HttpNfcLeaseAbort()
        ova.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return ova


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export a powered-off VM from ESXi as an OVA. The password is read from ESXI_PASSWORD only."
    )
    parser.add_argument(
        "--repack",
        metavar="OVA",
        help="correct an OVA exported earlier instead of exporting (no host needed): writes <name>.ova into --out",
    )
    parser.add_argument("--host", help="ESXi host (address)")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--user", default="root")
    parser.add_argument("--vm", help="name of the powered-off VM")
    parser.add_argument("--out", required=True, help="folder for the .ova")
    parser.add_argument(
        "--name", default="Ginnungagap_Appliance", help="name of the OVF/OVA files"
    )
    parser.add_argument(
        "--network-name", default="Network", help="network name written into the OVF"
    )
    parser.add_argument(
        "--os-type",
        help="guest operating system id for the OVF (vmw:osType), e.g. debian12_64Guest; default: as the VM has it",
    )
    args = parser.parse_args(argv)

    if args.repack:
        target = Path(args.out) / f"{args.name}.ova"
        try:
            repack_ova(Path(args.repack), target, args.os_type)
        except (OSError, ValueError, tarfile.TarError) as exc:
            print(f"repack failed: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {target} ({target.stat().st_size} bytes)")
        print(f"sha256 {common.sha256_of(target)}")
        return 0
    if not args.host or not args.vm:
        parser.error("--host and --vm are needed (unless --repack is given)")

    common.password_from_env()  # fails early, before any connection
    instance = common.connect(args.host, args.user, args.port)
    vm = common.find_vm(instance, args.vm)
    if vm is None:
        print(f"no VM named {args.vm} on {args.host}", file=sys.stderr)
        return 1
    try:
        ova = export_vm(
            instance,
            vm,
            args.host,
            Path(args.out),
            args.name,
            args.network_name,
            os_type=args.os_type,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {ova} ({ova.stat().st_size} bytes)")
    print(f"sha256 {common.sha256_of(ova)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
