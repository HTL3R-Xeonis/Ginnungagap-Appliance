"""
Release folders for the tests of `ginnungagap update`, written against the manifest described in the update plan
(section 5.1/5.2) and NOT against `ginnungagap.manifest`, so a mistake there can't hide itself here.

`make_release` writes what a GitHub release of the appliance holds: `components.json`, `SHA256SUMS`, the five tarballs
(and `components.json.sig` when asked). The tarballs are tiny: a version file and, for every service, a Dockerfile of a few
lines (a stdlib HTTP server that answers /health), so a smoke test builds them in seconds.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

REPOS = {
    "appliance": "Ginnungagap-Appliance",
    "operator": "Igdrasyl",
    "builder": "TopologyBuilder",
    "template-services": "Hugyn-and-Munyn",
    "generator": "Mimyr",
}
# the names the manifest table reads but never writes (for releases, state and checkouts of the old layout; the builder's
# repository keeps TopologyBuilder, "Mjonyr" is only a name from while a rename was planned)
OLD_REPOS = {
    "appliance": "Ginnungagap-Appliance",
    "operator": "TopologyOperator",
    "builder": "Mjonyr",
    "template-services": "Docker-Server-Files",
    "generator": "TopologyGenerator",
}
# the keys of before the roles: components.json and installed.json of that time
OLD_KEYS = {
    "template-services": "docker-server-files",
    "generator": "topology-generator",
}
SERVICES = {
    "appliance": ["setup"],
    "operator": ["ginnungagap"],
    "builder": ["ginnungagap"],
    "template-services": [
        "template-esxi",
        "collector-esxi",
        "template-gns3",
        "collector-gns3",
    ],
    "generator": ["generator"],
}
DEFAULT_VERSIONS = {
    "operator": "1.2.1",
    "builder": "1.0.0",
    "template-services": "1.0.0",
    "generator": "1.0.0",
}
OWNER = "HTL3R-Xeonis"

# where a service's Dockerfile sits in its component's tarball (the context compose builds from)
DOCKERFILES = {
    "ginnungagap": ("operator", "Dockerfile"),
    "template-esxi": ("template-services", "yaml-api/Dockerfile"),
    "template-gns3": ("template-services", "gns3-template-api/gns3_api.Dockerfile"),
    "generator": ("generator", "topology.Dockerfile"),
    "setup": ("appliance", "setup/Dockerfile"),
}

SERVER = """\
import http.server, json, os

VERSION = open("/VERSION").read().strip()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", "version": VERSION, "warning": None}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


http.server.HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
"""


def dockerfile(version: str, broken: bool) -> str:
    lines = [
        f"FROM {os.environ.get('FAKE_BASE_IMAGE', 'python:3.12-slim')}",
        f'RUN echo "{version}" > /VERSION',
    ]
    if broken:
        lines.append("RUN echo 'this service is broken on purpose' && false")
    lines += ["COPY server.py /server.py", 'CMD ["python", "/server.py"]']
    return "\n".join(lines) + "\n"


def fake_commit(name: str, version: str) -> str:
    return hashlib.sha1(f"{name} {version}".encode()).hexdigest()


def _files(name: str, version: str, broken: set[str]) -> dict[str, str]:
    """The files of a component's tarball, relative to its top folder."""
    files = {
        "README.md": f"{REPOS[name]} {version}\n",
        "LICENSE": "test licence\n",
    }
    if name == "appliance":
        files["ginnungagap/version.py"] = f'__version__ = "{version}"\n'
        # the program of a release is installed by its install.sh: the fake one only records that it was called
        files["install.sh"] = (
            "#!/bin/sh\n"
            "# options: --repos-dir --config-dir --state-dir --manifest --prefix --no-start (the real script lists them like this)\n"
            'echo "install.sh $*" >> "${GINNUNGAGAP_FAKE_INSTALL_LOG:-/dev/null}"\n'
        )
    elif name == "operator":
        files["app/version.py"] = f'__version__ = "{version}"\n'
    elif name == "builder":
        files["pyproject.toml"] = (
            f'[project]\nname = "topologybuilder"\nversion = "{version}"\n'
        )
    else:
        files["VERSION"] = f"{version}\n"
    if name == "generator":
        # what `ginnungagap render` reads from the generator's checkout
        files["config/prompt_template.txt"] = "PROMPT {request}\n"
        files["config/example_topology.yml"] = "nodes: []\n"
    for service, (owner, path) in DOCKERFILES.items():
        if owner == name:
            files[path] = dockerfile(version, service in broken)
            files[str(Path(path).with_name("server.py"))] = SERVER
    return files


def write_tarball(path: Path, top: str, files: dict[str, str | bytes]) -> None:
    """A gzip tarball with one top folder and fixed times, so equal content gives an equal checksum."""
    with (
        open(path, "wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        folder = tarfile.TarInfo(top)
        folder.type = tarfile.DIRTYPE
        folder.mode = 0o755
        tar.addfile(folder)
        for name, content in sorted(files.items()):
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            tar.addfile(info, io.BytesIO(data))


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_signing_key(folder: Path) -> tuple[Path, Path]:
    """A throw-away ssh key and the `allowed_signers` file that trusts it: (private key, allowed_signers)."""
    folder.mkdir(parents=True, exist_ok=True)
    key = folder / "release_key"
    if not key.exists():
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "test",
                "-f",
                str(key),
            ],
            check=True,
        )
    public = (folder / "release_key.pub").read_text().split()
    signers = folder / "allowed_signers"
    signers.write_text(
        f'ginnungagap-release namespaces="ginnungagap-release" {public[0]} {public[1]}\n'
    )
    return key, signers


def sign_file(key: Path, path: Path) -> Path:
    """`path`.sig, the signature `ssh-keygen -Y sign` makes in the namespace of the releases."""
    sig = Path(str(path) + ".sig")
    sig.unlink(missing_ok=True)
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-Y",
            "sign",
            "-f",
            str(key),
            "-n",
            "ginnungagap-release",
            str(path),
        ],
        check=True,
    )
    return sig


def make_release(
    dest: str | Path,
    appliance: str = "1.3.0",
    components: dict[str, str] | None = None,
    sign: bool | Path = False,
    break_service: str | None = None,
    data_migration: bool | set[str] = False,
    released: str = "2026-12-01T10:00:00Z",
    min_installed: str | None = None,
    notes: str = "",
    owner: str = OWNER,
    legacy: bool = False,
    repo_names: dict[str, str] | None = None,
) -> Path:
    """
    Writes one release into the folder `dest` (made if needed) and returns it.

    components      version per component name; the others get a default; `appliance` is always the appliance version
    sign            True: sign components.json with a new throw-away key kept in `<dest>.keys/` (its allowed_signers file is
                    `keys_of(dest)`); a Path: sign with that private key
    break_service   the service whose Dockerfile fails to build
    data_migration  True: the operator component says it migrates data; or a set of component names
    legacy          True: a release as it was built before the code names (old keys, old repository and asset names)
    repo_names      other repository names for some components (a later rename), by component key
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    versions = {**DEFAULT_VERSIONS, **(components or {}), "appliance": appliance}
    broken = {break_service} if break_service else set()
    migrating = {"operator"} if data_migration is True else set(data_migration or ())
    entries = {}
    sums = {}
    for name, repo in ((OLD_REPOS if legacy else REPOS) | (repo_names or {})).items():
        version = versions[name]
        asset = f"{repo}-{version}.tar.gz"
        path = dest / asset
        write_tarball(path, f"{repo}-{version}", _files(name, version, broken))
        entries[OLD_KEYS.get(name, name) if legacy else name] = {
            "repo": f"{owner}/{repo}",
            "version": version,
            "tag": f"v{version}",
            "commit": fake_commit(name, version),
            "asset": asset,
            "sha256": sha256_of(path),
            "size": path.stat().st_size,
            "services": SERVICES[name],
            "data_migration": name in migrating,
        }
        sums[asset] = sha256_of(path)
    manifest = {
        "schema": 1,
        "appliance": {
            "version": appliance,
            "tag": f"v{appliance}",
            "channel": "prerelease" if "-" in appliance else "stable",
            "released": released,
            "min_installed": min_installed,
            "notes": notes,
        },
        "components": entries,
        "images": {},
    }
    manifest_path = dest / "components.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    sums["components.json"] = sha256_of(manifest_path)
    (dest / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items())),
        encoding="utf-8",
    )
    if sign:
        key = (
            sign
            if isinstance(sign, Path)
            else make_signing_key(keys_of(dest).parent)[0]
        )
        sign_file(key, manifest_path)
    return dest


def keys_of(release: Path) -> Path:
    """The `allowed_signers` file that trusts the key `make_release(..., sign=True)` made for the release folder."""
    return Path(str(release) + ".keys") / "allowed_signers"


def rewrite_manifest(release: Path, change) -> None:
    """Applies `change(dict)` to a release's components.json and fixes SHA256SUMS (a signature is left stale on purpose)."""
    path = Path(release) / "components.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    sums = Path(release) / "SHA256SUMS"
    lines = [
        line
        for line in sums.read_text().splitlines()
        if not line.endswith("  components.json")
    ]
    lines.append(f"{sha256_of(path)}  components.json")
    sums.write_text("\n".join(sorted(lines, key=lambda x: x.split("  ")[1])) + "\n")


def copy_release(source: Path, dest: Path) -> Path:
    shutil.copytree(source, dest)
    return dest


def hostile_tarball(path: Path, kind: str, top_name: str = "Repo-1.0.0") -> None:
    """A tarball that tries something a safe unpacking must refuse: parent, absolute, symlink, device, bomb."""
    with (
        open(path, "wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        top = tarfile.TarInfo(top_name)
        top.type = tarfile.DIRTYPE
        tar.addfile(top)

        def add(name: str, data: bytes = b"x") -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        if kind == "parent":
            add(f"{top_name}/../../escaped.txt")
        elif kind == "absolute":
            add("/tmp/ginnungagap-escaped.txt")
        elif kind == "symlink":
            link = tarfile.TarInfo(f"{top_name}/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../outside"
            tar.addfile(link)
        elif kind == "hardlink":
            link = tarfile.TarInfo(f"{top_name}/hard")
            link.type = tarfile.LNKTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
        elif kind == "device":
            node = tarfile.TarInfo(f"{top_name}/null")
            node.type = tarfile.CHRTYPE
            node.devmajor, node.devminor = 1, 3
            tar.addfile(node)
        elif kind == "two-tops":
            add("Other-1.0.0/file")
        elif kind == "big":
            add(f"{top_name}/big.bin", b"\0" * (3 * 1024 * 1024))
        else:
            raise ValueError(kind)
