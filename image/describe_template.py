"""
Writes the description of an .ova in the format of the lab's template store, so the ESXi template API lists it:

    python describe_template.py Ginnungagap_Appliance.ova --name ginnungagap-appliance --cpu 2 --memory 4 --disk 20 --rename --write

The store's rule for the file name is <NAME>_<SHA-256 in capitals>.ova; `--rename` renames the .ova accordingly, and the YAML
refers to it. No password is written: `note` carries free text (by default that the login has to be changed).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import appliance_common as common
import yaml

DEFAULT_NOTE = "Change the login before use; the SSH key and the host name can be set with the guestinfo.ginnungagap.* keys."
_HASHED_NAME = re.compile(r"^(?P<base>.+)_(?P<hash>[0-9A-Fa-f]{64})$")


class _IndentedDumper(yaml.SafeDumper):
    """Indents list items below their key, as the files in the store do."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def store_file_name(stem: str, sha256: str) -> str:
    """<NAME>_<SHA-256 in capitals>.ova; a hash that is already part of the name is replaced, not added twice."""
    match = _HASHED_NAME.match(stem)
    base = match.group("base") if match else stem
    return f"{base}_{sha256.upper()}.ova"


def size_text(size_bytes: int) -> str:
    """The size as the store writes it: GiB with two decimals and a comma, e.g. 4,57GB."""
    gib = round(size_bytes / 2**30, 2)
    return f"{gib:.2f}".replace(".", ",") + "GB"


def build_template(
    sha256: str,
    file_name: str,
    size_bytes: int,
    *,
    name: str,
    category: str,
    tags: Sequence[str],
    os_family: str,
    os_version: str,
    cpu: int,
    memory_gb: int,
    disk_gb: int,
    users: Sequence[str],
    hostname: str,
    domain: str | None,
    note: str,
) -> dict[str, Any]:
    """One template entry, with the keys the template collector requires (id, name, file, category, os, cpu, memory, disk, hostname)."""
    return {
        "id": sha256.upper(),
        "name": name,
        "file": file_name,
        "size": size_text(size_bytes),
        "category": category,
        "tags": list(tags),
        "os": {"family": os_family, "version": os_version},
        "cpu": cpu,
        "memory": f"{memory_gb}GB",
        "disk": f"{disk_gb}GB",
        "users": list(users),
        "hostname": hostname,
        "domain": domain,
        "note": note,
    }


def render_yaml(template: dict[str, Any]) -> str:
    """The description file's text."""
    return yaml.dump(
        {"version": 1, "templates": [template]},
        Dumper=_IndentedDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1000,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Describe an .ova for the lab's template store."
    )
    parser.add_argument("ova", help="the .ova file")
    parser.add_argument(
        "--name",
        default="ginnungagap-appliance",
        help="template name the apps look it up by",
    )
    parser.add_argument("--category", default="Appliance")
    parser.add_argument(
        "--tag",
        action="append",
        dest="tags",
        help="repeatable (default: appliance, linux, server)",
    )
    parser.add_argument("--os-family", default="Linux")
    parser.add_argument("--os-version", default="Debian 13 (trixie)")
    parser.add_argument("--cpu", type=int, default=2)
    parser.add_argument("--memory", type=int, default=4, help="GB")
    parser.add_argument("--disk", type=int, default=20, help="GB")
    parser.add_argument(
        "--user",
        action="append",
        dest="users",
        help="repeatable (default: junioradmin)",
    )
    parser.add_argument("--hostname", default="ginnungagap")
    parser.add_argument("--domain", default=None)
    parser.add_argument(
        "--note", default=DEFAULT_NOTE, help="free text; do not put a password in it"
    )
    parser.add_argument(
        "--rename", action="store_true", help="rename the .ova to <NAME>_<SHA256>.ova"
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write <NAME>.yml next to the .ova instead of printing",
    )
    args = parser.parse_args(argv)

    ova = Path(args.ova)
    if not ova.is_file():
        print(f"{ova} is not a file", file=sys.stderr)
        return 1
    sha256 = common.sha256_of(ova)
    wanted = store_file_name(ova.stem, sha256)
    if args.rename and ova.name != wanted:
        ova = ova.rename(ova.with_name(wanted))
    template = build_template(
        sha256,
        wanted,
        ova.stat().st_size,
        name=args.name,
        category=args.category,
        tags=args.tags or ["appliance", "linux", "server"],
        os_family=args.os_family,
        os_version=args.os_version,
        cpu=args.cpu,
        memory_gb=args.memory,
        disk_gb=args.disk,
        users=args.users or ["junioradmin"],
        hostname=args.hostname,
        domain=args.domain,
        note=args.note,
    )
    text = render_yaml(template)
    if args.write:
        target = ova.with_name(_HASHED_NAME.sub(r"\g<base>", ova.stem) + ".yml")
        target.write_text(text, encoding="utf-8")
        print(f"wrote {target}")
    else:
        print(text, end="")
    if ova.name != wanted:
        print(
            f"note: the store expects the file to be named {wanted} (use --rename)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
