"""
The release manifest (`components.json`) and the state `ginnungagap update` keeps on the appliance.

A release of the appliance is a set of source tarballs, one per component, and a manifest that says which version of which
component is in it, with the checksum of every tarball. `validate` checks a manifest as a whole and reports every problem
at once (like `config.ConfigError`); nothing here touches the network or Docker. Standard library only.

The state folder holds `installed.json` (the manifest that is installed, plus when and what came before), a copy of every
installed manifest in `history/`, and is always written atomically so a crash never leaves half a file behind.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = 1

# The five components of schema 1 (section 5 of the update plan) and what belongs to each of them. New components need a new
# schema, so a client can refuse a manifest it does not understand. A key names the ROLE of a component, never a repository
# or a code name: the repositories can be renamed without touching a key (PLAN_component_codenames.md, section 2).
COMPONENT_NAMES = (
    "appliance",
    "operator",
    "builder",
    "template-services",
    "generator",
)
# The current repository of each component. The same name is the prefix of the release tarball (`<name>-<version>.tar.gz`), its
# top folder, the folder under releases/ and components/ on the appliance and the sibling folder compose.yml builds from. The
# tooling that BUILDS a release (release/build_release.py) enforces this table; a manifest that is only READ is checked for
# consistency instead (see `_component`), so a later rename of a component repository does not lock an installed appliance out.
REPO_NAMES = {
    "appliance": "Ginnungagap-Appliance",
    "operator": "Igdrasyl",
    "builder": "TopologyBuilder",  # the repository keeps its name; Mjonyr is only the code name (CODE_NAMES)
    "template-services": "Hugyn-and-Munyn",
    "generator": "Mimyr",
}
CHECKOUT_NAMES = REPO_NAMES
# The names this appliance used before the code names (read, never written): the old component keys, and per component the
# old repository / checkout folder names. They are needed for `--adopt` of checkouts that still carry them, for manifests of
# releases that were built with them and for an installed.json written by an earlier version of this program.
LEGACY_KEYS = {
    "docker-server-files": "template-services",
    "topology-generator": "generator",
}
LEGACY_NAMES = {
    "appliance": (),
    "operator": ("TopologyOperator",),
    "builder": (
        "Mjonyr",
    ),  # a state or checkout from while the repository was planned to be renamed
    "template-services": ("Docker-Server-Files",),
    "generator": ("TopologyGenerator", "topology-generator"),
}
# The names people use for the parts. The spelling is deliberate (the standard mythological spellings are wrong here;
# tests/test_codenames.py guards it). The appliance itself has no code name: it is the product, Ginnungagap.
CODE_NAMES = {
    "operator": "Igdrasyl",
    "builder": "Mjonyr",
    "template-services": "Hugyn and Munyn",
    "generator": "Mimyr",
}
# the same for the services of the compose project (the ESXi side is Hugyn, the GNS3 side Munyn)
SERVICE_CODE_NAMES = {
    "ginnungagap": "Igdrasyl",
    "template-esxi": "Hugyn",
    "collector-esxi": "Hugyn",
    "template-gns3": "Munyn",
    "collector-gns3": "Munyn",
    "generator": "Mimyr",
}
DEFAULT_SERVICES = {
    "appliance": ("setup",),
    "operator": ("ginnungagap",),
    "builder": ("ginnungagap",),
    "template-services": (
        "template-esxi",
        "collector-esxi",
        "template-gns3",
        "collector-gns3",
    ),
    "generator": ("generator",),
}
KNOWN_SERVICES = frozenset(
    service for services in DEFAULT_SERVICES.values() for service in services
)
CHANNELS = ("stable", "prerelease")


def _alias_table() -> dict[str, str]:
    table = {key: key for key in COMPONENT_NAMES}
    table.update(LEGACY_KEYS)
    for key, code in CODE_NAMES.items():
        table[code.lower().replace(" ", "-")] = key
    table["hugyn"] = table["munyn"] = (
        "template-services"  # both halves are one component (one release, one tarball)
    )
    for key, repo in REPO_NAMES.items():
        table[repo.lower()] = key
    for key, names in LEGACY_NAMES.items():
        for name in names:
            table[name.lower()] = key
    return table


_ALIASES = _alias_table()


def resolve_component(name: str) -> str | None:
    """
    The component key for what a person typed: the key itself, an old key, a code name (`igdrasyl`, `hugyn-and-munyn`) or a
    repository name, in any letter case. None when it is none of them.
    """
    return _ALIASES.get(name.strip().lower()) if isinstance(name, str) else None


def display_name(key: str) -> str:
    """`operator (Igdrasyl)`: the key with the code name of the part, for tables people read (the appliance has none)."""
    code = CODE_NAMES.get(key)
    return f"{key} ({code})" if code else key


def service_display_name(service: str) -> str:
    """`template-esxi (Hugyn)`: the same for a service of the compose project."""
    code = SERVICE_CODE_NAMES.get(service)
    return f"{service} ({code})" if code else service


_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/([A-Za-z0-9][A-Za-z0-9._-]*)$")
_DIRNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ManifestError(ValueError):
    """The manifest is not usable; `problems` lists everything that is wrong, one line each."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


# ---- versions -------------------------------------------------------------------------------------------------------


def is_semver(version: Any) -> bool:
    return isinstance(version, str) and _SEMVER.match(version) is not None


def is_prerelease(version: str) -> bool:
    return "-" in version


def semver_key(version: str) -> tuple:
    """
    A sort key for `MAJOR.MINOR.PATCH[-pre.release]`: `1.3.0-rc.1 < 1.3.0-rc.2 < 1.3.0 < 1.3.1`. Numeric identifiers of a
    pre-release compare as numbers and rank below alphanumeric ones (SemVer 2.0, without build metadata).
    :raises ValueError: not a version of that shape (`adopted`, `unknown`, `1.3`, `v1.3.0` ...).
    """
    match = _SEMVER.match(version) if isinstance(version, str) else None
    if match is None:
        raise ValueError(f"{version!r} is not a version like 1.3.0 or 1.3.0-rc.1")
    major, minor, patch, pre = match.groups()
    if pre is None:
        tail: tuple = (1,)
    else:
        parts = tuple(
            (0, int(part), "") if part.isdigit() else (1, 0, part)
            for part in pre.split(".")
        )
        tail = (0, parts)
    return (int(major), int(minor), int(patch), tail)


def version_kind(old: str | None, new: str) -> str:
    """
    How `new` differs from `old`: patch | minor | major | pre (only the pre-release part) | new (nothing installed) |
    replace (the installed one has no version number, e.g. an adopted checkout) | downgrade | same.
    """
    if old is None:
        return "new"
    if old == new:
        return "same"
    if not is_semver(old) or not is_semver(new):
        return "replace"
    old_key, new_key = semver_key(old), semver_key(new)
    if new_key < old_key:
        return "downgrade"
    for index, kind in ((0, "major"), (1, "minor"), (2, "patch")):
        if old_key[index] != new_key[index]:
            return kind
    return "pre"


# ---- the manifest ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Component:
    name: str
    repo: str
    version: str
    tag: str
    commit: str
    asset: str
    sha256: str
    size: int
    services: tuple[str, ...]
    data_migration: bool

    @property
    def repo_name(self) -> str:
        """The repository's name as the manifest gives it: the prefix of the tarball, its top folder and the asset name."""
        return self.repo.rsplit("/", 1)[-1]

    @property
    def checkout(self) -> str:
        """The folder name under releases/ and components/: this program's own name for the component (compose.yml uses it)."""
        return CHECKOUT_NAMES[self.name]


@dataclass(frozen=True)
class Manifest:
    schema: int
    version: str
    tag: str
    channel: str
    released: str
    min_installed: str | None
    notes: str
    components: dict[str, Component]
    images: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def data_migration(self) -> bool:
        return any(c.data_migration for c in self.components.values())

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)


def _asset_name(repo_name: str, version: str) -> str:
    return f"{repo_name}-{version}.tar.gz"


def _component(name: str, data: Any, problems: list[str]) -> Component | None:
    where = f"components.{name}"
    if not isinstance(data, dict):
        problems.append(f"{where}: must be an object")
        return None
    start = len(problems)

    def text(key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            problems.append(f"{where}.{key}: missing or not text")
            return ""
        return value

    repo = text("repo")
    match = _REPO.match(repo) if repo else None
    # consistency, not identity: whatever the repository is called now (or was called when the release was built), the asset
    # has to be named after it. The signature and the checksums are the trust anchor, the name is only a label.
    repo_name = match.group(1) if match else ""
    if repo and (match is None or not _DIRNAME.match(repo_name)):
        problems.append(
            f"{where}.repo: must look like <owner>/<repository>, not {repo!r}"
        )
        repo_name = ""
    version = text("version")
    if version and not is_semver(version):
        problems.append(f"{where}.version: {version!r} is not a version like 1.3.0")
    tag = text("tag")
    if tag and version and tag != f"v{version}":
        problems.append(f"{where}.tag: must be v{version}, not {tag!r}")
    commit = text("commit")
    if commit and not _COMMIT.match(commit):
        problems.append(f"{where}.commit: must be 40 lower-case hex digits")
    asset = text("asset")
    if asset and version and repo_name and asset != _asset_name(repo_name, version):
        problems.append(
            f"{where}.asset: must be {_asset_name(repo_name, version)} (the repository's name, the version), not {asset!r}"
        )
    sha = text("sha256")
    if sha and not _SHA256.match(sha):
        problems.append(f"{where}.sha256: must be 64 lower-case hex digits")
    size = data.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        problems.append(f"{where}.size: must be the size of the tarball in bytes")
        size = 0
    services = data.get("services")
    if (
        not isinstance(services, list)
        or not services
        or not all(isinstance(s, str) for s in services)
    ):
        problems.append(f"{where}.services: must be a list of service names")
        services = []
    else:
        unknown = sorted(set(services) - KNOWN_SERVICES)
        if unknown:
            problems.append(
                f"{where}.services: unknown service(s) {', '.join(unknown)}"
            )
        if len(set(services)) != len(services):
            problems.append(f"{where}.services: a service is listed twice")
    migration = data.get("data_migration", False)
    if not isinstance(migration, bool):
        problems.append(f"{where}.data_migration: must be true or false")
        migration = False
    if len(problems) > start:
        return None
    return Component(
        name=name,
        repo=repo,
        version=version,
        tag=tag,
        commit=commit,
        asset=asset,
        sha256=sha,
        size=size,
        services=tuple(services),
        data_migration=migration,
    )


def validate(data: Any) -> Manifest:
    """
    The manifest as the typed object. Unknown extra keys are ignored (a newer release may add some without a new schema).
    :raises ManifestError: with every problem found.
    """
    if not isinstance(data, dict):
        raise ManifestError(["the manifest must be a JSON object"])
    problems: list[str] = []
    schema = data.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise ManifestError(["schema: must be a whole number"])
    if schema > SCHEMA:
        raise ManifestError(
            [
                (
                    f"schema: this release uses manifest schema {schema}, this program understands up to {SCHEMA} - update "
                    "the appliance tooling first (a newer ginnungagap, e.g. from the appliance image of that release)"
                )
            ]
        )
    if schema < 1:
        raise ManifestError(["schema: must be 1 or more"])

    appliance = data.get("appliance")
    version = tag = channel = released = notes = ""
    min_installed: str | None = None
    if not isinstance(appliance, dict):
        problems.append("appliance: must be an object")
    else:
        version = appliance.get("version") or ""
        if not is_semver(version):
            problems.append(
                f"appliance.version: {version!r} is not a version like 1.3.0"
            )
        tag = appliance.get("tag") or ""
        if is_semver(version) and tag != f"v{version}":
            problems.append(f"appliance.tag: must be v{version}, not {tag!r}")
        channel = appliance.get("channel") or ""
        if channel not in CHANNELS:
            problems.append("appliance.channel: must be stable or prerelease")
        elif is_semver(version) and (channel == "prerelease") != is_prerelease(version):
            problems.append(
                f"appliance.channel: {version} is {'a pre-release' if is_prerelease(version) else 'a stable release'}, "
                f"so the channel must be {'prerelease' if is_prerelease(version) else 'stable'}"
            )
        released = appliance.get("released") or ""
        if not _valid_time(released):
            problems.append(
                "appliance.released: must be a UTC time like 2026-12-01T10:00:00Z"
            )
        raw_min = appliance.get("min_installed")
        if raw_min not in (None, ""):
            if is_semver(raw_min):
                min_installed = raw_min
            else:
                problems.append(
                    "appliance.min_installed: must be a version like 1.2.0 (or left out)"
                )
        notes = appliance.get("notes") or ""
        if not isinstance(notes, str):
            problems.append("appliance.notes: must be text")
            notes = ""

    components: dict[str, Component] = {}
    raw_components = data.get("components")
    if not isinstance(raw_components, dict):
        problems.append("components: must be an object")
    else:
        # a release built before the code names carries the old keys: read them as the roles they stand for
        raw_components = _modernize_keys(raw_components, "components", problems)
        data = {**data, "components": raw_components}
        missing = [n for n in COMPONENT_NAMES if n not in raw_components]
        extra = sorted(set(raw_components) - set(COMPONENT_NAMES))
        if missing:
            problems.append(f"components: missing {', '.join(missing)}")
        if extra:
            problems.append(
                f"components: unknown component(s) {', '.join(extra)} (schema {SCHEMA} has exactly {', '.join(COMPONENT_NAMES)})"
            )
        for name in COMPONENT_NAMES:
            if name in raw_components:
                component = _component(name, raw_components[name], problems)
                if component is not None:
                    components[name] = component
        appliance_component = components.get("appliance")
        if (
            appliance_component
            and is_semver(version)
            and appliance_component.version != version
        ):
            problems.append(
                f"components.appliance.version: {appliance_component.version} differs from appliance.version {version} "
                "(the appliance release is the release of the appliance component)"
            )
    images = data.get("images", {})
    if not isinstance(images, dict):
        problems.append("images: must be an object")
        images = {}
    if problems:
        raise ManifestError(problems)
    return Manifest(
        schema=schema,
        version=version,
        tag=tag,
        channel=channel,
        released=released,
        min_installed=min_installed,
        notes=notes,
        components=components,
        images=images,
        raw=copy.deepcopy(data),
    )


def _modernize_keys(
    mapping: dict[str, Any], where: str, problems: list[str] | None = None
) -> dict[str, Any]:
    """`mapping` with the old component keys renamed to their roles (a new dict; the same order)."""
    result: dict[str, Any] = {}
    for key, value in mapping.items():
        current = LEGACY_KEYS.get(key, key)
        if current in result or (current != key and current in mapping):
            if problems is not None:
                problems.append(
                    f"{where}: {key} and {current} are the same component and are both given"
                )
            continue
        result[current] = value
    return result


def _modernize_entry(key: str, entry: Any) -> Any:
    """A component entry of a state file with the old repository / asset name replaced by the current one."""
    if not isinstance(entry, dict):
        return entry
    entry = dict(entry)
    current, legacy = REPO_NAMES.get(key), LEGACY_NAMES.get(key, ())
    repo = entry.get("repo")
    if current and isinstance(repo, str):
        owner, _, name = repo.rpartition("/")
        if name in legacy:
            entry["repo"] = f"{owner}/{current}" if owner else current
    asset, version = entry.get("asset"), entry.get("version")
    if current and isinstance(asset, str) and isinstance(version, str):
        for old in legacy:
            if asset == _asset_name(old, version):
                entry["asset"] = _asset_name(current, version)
    return entry


def has_legacy_names(data: Any) -> bool:
    """True when a state file (installed.json, a history file) still uses the old component keys or repository names."""
    return isinstance(data, dict) and modernize_state(data) != data


def modernize_state(data: Any) -> Any:
    """A state file read as the current program would write it: old keys renamed, old repository names replaced."""
    if not isinstance(data, dict):
        return data
    result = dict(data)
    components = data.get("components")
    if isinstance(components, dict):
        renamed = _modernize_keys(components, "components")
        result["components"] = {k: _modernize_entry(k, v) for k, v in renamed.items()}
    dirs = data.get("dirs")
    if isinstance(dirs, dict):
        result["dirs"] = _modernize_keys(dirs, "dirs")
    return result


def _valid_time(value: Any) -> bool:
    if not isinstance(value, str) or not _TIME.match(value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def loads(text: str | bytes) -> Manifest:
    """Parses and validates the text of a `components.json`. :raises ManifestError:"""
    try:
        data = json.loads(text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ManifestError([f"not valid JSON ({exc})"]) from exc
    return validate(data)


def load(path: str | Path) -> Manifest:
    """Reads and validates a `components.json`. :raises ManifestError:"""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ManifestError([f"{path}: can't be read ({exc.strerror})"]) from exc
    return loads(raw)


# ---- what changes between two releases ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    component: str
    from_version: str | None
    to_version: str
    kind: (
        str  # patch | minor | major | pre | new | replace | downgrade | same | skipped
    )
    services: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.kind not in ("same", "skipped")


# ---- the installed state ---------------------------------------------------------------------------------------------


@dataclass
class InstalledState:
    """
    What `installed.json` says: the manifest that is installed (components that were adopted from a checkout have a version
    that may not be a release number, e.g. `unknown`), where each component's source is under releases/<Repo>/, when it was
    installed and which release came before it.
    """

    appliance_version: str
    appliance_tag: str = ""
    channel: str = "stable"
    released: str = ""
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    dirs: dict[str, str] = field(default_factory=dict)
    installed_at: str = ""
    previous: str | None = None
    adopted: bool = False
    partial: bool = False
    backup: str | None = None
    data_migration: bool = False
    images: dict[str, Any] = field(default_factory=dict)
    entry: str | None = (
        None  # the file of this state in history/ (set when it is recorded)
    )
    previous_entry: str | None = None  # the history file of the state this one replaced

    def version_of(self, name: str) -> str | None:
        entry = self.components.get(name)
        return entry.get("version") if entry else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "appliance": {
                "version": self.appliance_version,
                "tag": self.appliance_tag,
                "channel": self.channel,
                "released": self.released,
            },
            "components": copy.deepcopy(self.components),
            "images": copy.deepcopy(self.images),
            "dirs": dict(self.dirs),
            "installed_at": self.installed_at,
            "previous": self.previous,
            "adopted": self.adopted,
            "partial": self.partial,
            "backup": self.backup,
            "data_migration": self.data_migration,
            "entry": self.entry,
            "previous_entry": self.previous_entry,
        }

    @classmethod
    def from_dict(cls, data: Any) -> InstalledState:
        """
        :raises ManifestError: the file is not a state file this program wrote. A file written before the code names (old
        component keys, old repository names) is read as the current names.
        """
        data = modernize_state(data)
        if not isinstance(data, dict) or not isinstance(data.get("appliance"), dict):
            raise ManifestError(["installed.json: not an installed-state file"])
        appliance = data["appliance"]
        components = data.get("components")
        dirs = data.get("dirs", {})
        problems = []
        if not isinstance(appliance.get("version"), str) or not appliance["version"]:
            problems.append("installed.json: appliance.version is missing")
        if not isinstance(components, dict) or not all(
            isinstance(entry, dict) and isinstance(entry.get("version"), str)
            for entry in components.values()
        ):
            problems.append("installed.json: components must map names to versions")
        if not isinstance(dirs, dict) or not all(
            name in COMPONENT_NAMES
            and isinstance(folder, str)
            and _DIRNAME.match(folder)
            for name, folder in dirs.items()
        ):
            problems.append("installed.json: dirs must map component names to folders")
        if problems:
            raise ManifestError(problems)
        previous = data.get("previous")
        return cls(
            appliance_version=appliance["version"],
            appliance_tag=str(appliance.get("tag") or ""),
            channel=str(appliance.get("channel") or "stable"),
            released=str(appliance.get("released") or ""),
            components=components,
            dirs=dirs,
            installed_at=str(data.get("installed_at") or ""),
            previous=previous if isinstance(previous, str) else None,
            adopted=bool(data.get("adopted", False)),
            partial=bool(data.get("partial", False)),
            backup=data.get("backup") if isinstance(data.get("backup"), str) else None,
            data_migration=bool(data.get("data_migration", False)),
            images=data.get("images") if isinstance(data.get("images"), dict) else {},
            entry=data.get("entry") if isinstance(data.get("entry"), str) else None,
            previous_entry=data.get("previous_entry")
            if isinstance(data.get("previous_entry"), str)
            else None,
        )


def state_from_manifest(
    manifest: Manifest,
    dirs: dict[str, str],
    installed_at: str,
    previous: str | None,
    backup: str | None = None,
    partial: bool = False,
) -> InstalledState:
    """The state after a full install of `manifest` (`dirs`: the folder of each component under releases/<Repo>/)."""
    raw_components = manifest.raw["components"]
    return InstalledState(
        appliance_version=manifest.version,
        appliance_tag=manifest.tag,
        channel=manifest.channel,
        released=manifest.released,
        components={
            name: copy.deepcopy(raw_components[name]) for name in COMPONENT_NAMES
        },
        dirs=dict(dirs),
        installed_at=installed_at,
        previous=previous,
        partial=partial,
        backup=backup,
        data_migration=manifest.data_migration,
        images=copy.deepcopy(manifest.images),
    )


def diff(
    installed: InstalledState | None,
    target: Manifest,
    only: Collection[str] | None = None,
) -> list[Change]:
    """
    One `Change` per component, in the fixed order, from what is installed to `target`. A component outside `only` (a partial
    update) is `skipped`; one whose version is the same is `same`; with nothing installed every component is `new`.
    """
    changes = []
    for name in COMPONENT_NAMES:
        component = target.components[name]
        old = installed.version_of(name) if installed else None
        if only is not None and name not in only:
            kind = "skipped"
        else:
            kind = version_kind(old, component.version)
        changes.append(Change(name, old, component.version, kind, component.services))
    return changes


# ---- files ----------------------------------------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(UTC)


def timestamp(moment: datetime) -> str:
    """`2026-12-01T10:00:00Z` (UTC)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json_atomic(path: str | Path, data: Any, mode: int = 0o644) -> None:
    """Writes `data` as JSON next to the target and renames it over it, so a crash leaves the old file or the new one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: str | Path) -> Any | None:
    """The parsed file, None when it does not exist. :raises ManifestError: unreadable or not JSON."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ManifestError([f"{path}: can't be read ({exc})"]) from exc


def load_installed(path: str | Path) -> InstalledState | None:
    """The installed state, None when there is none. :raises ManifestError: the file is damaged."""
    data = read_json(path)
    return None if data is None else InstalledState.from_dict(data)


def save_installed(path: str | Path, state: InstalledState) -> None:
    write_json_atomic(path, state.to_dict())


def release_version(update_dir: str | Path | None = None) -> str:
    """
    The appliance release this machine runs, as `ginnungagap status` and the console login text name it: the release
    `installed.json` records, and the program's own version (`ginnungagap/version.py`) when none is recorded yet, the record is
    damaged or the machine only has adopted checkouts (those are no release).
    """
    from ginnungagap import statelock
    from ginnungagap.version import __version__

    folder = Path(update_dir) if update_dir is not None else statelock.update_dir()
    try:
        state = load_installed(folder / "installed.json")
    except (ManifestError, OSError):
        return __version__
    if state is None or state.adopted or not is_semver(state.appliance_version):
        return __version__
    return state.appliance_version


@dataclass(frozen=True)
class HistoryEntry:
    path: Path
    stamp: str  # the UTC time in the file name, e.g. 20261201T100000Z
    version: str

    @property
    def state(self) -> InstalledState:
        return InstalledState.from_dict(read_json(self.path))


_HISTORY_NAME = re.compile(r"^(\d{8}T\d{6}Z)-(.+?)(?:~\d+)?\.json$")


def add_history(folder: str | Path, state: InstalledState, moment: datetime) -> Path:
    """Keeps a copy of an installed state as `<UTC time>-<version>.json` (never overwrites an earlier one)."""
    folder = Path(folder)
    stamp = moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = folder / f"{stamp}-{state.appliance_version}.json"
    number = 1
    while target.exists():
        number += 1
        target = folder / f"{stamp}-{state.appliance_version}~{number}.json"
    state.entry = target.name
    write_json_atomic(target, state.to_dict())
    return target


def list_history(folder: str | Path) -> list[HistoryEntry]:
    """The history, oldest first."""
    entries = []
    folder = Path(folder)
    if not folder.is_dir():
        return entries
    for path in sorted(folder.iterdir()):
        match = _HISTORY_NAME.match(path.name)
        if match:
            entries.append(HistoryEntry(path, match.group(1), match.group(2)))
    return entries
