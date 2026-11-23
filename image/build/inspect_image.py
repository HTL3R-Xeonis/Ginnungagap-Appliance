"""Fail-closed inspection of a prepared filesystem and raw disk.

The root-owned helper invokes this file with Python's isolated mode.
Inspection therefore uses only the standard library and never imports
code from an exported image or an unprivileged run directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tarfile
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CONFIG_MARKER = "imgbuild-marker-3c9e1f7a"
RUNS_ROOT = Path("/var/lib/ginnungagap-image-build/runs")
# Kept in sync with ginnungagap.image_network by a regression; the root helper
# remains a standalone, standard-library-only script under Python -I.
TEMPORARY_NETWORK_FILES = (
    "etc/NetworkManager/system-connections/05-ginnungagap-image-static.nmconnection",
    "etc/systemd/network/05-ginnungagap-image-static.network",
    "run/ginnungagap-image-network.json",
)
_TOKEN = re.compile(rb"(?:ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,})")
_PRIVATE_KEY = re.compile(
    rb"-----BEGIN (?P<label>(?:OPENSSH |RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY)-----[\r\n]+"
    rb"(?:[A-Za-z0-9+/=\r\n]{32,}|Proc-Type:[ ]*4,ENCRYPTED)"
)
_COMPLETE_PRIVATE_KEY = re.compile(
    rb"-----BEGIN (?P<label>(?:OPENSSH |RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY)-----\r?\n"
    rb"(?:Proc-Type:[ ]*4,ENCRYPTED\r?\nDEK-Info:[ ]*[A-Z0-9-]+,[A-Fa-f0-9]+\r?\n\r?\n)?"
    rb"(?:[A-Za-z0-9+/=]+\r?\n)+-----END (?P=label)-----"
)
_RAW_CHUNK = 1024 * 1024
_MAX_PUBLIC_PEM = 16384


@lru_cache(maxsize=1)
def _public_key_policy():
    """Pinned public vectors, never a policy supplied by an inspected image."""
    path = Path(__file__).with_name("public-test-keys.json")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        raise ValueError("invalid pinned public-key policy")
    data = json.loads(path.read_bytes())
    entries = data.get("files")
    if data.get("schema") != 1 or not isinstance(entries, list) or not entries:
        raise ValueError("invalid pinned public-key policy")
    for entry in entries:
        relative = entry.get("path", "")
        if (
            not relative.startswith("usr/")
            or Path(relative).as_posix() != relative
            or ".." in Path(relative).parts
            or not re.fullmatch("[0-9a-f]{64}", entry.get("sha256", ""))
            or not isinstance(entry.get("key_sha256"), list)
            or not entry["key_sha256"]
            or not all(
                re.fullmatch("[0-9a-f]{64}", value) for value in entry["key_sha256"]
            )
        ):
            raise ValueError("invalid pinned public-key policy entry")
    return entries


def _vendor_public_file(relative, digest):
    # A public test vector used as a machine identity is still forbidden.
    layer = re.fullmatch(
        r"var/lib/docker/overlay2/[A-Za-z0-9_-]{1,128}/diff/(usr/.+)", relative
    )
    candidate = layer.group(1) if layer else relative
    return any(
        candidate == entry["path"] and digest == entry["sha256"]
        for entry in _public_key_policy()
    )


class _PrivateKeyScanner:
    """Bounded streaming whole-PEM comparison; unknown/truncated data fails."""

    def __init__(self):
        self.known = frozenset(
            key for entry in _public_key_policy() for key in entry["key_sha256"]
        )
        self.buffer = b""
        self.position = 0
        self.saw_private = False
        self.unknown = False
        self.offsets = set()

    def _discard(self, count):
        self.buffer = self.buffer[count:]
        self.position += count

    def _reject(self, offset):
        self.unknown = True
        if len(self.offsets) < 16:
            self.offsets.add(offset)

    def feed(self, data, *, final=False):
        self.buffer += data
        while True:
            match = _PRIVATE_KEY.search(self.buffer)
            if match is None:
                keep = min(len(self.buffer), 1024) if not final else 0
                self._discard(len(self.buffer) - keep)
                return
            self.saw_private = True
            footer = b"-----END " + match["label"] + b"-----"
            end = self.buffer.find(footer, match.end())
            if end < 0:
                if not final and len(self.buffer) - match.start() <= _MAX_PUBLIC_PEM:
                    self._discard(match.start())
                    return
                self._reject(self.position + match.start())
                self._discard(match.end())
                continue
            stop = end + len(footer)
            pem = self.buffer[match.start() : stop]
            if (
                len(pem) > _MAX_PUBLIC_PEM
                or _COMPLETE_PRIVATE_KEY.fullmatch(pem) is None
                or hashlib.sha256(pem.replace(b"\r\n", b"\n")).hexdigest()
                not in self.known
            ):
                self._reject(self.position + match.start())
            self._discard(stop)


def row(name: str, passed: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "required": True, "detail": detail}


def _files(root: Path):
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in dirs:
            if (base / name).is_symlink():
                yield base / name
        dirs[:] = [name for name in dirs if not (base / name).is_symlink()]
        for name in files:
            yield base / name


def _read(root: Path, relative: str) -> bytes | None:
    current = root
    for component in Path(relative).parts:
        current /= component
        if current.is_symlink():
            return None
    try:
        return (root / relative).read_bytes()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None


def _nonempty(root: Path, relative: str) -> bool:
    path = root / relative
    if path.is_symlink():
        return True
    if not path.exists():
        return False
    if path.is_dir():
        return any(path.iterdir())
    return path.stat().st_size != 0


def repository_tags(data: Any) -> set[str]:
    """Read Docker's classic tag index; unknown formats fail closed."""
    if not isinstance(data, dict) or not isinstance(data.get("Repositories"), dict):
        raise TypeError("Docker repository index has an unsupported structure")
    result: set[str] = set()
    for repository, tags in data["Repositories"].items():
        if not isinstance(repository, str) or not isinstance(tags, dict):
            raise TypeError("Docker repository index has an unsupported structure")
        tagged_ids = {
            image_id
            for tag, image_id in tags.items()
            if isinstance(tag, str)
            and tag.startswith(repository + ":")
            and isinstance(image_id, str)
            and image_id
        }
        for tag, image_id in tags.items():
            if not isinstance(tag, str) or not (
                tag.startswith((repository + ":", repository + "@"))
            ):
                raise ValueError("Docker repository index contains an invalid tag")
            if not isinstance(image_id, str) or not image_id:
                raise ValueError("Docker repository index contains an invalid image id")
            # Docker records RepoDigests beside tags for a pulled image. An
            # untagged/different image is still included and fails the exact set.
            if (
                re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", tag)
                and image_id in tagged_ids
            ):
                continue
            result.add(tag)
    return result


def _current_history_only(root: Path, installed: Any) -> bool:
    """Keep the current rollback seed, never another machine's runtime history."""
    history = root / "var/lib/ginnungagap/update/history"
    if history.is_symlink():
        return False
    if not history.exists():
        return True
    if not history.is_dir():
        return False
    entries = list(history.iterdir())
    if not entries:
        return True
    if not isinstance(installed, dict):
        return False
    name = installed.get("entry")
    if (
        not isinstance(name, str)
        or re.fullmatch(r"\d{8}T\d{6}Z-[^/\\]+\.json", name) is None
        or len(entries) != 1
        or entries[0].name != name
        or entries[0].is_symlink()
        or not entries[0].is_file()
    ):
        return False
    try:
        retained = json.loads(
            _read(root, "var/lib/ginnungagap/update/history/" + name) or b"null"
        )
    except (ValueError, OSError):
        return False
    if not isinstance(retained, dict):
        return False
    identity = installed.get("appliance")
    if not isinstance(identity, dict) or not identity.get("version"):
        return False
    # prepare-image.sh clears these three pointers in installed.json, while the
    # retained history file keeps its original values for the next rollback.
    stripped = {"previous", "previous_entry", "backup"}
    return {k: v for k, v in retained.items() if k not in stripped} == {
        k: v for k, v in installed.items() if k not in stripped
    }


def inspect_tree(root: Path, expected_images: list[str]) -> list[dict[str, Any]]:
    """Inspect mounted content without executing anything inside it."""
    root = root.resolve()
    checks = []
    shadow = _read(root, "etc/shadow")
    valid_shadow = bool(shadow)
    if shadow:
        entries = [line.split(b":") for line in shadow.splitlines() if line.strip()]
        valid_shadow = all(
            len(parts) >= 2 and parts[1].startswith((b"!", b"*")) for parts in entries
        )
        valid_shadow = valid_shadow and any(parts[0] == b"root" for parts in entries)
    checks.append(
        row(
            "shadow passwords locked",
            valid_shadow,
            "every account must have a locked password field",
        )
    )
    checks.append(
        row(
            "no shadow backups",
            not any(
                (root / p).exists() or (root / p).is_symlink()
                for p in (
                    "etc/shadow-",
                    "var/backups/shadow.bak",
                    "var/backups/shadow.bak.1",
                )
            ),
        )
    )
    checks.append(row("machine id empty", _read(root, "etc/machine-id") == b""))
    checks.append(
        row(
            "no temporary management network overrides",
            not any(
                (root / name).exists() or (root / name).is_symlink()
                for name in TEMPORARY_NETWORK_FILES
            ),
            "the exported guest must retain its original network profiles",
        )
    )
    paths = list(_files(root))
    private_files, unreadable = _private_key_files(root, paths)
    checks.append(
        row(
            "no private-key files in mounted filesystem",
            not private_files and not unreadable,
            json.dumps({"files": private_files, "unreadable": unreadable}),
        )
    )
    keys = [
        p
        for p in paths
        if p.name in ("authorized_keys", "authorized_keys2")
        or (p.parent == root / "etc/ssh" and p.name.startswith("ssh_host_"))
    ]
    checks.append(row("no SSH identities", not keys, f"unexpected files: {len(keys)}"))
    sudo = [_read(root, "etc/sudoers") or b""]
    sudo += [
        p.read_bytes()
        for p in paths
        if p.parent == root / "etc/sudoers.d" and not p.is_symlink()
    ]
    if any(p.is_symlink() for p in paths if p.parent == root / "etc/sudoers.d"):
        sudo.append(b"root ALL=(ALL) NOPASSWD: ALL")
    checks.append(
        row(
            "no passwordless sudo",
            not any(re.search(rb"(?m)^[^#\r\n]*\bNOPASSWD\b", data) for data in sudo),
        )
    )
    histories = [
        p
        for p in paths
        if p.name
        in {".bash_history", ".zsh_history", ".python_history", ".lesshst", ".viminfo"}
    ]
    checks.append(
        row("no shell histories", not histories, f"unexpected files: {len(histories)}")
    )
    allowed_config = {
        "ginnungagap.example.yml",
        "install.env",
        "update-trust/allowed_signers",
    }
    config_files = {
        str(p.relative_to(root / "etc/ginnungagap")).replace(os.sep, "/")
        for p in paths
        if p.is_relative_to(root / "etc/ginnungagap")
    }
    checks.append(
        row(
            "configuration removed",
            config_files <= allowed_config
            and not any(
                p.is_symlink()
                for p in paths
                if p.is_relative_to(root / "etc/ginnungagap")
            ),
            f"unexpected configuration files: {len(config_files - allowed_config)}",
        )
    )
    forbidden = (
        "var/lib/ginnungagap/ssh-access.json",
        "var/lib/ginnungagap/firstboot.done",
        "var/lib/ginnungagap/update/transaction.json",
        "var/lib/ginnungagap/update/audit.log",
        "var/lib/ginnungagap/update/downloads",
        "var/lib/ginnungagap/update/backups",
        "var/lib/docker/containers",
        "var/log/journal",
        "run/log/journal",
        "zero.fill",
    )
    directory_names = {"downloads", "backups", "containers", "journal"}
    leftovers = any(
        _nonempty(root, p)
        if Path(p).name in directory_names
        else (root / p).exists() or (root / p).is_symlink()
        for p in forbidden
    )
    checks.append(row("no runtime/update leftovers", not leftovers))
    volumes = root / "var/lib/docker/volumes"
    volume_ok = not volumes.is_symlink() and (
        not volumes.exists()
        or all(
            p.name in {"metadata.db", "backingFsBlockDev"}
            and not p.is_symlink()
            and not p.is_dir()
            for p in volumes.iterdir()
        )
    )
    checks.append(row("no Docker volumes", volume_ok))
    installed_bytes = _read(root, "var/lib/ginnungagap/update/installed.json")
    try:
        installed = json.loads(installed_bytes or b"null")
        installed_ok = (
            isinstance(installed, dict) and installed.get("previous", "missing") is None
        )
    except (ValueError, TypeError):
        installed = None
        installed_ok = False
    checks.append(row("installed release has no previous state", installed_ok))
    checks.append(
        row(
            "only current rollback seed retained in history",
            _current_history_only(root, installed),
        )
    )
    indexes = [
        p
        for p in paths
        if p.name == "repositories.json" and p.is_relative_to(root / "var/lib/docker")
    ]
    try:
        tags: set[str] = set()
        for index in indexes:
            if index.is_symlink():
                raise ValueError("linked image-store index")
            tags.update(repository_tags(json.loads(index.read_bytes())))
        image_ok = (
            bool(expected_images) and bool(indexes) and tags == set(expected_images)
        )
        image_ok = image_ok and not any(
            "rollback-" in value or "<none>" in value or "ova-deploy" in value
            for value in tags
        )
    except (ValueError, TypeError, OSError):
        image_ok = False
    checks.append(
        row(
            "exact compose image set",
            image_ok,
            "missing or unsupported image-store metadata is a failure",
        )
    )
    return checks


def _private_key_files(root: Path, paths) -> tuple[list[dict[str, str]], list[str]]:
    """Only relative paths and whole-file hashes leave this bounded scanner."""
    found, unreadable = [], []
    for path in paths:
        if len(found) + len(unreadable) >= 16:
            break
        relative = path.relative_to(root).as_posix()
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("inspection input changed")
                digest, scanner = hashlib.sha256(), _PrivateKeyScanner()
                for block in iter(lambda: stream.read(_RAW_CHUNK), b""):
                    digest.update(block)
                    scanner.feed(block)
                scanner.feed(b"", final=True)
            if scanner.saw_private and (
                scanner.unknown or not _vendor_public_file(relative, digest.hexdigest())
            ):
                found.append({"path": relative, "sha256": digest.hexdigest()})
        except (OSError, ValueError):
            unreadable.append(relative)
    return found, unreadable


def inspect_raw(path: Path, needles: list[str]) -> list[dict[str, Any]]:
    """Scan in bounded memory; findings never include credential values."""
    values = [value.encode() for value in needles if value]
    overlap = max([1024, *(len(value) for value in values)])
    if overlap > 1024 * 1024:
        raise ValueError("inspection needle exceeds the size limit")
    exact = token = False
    private = _PrivateKeyScanner()
    tail = b""
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(_RAW_CHUNK), b""):
            window = tail + block
            exact |= any(value in window for value in values)
            token |= _TOKEN.search(window) is not None
            private.feed(block)
            tail = window[-overlap:]
    private.feed(b"", final=True)
    return [
        row("no run credentials or configuration marker on raw disk", not exact),
        row("no GitHub tokens on raw disk", not token),
        row(
            "no private keys on raw disk",
            not private.unknown,
            "byte_offsets=" + json.dumps(sorted(private.offsets)),
        ),
    ]


def extract_vmdk(ova: Path, destination: Path) -> Path:
    """Extract exactly one regular VMDK, never tar paths, links or devices."""
    with tarfile.open(ova, "r:*") as archive:
        members = archive.getmembers()
        disks = [m for m in members if m.name.lower().endswith(".vmdk")]
        if len(disks) != 1 or not disks[0].isfile():
            raise ValueError("OVA must contain exactly one regular VMDK")
        if (
            not members
            or not members[0].name.lower().endswith(".ovf")
            or not members[0].isfile()
        ):
            raise ValueError("OVA must start with a regular OVF")
        disk = archive.extractfile(disks[0])
        if disk is None:
            raise ValueError("OVA disk cannot be read")
        target = destination / "disk.vmdk"
        with target.open("xb") as out, disk:
            for block in iter(lambda: disk.read(_RAW_CHUNK), b""):
                out.write(block)
        return target


def _request(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o077
        or info.st_size > 1024 * 1024
    ):
        raise ValueError(
            "inspection request must be a private regular file under 1 MiB"
        )
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict) or set(data) != {"expected_images", "needles"}:
        raise ValueError("invalid inspection request")
    for field in ("expected_images", "needles"):
        if not isinstance(data[field], list) or not all(
            isinstance(v, str) for v in data[field]
        ):
            raise ValueError("invalid inspection request")
    if not data["expected_images"]:
        raise ValueError("inspection requires the exact compose image set")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree-root", type=Path)
    parser.add_argument("--raw-disk", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--extract-ova", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.extract_ova:
            if args.destination is None:
                raise ValueError("extraction requires a destination")
            extract_vmdk(args.extract_ova, args.destination)
            return 0
        if not args.tree_root or not args.raw_disk or not args.request:
            raise ValueError("tree, raw disk and inspection request are required")
        request = _request(args.request)
        checks = inspect_tree(args.tree_root, request["expected_images"])
        checks.extend(inspect_raw(args.raw_disk, request["needles"]))
        report = {
            "passed": bool(checks) and all(c["passed"] for c in checks),
            "checks": checks,
        }
    except (OSError, ValueError, tarfile.TarError):
        report = {
            "passed": False,
            "checks": [
                row(
                    "inspection completed",
                    False,
                    "inspection input could not be verified",
                )
            ],
        }
    print(json.dumps(report))
    return 0 if report["passed"] else 1


def _artifact_attempt(ctx):
    """The exported guest's attempt survives a new resume invocation's counter."""
    from .pipeline import StateFile
    from .ports import PermanentError

    path = ctx.run_dir / "state.json"
    if path.exists() or path.is_symlink():
        state = StateFile.load(ctx.run_dir)
        if state is None or state.data.get("run_id") != ctx.run_id:
            raise PermanentError("inspection cannot verify the recorded build attempt")
        phases = state.data.get("phases")
        phase = phases.get("P5", {}) if isinstance(phases, dict) else None
        if not isinstance(phase, dict) or phase.get("status") != "ok":
            raise PermanentError("inspection needs the successful recorded bootstrap")
        attempt = phase.get("attempt")
    else:
        # Direct phase callers without a persisted pipeline use their explicit
        # attempt; an on-disk state always takes precedence during real resumes.
        attempt = ctx.attempt
    if type(attempt) is not int or not 1 <= attempt <= 1000:
        raise PermanentError("inspection has an invalid recorded build attempt")
    return attempt


def inspect_phase(ctx):
    """P11 invokes only the root-owned helper, never release-provided code."""
    from .config import read_secret
    from .ports import PermanentError
    from .remote import config_marker

    exported = ctx.result_of("P10")
    path = Path(exported["path"]).resolve()
    run = ctx.run_dir.resolve()
    fixed_root = RUNS_ROOT.resolve()
    if run.parent != fixed_root or path.parent != run / "export":
        raise PermanentError(
            "offline inspection requires the installed helper's fixed runs directory"
        )
    expected = ctx.results.get("P8", {}).get("images") or ctx.results.get("P7", {}).get(
        "images"
    )
    if not isinstance(expected, list) or not expected:
        raise PermanentError(
            "install/prepare did not record the exact compose image set"
        )
    attempt = _artifact_attempt(ctx)
    marker = ctx.results.get("P5", {}).get("config_marker")
    if (
        not isinstance(marker, str)
        or marker != config_marker(SimpleNamespace(run_id=ctx.run_id, attempt=attempt))
        or marker == CONFIG_MARKER
    ):
        raise PermanentError("inspection needs the unique per-run configuration marker")
    needles = [marker]
    saved = ctx.results.get("P5", {})
    key_file = saved.get("key_file")
    public_key_file = saved.get("public_key_file")
    if not key_file and not public_key_file:
        raise PermanentError("inspection needs the build VM's run key")
    public = Path(public_key_file) if public_key_file else Path(str(key_file) + ".pub")
    if public_key_file and (
        public.parent != run / "evidence"
        or public.name != f"public-attempt-{attempt}.pub"
        or public.parent.is_symlink()
    ):
        raise PermanentError("inspection needs the run's own public-key evidence")
    if not public.is_file() or public.is_symlink():
        raise PermanentError("inspection cannot read the build VM's public key")
    if public_key_file and (
        public.stat().st_mode & 0o077
        or public.stat().st_nlink != 1
        or public.stat().st_uid != os.geteuid()
    ):
        raise PermanentError("inspection public-key evidence is not private")
    key_text = public.read_text().strip()
    if not key_text.startswith("ssh-ed25519 ") or "\n" in key_text:
        raise PermanentError("inspection needs the run's complete ed25519 public key")
    needles.append(key_text)
    secret = read_secret(ctx.config.esxi.password_file, credential_name="esxi205")
    ctx.secret(secret)
    needles.append(secret)
    request = run / "inspection-request.json"
    ctx.track_path(request)
    descriptor = os.open(request, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump({"expected_images": expected, "needles": needles}, handle)
    import subprocess

    try:
        result = subprocess.run(
            [
                "sudo",
                "--",
                "/opt/ginnungagap-image-build/tooling/image/build/inspect-disk.sh",
                ctx.run_id,
            ],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        report = json.loads(result.stdout)
        checks = report.get("checks", [])
        valid = result.returncode == 0 and report.get("passed") is True and bool(checks)
        valid = valid and any(c.get("required", True) for c in checks)
        valid = valid and all(
            c.get("passed") is True for c in checks if c.get("required", True)
        )
        (run / "inspection.json").write_text(json.dumps(report, indent=2) + "\n")
        if not valid:
            raise PermanentError("offline image inspection failed; see inspection.json")
        ctx.note("inspection", report)
        return report
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise PermanentError("offline image inspection could not complete") from exc
    finally:
        request.unlink(missing_ok=True)


def register_phases(registry=None):
    from . import pipeline

    chosen = registry if registry is not None else pipeline.DEFAULT_REGISTRY
    chosen.register(
        "P11",
        inspect_phase,
        describe=lambda ctx: (
            "inspect exported disk read-only with the pinned root helper"
        ),
    )
    return chosen


if __name__ == "__main__":
    raise SystemExit(main())
