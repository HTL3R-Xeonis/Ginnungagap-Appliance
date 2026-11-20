"""
Every name the image build creates or recognises, in one place (plan section 8). Pure functions, no I/O.

    for_version("1.3.0")  ->  template  ginnungagap-appliance-v1.3.0
                              folder    Ginnungagap_Appliance_v1.3.0
                              stem      Ginnungagap_Appliance_v1.3.0     (store file: <stem>_<64 CAPITAL HEX>.ova, <stem>.yml)

    run id            <version>-<UTC stamp>             1.3.0-20261120T101500Z
    base VM           imgbuild-v<version>-<stamp>       imgbuild-v1.3.0-20261120T101500Z
    acceptance VM     imgaccept-v<version>-<stamp>-<a|b|c>
    annotation        ginnungagap-image-build run=<run id>   (how the janitor attributes a VM to a run)

The version must match `^\\d+\\.\\d+\\.\\d+(-rc\\.\\d+)?$`; a revision suffix like `-r2` (the one hand-made name of the past) is refused.
Everything the janitor may delete starts with `imgbuild-` or `imgaccept-` (`is_ours_vm`, `is_ours_folder`): nothing else.
Every template name starts with `ginnungagap-appliance-` (the Operator hides exactly that prefix, default list only).

NEVER OVERWRITE: `check_free` raises AlreadyExists for a template name, share folder or VM name that exists already; the
old store entries (`ginnungagap-appliance`, `-v1.0.0`, `-v1.0.0-r2`, `-v1.1.0`, `-v1.2.0`, `-v1.2.1`) are never touched.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

from .ports import AlreadyExists, PermanentError

VERSION = re.compile(r"\d+\.\d+\.\d+(-rc\.\d+)?", re.ASCII)
STAMP = re.compile(r"\d{8}T\d{6}Z", re.ASCII)
RUN_ID = re.compile(
    r"(?P<version>\d+\.\d+\.\d+(?:-rc\.\d+)?)-(?P<stamp>\d{8}T\d{6}Z)", re.ASCII
)
SHA256_CAPITALS = re.compile(r"[0-9A-F]{64}", re.ASCII)
STORE_FILE = re.compile(r"(?P<stem>.+)_(?P<sha>[0-9A-F]{64})\.ova", re.ASCII)

TEMPLATE_PREFIX = "ginnungagap-appliance-"
FOLDER_PREFIX = "Ginnungagap_Appliance_v"
BUILD_VM_PREFIX = "imgbuild-"
ACCEPT_VM_PREFIX = "imgaccept-"
OUR_PREFIXES = (BUILD_VM_PREFIX, ACCEPT_VM_PREFIX)
ANNOTATION_PREFIX = "ginnungagap-image-build run="
RUN_MARKER = (
    ".imgbuild-run"  # the file in a staged share folder that proves which run made it
)
ACCEPTANCE_BOOTS = ("a", "b", "c")

_OUR_VM = re.compile(r"(imgbuild|imgaccept)-.+", re.ASCII)
_OUR_FOLDER = re.compile(
    r"(imgbuild|imgaccept)-.+?(_\d+)?", re.ASCII
)  # `<vm>` or the `<vm>_<n>` ESXi picks when the name is taken


class InvalidVersion(PermanentError, ValueError):
    """The version is not `MAJOR.MINOR.PATCH` or `MAJOR.MINOR.PATCH-rc.N`."""


@dataclass(frozen=True)
class ImageNames:
    version: str
    template: str  # ginnungagap-appliance-v<version>
    folder: str  # Ginnungagap_Appliance_v<version>   (folder on the share)
    stem: str  # Ginnungagap_Appliance_v<version>   (OVA stem and .yml name)

    @property
    def yml(self) -> str:
        """`<stem>.yml`, the file `describe_template.py --write` makes and the collector lists the template by."""
        return f"{self.stem}.yml"

    def ova(self, sha256: str) -> str:
        """`<stem>_<SHA-256 in capitals>.ova` (the store's rule)."""
        return store_file_name(self.stem, sha256)


def check_version(version: str) -> str:
    """The version unchanged. :raises InvalidVersion: it is not `1.3.0` / `1.3.0-rc.1` (no `v`, no `-r2`, no spaces)."""
    if not isinstance(version, str) or VERSION.fullmatch(version) is None:
        raise InvalidVersion(
            f"{str(version)[:40]!r} is not a version like 1.3.0 or 1.3.0-rc.1 (a revision suffix such as -r2 is not allowed)"
        )
    return version


def for_version(version: str) -> ImageNames:
    """Template, folder and stem of an image of this appliance release. :raises InvalidVersion:"""
    check_version(version)
    return ImageNames(
        version=version,
        template=f"{TEMPLATE_PREFIX}v{version}",
        folder=f"{FOLDER_PREFIX}{version}",
        stem=f"{FOLDER_PREFIX}{version}",
    )


def store_file_name(stem: str, sha256: str) -> str:
    """`<stem>_<SHA-256 in capitals>.ova`. :raises ValueError: the hash is not 64 hex digits."""
    if not re.fullmatch(r"[0-9A-Fa-f]{64}", sha256 or ""):
        raise ValueError("the SHA-256 must be 64 hex digits")
    return f"{stem}_{sha256.upper()}.ova"


def parse_store_file_name(name: str) -> tuple[str, str] | None:
    """(stem, SHA-256 in capitals) of a store file name, None when it does not follow the rule."""
    match = STORE_FILE.fullmatch(name)
    return (match.group("stem"), match.group("sha")) if match else None


# ---------- runs, VMs, annotation ----------


def make_stamp(now: datetime) -> str:
    """`20261120T101500Z` (UTC, seconds)."""
    if now.tzinfo is None:
        raise ValueError("the time must be timezone-aware")
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def run_id(version: str, now: datetime) -> str:
    """`<version>-<UTC stamp>`."""
    return f"{check_version(version)}-{make_stamp(now)}"


def parse_run_id(value: str) -> tuple[str, str] | None:
    """(version, stamp) of a run id, None when it is not one."""
    match = RUN_ID.fullmatch(value or "")
    return (match.group("version"), match.group("stamp")) if match else None


def _stamp_of(run: str) -> tuple[str, str]:
    parsed = parse_run_id(run)
    if parsed is None:
        raise ValueError(f"{run[:60]!r} is not a run id like 1.3.0-20261120T101500Z")
    return parsed


def build_vm_name(run: str) -> str:
    """`imgbuild-v<version>-<stamp>` for run id `<version>-<stamp>`."""
    version, stamp = _stamp_of(run)
    return f"{BUILD_VM_PREFIX}v{version}-{stamp}"


def accept_vm_name(run: str, boot: str) -> str:
    """`imgaccept-v<version>-<stamp>-<a|b|c>`."""
    if boot not in ACCEPTANCE_BOOTS:
        raise ValueError(f"the boot is one of {', '.join(ACCEPTANCE_BOOTS)}")
    version, stamp = _stamp_of(run)
    return f"{ACCEPT_VM_PREFIX}v{version}-{stamp}-{boot}"


def annotation(run: str) -> str:
    """The notes text of a build VM; no secret, one line."""
    _stamp_of(run)
    return f"{ANNOTATION_PREFIX}{run}"


def parse_annotation(text: str | None) -> str | None:
    """The run id named by a VM's notes, None when the notes are not ours or the id is not valid."""
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(ANNOTATION_PREFIX):
            candidate = line[len(ANNOTATION_PREFIX) :].split()[0:1]
            if candidate and parse_run_id(candidate[0]):
                return candidate[0]
    return None


def is_ours_vm(name: str) -> bool:
    """True only for names the janitor may delete: they start with `imgbuild-` or `imgaccept-` and have something after it."""
    return _OUR_VM.fullmatch(name or "") is not None


def is_ours_folder(name: str) -> bool:
    """True for a datastore folder of one of our VMs (`imgbuild-...` or the numbered `imgbuild-..._1`)."""
    return _OUR_FOLDER.fullmatch(name or "") is not None


# ---------- never overwrite ----------


def check_free(
    names: ImageNames,
    *,
    template_names: Iterable[str] = (),
    share_folders: Iterable[str] = (),
    vm_names: Iterable[str] = (),
    run: str | None = None,
) -> None:
    """
    P1's rule: the template, the share folder and (with `run`) the VM names of this build must not exist yet.
    :raises AlreadyExists: names the first thing that is taken; a human resolves it, the pipeline never deletes or replaces it.
    """
    if names.template in set(template_names):
        raise AlreadyExists(
            f"the template {names.template} is already in the store (a rebuild of the same release is refused: "
            "make a new patch release instead)"
        )
    if names.folder in set(share_folders):
        raise AlreadyExists(
            f"the folder {names.folder} already exists on the share (never overwritten; a human must look at it)"
        )
    if run is not None:
        taken = set(vm_names)
        wanted = [build_vm_name(run)] + [
            accept_vm_name(run, b) for b in ACCEPTANCE_BOOTS
        ]
        clash = next((name for name in wanted if name in taken), None)
        if clash:
            raise AlreadyExists(f"a VM named {clash} exists already")


def check_template_name(name: str) -> str:
    """The name unchanged. :raises ValueError: it does not start with `ginnungagap-appliance-` (the Operator would list it in the palettes)."""
    if not name.startswith(TEMPLATE_PREFIX):
        raise ValueError(f"a template name must start with {TEMPLATE_PREFIX}")
    return name


# ---------- URLs ----------


def ova_url(base_url: str, source_dir: str, file_name: str) -> str:
    """
    `<base>/<source dir>/<file>` with every part percent-encoded: the same rule as `pull_importer.ova_url` of TopologyBuilder, so
    the URL the importer gets is the one the template collector would produce.
    """
    parts = [
        quote(part, safe="") for part in (*source_dir.split("/"), file_name) if part
    ]
    return f"{base_url.rstrip('/')}/{'/'.join(parts)}"
