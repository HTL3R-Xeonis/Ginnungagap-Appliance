"""Temporary guest management networking for disposable image-build VMs.

Only explicit guestinfo opts into this. Original DHCP profiles stay untouched;
the root-only /run record allows prepare to remove our exact override without
changing the live kernel address before shutdown. Standard library only.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

BACKUP = "run/ginnungagap-image-network.json"
NM_FILE = (
    "etc/NetworkManager/system-connections/05-ginnungagap-image-static.nmconnection"
)
NETWORKD_FILE = "etc/systemd/network/05-ginnungagap-image-static.network"


def validate_network(config):
    if not isinstance(config, dict) or set(config) != {
        "address",
        "prefix",
        "gateway",
        "dns",
    }:
        raise ValueError("network needs address, prefix, gateway and dns")
    if type(config["prefix"]) is not int or not 1 <= config["prefix"] <= 30:
        raise ValueError("network prefix must be between 1 and 30")
    if not isinstance(config["address"], str) or not isinstance(config["gateway"], str):
        raise TypeError("management address and gateway must be IPv4 strings")
    address = ipaddress.IPv4Address(config["address"])
    gateway = ipaddress.IPv4Address(config["gateway"])
    network = ipaddress.IPv4Network(f"{address}/{config['prefix']}", strict=False)
    if any(
        ip.is_unspecified or ip.is_multicast or ip.is_loopback or ip.is_link_local
        for ip in (address, gateway)
    ):
        raise ValueError("management address and gateway must be routable IPv4")
    if (
        gateway not in network
        or address == gateway
        or any(
            ip in (network.network_address, network.broadcast_address)
            for ip in (address, gateway)
        )
    ):
        raise ValueError(
            "management address and gateway must be different hosts in one subnet"
        )
    if not isinstance(config["dns"], list) or not 1 <= len(config["dns"]) <= 4:
        raise ValueError("network needs one to four DNS addresses")
    dns = [str(ipaddress.IPv4Address(value)) for value in config["dns"]]
    if not all(isinstance(value, str) for value in config["dns"]):
        raise ValueError("DNS addresses must be IPv4 strings")
    if any(
        ipaddress.IPv4Address(value).is_unspecified
        or ipaddress.IPv4Address(value).is_multicast
        for value in dns
    ):
        raise ValueError("invalid DNS server")
    return {
        "address": str(address),
        "prefix": config["prefix"],
        "gateway": str(gateway),
        "dns": dns,
    }


def _safe_path(root, relative, *, missing=False):
    path = root / relative
    current = root
    for part in Path(relative).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise ValueError("missing network configuration path") from None
        if (
            stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise ValueError("unsafe network configuration path")
    return path


def _atomic(path, data, mode=0o600):
    temporary = path.with_name(path.name + ".image-new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_backup(root):
    path = _safe_path(root, BACKUP, missing=True)
    if not path.exists():
        return None
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_mode & 0o077
        or info.st_size > 65536
    ):
        raise ValueError("unsafe network backup")
    value = json.loads(path.read_bytes())
    if (
        not isinstance(value, dict)
        or set(value)
        != {"schema", "config", "interface", "backend", "path", "contents"}
        or value["schema"] != 1
    ):
        raise ValueError("invalid network backup")
    if value["path"] not in (NM_FILE, NETWORKD_FILE):
        raise ValueError("invalid network backup target")
    validate_network(value["config"])
    return value


def _command(runner, args):
    result = runner(args, capture_output=True, text=True, check=False, timeout=60)
    if result.returncode:
        raise RuntimeError("guest management network command failed: " + args[0])
    return result


def apply_network(config, *, root=Path("/"), runner=subprocess.run, which=shutil.which):
    config = validate_network(config)
    root = Path(root)
    backup = _read_backup(root)
    if backup is not None:
        if backup["config"] != config:
            raise ValueError("another temporary network is already recorded")
        path = _safe_path(root, backup["path"])
        if path.read_bytes() != base64.b64decode(backup["contents"], validate=True):
            raise ValueError("temporary network override was changed")
        return {
            "address": config["address"],
            "interface": backup["interface"],
            "backend": backup["backend"],
            "changed": False,
        }
    interfaces = [
        p.name
        for p in (root / "sys/class/net").iterdir()
        if p.name != "lo" and (p / "device").exists()
    ]
    if (
        len(interfaces) != 1
        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interfaces[0]) is None
    ):
        raise ValueError("management network needs exactly one physical interface")
    interface = interfaces[0]
    if which("nmcli"):
        _command(runner, ["systemctl", "start", "NetworkManager.service"])
        backend, relative = "NetworkManager", NM_FILE
        connection = str(
            uuid.uuid5(uuid.NAMESPACE_DNS, "ginnungagap-image-static:" + interface)
        )
        contents = f"[connection]\nid=ginnungagap-image-static\nuuid={connection}\ntype=ethernet\ninterface-name={interface}\nautoconnect=true\nautoconnect-priority=999\n\n[ipv4]\nmethod=manual\naddress1={config['address']}/{config['prefix']},{config['gateway']}\ndns={';'.join(config['dns'])};\nignore-auto-dns=true\n\n[ipv6]\nmethod=auto\naddr-gen-mode=0\n"
        commands = [
            ["nmcli", "connection", "load", str(root / relative)],
            ["nmcli", "connection", "up", "uuid", connection, "ifname", interface],
        ]
    else:
        _command(
            runner, ["systemctl", "is-active", "--quiet", "systemd-networkd.service"]
        )
        backend, relative = "networkd", NETWORKD_FILE
        contents = (
            f"[Match]\nName={interface}\n\n[Network]\nDHCP=no\nAddress={config['address']}/{config['prefix']}\nGateway={config['gateway']}\n"
            + "".join(f"DNS={dns}\n" for dns in config["dns"])
        )
        commands = [["networkctl", "reload"], ["networkctl", "reconfigure", interface]]
    path = _safe_path(root, relative, missing=True)
    if path.exists() and (not path.is_file() or path.read_bytes() != contents.encode()):
        raise ValueError("refusing to replace an existing network override")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    record = {
        "schema": 1,
        "config": config,
        "interface": interface,
        "backend": backend,
        "path": relative,
        "contents": base64.b64encode(contents.encode()).decode(),
    }
    backup_path = _safe_path(root, BACKUP, missing=True)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic(backup_path, (json.dumps(record) + "\n").encode())
    try:
        if not path.exists():
            _atomic(path, contents.encode())
        for command in commands:
            _command(runner, command)
    except BaseException:
        restore_network(root=root)
        raise
    return {
        "address": config["address"],
        "interface": interface,
        "backend": backend,
        "changed": True,
    }


def restore_network(*, root=Path("/")):
    """Restore files only: retain the current live address until shutdown."""
    root = Path(root)
    backup = _read_backup(root)
    if backup is None:
        return False
    path = _safe_path(root, backup["path"], missing=True)
    if path.exists():
        if not path.is_file() or path.read_bytes() != base64.b64decode(
            backup["contents"], validate=True
        ):
            raise ValueError("refusing to remove a changed network override")
        path.unlink()
    _safe_path(root, BACKUP).unlink()
    return True


def bootstrap_script():
    """Pinned standalone bootstrap for older bases, accepting argv JSON or stdin."""
    return (
        Path(__file__).read_text()
        + "\nimport sys\nprint(json.dumps(apply_network(json.loads(sys.argv[1] if len(sys.argv)>1 else sys.stdin.read()))))\n"
    )


def restore_script():
    return Path(__file__).read_text() + "\nrestore_network()\n"
