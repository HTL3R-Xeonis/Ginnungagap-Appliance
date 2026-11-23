"""Template-store adapters and the acceptance-gated publication phases."""

from __future__ import annotations

import errno
import functools
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ginnungagap.version import __version__ as TOOLING_VERSION

from . import names, pipeline, status
from .ports import (
    AlreadyExists,
    AuthFailed,
    HeadResult,
    NotFound,
    NotOurs,
    PermanentError,
    TemplateEntry,
    Timeout,
    TransientError,
    Unreachable,
)

_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,240}", re.ASCII)
_MAX_TEXT = 16 * 1024 * 1024


def _part(value: str) -> str:
    if value != names.RUN_MARKER and (
        not isinstance(value, str) or not _PART.fullmatch(value)
    ):
        raise PermanentError("share names must be plain file or folder names")
    return value


def _share_errors(cls):
    def wrap(fn):
        @functools.wraps(fn)
        def invoke(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except OSError as exc:
                if exc.errno in (
                    errno.EIO,
                    errno.ESTALE,
                    errno.ETIMEDOUT,
                    errno.ENOTCONN,
                ):
                    raise Unreachable("template share I/O temporarily failed") from exc
                raise PermanentError(
                    "template share filesystem operation failed"
                ) from exc

        return invoke

    for name, fn in tuple(vars(cls).items()):
        if not name.startswith("_") and callable(fn):
            setattr(cls, name, wrap(fn))
    return cls


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(
    url: str,
    method: str = "GET",
    *,
    opener=None,
    maximum: int = _MAX_TEXT,
    context: str = "template service",
) -> tuple[int, Any, bytes]:
    opener = opener or urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(
            urllib.request.Request(url, method=method), timeout=30
        ) as response:
            data = response.read(maximum + 1) if method != "HEAD" else b""
            if len(data) > maximum:
                raise PermanentError("template API response is too large")
            return response.status, response.headers, data
    except urllib.error.HTTPError as exc:
        if method == "HEAD" and exc.code == 404:
            return 404, exc.headers, b""
        if exc.code == 401:
            raise AuthFailed(f"{context} refused authentication: HTTP 401") from exc
        if exc.code == 403:
            raise PermanentError(f"{context} refused access: HTTP 403") from exc
        if exc.code >= 500:
            raise Unreachable(f"{context} is temporarily unavailable") from exc
        raise PermanentError(f"{context} refused {method}: HTTP {exc.code}") from exc
    except TimeoutError as exc:
        raise Timeout(f"{context} did not answer in time") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise Unreachable(f"{context} could not be reached") from exc


class TemplateApi:
    def __init__(self, url: str, *, opener=None):
        self.url = url.rstrip("/")
        self.opener = opener

    def check(self) -> None:
        self.list_templates()

    def list_templates(self) -> list[TemplateEntry]:
        _, _, body = _request(self.url + "/api/templates", opener=self.opener)
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise PermanentError("template API did not return JSON") from exc
        rows = data.get("templates") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise PermanentError("template API has no template list")
        found = []
        for row in rows:
            if not isinstance(row, dict) or not all(
                isinstance(row.get(k), str) for k in ("name", "id", "file")
            ):
                raise PermanentError("template API returned a malformed entry")
            folder = row.get("_source_dir", "")
            if not isinstance(folder, str):
                raise PermanentError(
                    "template API returned an invalid source directory"
                )
            found.append(
                TemplateEntry(row["name"], row["id"], row["file"], folder, row)
            )
        return found

    def refresh(self) -> None:
        _request(self.url + "/api/refresh", "POST", opener=self.opener)


@_share_errors
class LocalShare:
    """A mounted NFS share. Exclusive hard-link publication prevents overwrite races."""

    def __init__(self, root: Path, ova_base_url: str, *, opener=None):
        self.root = Path(root)
        self.ova_base_url = ova_base_url
        self.opener = opener

    def _root(self) -> Path:
        if not self.root.is_dir():
            raise NotFound("the template share is not mounted or is unavailable")
        return self.root.resolve()

    def _folder(self, folder: str) -> Path:
        path = self._root() / _part(folder)
        if path.is_symlink():
            raise NotOurs("refusing a symlink on the template share")
        return path

    def _file(self, folder: str, name: str) -> Path:
        path = self._folder(folder) / _part(name)
        if path.is_symlink():
            raise NotOurs("refusing a symlink on the template share")
        return path

    def free_bytes(self) -> int:
        return shutil.disk_usage(self._root()).free

    def list_folders(self) -> list[str]:
        return sorted(
            p.name for p in self._root().iterdir() if p.is_dir() and not p.is_symlink()
        )

    def folder_files(self, folder: str) -> list[str]:
        path = self._folder(folder)
        if not path.is_dir():
            raise NotFound("share folder does not exist")
        return sorted(p.name for p in path.iterdir())

    def read_text(self, folder: str, name: str) -> str | None:
        path = self._file(folder, name)
        if not path.is_file():
            return None
        if path.stat().st_size > _MAX_TEXT:
            raise PermanentError("share text file is too large")
        return path.read_text(encoding="utf-8")

    def size(self, folder: str, name: str) -> int:
        path = self._file(folder, name)
        if not path.is_file():
            raise NotFound("share file does not exist")
        return path.stat().st_size

    def sha256(self, folder: str, name: str) -> str:
        path = self._file(folder, name)
        if not path.is_file():
            raise NotFound("share file does not exist")
        return _digest(path)

    def url_for(self, folder: str, name: str) -> str:
        if any(part in (".", "..") for part in folder.split("/")) or "\\" in folder:
            raise PermanentError("invalid template source directory")
        return names.ova_url(self.ova_base_url, folder, _part(name))

    def head(self, url: str) -> HeadResult:
        code, headers, _ = _request(
            url, "HEAD", opener=self.opener, context="OVA web server"
        )
        try:
            length = int(headers.get("Content-Length"))
        except (TypeError, ValueError):
            length = None
        return HeadResult(code, length, headers.get("Accept-Ranges"))

    def make_folder(self, folder: str) -> None:
        try:
            path = self._folder(folder)
            path.mkdir(mode=0o755)
            created = path.lstat()
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (created.st_dev, created.st_ino):
                    raise NotOurs("share folder changed during creation")
                # mkdir's mode is restricted by the service's 0077 umask.
                # Change only the directory we just created, through its fd.
                os.fchmod(descriptor, 0o755)
            finally:
                os.close(descriptor)
        except FileExistsError as exc:
            raise AlreadyExists("share folder already exists; never overwrite") from exc
        except OSError as exc:
            raise PermanentError("share folder could not be created") from exc

    def _write(self, folder: str, name: str, writer: Callable, replace: bool) -> None:
        target = self._file(folder, name)
        if not target.parent.is_dir():
            raise NotFound("share folder does not exist")
        if target.exists() and not replace:
            raise AlreadyExists("share file already exists; never overwrite")
        fd, temp = tempfile.mkstemp(prefix="." + name + ".partial.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                writer(handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o644)
            if replace:
                os.replace(temp, target)
            else:
                try:
                    os.link(temp, target)
                except FileExistsError as exc:
                    raise AlreadyExists(
                        "share file appeared during copy; never overwrite"
                    ) from exc
        finally:
            Path(temp).unlink(missing_ok=True)

    def put_file(
        self, folder: str, name: str, source: Path, *, replace: bool = False
    ) -> None:
        with Path(source).open("rb") as handle:
            self._write(
                folder,
                name,
                lambda out: shutil.copyfileobj(handle, out, 1024 * 1024),
                replace,
            )

    def put_text(
        self, folder: str, name: str, text: str, *, replace: bool = False
    ) -> None:
        self._write(folder, name, lambda out: out.write(text.encode("utf-8")), replace)

    def remove_folder(self, folder: str, *, marker_name: str, marker_text: str) -> bool:
        path = self._folder(folder)
        if not path.exists():
            return False
        if self.read_text(folder, marker_name) != marker_text:
            raise NotOurs("share folder has no matching run marker")
        shutil.rmtree(path)
        return True


# This helper is fixed tooling, not downloaded release code. Binary file data travels
# through stdin; secrets and archive bytes never become arguments or log messages.
_SSH_HELPER = r"""
import hashlib,json,os,re,shutil,sys,tempfile
from pathlib import Path
root=Path(sys.argv[1]).resolve();op=sys.argv[2];a=json.loads(sys.argv[3])
def part(v):
 if v!='.imgbuild-run' and (not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,240}',v)): raise ValueError('unsafe name')
 return v
def folder():
 p=root/part(a['folder'])
 if p.is_symlink(): raise ValueError('symlink refused')
 return p
def file():
 p=folder()/part(a['name'])
 if p.is_symlink(): raise ValueError('symlink refused')
 return p
def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
try:
 if not root.is_dir(): raise FileNotFoundError()
 if op=='free':result=shutil.disk_usage(root).free
 elif op=='folders':result=sorted(p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink())
 elif op=='files':result=sorted(p.name for p in folder().iterdir())
 elif op=='read':
  p=file()
  if p.exists() and p.stat().st_size>16777216:raise ValueError('text too large')
  result=p.read_text() if p.exists() else None
 elif op=='size':result=file().stat().st_size
 elif op=='sha256':result=digest(file())
 elif op=='mkdir':
  p=folder();p.mkdir(mode=0o755);created=p.lstat()
  fd=os.open(p,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
  try:
   opened=os.fstat(fd)
   if (opened.st_dev,opened.st_ino)!=(created.st_dev,created.st_ino):raise PermissionError('folder changed during creation')
   os.fchmod(fd,0o755)
  finally:os.close(fd)
  result=None
 elif op=='remove':
  p=folder()
  if not p.exists():result=False
  else:
   marker=p/part(a['marker_name'])
   if marker.is_symlink() or not marker.is_file() or marker.read_text()!=a['marker_text']:raise PermissionError()
   shutil.rmtree(p);result=True
 elif op=='put':
  p=file();fd,tmp=tempfile.mkstemp(prefix='.'+p.name+'.partial.',dir=p.parent)
  try:
   with os.fdopen(fd,'wb') as out:shutil.copyfileobj(sys.stdin.buffer,out,1048576);out.flush();os.fsync(out.fileno())
   os.chmod(tmp,0o644)
   if a.get('replace'):os.replace(tmp,p)
   else:os.link(tmp,p)
  finally:Path(tmp).unlink(missing_ok=True)
  result=None
 else:raise ValueError('unknown operation')
 print(json.dumps({'result':result}))
except Exception as e:
 print(json.dumps({'error':type(e).__name__}));sys.exit(1)
"""


@_share_errors
class SshShare(LocalShare):
    """The explicit SSH fallback; its account needs the configured restricted copy permission."""

    def __init__(
        self,
        root: Path,
        ova_base_url: str,
        *,
        host: str,
        user: str,
        key_file: Path,
        runner: Callable = subprocess.run,
        sudo: bool = True,
        opener=None,
    ):
        super().__init__(root, ova_base_url, opener=opener)
        self.host, self.user, self.key_file = host, user, Path(key_file)
        self.runner, self.sudo = runner, sudo

    def _call(
        self,
        op: str,
        data: dict | None = None,
        *,
        content: bytes | None = None,
        source=None,
    ):
        remote = (["sudo", "-n"] if self.sudo else []) + [
            "python3",
            "-c",
            _SSH_HELPER,
            str(self.root),
            op,
            json.dumps(data or {}),
        ]
        command = [
            "ssh",
            "-i",
            str(self.key_file),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=15",
            "--",
            f"{self.user}@{self.host}",
            shlex.join(remote),
        ]
        try:
            result = (
                self.runner(
                    command,
                    stdin=source,
                    capture_output=True,
                    timeout=1800,
                    check=False,
                )
                if source is not None
                else self.runner(
                    command,
                    input=content,
                    capture_output=True,
                    timeout=1800,
                    check=False,
                )
            )
        except subprocess.TimeoutExpired as exc:
            raise Timeout("SSH share operation timed out") from exc
        except OSError as exc:
            raise Unreachable("SSH share could not be reached") from exc
        if result.returncode == 255:
            raise Unreachable("SSH share connection failed")
        try:
            value = json.loads(result.stdout)
        except ValueError as exc:
            raise PermanentError("SSH share helper returned no valid result") from exc
        if result.returncode:
            kind = value.get("error")
            if kind == "FileExistsError":
                raise AlreadyExists("SSH share target already exists")
            if kind == "FileNotFoundError":
                raise NotFound("SSH share source or folder does not exist")
            if kind == "PermissionError":
                raise NotOurs("SSH share permission or run marker refused")
            raise PermanentError("SSH share operation was refused")
        return value.get("result")

    def free_bytes(self) -> int:
        return int(self._call("free"))

    def list_folders(self) -> list[str]:
        return self._call("folders")

    def folder_files(self, folder: str) -> list[str]:
        return self._call("files", {"folder": folder})

    def read_text(self, folder: str, name: str) -> str | None:
        return self._call("read", {"folder": folder, "name": name})

    def size(self, folder: str, name: str) -> int:
        return int(self._call("size", {"folder": folder, "name": name}))

    def sha256(self, folder: str, name: str) -> str:
        return self._call("sha256", {"folder": folder, "name": name})

    def make_folder(self, folder: str) -> None:
        self._call("mkdir", {"folder": folder})

    def put_file(
        self, folder: str, name: str, source: Path, *, replace: bool = False
    ) -> None:
        with Path(source).open("rb") as handle:
            self._call(
                "put",
                {"folder": folder, "name": name, "replace": replace},
                source=handle,
            )

    def put_text(
        self, folder: str, name: str, text: str, *, replace: bool = False
    ) -> None:
        self._call(
            "put",
            {"folder": folder, "name": name, "replace": replace},
            content=text.encode(),
        )

    def remove_folder(self, folder: str, *, marker_name: str, marker_text: str) -> bool:
        return bool(
            self._call(
                "remove",
                {
                    "folder": folder,
                    "marker_name": marker_name,
                    "marker_text": marker_text,
                },
            )
        )


def _passed(result: dict, label: str) -> None:
    checks = result.get("checks")
    if result.get("passed") is not True or not isinstance(checks, list) or not checks:
        raise PermanentError(f"{label} has no successful check report")
    if any(
        not isinstance(check, dict) or not isinstance(check.get("required", True), bool)
        for check in checks
    ):
        raise PermanentError(f"{label} has a malformed check report")
    required = [check for check in checks if check.get("required", True)]
    if not required or any(check.get("passed") is not True for check in required):
        raise PermanentError(f"{label} has failed required checks")


def _wait_staged_ova(ctx, url: str, size: int) -> bool:
    """Only a transient 404 may wait for another NFS client's visibility."""
    started = ctx.clock.monotonic()
    deadline = started + 120
    retried = False
    while True:
        head = ctx.share.head(url)
        if head.importable and head.content_length == size:
            if retried:
                ctx.note("ova_visibility_wait_seconds", ctx.clock.monotonic() - started)
            return retried
        detail = (
            f"HTTP {head.status}; Content-Length={head.content_length!r} "
            f"(expected {size}); Accept-Ranges={str(head.accept_ranges)[:80]!r}"
        )
        if head.status != 404:
            raise PermanentError(
                "staged OVA is not served with the required size and byte ranges: "
                + detail
            )
        remaining = deadline - ctx.clock.monotonic()
        if remaining <= 0:
            raise PermanentError(
                "staged OVA is still not visible after 120 seconds: " + detail
            )
        retried = True
        ctx.clock.sleep(min(2, remaining))


def _template(ctx: pipeline.PhaseContext, file: str, sha: str, size: int) -> str:
    gb = f"{size / 1024**3:.2f}".replace(".", ",") + "GB"
    value = {
        "version": 1,
        "templates": [
            {
                "id": sha.upper(),
                "name": ctx.names.template,
                "file": file,
                "size": gb,
                "category": "Appliance",
                "tags": ["appliance", "linux", "server"],
                "os": {"family": "Linux", "version": "Debian 13 (trixie)"},
                "cpu": ctx.config.image.cpu,
                "memory": f"{ctx.config.image.memory_gb}GB",
                "disk": f"{ctx.config.image.disk_gb}GB",
                "users": ["junioradmin"],
                "hostname": "ginnungagap",
                "domain": None,
                "note": "Login credentials are generated on first boot; change the login before use.",
            }
        ],
    }
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True)


def register_phases(
    registry: pipeline.PhaseRegistry, *, notify_runner: Callable = subprocess.run
) -> None:
    def stage(ctx: pipeline.PhaseContext) -> dict:
        _passed(ctx.result_of("P11"), "offline inspection")
        exported = ctx.result_of("P10")
        path = Path(exported["path"])
        if not path.is_file() or path.is_symlink():
            raise PermanentError("exported OVA is missing or is a symlink")
        sha = _digest(path)
        size = path.stat().st_size
        if sha != exported.get("sha256") or size != exported.get("size"):
            raise PermanentError("the inspected OVA changed before staging")
        folder, file = ctx.names.folder, ctx.names.ova(sha)
        catalog = ctx.templates.list_templates()
        baseline = ctx.results.get("P1", {}).get("catalog") or [
            {"name": e.name, "id": e.id} for e in catalog
        ]
        old = {(e["name"], e["id"]) for e in baseline}
        current = {(e.name, e.id) for e in catalog}
        text = _template(ctx, file, sha, size)
        folders = ctx.share.list_folders()
        resuming = ctx.options.resume == ctx.run_id and folder in folders
        existing_yaml = None
        if resuming:
            if ctx.share.read_text(folder, names.RUN_MARKER) != ctx.run_id:
                raise NotOurs("resume folder does not have this run's marker")
            existing_yaml = ctx.share.read_text(folder, ctx.names.yml)
            if existing_yaml is not None and existing_yaml != text:
                raise PermanentError(
                    "resume description differs from the inspected image"
                )
        review = None
        if ctx.options.refresh_catalog:
            # This is an explicit recovery review, never an automatic rebase of
            # the original preflight or an approval of a changed build input.
            if ctx.options.resume != ctx.run_id:
                raise PermanentError("catalog refresh requires resuming this run")
            original = ctx.results.get("P1", {}).get("catalog")
            base = ctx.results.get("P3", {})
            if not isinstance(original, list) or not original:
                raise PermanentError(
                    "catalog refresh needs the original preflight catalog"
                )
            original_ids = {entry["name"]: entry["id"] for entry in original}
            current_ids = {entry.name: entry.id for entry in catalog}
            if len(original_ids) != len(original) or len(current_ids) != len(catalog):
                raise PermanentError("catalog refresh refuses duplicate template names")
            base_name = base.get("name") or base.get("template")
            base_id = base.get("id")
            selected = next(
                (entry for entry in catalog if entry.name == base_name), None
            )
            if (
                not base_name
                or not base_id
                or original_ids.get(base_name) != base_id
                or selected is None
                or selected.id != base_id
                or selected.file != base.get("file")
                or selected.source_dir != base.get("source_dir")
            ):
                raise PermanentError(
                    "catalog refresh refuses a changed or missing base image"
                )
            target = [entry for entry in catalog if entry.name == ctx.names.template]
            # An already visible target is reusable only as this run's exact,
            # previously published artifact; a matching ID alone is insufficient.
            if target and not (
                resuming
                and existing_yaml is not None
                and len(target) == 1
                and target[0].id == sha.upper()
                and target[0].file == file
                and target[0].source_dir == folder
                and file in ctx.share.folder_files(folder)
                and ctx.share.sha256(folder, file) == sha
                and ctx.share.size(folder, file) == size
            ):
                raise PermanentError(
                    "catalog refresh refuses a conflicting target template"
                )
            review = {
                "previous": [
                    {"name": key, "id": value}
                    for key, value in sorted(original_ids.items())
                ],
                "current": [
                    {"name": key, "id": value}
                    for key, value in sorted(current_ids.items())
                ],
                "added": [
                    {"name": key, "id": current_ids[key]}
                    for key in sorted(current_ids.keys() - original_ids.keys())
                ],
                "removed": [
                    {"name": key, "id": original_ids[key]}
                    for key in sorted(original_ids.keys() - current_ids.keys())
                ],
                "changed": [
                    {
                        "name": key,
                        "before_id": original_ids[key],
                        "after_id": current_ids[key],
                    }
                    for key in sorted(original_ids.keys() & current_ids.keys())
                    if original_ids[key] != current_ids[key]
                ],
            }
            baseline = review["current"]
            old = current
        if resuming:
            allowed_catalog = (
                old | {(ctx.names.template, sha.upper())} if existing_yaml else old
            )
            if current != old and current != allowed_catalog:
                raise PermanentError("the catalog changed since the original preflight")
        else:
            if current != old:
                raise PermanentError("the catalog changed since the original preflight")
            names.check_free(
                ctx.names,
                template_names=[e.name for e in catalog],
                share_folders=folders,
            )
        # Register cleanup first; it can only remove a folder carrying this run's marker.
        ctx.track_share_folder(folder)
        if existing_yaml is not None and ctx.cleanup is not None:
            ctx.cleanup.commit()
        if not resuming:
            ctx.share.make_folder(folder)
            ctx.share.put_text(folder, names.RUN_MARKER, ctx.run_id)
        if not resuming or file not in ctx.share.folder_files(folder):
            ctx.share.put_file(folder, file, path)
        if (
            ctx.share.sha256(folder, file) != sha
            or ctx.share.size(folder, file) != size
        ):
            raise PermanentError(
                "staged OVA size or checksum differs from the inspected export"
            )
        url = ctx.share.url_for(folder, file)
        waited = _wait_staged_ova(ctx, url, size)
        if waited and (
            ctx.share.sha256(folder, file) != sha
            or ctx.share.size(folder, file) != size
        ):
            raise PermanentError("staged OVA changed while waiting for web visibility")
        if {(e.name, e.id) for e in ctx.templates.list_templates()} != current:
            raise PermanentError(
                "the template catalog changed while staging an unpublished OVA"
            )
        if review is not None:
            ctx.note("catalog_refresh", review)
        return {
            "folder": folder,
            "file": file,
            "url": url,
            "path": str(path),
            "sha256": sha,
            "size": size,
            "yaml": text,
            "catalog": baseline,
            "catalog_refresh": review,
        }

    def publish(ctx: pipeline.PhaseContext) -> dict:
        _passed(ctx.result_of("P11"), "offline inspection")
        _passed(ctx.result_of("P13"), "acceptance")
        staged = ctx.result_of("P12")
        folder, file = staged["folder"], staged["file"]
        if folder != ctx.names.folder or file != ctx.names.ova(staged["sha256"]):
            raise PermanentError(
                "staged OVA identity differs from the requested release"
            )
        if ctx.share.read_text(folder, names.RUN_MARKER) != ctx.run_id:
            raise NotOurs("staged folder belongs to another run")
        if (
            ctx.share.sha256(folder, file) != staged["sha256"]
            or ctx.share.size(folder, file) != staged["size"]
        ):
            raise PermanentError("staged OVA changed during acceptance")
        old = {(e["name"], e["id"]) for e in staged["catalog"]}
        expected = (ctx.names.template, staged["sha256"].upper())
        current = {(e.name, e.id) for e in ctx.templates.list_templates()}
        description = ctx.share.read_text(folder, ctx.names.yml)
        resumed_publication = (
            ctx.options.resume == ctx.run_id and description == staged["yaml"]
        )
        if current != old and not (resumed_publication and current == old | {expected}):
            raise PermanentError(
                "the catalog changed during acceptance; publication needs a new review"
            )
        if not resumed_publication:
            ctx.share.put_text(folder, ctx.names.yml, staged["yaml"])
        # Atomic YAML publication is irreversible. A refresh/check failure must never delete a visible accepted image.
        if ctx.cleanup is not None:
            ctx.cleanup.commit()
        ctx.note("publication_attempted", True)
        try:
            ctx.templates.refresh()
            deadline = ctx.clock.monotonic() + 300
            while True:
                now = {(e.name, e.id) for e in ctx.templates.list_templates()}
                if not old.issubset(now) or now - old - {expected}:
                    raise PermanentError(
                        "previous catalog entries changed or unexpected entries appeared"
                    )
                if expected in now:
                    head = ctx.share.head(staged["url"])
                    if not head.importable or head.content_length != staged["size"]:
                        raise PermanentError(
                            "published OVA is not readable from the OVA server"
                        )
                    ctx.note(
                        "ova",
                        {
                            k: staged[k]
                            for k in ("folder", "file", "url", "sha256", "size")
                        },
                    )
                    return {
                        "published": True,
                        "template": ctx.names.template,
                        "id": expected[1],
                    }
                if ctx.clock.monotonic() >= deadline:
                    raise PermanentError(
                        "the new template was not listed within five minutes"
                    )
                ctx.clock.sleep(5)
        except TransientError as exc:
            raise PermanentError(
                "publication is staged but its catalog visibility could not be confirmed; inspect the preserved folder"
            ) from exc

    def summary(ctx: pipeline.PhaseContext) -> dict:
        document = ctx.redactor.redact_deep(ctx.summary.to_dict())
        document["release"] = ctx.version
        document["manifest_sha256"] = ctx.results.get("P2", {}).get("manifest_sha256")
        document["components"] = ctx.results.get("P2", {}).get("components", {})
        document["base"] = ctx.results.get("P3", {})
        document["ova"] = {
            k: v
            for k, v in ctx.results.get("P12", {}).items()
            if k not in ("yaml", "catalog", "path")
        }
        document["acceptance"] = ctx.results.get("P13", {})
        versions = {
            "python": platform.python_version(),
            "image_builder": TOOLING_VERSION,
        }
        for package in ("pyvmomi", "PyYAML", "requests", "topologybuilder"):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                pass
        document["tool_versions"] = versions
        document = ctx.redactor.redact_deep(document)
        destination = ctx.state_dir / "builds" / ctx.version / "summary.json"
        previous = status.read_json(destination)
        if previous and previous.get("published") and not document.get("published"):
            destination = ctx.run_dir / "status-summary.json"
        status.write_json(destination, document)
        status.write_json(ctx.state_dir / "latest.json", document)
        if ctx.summary.published:
            ctx.share.put_text(
                ctx.names.folder,
                "BUILD-INFO.json",
                json.dumps(document, indent=2) + "\n",
            )
        if ctx.config.notify.command:
            argv = shlex.split(ctx.config.notify.command) + [str(destination)]
            result = notify_runner(argv, capture_output=True, timeout=30, check=False)
            if result.returncode:
                raise PermanentError("build notification command failed")
        return {"summary": str(destination)}

    for pid, fn, description in [
        ("P12", stage, "stage the inspected OVA without a catalog description"),
        ("P14", publish, "publish the accepted image and verify catalog preservation"),
        ("P16", summary, "write redacted local and template-store build records"),
    ]:
        registry.register(pid, fn, describe=lambda ctx, text=description: text)
