"""Pinned release discovery, verification and read-only preflight (P0-P3)."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ginnungagap import manifest as mf
from ginnungagap.releases import (
    DirectorySource,
    GitHubSource,
    ReleaseInfo,
    ReleaseSource,
    SourceError,
    check_token_text,
)
from ginnungagap.update import verify_with_ssh_keygen
from release import build_release

from . import names, pipeline
from .config import BuildHostConfig, SecretFileError, read_secret
from .ports import (
    AuthFailed,
    Blocked,
    Clock,
    Deferred,
    PermanentError,
    TemplateApiPort,
    TemplateEntry,
    TransientError,
)

CONTROL_LIMIT = 8 * 1024 * 1024
ASSET_LIMIT = 4 * 1024**3
_SUM = re.compile(r"([0-9a-fA-F]{64})  ([A-Za-z0-9][A-Za-z0-9._-]{0,200})")
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}", re.ASCII)


class BuildGitHubSource(GitHubSource):
    """GitHub source with the build service's verified LoadCredential support."""

    @property
    def token(self) -> str | None:
        if not self._token_read:
            token = None
            if self.token_file is not None:
                path = Path(self.token_file)
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    pass  # Public repositories work without a configured token file.
                except OSError as exc:
                    raise SourceError(
                        "config", "the token file cannot be read"
                    ) from exc
                else:
                    if info.st_uid not in (0, os.geteuid()):
                        raise SourceError(
                            "config", "the token file belongs to another user"
                        )
                    try:
                        token = check_token_text(read_secret(path))
                    except (SecretFileError, ValueError) as exc:
                        raise SourceError(
                            "config",
                            "the token file is unsafe or does not hold a single token",
                        ) from exc
            self._token = token
            self._token_read = True
        return self._token


def source_for(config: BuildHostConfig, directory: Path | None = None) -> ReleaseSource:
    if directory is not None:
        return DirectorySource(directory)
    return BuildGitHubSource(
        config.github.repo,
        token_file=config.github.token_file,
        cache_path=config.paths.state_dir / "release-cache.json",
        api="always",
    )


def make_source(
    config: BuildHostConfig, options: pipeline.RunOptions | None = None
) -> ReleaseSource:
    return source_for(config, options.release_dir if options is not None else None)


def _source_error(exc: SourceError) -> PermanentError | TransientError:
    if exc.kind == "auth":
        return AuthFailed("the release source refused authentication")
    if exc.kind in {"network", "server", "rate_limit"}:
        return TransientError(f"release source unavailable: {exc.message}")
    return Blocked(f"release source: {exc.message}")


def _listed(source: ReleaseSource, allow_prerelease: bool) -> list[ReleaseInfo]:
    try:
        found = source.list_releases("prerelease" if allow_prerelease else "stable")
    except SourceError as exc:
        raise _source_error(exc) from exc
    return sorted(
        (r for r in found if _valid_release(r, allow_prerelease)),
        key=lambda r: mf.semver_key(r.version),
        reverse=True,
    )


def _valid_release(info: ReleaseInfo, allow_prerelease: bool) -> bool:
    try:
        names.check_version(info.version)
    except ValueError:
        return False
    return info.tag == f"v{info.version}" and (allow_prerelease or not info.prerelease)


def select_release(
    config: BuildHostConfig,
    templates: TemplateApiPort,
    *,
    source: ReleaseSource | None = None,
    allow_prerelease: bool = False,
    state_dir: Path | None = None,
    clock: Clock | None = None,
    retry: bool = False,
) -> ReleaseInfo | None:
    """Newest unbuilt release; the catalog, not a cached success, is authoritative."""
    source = source or source_for(config)
    catalog = templates.list_templates()
    published = {entry.name for entry in catalog}
    stable_built = [
        v
        for entry in catalog
        if (v := _template_version(entry)) and not mf.is_prerelease(v)
    ]
    newest_built = max(stable_built, key=mf.semver_key) if stable_built else None
    root = state_dir or config.paths.state_dir
    clock = clock or pipeline.SystemClock()
    states = pipeline.list_runs(root)
    for info in _listed(source, allow_prerelease):
        if newest_built and mf.semver_key(info.version) <= mf.semver_key(newest_built):
            continue
        if names.for_version(info.version).template in published:
            continue
        if not retry:
            if any(
                s.data.get("version") == info.version
                and s.data.get("status") == "blocked"
                for s in states
            ):
                continue
            if (
                pipeline.attempts_in_window(root, info.version, clock.now())
                >= config.limits.max_attempts_per_release
            ):
                continue
        return info
    return None


def _info(info: ReleaseInfo) -> dict[str, Any]:
    return {
        "version": info.version,
        "tag": info.tag,
        "prerelease": info.prerelease,
        "date": info.date,
        "assets": info.assets,
        "location": info.location,
        "web": info.web,
    }


def _restore(data: dict[str, Any]) -> ReleaseInfo:
    return ReleaseInfo(
        **{
            k: data[k]
            for k in (
                "version",
                "tag",
                "prerelease",
                "date",
                "assets",
                "location",
                "web",
            )
        }
    )


def verify_release_folder(
    folder: Path, version: str, allowed_signers: Path, allow_unsigned: bool = False
) -> tuple[dict[str, Any], str]:
    """Verify with installed tooling only; never import any downloaded Python."""
    try:
        manifest = mf.load(folder / "components.json")
    except mf.ManifestError as exc:
        raise Blocked("invalid release manifest: " + "; ".join(exc.problems)) from exc
    if manifest.version != version or manifest.tag != f"v{version}":
        raise Blocked("requested release, tag and manifest versions differ")
    signature = folder / "components.json.sig"
    signature_note = "none (manual unsigned override)"
    if signature.is_file():
        valid, fingerprint = verify_with_ssh_keygen(
            (folder / "components.json").read_bytes(), signature, allowed_signers
        )
        if not valid:
            raise Blocked("release signature is not trusted: " + fingerprint)
        signature_note = fingerprint
    elif not allow_unsigned:
        raise Blocked(
            "release signature is missing; automatic builds require a signed release"
        )
    expected = {"components.json", *(c.asset for c in manifest.components.values())}
    try:
        lines = (folder / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise Blocked("release SHA256SUMS is missing") from exc
    summed: set[str] = set()
    for line in lines:
        match = _SUM.fullmatch(line)
        if match is None or match[2] not in expected or match[2] in summed:
            raise Blocked(
                "SHA256SUMS contains an invalid, unexpected or duplicate filename"
            )
        summed.add(match[2])
    if summed != expected:
        raise Blocked("SHA256SUMS must cover the manifest and every component")
    try:
        build_release.verify(folder)
        for component in manifest.components.values():
            declared, recorded = build_release.inspect_tarball(
                folder / component.asset,
                component.name,
                component.repo_name,
                component.version,
            )
            if declared != component.version or (
                recorded is not None and recorded != component.commit
            ):
                raise Blocked(
                    f"{component.name}: archive version/commit differs from the signed manifest"
                )
    except build_release.ReleaseError as exc:
        raise Blocked(str(exc)) from exc
    return manifest.to_dict(), signature_note


def _fetch(
    source: ReleaseSource, info: ReleaseInfo, name: str, folder: Path, maximum: int
) -> Path:
    try:
        return source.fetch(info, name, folder, max_bytes=maximum)
    except SourceError as exc:
        raise _source_error(exc) from exc


def verify_release_directory(
    directory: Path,
    version: str,
    allowed_signers: Path,
    *,
    allow_unsigned: bool = False,
) -> dict[str, Any]:
    document, signature = verify_release_folder(
        directory, version, allowed_signers, allow_unsigned
    )
    return {
        "manifest": document,
        "components": document["components"],
        "signature": signature,
        "directory": str(directory),
        "release_dir": str(directory),
        "manifest_sha256": hashlib.sha256(
            (directory / "components.json").read_bytes()
        ).hexdigest(),
    }


def _template_version(entry: TemplateEntry) -> str | None:
    prefix = names.TEMPLATE_PREFIX + "v"
    if not entry.name.startswith(prefix):
        return None
    try:
        return names.check_version(entry.name[len(prefix) :])
    except ValueError:
        return None


def _username(entry: TemplateEntry) -> str | None:
    raw = entry.raw
    possibilities = [raw.get("username"), raw.get("default_username")]
    for key in ("default_credentials", "credentials"):
        value = raw.get(key)
        if isinstance(value, dict):
            possibilities.append(value.get("username"))
    users = raw.get("users")
    if isinstance(users, list):
        for user in users:
            possibilities.append(
                user.get("username") if isinstance(user, dict) else user
            )
    return next(
        (u for u in possibilities if isinstance(u, str) and _USER.fullmatch(u)), None
    )


def _catalog_size(entry: TemplateEntry) -> int:
    value = entry.raw.get("size_bytes", entry.raw.get("size"))
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    if isinstance(value, str):
        match = re.fullmatch(
            r"\s*(\d+(?:[.,]\d+)?)\s*(GB|GiB|MB|MiB)\s*", value, re.IGNORECASE
        )
        if match:
            unit = 1024**3 if match[2].lower() in ("gb", "gib") else 1024**2
            return int(float(match[1].replace(",", ".")) * unit)
    return 0


def register_phases(
    registry: pipeline.PhaseRegistry,
    *,
    source: ReleaseSource | None = None,
    verifier: Callable = verify_release_folder,
    free_bytes: Callable[[Path], int] = lambda p: shutil.disk_usage(p).free,
) -> None:
    def pick(ctx: pipeline.PhaseContext) -> dict:
        if any(t.name == ctx.names.template for t in ctx.templates.list_templates()):
            raise Deferred(f"already published externally: {ctx.names.template}")
        src = source or source_for(ctx.config, ctx.options.release_dir)
        selected = next(
            (
                r
                for r in _listed(src, ctx.options.allow_prerelease)
                if r.version == ctx.version
            ),
            None,
        )
        if selected is None:
            raise Blocked(
                f"release v{ctx.version} is unavailable in the selected channel"
            )
        return _info(selected)

    def _preflight(ctx: pipeline.PhaseContext) -> dict:
        if ctx.esxi.host not in ctx.config.esxi.allowed_hosts:
            raise Blocked("ESXi host is outside the configured allow list")
        try:
            ctx.esxi.check_login()
            ctx.templates.check()
            catalog = ctx.templates.list_templates()
            folders = ctx.share.list_folders()
            space = ctx.share.free_bytes()
        except TransientError as exc:
            raise Deferred(
                "preflight services are unavailable: " + exc.message
            ) from exc
        if ctx.config.esxi.datastore not in ctx.esxi.list_datastores():
            raise Blocked("configured ESXi datastore does not exist")
        if ctx.config.esxi.network not in ctx.esxi.list_networks():
            raise Blocked("configured ESXi network does not exist")
        limits = ctx.config.limits
        if free_bytes(ctx.state_dir) < limits.build_disk_free_gb * 1024**3:
            raise Deferred("not enough free space on the build host")
        required_share = limits.share_free_gb * 1024**3
        for t in catalog:
            if t.name.startswith(names.TEMPLATE_PREFIX):
                required_share = max(required_share, 3 * _catalog_size(t))
        if space < required_share:
            raise Deferred("not enough free space on the template share")
        if (
            ctx.esxi.datastore_free_bytes(ctx.config.esxi.datastore)
            < limits.datastore_free_gb * 1024**3
        ):
            raise Deferred("not enough free space on the ESXi datastore")
        names.check_free(
            ctx.names,
            template_names=[t.name for t in catalog],
            share_folders=folders,
            vm_names=[vm.name for vm in ctx.esxi.list_vms()],
            run=ctx.run_id,
        )
        return {
            "catalog": [{"name": t.name, "id": t.id} for t in catalog],
            "share_free_bytes": space,
        }

    def preflight(ctx: pipeline.PhaseContext) -> dict:
        try:
            return _preflight(ctx)
        except TransientError as exc:
            raise Deferred(
                "preflight services are unavailable: " + exc.message
            ) from exc

    def download(ctx: pipeline.PhaseContext) -> dict:
        src = source or source_for(ctx.config, ctx.options.release_dir)
        info = _restore(ctx.result_of("P0"))
        folder = ctx.subdir("release")
        _fetch(src, info, "components.json", folder, CONTROL_LIMIT)
        try:
            manifest = mf.load(folder / "components.json")
        except mf.ManifestError as exc:
            raise Blocked(
                "invalid release manifest: " + "; ".join(exc.problems)
            ) from exc
        if manifest.version != ctx.version or manifest.tag != info.tag:
            raise Blocked("requested release, tag and manifest versions differ")
        try:
            src.fetch(info, "components.json.sig", folder, max_bytes=CONTROL_LIMIT)
        except SourceError as exc:
            if exc.kind != "not_found" or not ctx.options.allow_unsigned:
                raise _source_error(exc) from exc
        signature = folder / "components.json.sig"
        if signature.is_file():
            valid, reason = verify_with_ssh_keygen(
                (folder / "components.json").read_bytes(),
                signature,
                ctx.config.paths.allowed_signers,
            )
            if not valid:
                raise Blocked("release signature is not trusted: " + reason)
        elif not ctx.options.allow_unsigned:
            raise Blocked("release signature is missing")
        _fetch(src, info, "SHA256SUMS", folder, CONTROL_LIMIT)
        for component in manifest.components.values():
            if component.size > ASSET_LIMIT:
                raise Blocked("component archive exceeds the build host asset limit")
            _fetch(src, info, component.asset, folder, component.size)
        document, signature_note = verifier(
            folder,
            ctx.version,
            ctx.config.paths.allowed_signers,
            ctx.options.allow_unsigned,
        )
        digest = hashlib.sha256((folder / "components.json").read_bytes()).hexdigest()
        ctx.note("manifest_sha256", digest)
        ctx.note("components", document["components"])
        ctx.note("signature", signature_note)
        return {
            "directory": str(folder),
            "release_dir": str(folder),
            "manifest": document,
            "manifest_sha256": digest,
            "signature": signature_note,
            "components": document["components"],
        }

    def base(ctx: pipeline.PhaseContext) -> dict:
        if ctx.options.clean_base:
            raise Blocked(
                "clean Debian base creation is a later work package; choose a published appliance base"
            )
        entries = ctx.templates.list_templates()
        if ctx.options.base:
            candidates = [e for e in entries if e.name == ctx.options.base]
        else:
            candidates = [
                e
                for e in entries
                if (v := _template_version(e))
                and not mf.is_prerelease(v)
                and mf.semver_key(v) < mf.semver_key(ctx.version)
            ]
            candidates.sort(
                key=lambda e: mf.semver_key(_template_version(e)), reverse=True
            )
        if not candidates:
            raise Blocked("no suitable published base image is available")
        chosen = candidates[0]
        version = _template_version(chosen)
        if version is None or (
            mf.is_prerelease(version) and not ctx.options.allow_prerelease
        ):
            raise Blocked("the selected base must have a supported appliance version")
        url = ctx.share.url_for(chosen.source_dir, chosen.file)
        head = ctx.share.head(url)
        if not head.importable or not head.content_length:
            raise Blocked(
                "base OVA must support HTTP HEAD, Content-Length and byte ranges"
            )
        data = {
            "name": chosen.name,
            "template": chosen.name,
            "version": version,
            "id": chosen.id,
            "file": chosen.file,
            "source_dir": chosen.source_dir,
            "url": url,
        }
        user = _username(chosen)
        if user:
            data["username"] = user
        ctx.note("base", data)
        return data

    for pid, fn, description in [
        ("P0", pick, "select the requested unpublished release"),
        ("P1", preflight, "check the host, share, space and immutable names"),
        ("P2", download, "download and verify the signed release with pinned tooling"),
        ("P3", base, "select a readable stable base image"),
    ]:
        registry.register(pid, fn, describe=lambda ctx, text=description: text)
