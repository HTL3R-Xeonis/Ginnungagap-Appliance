"""
Sets the guestinfo keys of a VM that the appliance reads at its first boot (through open-vm-tools), optionally powering it on:

    ESXI_PASSWORD=... python set_guestinfo.py --host 10.20.20.202 --vm my-appliance --config ginnungagap.yml --power-on

Keys: guestinfo.ginnungagap.config (the configuration file, base64) with guestinfo.ginnungagap.config.encoding = base64,
and optionally guestinfo.ginnungagap.ssh_authorized_key and guestinfo.ginnungagap.hostname. The password of the ESXi host
is read from ESXI_PASSWORD only; values (the configuration may hold API keys) are never printed - only the key names.
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import appliance_common as common

CONFIG_KEY = "guestinfo.ginnungagap.config"
MAX_VALUE = 60000  # an ExtraConfig value must stay well below 64 KiB
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_SSH_KEY = re.compile(
    r"^(ssh-[a-z0-9]+|ecdsa-sha2-[a-z0-9]+|sk-[a-z0-9@.-]+) [A-Za-z0-9+/=]+( .*)?$"
)


def encode_config(config: bytes) -> str:
    """The configuration file as the base64 text that goes into the guestinfo key."""
    return base64.b64encode(config).decode("ascii")


def guestinfo_options(
    config: bytes,
    ssh_key: str | None = None,
    hostname: str | None = None,
    sudo_nopasswd: bool = False,
) -> dict[str, str]:
    """
    The key/value pairs to set. :raises ValueError: the config is empty or too big, the host name is not valid, or the
    SSH key is not one public key on a single line.
    """
    if not config.strip():
        raise ValueError("the configuration file is empty")
    options = {CONFIG_KEY: encode_config(config), f"{CONFIG_KEY}.encoding": "base64"}
    if len(options[CONFIG_KEY]) > MAX_VALUE:
        raise ValueError(
            f"the configuration is too big for a guestinfo value ({len(options[CONFIG_KEY])} > {MAX_VALUE} characters)"
        )
    if hostname is not None:
        if not _HOSTNAME.match(hostname):
            raise ValueError(f"{hostname!r} is not a valid host name")
        options["guestinfo.ginnungagap.hostname"] = hostname
    if ssh_key is not None:
        key = ssh_key.strip()
        if "\n" in key or not _SSH_KEY.match(key):
            raise ValueError(
                "the SSH key must be one public key on a single line (ssh-ed25519 AAAA... comment)"
            )
        options["guestinfo.ginnungagap.ssh_authorized_key"] = key
    if sudo_nopasswd:
        options["guestinfo.ginnungagap.admin_sudo_nopasswd"] = "true"
    return options


def config_spec(options: dict[str, str]) -> Any:
    """The vSphere reconfiguration that sets the keys as extra configuration."""
    from pyVmomi import vim

    return vim.vm.ConfigSpec(
        extraConfig=[
            vim.option.OptionValue(key=key, value=value)
            for key, value in options.items()
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set the guestinfo keys of a VM. The password is read from ESXI_PASSWORD only."
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--user", default="root")
    parser.add_argument("--vm", required=True)
    parser.add_argument(
        "--config", required=True, help="the ginnungagap.yml to hand to the appliance"
    )
    parser.add_argument(
        "--ssh-key-file",
        help="a public key file; installed for the admin user at first boot",
    )
    parser.add_argument("--hostname", help="host name of the appliance")
    parser.add_argument(
        "--sudo-nopasswd",
        action="store_true",
        help="let the login user use sudo without a password (for automation; the image has it off)",
    )
    parser.add_argument("--power-on", action="store_true")
    args = parser.parse_args(argv)

    try:
        key = (
            Path(args.ssh_key_file).read_text(encoding="utf-8")
            if args.ssh_key_file
            else None
        )
        options = guestinfo_options(
            Path(args.config).read_bytes(), key, args.hostname, args.sudo_nopasswd
        )
    except (OSError, ValueError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    common.password_from_env()
    instance = common.connect(args.host, args.user, args.port)
    vm = common.find_vm(instance, args.vm)
    if vm is None:
        print(f"no VM named {args.vm} on {args.host}", file=sys.stderr)
        return 1
    try:
        common.wait_for_task(vm.ReconfigVM_Task(config_spec(options)))
        print("set: " + ", ".join(sorted(options)))
        if args.power_on and str(vm.runtime.powerState) != "poweredOn":
            common.wait_for_task(vm.PowerOnVM_Task())
            print(f"{args.vm} powered on")
    except RuntimeError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
