"""
`/etc/ginnungagap-image-build/build-host.yml`: loader and validator (plan section 10). Plain Python + PyYAML.

The rules:
    * An unknown key anywhere is an ERROR (a typo must not silently fall back to a default), a wrong type is an error, and
      every problem is reported at once (`ConfigError.problems`), not one per run.
    * The file holds NO secret. A secret is only ever a PATH to a file (`esxi.password_file`, `github.token_file`,
      `share.ssh_key_file`); `read_secret` reads it and refuses a link, a non-regular file, an empty file and a file that is
      readable by group or others (mode & 0o077), the same rule `ginnungagap.releases.read_token` applies to the update token.
      Preferred delivery is systemd `LoadCredential=esxi205:<file>`: `read_secret(..., credential_name="esxi205")` then reads
      `$CREDENTIALS_DIRECTORY/esxi205` first.
    * Only `esxi.datastore` and `esxi.network` have no default (plan section 14, step 5).
    * `esxi.host` must be on `esxi.allowed_hosts` (the pipeline refuses to touch any other host; A3).
    * URLs must be http(s) without credentials; the stored form has no trailing slash.

Keys (defaults in brackets; the ones marked + are not listed in plan section 10 but the other phases need them):
    github.repo [HTL3R-Xeonis/Ginnungagap-Appliance]  github.token_file [none]  github.channel [stable: stable|prerelease]
    poll_minutes [15]
    esxi.host [10.20.20.205]  esxi.user [root]  esxi.password_file [/etc/ginnungagap-image-build/secrets/esxi-205.password]
    esxi.datastore (required)  esxi.network (required)  esxi.allowed_hosts [[10.20.20.205]]  esxi.allowed_running_vms [[GNS3VM5]]
    template_api [http://10.20.20.171:8000]  ova_base_url [http://10.20.20.181:80]
    share.mode [nfs: nfs|ssh]  share.path [/mnt/esxi_templates]  share.ssh_host/ssh_user/ssh_key_file (required for mode ssh)+
    image.cpu [4]  image.memory_gb [4]  image.disk_gb [20]  image.install_method [update: update|checkouts]  image.os_type+
    acceptance.upgrade_test [true]  acceptance.extra_hosts [[]]
    keep_failed [false]  status_http [false]  notify.command [""]
    paths.state_dir+ [/var/lib/ginnungagap-image-build]  paths.allowed_signers+ [/etc/ginnungagap-image-build/allowed_signers]
    limits.*+  build_disk_free_gb 25, share_free_gb 10, datastore_free_gb 50, max_attempts 2 (attempts per run: one retry),
               max_attempts_per_release 2 (per 24 h), orphan_age_hours 6, keep_hours 24
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

CONFIG_DIR = Path("/etc/ginnungagap-image-build")
DEFAULT_CONFIG_FILE = CONFIG_DIR / "build-host.yml"
DEFAULT_PASSWORD_FILE = CONFIG_DIR / "secrets" / "esxi-205.password"
DEFAULT_STATE_DIR = Path("/var/lib/ginnungagap-image-build")
DEFAULT_ALLOWED_SIGNERS = CONFIG_DIR / "allowed_signers"
PASSWORD_CREDENTIAL = "esxi205"

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_VM_NAME = re.compile(r"[^\x00-\x1f/\\]{1,80}")


class ConfigError(ValueError):
    """The file is not valid. `problems` holds every finding, each naming the key it is about."""

    def __init__(self, problems: list[str], source: str = "build-host.yml") -> None:
        self.problems = list(problems)
        self.source = source
        body = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"{source} is not valid:\n{body}")


class SecretFileError(ValueError):
    """A secret file is missing or not safe to use. The message never contains the secret."""


@dataclass(frozen=True)
class GithubConfig:
    repo: str = "HTL3R-Xeonis/Ginnungagap-Appliance"
    token_file: Path | None = None
    channel: str = "stable"


@dataclass(frozen=True)
class EsxiConfig:
    datastore: str
    network: str
    host: str = "10.20.20.205"
    user: str = "root"
    password_file: Path = DEFAULT_PASSWORD_FILE
    allowed_hosts: tuple[str, ...] = ("10.20.20.205",)
    allowed_running_vms: tuple[str, ...] = ("GNS3VM5",)


@dataclass(frozen=True)
class ShareConfig:
    mode: str = "nfs"
    path: Path = Path("/mnt/esxi_templates")
    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_key_file: Path | None = None


@dataclass(frozen=True)
class ImageConfig:
    cpu: int = 4
    memory_gb: int = 4
    disk_gb: int = 20
    install_method: str = "update"
    os_type: str = "debian12_64Guest"


@dataclass(frozen=True)
class AcceptanceConfig:
    upgrade_test: bool = True
    extra_hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class NotifyConfig:
    command: str = ""


@dataclass(frozen=True)
class PathsConfig:
    state_dir: Path = DEFAULT_STATE_DIR
    allowed_signers: Path = DEFAULT_ALLOWED_SIGNERS


@dataclass(frozen=True)
class LimitsConfig:
    build_disk_free_gb: int = 25
    share_free_gb: int = 10
    datastore_free_gb: int = 50
    max_attempts: int = 2
    max_attempts_per_release: int = 2
    orphan_age_hours: float = 6
    keep_hours: float = 24


@dataclass(frozen=True)
class BuildHostConfig:
    esxi: EsxiConfig
    github: GithubConfig = field(default_factory=GithubConfig)
    poll_minutes: int = 15
    template_api: str = "http://10.20.20.171:8000"
    ova_base_url: str = "http://10.20.20.181:80"
    share: ShareConfig = field(default_factory=ShareConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    acceptance: AcceptanceConfig = field(default_factory=AcceptanceConfig)
    keep_failed: bool = False
    status_http: bool = False
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)

    @property
    def secret_files(self) -> list[Path]:
        """Every secret file the configuration names (the loader checks their mode)."""
        files = [self.esxi.password_file]
        for optional in (self.github.token_file, self.share.ssh_key_file):
            if optional is not None:
                files.append(optional)
        return files


# ---------- secrets ----------


def check_secret_file(path: str | Path) -> Path:
    """
    The path when it is a plain file that only its owner may read. :raises SecretFileError: missing, a link, not a regular
    file, readable by group or others (the message says `chmod 600`), or empty.
    """
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise SecretFileError(f"the secret file {path} does not exist") from None
    except OSError as exc:
        raise SecretFileError(
            f"the secret file {path} can't be read ({exc.strerror})"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise SecretFileError(
            f"the secret file {path} is not a plain file - refusing to use it"
        )
    if info.st_mode & 0o077:
        raise SecretFileError(
            f"the secret file {path} is readable by other users (mode {stat.S_IMODE(info.st_mode):04o}); "
            f"run `chmod 600 {path}`"
        )
    if info.st_size == 0:
        raise SecretFileError(f"the secret file {path} is empty")
    return path


def read_secret(
    path: str | Path,
    *,
    credential_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """
    The content of a secret file without its final newline. With `credential_name` and systemd's `$CREDENTIALS_DIRECTORY`
    (`LoadCredential=`), that credential is read instead of `path`. :raises SecretFileError: see `check_secret_file`.
    """
    env = os.environ if environ is None else environ
    chosen = Path(path)
    directory = env.get("CREDENTIALS_DIRECTORY")
    if credential_name and directory:
        candidate = Path(directory) / credential_name
        if candidate.is_file():
            chosen = candidate
    check_secret_file(chosen)
    try:
        text = chosen.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SecretFileError(
            f"the secret file {chosen} can't be read as text"
        ) from exc
    text = text.removesuffix("\n")
    if not text:
        raise SecretFileError(f"the secret file {chosen} is empty")
    return text


# ---------- the reader: collects problems instead of stopping at the first ----------


class _Section:
    def __init__(self, data: Any, where: str, problems: list[str]) -> None:
        self.where = where
        self.problems = problems
        self.seen: set[str] = set()
        if data is None:
            data = {}
        if not isinstance(data, dict):
            problems.append(
                f"{where or 'the file'} must be a mapping (key: value), not {type(data).__name__}"
            )
            data = {}
        self.data: dict[str, Any] = data

    def _name(self, key: str) -> str:
        return f"{self.where}.{key}" if self.where else key

    def section(self, key: str) -> _Section:
        self.seen.add(key)
        return _Section(self.data.get(key), self._name(key), self.problems)

    def _get(self, key: str, default: Any) -> tuple[bool, Any]:
        self.seen.add(key)
        if key not in self.data or self.data[key] is None:
            return False, default
        return True, self.data[key]

    def string(
        self, key: str, default: str | None, *, pattern: re.Pattern[str] | None = None
    ) -> str | None:
        present, value = self._get(key, default)
        if not present:
            if default is None:
                self.problems.append(f"{self._name(key)} is required")
            return default
        if not isinstance(value, str) or not value.strip():
            self.problems.append(f"{self._name(key)} must be a non-empty text")
            return default
        if pattern is not None and not pattern.fullmatch(value):
            self.problems.append(f"{self._name(key)}: {value[:60]!r} is not valid")
            return default
        return value

    def optional_string(self, key: str) -> str | None:
        present, value = self._get(key, None)
        if not present:
            return None
        if not isinstance(value, str) or not value.strip():
            self.problems.append(f"{self._name(key)} must be a non-empty text")
            return None
        return value

    def text_allowing_empty(self, key: str, default: str) -> str:
        present, value = self._get(key, default)
        if not present:
            return default
        if not isinstance(value, str):
            self.problems.append(f"{self._name(key)} must be a text")
            return default
        return value

    def choice(self, key: str, default: str, allowed: tuple[str, ...]) -> str:
        value = self.string(key, default)
        if value not in allowed:
            self.problems.append(
                f"{self._name(key)} must be one of {', '.join(allowed)} (not {str(value)[:40]!r})"
            )
            return default
        return value

    def integer(self, key: str, default: int, minimum: int = 1) -> int:
        present, value = self._get(key, default)
        if not present:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            self.problems.append(
                f"{self._name(key)} must be a whole number >= {minimum}"
            )
            return default
        return value

    def number(self, key: str, default: float, minimum: float = 0) -> float:
        present, value = self._get(key, default)
        if not present:
            return default
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or value < minimum
        ):
            self.problems.append(f"{self._name(key)} must be a number >= {minimum:g}")
            return default
        return value

    def boolean(self, key: str, default: bool) -> bool:
        present, value = self._get(key, default)
        if not present:
            return default
        if not isinstance(value, bool):
            self.problems.append(f"{self._name(key)} must be true or false")
            return default
        return value

    def path(self, key: str, default: Path | None) -> Path | None:
        present, value = self._get(key, default)
        if not present:
            return default
        if not isinstance(value, str) or not value.startswith("/"):
            self.problems.append(f"{self._name(key)} must be an absolute path")
            return default
        if "\x00" in value or ".." in Path(value).parts:
            self.problems.append(f"{self._name(key)} must not contain '..'")
            return default
        return Path(value)

    def url(self, key: str, default: str) -> str:
        value = self.string(key, default)
        parsed = urlparse(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            self.problems.append(
                f"{self._name(key)} must be an http(s) URL without credentials, query or fragment"
            )
            return default
        return value.rstrip("/")

    def host_list(
        self, key: str, default: tuple[str, ...], pattern: re.Pattern[str]
    ) -> tuple[str, ...]:
        present, value = self._get(key, default)
        if not present:
            return default
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not pattern.fullmatch(item) for item in value
        ):
            self.problems.append(f"{self._name(key)} must be a list of valid names")
            return default
        if len(set(value)) != len(value):
            self.problems.append(f"{self._name(key)} lists a name twice")
        return tuple(value)

    def finish(self) -> None:
        """Every key that was not asked for is an error."""
        unknown = sorted(str(k) for k in self.data if k not in self.seen)
        for key in unknown:
            self.problems.append(
                f"unknown key {self._name(key)} (allowed here: {', '.join(sorted(self.seen))})"
            )


def parse_config(data: Any, *, source: str = "build-host.yml") -> BuildHostConfig:
    """
    Validates a loaded document. Checks nothing on disk (no secret file is opened): `load_config` does that.
    :raises ConfigError: with every problem found.
    """
    problems: list[str] = []
    root = _Section(data, "", problems)

    gh = root.section("github")
    github = GithubConfig(
        repo=gh.string("repo", GithubConfig.repo, pattern=_REPO) or GithubConfig.repo,
        token_file=gh.path("token_file", None),
        channel=gh.choice("channel", "stable", ("stable", "prerelease")),
    )
    gh.finish()
    poll_minutes = root.integer("poll_minutes", 15)

    es = root.section("esxi")
    host = es.string("host", EsxiConfig.host, pattern=_HOST) or EsxiConfig.host
    allowed_hosts = es.host_list("allowed_hosts", EsxiConfig.allowed_hosts, _HOST)
    esxi_kwargs: dict[str, Any] = {
        "datastore": es.string("datastore", None) or "",
        "network": es.string("network", None) or "",
        "host": host,
        "user": es.string("user", "root") or "root",
        "password_file": es.path("password_file", DEFAULT_PASSWORD_FILE)
        or DEFAULT_PASSWORD_FILE,
        "allowed_hosts": allowed_hosts,
        "allowed_running_vms": es.host_list(
            "allowed_running_vms", EsxiConfig.allowed_running_vms, _VM_NAME
        ),
    }
    es.finish()
    if host not in allowed_hosts:
        problems.append(
            f"esxi.host {host} is not in esxi.allowed_hosts ({', '.join(allowed_hosts)}): the build never touches a host that is not on the list"
        )
    esxi = EsxiConfig(**esxi_kwargs)

    template_api = root.url("template_api", "http://10.20.20.171:8000")
    ova_base_url = root.url("ova_base_url", "http://10.20.20.181:80")

    sh = root.section("share")
    mode = sh.choice("mode", "nfs", ("nfs", "ssh"))
    share = ShareConfig(
        mode=mode,
        path=sh.path("path", ShareConfig.path) or ShareConfig.path,
        ssh_host=sh.string("ssh_host", None, pattern=_HOST)
        if mode == "ssh"
        else sh.optional_string("ssh_host"),
        ssh_user=sh.string("ssh_user", None)
        if mode == "ssh"
        else sh.optional_string("ssh_user"),
        ssh_key_file=sh.path("ssh_key_file", None),
    )
    if mode == "ssh" and share.ssh_key_file is None:
        problems.append("share.ssh_key_file is required when share.mode is ssh")
    sh.finish()

    im = root.section("image")
    image = ImageConfig(
        cpu=im.integer("cpu", 4),
        memory_gb=im.integer("memory_gb", 4),
        disk_gb=im.integer("disk_gb", 20),
        install_method=im.choice("install_method", "update", ("update", "checkouts")),
        os_type=im.string(
            "os_type", "debian12_64Guest", pattern=re.compile(r"[A-Za-z0-9_]+")
        )
        or "debian12_64Guest",
    )
    im.finish()

    ac = root.section("acceptance")
    acceptance = AcceptanceConfig(
        upgrade_test=ac.boolean("upgrade_test", True),
        extra_hosts=ac.host_list("extra_hosts", (), _HOST),
    )
    ac.finish()

    keep_failed = root.boolean("keep_failed", False)
    status_http = root.boolean("status_http", False)

    no = root.section("notify")
    command = no.text_allowing_empty("command", "")
    if command and (not command.startswith("/") or "\x00" in command):
        problems.append(
            "notify.command must be empty or the absolute path of a script (it gets the summary path as its argument)"
        )
        command = ""
    notify = NotifyConfig(command=command)
    no.finish()

    pa = root.section("paths")
    paths = PathsConfig(
        state_dir=pa.path("state_dir", DEFAULT_STATE_DIR) or DEFAULT_STATE_DIR,
        allowed_signers=pa.path("allowed_signers", DEFAULT_ALLOWED_SIGNERS)
        or DEFAULT_ALLOWED_SIGNERS,
    )
    pa.finish()

    li = root.section("limits")
    limits = LimitsConfig(
        build_disk_free_gb=li.integer("build_disk_free_gb", 25),
        share_free_gb=li.integer("share_free_gb", 10),
        datastore_free_gb=li.integer("datastore_free_gb", 50),
        max_attempts=li.integer("max_attempts", 2),
        max_attempts_per_release=li.integer("max_attempts_per_release", 2),
        orphan_age_hours=li.number("orphan_age_hours", 6, minimum=0),
        keep_hours=li.number("keep_hours", 24, minimum=0),
    )
    li.finish()

    root.finish()
    if problems:
        raise ConfigError(problems, source)
    return BuildHostConfig(
        esxi=esxi,
        github=github,
        poll_minutes=poll_minutes,
        template_api=template_api,
        ova_base_url=ova_base_url,
        share=share,
        image=image,
        acceptance=acceptance,
        keep_failed=keep_failed,
        status_http=status_http,
        notify=notify,
        paths=paths,
        limits=limits,
    )


def load_config(
    path: str | Path = DEFAULT_CONFIG_FILE,
    *,
    check_secrets: bool = True,
    environ: Mapping[str, str] | None = None,
) -> BuildHostConfig:
    """
    Reads and validates the file. With `check_secrets` (the default) every secret file the configuration names must exist and
    pass `check_secret_file` (the optional ones only when they exist; a missing optional token is the normal case).
    :raises ConfigError: the file is missing, is not YAML, has an unknown key, a wrong type or an unsafe secret file.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([f"can't be read ({exc.strerror})"], str(path)) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(
            [f"is not valid YAML ({type(exc).__name__})"], str(path)
        ) from exc
    config = parse_config(data, source=str(path))
    if check_secrets:
        problems: list[str] = []
        env = os.environ if environ is None else environ
        for secret in config.secret_files:
            optional = secret != config.esxi.password_file
            if optional and not secret.exists() and not secret.is_symlink():
                continue
            if secret == config.esxi.password_file:
                directory = env.get("CREDENTIALS_DIRECTORY")
                if directory and (Path(directory) / PASSWORD_CREDENTIAL).is_file():
                    secret = Path(directory) / PASSWORD_CREDENTIAL
            try:
                check_secret_file(secret)
            except SecretFileError as exc:
                problems.append(str(exc))
        if problems:
            raise ConfigError(problems, str(path))
    return config
