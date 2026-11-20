#!/usr/bin/env python3
"""
Builds the asset set of one appliance release: the files `ginnungagap update` downloads (or reads from an offline folder).

    python3 release/build_release.py --pins release/components.in.yml --out DIR [options]

The pin file says which tag of which component repository goes into this appliance release. For each of the five components
the script gets the source tarball (`<Repo>-<version>.tar.gz`, one top folder `<Repo>-<version>/`), checks that the tag equals
the version file inside it, and writes into DIR

    components.json        the manifest (schema 1, validated with ginnungagap.manifest before it is written)
    components.json.sig    only with --sign: an SSH signature (ssh-keygen -Y sign -n ginnungagap-release)
    SHA256SUMS             sha256sum of every other file in DIR (the signature excepted)
    <Repo>-<version>.tar.gz   the five tarballs

Where a tarball comes from, in this order: --sources DIR (a folder that already has it), for the appliance itself `git archive`
of --appliance-dir at --ref, --checkouts DIR (a folder with a checkout per repository: `git archive` at the tag), --fetch (the
GitHub release of that repository: without a token through the predictable download addresses
github.com/<repo>/releases/download/<tag>/<file>, which are no API calls and so have no rate limit; with the token in the
environment variable COMPONENTS_READ_TOKEN through the REST API, which also reads private repositories).
Nothing is published and nothing is pushed: the script only writes files into DIR. Standard library plus PyYAML (the pin file).

    python3 release/build_release.py --verify DIR [--allowed-signers FILE]     checks an existing asset folder
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml

from ginnungagap import manifest as manifest_module

SIGNATURE_NAMESPACE = "ginnungagap-release"
SIGNER_IDENTITY = "ginnungagap-release"
DEFAULT_API = "https://api.github.com"
DEFAULT_WEB = "https://github.com"
API_MODES = ("auto", "always", "never")
TOKEN_VARIABLE = "COMPONENTS_READ_TOKEN"
MAX_TARBALL_BYTES = (
    500 * 1024 * 1024
)  # the appliance refuses larger ones too (update plan, 7.3)

# where each component keeps the version that its tag has to equal
VERSION_FILES = {
    "appliance": "ginnungagap/version.py",
    "operator": "app/version.py",
    "builder": "pyproject.toml",
    "template-services": "VERSION",
    "generator": "VERSION",
}
_PY_VERSION = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE)
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class ReleaseError(Exception):
    """The release can't be built; the message says what to fix. Nothing half-built is left in the output folder."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # the HTTP status of a failed download, else None


# ---- reading a tarball ----------------------------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def version_in_file(component: str, text: str) -> str | None:
    """The version a component's version file declares, None when it has none."""
    if component == "builder":
        try:
            version = tomllib.loads(text).get("project", {}).get("version")
        except tomllib.TOMLDecodeError:
            return None
        return version if isinstance(version, str) else None
    if component in ("template-services", "generator"):
        return text.strip() or None
    found = _PY_VERSION.search(text)
    return found.group(1) if found else None


def inspect_tarball(
    path: Path, component: str, repo_name: str, version: str
) -> tuple[str, str | None]:
    """
    Checks the tarball's layout and returns (the version its version file declares, the commit a `git archive` recorded).
    The layout is the one `git archive --prefix=<Repo>-<version>/` makes: every member below that single folder.
    :raises ReleaseError:
    """
    top = f"{repo_name}-{version}"
    try:
        with tarfile.open(path, "r:gz") as archive:
            wanted = f"{top}/{VERSION_FILES[component]}"
            text = None
            for member in archive:
                name = member.name.rstrip("/")
                if name.startswith("/") or ".." in Path(name).parts:
                    raise ReleaseError(
                        f"{path.name}: the member {member.name!r} leaves the folder {top}/"
                    )
                if name != top and not name.startswith(top + "/"):
                    raise ReleaseError(
                        f"{path.name}: every file must be below {top}/ (found {member.name!r}) - "
                        f"the archive prefix has to be {top}/"
                    )
                if member.name.rstrip("/") == wanted and member.isfile():
                    handle = archive.extractfile(member)
                    text = handle.read().decode("utf-8", "replace") if handle else None
            commit = archive.pax_headers.get("comment")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ReleaseError(f"{path.name}: not a readable .tar.gz ({exc})") from exc
    if text is None:
        raise ReleaseError(
            f"{path.name}: {top}/{VERSION_FILES[component]} is missing, so the version can't be checked"
        )
    declared = version_in_file(component, text)
    if declared is None:
        raise ReleaseError(
            f"{path.name}: {VERSION_FILES[component]} declares no version"
        )
    return declared, commit if commit and _COMMIT.match(commit) else None


# ---- getting a tarball ----------------------------------------------------------------------------------------------


def git_archive(repo_dir: Path, ref: str, prefix: str, dest: Path) -> str:
    """A reproducible `<prefix>…tar.gz` of `ref` in `repo_dir` (no timestamps in the gzip header). Returns the commit."""

    def git(*args: str) -> bytes:
        done = subprocess.run(
            ["git", "-C", str(repo_dir), *args], capture_output=True, check=False
        )
        if done.returncode != 0:
            raise ReleaseError(
                f"git {' '.join(args[:2])} failed in {repo_dir}: {done.stderr.decode(errors='replace').strip()}"
            )
        return done.stdout

    commit = git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    tar_bytes = git("archive", "--format=tar", f"--prefix={prefix}/", commit)
    with (
        dest.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=9) as packed,
    ):
        packed.write(tar_bytes)
    return commit


class _NoAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would copy the Authorization header to the signed download address, which answers 400 to it (and must not get it)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Authorization")
        return new


def _http(url: str, token: str | None, accept: str) -> bytes:
    headers = {
        "Accept": accept,
        "User-Agent": "ginnungagap-build-release",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = urllib.request.build_opener(_NoAuthOnRedirect)
    try:
        with opener.open(
            urllib.request.Request(url, headers=headers), timeout=60
        ) as response:
            data = response.read(MAX_TARBALL_BYTES + 1)
    except urllib.error.HTTPError as exc:
        hint = (
            " (is the token missing, expired or without read access to the repository?)"
            if exc.code in (401, 403, 404)
            else ""
        )
        raise ReleaseError(f"{url}: HTTP {exc.code}{hint}", exc.code) from None
    except (urllib.error.URLError, OSError) as exc:
        raise ReleaseError(f"{url}: {exc}") from None
    if len(data) > MAX_TARBALL_BYTES:
        raise ReleaseError(
            f"{url}: larger than {MAX_TARBALL_BYTES // (1024 * 1024)} MB"
        )
    return data


def fetch_release_asset(
    repo: str,
    tag: str,
    name: str,
    api: str,
    token: str | None,
    web: str = DEFAULT_WEB,
    api_mode: str = "auto",
) -> bytes:
    """
    One asset of the GitHub release `tag` of `owner/repo`. `auto`: with a token through the API (a private repository needs
    it), without one through the download address (no API call, no rate limit), and through the API when that fails with
    anything but a 404; `always`: the API; `never`: the download address.
    """
    if api_mode not in API_MODES:
        raise ReleaseError(f"--api-mode: must be one of {', '.join(API_MODES)}")
    use_web = api_mode == "never" or (api_mode == "auto" and not token)
    if use_web:
        try:
            return _http(
                f"{web}/{repo}/releases/download/{tag}/{name}",
                None,
                "application/octet-stream",
            )
        except ReleaseError as exc:
            if api_mode == "never" or exc.status == 404:
                raise
    return _fetch_via_api(repo, tag, name, api, token)


def _fetch_via_api(
    repo: str, tag: str, name: str, api: str, token: str | None
) -> bytes:
    release = json.loads(
        _http(
            f"{api}/repos/{repo}/releases/tags/{tag}",
            token,
            "application/vnd.github+json",
        )
    )
    for asset in release.get("assets", []):
        if asset.get("name") == name:
            return _http(
                f"{api}/repos/{repo}/releases/assets/{asset['id']}",
                token,
                "application/octet-stream",
            )
    raise ReleaseError(f"{repo} {tag}: the release has no asset {name}")


# ---- the pin file ---------------------------------------------------------------------------------------------------


def load_pins(path: Path) -> dict[str, Any]:
    """:raises ReleaseError: with every problem of the pin file at once."""
    try:
        pins = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ReleaseError(f"{path}: can't be read ({exc})") from exc
    problems = []
    if not isinstance(pins, dict) or not isinstance(pins.get("components"), dict):
        raise ReleaseError(f"{path}: needs a `components:` mapping")
    for name in manifest_module.COMPONENT_NAMES:
        entry = pins["components"].get(name)
        if not isinstance(entry, dict):
            problems.append(f"components.{name}: missing")
            continue
        repo, tag = entry.get("repo"), entry.get("tag")
        expected = manifest_module.REPO_NAMES[name]
        if (
            not isinstance(repo, str)
            or repo.split("/")[-1] != expected
            or repo.count("/") != 1
        ):
            problems.append(f"components.{name}.repo: must be <owner>/{expected}")
        if (
            not isinstance(tag, str)
            or not tag.startswith("v")
            or not manifest_module.is_semver(tag[1:])
        ):
            problems.append(
                f"components.{name}.tag: must look like v1.3.0 or v1.3.0-rc.1"
            )
        if not isinstance(entry.get("data_migration", False), bool):
            problems.append(f"components.{name}.data_migration: must be true or false")
    extra = sorted(set(pins["components"]) - set(manifest_module.COMPONENT_NAMES))
    if extra:
        problems.append(f"components: unknown component(s) {', '.join(extra)}")
    head = pins.get("appliance") or {}
    if not isinstance(head, dict):
        problems.append("appliance: must be a mapping")
    else:
        minimum = head.get("min_installed")
        if minimum not in (None, "") and not manifest_module.is_semver(minimum):
            problems.append("appliance.min_installed: must be a version like 1.2.0")
        if not isinstance(head.get("notes", ""), str):
            problems.append("appliance.notes: must be text")
    if problems:
        raise ReleaseError(f"{path}:\n  " + "\n  ".join(problems))
    return pins


# ---- building -------------------------------------------------------------------------------------------------------


def _obtain(
    name: str,
    entry: dict[str, Any],
    dest: Path,
    *,
    sources: Path | None,
    checkouts: Path | None,
    appliance_dir: Path,
    ref: str | None,
    fetch: bool,
    api: str,
    token: str | None,
    web: str = DEFAULT_WEB,
    api_mode: str = "auto",
) -> str | None:
    """Puts the component's tarball at `dest`; returns the commit when git told it. :raises ReleaseError:"""
    repo_name = manifest_module.REPO_NAMES[name]
    version = entry["tag"][1:]
    asset = dest.name
    prefix = f"{repo_name}-{version}"
    if sources is not None and (sources / asset).is_file():
        shutil.copyfile(sources / asset, dest)
        return None
    if name == "appliance":
        return git_archive(appliance_dir, ref or entry["tag"], prefix, dest)
    if checkouts is not None and (checkouts / repo_name).is_dir():
        return git_archive(checkouts / repo_name, entry["tag"], prefix, dest)
    if fetch:
        data = fetch_release_asset(
            entry["repo"], entry["tag"], asset, api, token, web, api_mode
        )
        try:
            checksum = (
                fetch_release_asset(
                    entry["repo"],
                    entry["tag"],
                    asset + ".sha256",
                    api,
                    token,
                    web,
                    api_mode,
                )
                .decode()
                .split()[0]
            )
        except (ReleaseError, IndexError):
            raise ReleaseError(
                f"{entry['repo']} {entry['tag']}: the release has no usable {asset}.sha256"
            ) from None
        if hashlib.sha256(data).hexdigest() != checksum:
            raise ReleaseError(
                f"{asset}: the download does not match the release's own {asset}.sha256"
            )
        dest.write_bytes(data)
        return None
    raise ReleaseError(
        f"no source for {asset}: put it into --sources, give --checkouts with a checkout of {repo_name}, or use --fetch"
    )


def build(
    pins_path: Path,
    out: Path,
    *,
    sources: Path | None = None,
    checkouts: Path | None = None,
    appliance_dir: Path = ROOT,
    ref: str | None = None,
    fetch: bool = False,
    api: str = DEFAULT_API,
    token: str | None = None,
    web: str = DEFAULT_WEB,
    api_mode: str = "auto",
    sign_key: Path | None = None,
    expect_tag: str | None = None,
    released: str | None = None,
    previous: Path | None = None,
    notes_file: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Writes the asset set into `out` and returns the manifest. :raises ReleaseError: nothing is left in `out` then."""
    pins = load_pins(pins_path)
    if expect_tag is not None and pins["components"]["appliance"]["tag"] != expect_tag:
        raise ReleaseError(
            f"the release is for the tag {expect_tag}, but {pins_path.name} pins the appliance at "
            f"{pins['components']['appliance']['tag']}"
        )
    if out.exists() and any(out.iterdir()) and not force:
        raise ReleaseError(
            f"{out} is not empty (use an empty folder, or --force to replace its release files)"
        )
    if sign_key is not None and not shutil.which("ssh-keygen"):
        raise ReleaseError("--sign needs ssh-keygen (openssh-client)")
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".release-", dir=out.parent))
    try:
        components: dict[str, Any] = {}
        for name in manifest_module.COMPONENT_NAMES:
            entry = pins["components"][name]
            repo_name = manifest_module.REPO_NAMES[name]
            version = entry["tag"][1:]
            dest = work / f"{repo_name}-{version}.tar.gz"
            commit = _obtain(
                name, entry, dest, sources=sources, checkouts=checkouts, appliance_dir=appliance_dir, ref=ref,
                fetch=fetch, api=api, token=token, web=web, api_mode=api_mode,
            )  # fmt: skip
            declared, recorded = inspect_tarball(dest, name, repo_name, version)
            if declared != version:
                raise ReleaseError(
                    f"{repo_name}: the tag {entry['tag']} but {VERSION_FILES[name]} says {declared} - "
                    "raise the version file (or fix the pin) before releasing"
                )
            commit = commit or recorded
            if commit is None:
                raise ReleaseError(
                    f"{dest.name}: no commit id recorded in the tarball (use a `git archive` tarball)"
                )
            components[name] = {
                "repo": entry["repo"],
                "version": version,
                "tag": entry["tag"],
                "commit": commit,
                "asset": dest.name,
                "sha256": sha256_of(dest),
                "size": dest.stat().st_size,
                "services": list(manifest_module.DEFAULT_SERVICES[name]),
                "data_migration": bool(entry.get("data_migration", False)),
            }
        version = components["appliance"]["version"]
        head = pins.get("appliance") or {}
        appliance: dict[str, Any] = {
            "version": version,
            "tag": f"v{version}",
            "channel": "prerelease"
            if manifest_module.is_prerelease(version)
            else "stable",
            "released": released
            or manifest_module.timestamp(manifest_module.now_utc()),
        }
        if head.get("min_installed"):
            appliance["min_installed"] = head["min_installed"]
        appliance["notes"] = head.get("notes") or ""
        document = {
            "schema": manifest_module.SCHEMA,
            "appliance": appliance,
            "components": components,
            "images": {},
        }
        try:
            manifest_module.validate(document)
        except manifest_module.ManifestError as exc:
            raise ReleaseError(
                "the manifest is not valid:\n  " + "\n  ".join(exc.problems)
            ) from exc
        (work / "components.json").write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )
        if sign_key is not None:
            sign(work / "components.json", sign_key)
        write_sums(work)
        if notes_file is not None:
            notes_file.write_text(release_notes(document, previous), encoding="utf-8")
        verify(work)
        if out.exists():
            for old in out.iterdir():
                old.unlink() if old.is_file() or old.is_symlink() else shutil.rmtree(
                    old
                )
            out.rmdir()
        work.rename(out)
        return document
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def sign(manifest_file: Path, key: Path) -> Path:
    """`components.json.sig` next to the manifest. The key file never leaves the machine and is never printed."""
    signature = manifest_file.with_name(manifest_file.name + ".sig")
    signature.unlink(missing_ok=True)
    done = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-q", "-f", str(key), "-n", SIGNATURE_NAMESPACE, str(manifest_file)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if done.returncode != 0 or not signature.is_file():
        raise ReleaseError(
            f"signing failed (is {key.name} a private SSH key without a passphrase?): {done.stderr.strip()}"
        )
    return signature


def write_sums(folder: Path) -> None:
    """
    SHA256SUMS in the format of `sha256sum`: the checksum of every other file in the folder except the signature (which is
    made from components.json and checked cryptographically, not by checksum).
    """
    lines = [
        f"{sha256_of(p)}  {p.name}"
        for p in sorted(folder.iterdir())
        if p.is_file() and p.name != "SHA256SUMS" and p.suffix != ".sig"
    ]
    (folder / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def release_notes(document: dict[str, Any], previous: Path | None) -> str:
    """The body of the appliance release: one line per component, from the previous release's manifest when it is given."""
    old_components: dict[str, Any] = {}
    old_version = None
    if previous is not None:
        try:
            old = json.loads(previous.read_text(encoding="utf-8"))
            old_components = old.get("components", {})
            old_version = old.get("appliance", {}).get("version")
        except (OSError, ValueError):
            old_components = {}
    appliance = document["appliance"]
    lines = [f"# Ginnungagap appliance {appliance['version']}", ""]
    if old_version:
        lines += [f"Update of {old_version}. Components that changed:", ""]
    changed = False
    for name, component in document["components"].items():
        before = old_components.get(name, {}).get("version")
        if before and before == component["version"]:
            continue
        changed = True
        arrow = (
            f"{before} -> {component['version']}" if before else component["version"]
        )
        lines.append(f"- {name} ({component['repo']}): {arrow}")
    if old_components and not changed:
        lines.append("- no component changed")
    if appliance.get("notes"):
        lines += ["", appliance["notes"]]
    lines += [
        "",
        "Install or update with `ginnungagap update` on the appliance. The files of this release are listed in `SHA256SUMS`.",
    ]
    return "\n".join(lines) + "\n"


# ---- checking an asset folder ---------------------------------------------------------------------------------------


def verify(
    folder: Path, allowed_signers: Path | None = None
) -> manifest_module.Manifest:
    """
    Everything the appliance checks, checked here: the manifest is valid, every tarball has the size and checksum it states,
    SHA256SUMS agrees, and (with `allowed_signers`) the signature is valid. :raises ReleaseError: with every problem.
    """
    problems: list[str] = []
    try:
        manifest = manifest_module.load(folder / "components.json")
    except manifest_module.ManifestError as exc:
        raise ReleaseError("components.json: " + "; ".join(exc.problems)) from exc
    sums: dict[str, str] = {}
    try:
        for line in (folder / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            checksum, _, name = line.partition("  ")
            sums[name] = checksum
    except OSError:
        problems.append("SHA256SUMS: missing")
    for component in manifest.components.values():
        path = folder / component.asset
        if not path.is_file():
            problems.append(f"{component.asset}: missing")
            continue
        if path.stat().st_size != component.size:
            problems.append(
                f"{component.asset}: {path.stat().st_size} bytes, components.json says {component.size}"
            )
        if sha256_of(path) != component.sha256:
            problems.append(
                f"{component.asset}: the checksum differs from components.json"
            )
        if sums and sums.get(component.asset) != component.sha256:
            problems.append(f"{component.asset}: the checksum differs from SHA256SUMS")
    for name, checksum in sums.items():
        target = folder / name
        if not target.is_file() or sha256_of(target) != checksum:
            problems.append(f"{name}: does not match SHA256SUMS")
    if sums and "components.json" not in sums:
        problems.append("SHA256SUMS: components.json is not listed")
    signature = folder / "components.json.sig"
    if allowed_signers is not None:
        if not signature.is_file():
            problems.append("components.json.sig: missing")
        else:
            with (folder / "components.json").open("rb") as handle:
                done = subprocess.run(
                    ["ssh-keygen", "-Y", "verify", "-f", str(allowed_signers), "-I", SIGNER_IDENTITY,
                     "-n", SIGNATURE_NAMESPACE, "-s", str(signature)],
                    stdin=handle, capture_output=True, text=True, check=False,
                )  # fmt: skip
            if done.returncode != 0:
                problems.append(
                    "components.json.sig: the signature is not valid for the keys in allowed_signers"
                )
    if problems:
        raise ReleaseError(
            "the asset folder is not consistent:\n  " + "\n  ".join(problems)
        )
    return manifest


# ---- command line ---------------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Builds the asset set of one appliance release (nothing is published)."
    )
    parser.add_argument(
        "--pins", type=Path, default=ROOT / "release" / "components.in.yml"
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="the folder that receives the release files (new or empty)",
    )
    parser.add_argument(
        "--sources",
        type=Path,
        help="a folder that already holds <Repo>-<version>.tar.gz files",
    )
    parser.add_argument(
        "--checkouts",
        type=Path,
        help="a folder with a git checkout per component repository, archived at its tag",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help=f"download missing tarballs from the GitHub releases ({TOKEN_VARIABLE} may hold a token)",
    )
    parser.add_argument(
        "--api-mode",
        choices=API_MODES,
        default=os.environ.get("GINNUNGAGAP_RELEASE_API", "auto"),
        help="with --fetch: auto = download addresses without a token (no API quota), the API with one; always = API; never = download addresses",
    )
    parser.add_argument("--api-url", default=DEFAULT_API, help=argparse.SUPPRESS)
    parser.add_argument("--web-url", default=DEFAULT_WEB, help=argparse.SUPPRESS)
    parser.add_argument(
        "--appliance-dir",
        type=Path,
        default=ROOT,
        help="the appliance checkout (default: this one)",
    )
    parser.add_argument(
        "--ref",
        help="what to archive of the appliance checkout (default: the tag in the pin file)",
    )
    parser.add_argument(
        "--sign",
        type=Path,
        metavar="KEY",
        help="private SSH key (no passphrase) that signs components.json",
    )
    parser.add_argument(
        "--tag",
        help="the tag this release is built for (the workflow's tag): refused when the pin file says another one",
    )
    parser.add_argument(
        "--released",
        help="the release time, UTC like 2026-12-01T10:00:00Z (default: now)",
    )
    parser.add_argument(
        "--previous",
        type=Path,
        help="components.json of the previous release, for the release notes",
    )
    parser.add_argument(
        "--notes-file",
        type=Path,
        help="write the release notes (markdown) here; it is not a release asset",
    )
    parser.add_argument(
        "--force", action="store_true", help="replace the files of a non-empty --out"
    )
    parser.add_argument(
        "--verify", type=Path, metavar="DIR", help="only check an existing asset folder"
    )
    parser.add_argument(
        "--allowed-signers",
        type=Path,
        help="with --verify: also check components.json.sig against this file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.verify is not None:
            manifest = verify(args.verify, args.allowed_signers)
            print(f"{args.verify}: release {manifest.version} is consistent")
            return 0
        if args.out is None:
            raise ReleaseError("--out is required")
        if args.sign is not None and not args.sign.is_file():
            raise ReleaseError(f"--sign: {args.sign} is not a file")
        document = build(
            args.pins, args.out, sources=args.sources, checkouts=args.checkouts, appliance_dir=args.appliance_dir,
            ref=args.ref, fetch=args.fetch, api=args.api_url.rstrip("/"), token=os.environ.get(TOKEN_VARIABLE) or None, web=args.web_url.rstrip("/"), api_mode=args.api_mode,
            sign_key=args.sign, expect_tag=args.tag, released=args.released, previous=args.previous, notes_file=args.notes_file, force=args.force,
        )  # fmt: skip
    except ReleaseError as exc:
        print(f"build_release: {exc}", file=sys.stderr)
        return 1
    version = document["appliance"]["version"]
    print(
        f"Release {version} ({document['appliance']['channel']}) written to {args.out}:"
    )
    for path in sorted(args.out.iterdir()):
        print(f"  {path.name}")
    if args.sign is None:
        print(
            "components.json is NOT signed (no --sign): the appliance accepts it only while update.require_signature is not enforced."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
