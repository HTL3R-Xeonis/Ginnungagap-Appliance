"""
Where releases come from: GitHub Releases (anonymously, with an optional token) or a folder (USB stick, NFS share).

A source lists the releases it offers and fetches one asset of a release into a folder; what is in the asset is checked
elsewhere (`ginnungagap.update`). Standard library only (`urllib`). The GitHub source works without any credentials, because
the repositories are public. A token is only sent when the token file exists (private repositories, or the 60 requests per
hour that GitHub allows per address without one) and only ever to the API host: the redirect to the signed download address
must not carry it, so the header is attached as an "unredirected" one and a redirect handler drops it as well.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from ginnungagap import manifest as manifest_module

API_BASE = "https://api.github.com"
USER_AGENT = "ginnungagap-update"
MAX_JSON_BYTES = 8 * 1024 * 1024
ANONYMOUS_LIMIT = 60

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
_TAG = re.compile(r"^v(.+)$")
_TOKEN_SHAPES = re.compile(r"(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})")
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9_.~+/=-]{8,255}$")


class SourceError(Exception):
    """
    A source could not be asked or did not have what was needed. `kind` is network | auth | rate_limit | not_found | server |
    config | data; the message is plain words, safe to print (a token never appears in it).
    """

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(message)


def redact(text: str, token: str | None = None) -> str:
    """`text` without the token (and without anything that looks like a GitHub token)."""
    if token:
        text = text.replace(token, "***")
    return _TOKEN_SHAPES.sub("***", text)


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    prerelease: bool
    date: str
    assets: dict[str, str] = field(default_factory=dict, compare=False, repr=False)
    location: str = field(default="", compare=False, repr=False)


class ReleaseSource(Protocol):
    def describe(self) -> str: ...

    def list_releases(self, channel: str) -> list[ReleaseInfo]:
        """The releases of the channel, newest first. `stable` hides pre-releases, `prerelease` shows everything."""
        ...

    def fetch(
        self,
        release: ReleaseInfo,
        name: str,
        dest: Path,
        max_bytes: int | None = None,
    ) -> Path:
        """Stores the asset `name` of the release as `dest/name` and returns that path. :raises SourceError:"""
        ...


# ---- the token ------------------------------------------------------------------------------------------------------


def read_token(path: str | Path | None) -> str | None:
    """
    The token in `path`, None when there is no such file (the normal case: public repositories need none).
    :raises SourceError: the file exists but is not safe to use (a link, readable by others, empty, not a token).
    """
    if not path:
        return None
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SourceError(
            "config", f"the token file {path} can't be read ({exc.strerror})"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise SourceError(
            "config", f"the token file {path} is not a plain file - refusing to use it"
        )
    if info.st_mode & 0o077:
        raise SourceError(
            "config",
            f"the token file {path} is readable by other users (mode {stat.S_IMODE(info.st_mode):04o}); "
            f"run `chmod 600 {path}` or write it again with `ginnungagap update --set-token`",
        )
    if info.st_uid not in (0, os.geteuid()):
        raise SourceError(
            "config",
            f"the token file {path} belongs to another user - refusing to use it",
        )
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise SourceError(
            "config", f"the token file {path} can't be read ({exc})"
        ) from exc
    if not text:
        raise SourceError(
            "config",
            f"the token file {path} is empty; delete it to go without a token or write it with `ginnungagap update --set-token`",
        )
    if not _TOKEN_CHARS.match(text):
        raise SourceError(
            "config",
            f"the token file {path} does not hold a single token (one line, no spaces)",
        )
    return text


def check_token_text(text: str) -> str:
    """A token as typed or piped in: the stripped text. :raises ValueError: it does not look like a token (nothing is quoted)."""
    token = text.strip()
    if not token:
        raise ValueError("no token was given")
    if not _TOKEN_CHARS.match(token):
        raise ValueError(
            "that is not a token (a token is one line of letters, digits and _ - . with no spaces)"
        )
    return token


def write_token(path: str | Path, token: str) -> None:
    """Writes the token file: mode 0600 from the first byte, replaced atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---- GitHub ----------------------------------------------------------------------------------------------------------


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follows a redirect without any credentials and never from https to plain http."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if (
            urlparse(req.full_url).scheme == "https"
            and urlparse(newurl).scheme != "https"
        ):
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "refusing a redirect from https to plain http",
                headers,
                fp,
            )
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            for key in [k for k in new.headers if k.lower() == "authorization"]:
                del new.headers[key]
            new.unredirected_hdrs.pop("Authorization", None)
        return new


def default_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_SafeRedirect())


class _NotModified(Exception):
    """The server answered 304: what was stored is still current."""


class GitHubSource:
    """
    The releases of one GitHub repository. Anonymous unless `token_file` exists. `opener` is anything with
    `.open(request, timeout=...)` (tests pass one pointed at a fake server); `api_base` is the API's address.
    The ETag of the release list is kept in `cache_path`, so asking again costs nothing against the rate limit.
    """

    def __init__(
        self,
        repo: str,
        token_file: str | Path | None = None,
        opener: Any = None,
        api_base: str = API_BASE,
        cache_path: str | Path | None = None,
        timeout: float = 30.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.token_file = token_file
        self.opener = opener or default_opener()
        self.api_base = api_base.rstrip("/")
        self.cache_path = Path(cache_path) if cache_path else None
        self.timeout = timeout
        self.now = now
        self._token: str | None = None
        self._token_read = False

    # -- helpers --
    def describe(self) -> str:
        return f"github {self.repo}"

    @property
    def token(self) -> str | None:
        """The token, read from the file the first time it is needed; None when there is no token file."""
        if not self._token_read:
            self._token = read_token(self.token_file)
            self._token_read = True
        return self._token

    def _request(
        self, url: str, accept: str, headers: dict[str, str] | None = None
    ) -> urllib.request.Request:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": accept,
                "User-Agent": USER_AGENT,
                "X-GitHub-Api-Version": "2022-11-28",
                **(headers or {}),
            },
        )
        if self.token:
            # unredirected: urllib does not copy it to the address the API redirects the download to
            request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        return request

    def _open(self, request: urllib.request.Request):
        try:
            return self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                raise _NotModified from None
            raise self._http_error(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            host = urlparse(request.full_url).hostname or "the server"
            raise SourceError(
                "network",
                redact(
                    f"could not reach {host} ({reason}); check the network connection, or use --offline-dir with a folder of release files",
                    self.token,
                ),
            ) from None

    def _http_error(self, exc: urllib.error.HTTPError) -> SourceError:
        code = exc.code
        try:
            body = exc.read(65536).decode("utf-8", "replace")
        except (OSError, ValueError):
            body = ""
        remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        reset = exc.headers.get("X-RateLimit-Reset") if exc.headers else None
        limited = code in (403, 429) and (
            remaining == "0"
            or code == 429
            or retry_after is not None
            or "rate limit" in body.lower()
        )
        if limited:
            return SourceError(
                "rate_limit", self._rate_limit_message(reset, retry_after)
            )
        if code == 401:
            return SourceError(
                "auth",
                "GitHub refused the stored token (HTTP 401): it is invalid or has expired. Write a new one with "
                "`ginnungagap update --set-token`, or delete the token file to go without one (public repositories need none).",
            )
        if code == 403:
            return SourceError(
                "auth",
                "GitHub refused the request (HTTP 403)"
                + (
                    "; the stored token may lack read access to this repository"
                    if self.token
                    else ""
                ),
            )
        if code == 404:
            hint = (
                "the stored token may not have access to it"
                if self.token
                else "if the repository is private, store a token with `ginnungagap update --set-token`"
            )
            return SourceError(
                "not_found",
                f"GitHub does not know {self.repo} or that release (HTTP 404); {hint}",
            )
        if code >= 500:
            return SourceError(
                "server",
                f"GitHub answered with an error (HTTP {code}); try again later",
            )
        return SourceError("server", f"GitHub answered HTTP {code} {exc.reason}")

    def _rate_limit_message(self, reset: str | None, retry_after: str | None) -> str:
        when = ""
        try:
            if reset:
                moment = datetime.fromtimestamp(int(reset), UTC)
            elif retry_after:
                moment = self.now().timestamp() + int(retry_after)
                moment = datetime.fromtimestamp(moment, UTC)
            else:
                moment = None
            if moment is not None:
                minutes = max(0, int((moment - self.now()).total_seconds() // 60) + 1)
                when = f" Try again after {moment:%H:%M} UTC (in about {minutes} min)."
        except (ValueError, OverflowError, OSError):
            when = ""
        if self.token:
            who = "the stored token's limit"
            more = ""
        else:
            who = f"the limit for anonymous requests ({ANONYMOUS_LIMIT} per hour and address)"
            more = " Storing a token with `ginnungagap update --set-token` raises it to 5000 per hour."
        return f"GitHub's API rate limit is used up ({who}).{when}{more}"

    def _read_limited(self, response: Any, limit: int) -> bytes:
        data = response.read(limit + 1)
        if len(data) > limit:
            raise SourceError(
                "data", "GitHub's answer is unexpectedly large - refusing it"
            )
        return data

    # -- cache --
    def _cache_load(self) -> dict[str, Any]:
        if self.cache_path is None:
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _cache_store(self, cache: dict[str, Any]) -> None:
        if self.cache_path is None:
            return
        try:
            manifest_module.write_json_atomic(self.cache_path, cache, mode=0o600)
        except OSError:
            pass  # a cache that can't be written only costs a request

    # -- the interface --
    def list_releases(self, channel: str) -> list[ReleaseInfo]:
        data = self._list_json_cached()
        found = []
        for item in data:
            if not isinstance(item, dict) or item.get("draft"):
                continue
            match = _TAG.match(str(item.get("tag_name") or ""))
            if not match or not manifest_module.is_semver(match.group(1)):
                continue
            version = match.group(1)
            pre = bool(item.get("prerelease")) or manifest_module.is_prerelease(version)
            if channel == "stable" and pre:
                continue
            assets = {}
            for asset in item.get("assets") or []:
                if (
                    isinstance(asset, dict)
                    and isinstance(asset.get("name"), str)
                    and _SAFE_NAME.match(asset["name"])
                    and isinstance(asset.get("id"), int)
                ):
                    assets[asset["name"]] = (
                        f"{self.api_base}/repos/{self.repo}/releases/assets/{asset['id']}"
                    )
            found.append(
                ReleaseInfo(
                    version=version,
                    tag=f"v{version}",
                    prerelease=pre,
                    date=str(item.get("published_at") or item.get("created_at") or ""),
                    assets=assets,
                )
            )
        found.sort(key=lambda r: manifest_module.semver_key(r.version), reverse=True)
        return found

    def _list_json_cached(self) -> list[Any]:
        """The release list; a 304 answers from the stored copy."""
        url = f"{self.api_base}/repos/{self.repo}/releases?per_page=100"
        cache = self._cache_load()
        entry = cache.get(url) if isinstance(cache.get(url), dict) else None
        headers = {}
        if (
            entry
            and isinstance(entry.get("etag"), str)
            and isinstance(entry.get("body"), str)
        ):
            headers["If-None-Match"] = entry["etag"]
        request = self._request(url, "application/vnd.github+json", headers)
        try:
            response = self._open(request)
        except _NotModified:
            if not entry:
                raise SourceError(
                    "data", "GitHub answered 304 without being asked to"
                ) from None
            text, etag = entry["body"], None
        else:
            with response:
                text = self._read_limited(response, MAX_JSON_BYTES).decode(
                    "utf-8", "replace"
                )
                etag = response.headers.get("ETag")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise SourceError(
                "data", "GitHub's release list is not valid JSON"
            ) from exc
        if etag:
            cache[url] = {"etag": etag, "body": text}
            self._cache_store(cache)
        if not isinstance(data, list):
            raise SourceError("data", "GitHub's release list has an unexpected shape")
        return data

    def fetch(
        self,
        release: ReleaseInfo,
        name: str,
        dest: Path,
        max_bytes: int | None = None,
    ) -> Path:
        _check_name(name)
        url = release.assets.get(name)
        if url is None:
            raise SourceError(
                "not_found", f"the release {release.tag} has no file {name}"
            )
        request = self._request(url, "application/octet-stream")
        response = self._open(request)
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / name
        fd, tmp = tempfile.mkstemp(dir=dest, prefix=f".{name}.")
        try:
            total = 0
            with response, os.fdopen(fd, "wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise SourceError(
                            "data",
                            f"{name} is larger than the {max_bytes} bytes the release says - refusing it",
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except SourceError:
            Path(tmp).unlink(missing_ok=True)
            raise
        except (OSError, TimeoutError) as exc:
            Path(tmp).unlink(missing_ok=True)
            raise SourceError(
                "network", redact(f"downloading {name} failed ({exc})", self.token)
            ) from None
        return target


def _check_name(name: str) -> None:
    if not _SAFE_NAME.match(name):
        raise SourceError("data", f"{name!r} is not a plain file name")


# ---- a folder --------------------------------------------------------------------------------------------------------


class DirectorySource:
    """
    The release files in a folder: the asset set of one release (`components.json`, the tarballs, ...), or a folder whose
    sub-folders each hold one release. Works with no network and no token.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def describe(self) -> str:
        return f"directory {self.path}"

    def _candidates(self) -> list[Path]:
        if (self.path / "components.json").is_file():
            return [self.path]
        if not self.path.is_dir():
            raise SourceError("not_found", f"{self.path} is not a folder")
        return sorted(
            p for p in self.path.iterdir() if (p / "components.json").is_file()
        )

    def list_releases(self, channel: str) -> list[ReleaseInfo]:
        folders = self._candidates()
        if not folders:
            raise SourceError(
                "not_found",
                f"{self.path} holds no release (no components.json in it or in a sub-folder)",
            )
        found = []
        for folder in folders:
            try:
                data = json.loads(
                    (folder / "components.json").read_text(encoding="utf-8")
                )
                appliance = data["appliance"]
                version = appliance["version"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if not manifest_module.is_semver(version):
                continue
            pre = manifest_module.is_prerelease(version)
            if channel == "stable" and pre:
                continue
            assets = {
                p.name: str(p)
                for p in folder.iterdir()
                if p.is_file() and _SAFE_NAME.match(p.name)
            }
            found.append(
                ReleaseInfo(
                    version=version,
                    tag=f"v{version}",
                    prerelease=pre,
                    date=str(appliance.get("released") or ""),
                    assets=assets,
                    location=str(folder),
                )
            )
        found.sort(key=lambda r: manifest_module.semver_key(r.version), reverse=True)
        return found

    def fetch(
        self,
        release: ReleaseInfo,
        name: str,
        dest: Path,
        max_bytes: int | None = None,
    ) -> Path:
        _check_name(name)
        source = Path(release.location or self.path) / name
        if not source.is_file():
            raise SourceError(
                "not_found", f"the folder {source.parent} has no file {name}"
            )
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise SourceError(
                "data", f"{source} can't be read ({exc.strerror})"
            ) from exc
        if max_bytes is not None and size > max_bytes:
            raise SourceError(
                "data",
                f"{name} is larger than the {max_bytes} bytes the release says - refusing it",
            )
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / name
        fd, tmp = tempfile.mkstemp(dir=dest, prefix=f".{name}.")
        try:
            with os.fdopen(fd, "wb") as handle, source.open("rb") as origin:
                shutil.copyfileobj(origin, handle, 1024 * 1024)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            Path(tmp).unlink(missing_ok=True)
            raise SourceError(
                "data", f"copying {name} failed ({exc.strerror})"
            ) from exc
        return target
