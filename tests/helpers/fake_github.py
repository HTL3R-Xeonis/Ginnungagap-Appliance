"""
A fake GitHub on localhost for the tests of the release source: a real `http.server` in a thread, so the real `urllib` code
path (redirects, ETag, status codes, headers) is what gets tested. It is written against what the GitHub REST API answers for
`/repos/{owner}/{repo}/releases`, `.../releases/latest`, `.../releases/tags/{tag}` and `.../releases/assets/{id}`.

A download is a redirect to a second path (`/__download/...`) that answers 400 when it gets an `Authorization` header, the way
the signed storage address GitHub redirects to does: a client that forwards its token there is caught.

The WEB side of github.com is served too, on the same address (no `/repos/` in front, so nothing collides with the API):
`/<owner>/<repo>/releases/latest` answers 302 to `.../releases/tag/<tag>` of the newest stable release (404 when there is none),
`/<owner>/<repo>/releases/tag/<tag>` is a page, and `/<owner>/<repo>/releases/download/<tag>/<file>` redirects to the signed
`/__download/...` address like the real one. None of it counts as an API call (`api_requests()`); it never looks at a token,
and a private repository (`token` set) answers 404 there. `web_mode` simulates trouble on that side (`not_found`, `server_error`,
`too_many`, `download_error` = only the downloads fail, `latest_ok` = `latest` answers 200 without a redirect), `latest_location` makes `releases/latest` redirect to that address instead (a hostile answer).

`mode` simulates trouble: `rate_limit` (403 with X-RateLimit headers), `secondary_limit` (403 with Retry-After), `too_many`
(429), `unauthorized` (401), `server_error` (503), `not_found` (404 for everything). `token` makes the repository private:
a request without the right `Authorization: Bearer <token>` is a 404, a wrong token a 401 (like the real thing).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self
from urllib.parse import unquote, urlparse

REPO = "HTL3R-Xeonis/Ginnungagap-Appliance"


@dataclass
class Seen:
    method: str
    path: str  # including the query
    headers: dict[str, str]  # lower-case names

    @property
    def authorization(self) -> str | None:
        return self.headers.get("authorization")


class FakeGitHub:
    def __init__(
        self,
        releases: list[Path] | None = None,
        repo: str = REPO,
        token: str | None = None,
    ) -> None:
        self.repo = repo
        self.token = token
        self.mode: str | None = None
        self.web_mode: str | None = None
        self.latest_location: str | None = None
        self.requests: list[Seen] = []
        self.releases: list[Path] = list(releases or [])
        self.drafts: list[Path] = []
        self._ids: dict[int, Path] = {}
        self._path_ids: dict[Path, int] = {}
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silent
                pass

            def do_GET(self):
                fake._handle(self)

            do_HEAD = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.thread.start()

    # -- the address and bookkeeping --
    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def paths(self) -> list[str]:
        return [seen.path for seen in self.requests]

    def count(self, part: str) -> int:
        return sum(1 for seen in self.requests if part in seen.path)

    def api_requests(self) -> list[Seen]:
        """The requests that count against the rate limit: everything under /repos/ (the web side and the downloads do not)."""
        return [seen for seen in self.requests if seen.path.startswith("/repos/")]

    def web_requests(self) -> list[Seen]:
        return [
            seen
            for seen in self.requests
            if not seen.path.startswith(("/repos/", "/__download/"))
        ]

    # -- what it serves --
    def _asset_id(self, path: Path) -> int:
        if path not in self._path_ids:
            self._path_ids[path] = 1000 + len(self._path_ids)
        identifier = self._path_ids[path]
        self._ids[identifier] = path
        return identifier

    def _release_json(self, release: Path) -> dict:
        manifest = json.loads((release / "components.json").read_text())
        version = manifest["appliance"]["version"]
        assets = []
        for path in sorted(p for p in release.iterdir() if p.is_file()):
            identifier = self._asset_id(path)
            assets.append(
                {
                    "id": identifier,
                    "name": path.name,
                    "size": path.stat().st_size,
                    "url": f"{self.url}/repos/{self.repo}/releases/assets/{identifier}",
                }
            )
        return {
            "tag_name": f"v{version}",
            "name": f"v{version}",
            "draft": release in self.drafts,
            "prerelease": "-" in version,
            "published_at": manifest["appliance"]["released"],
            "assets": assets,
        }

    # -- the handler --
    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        headers = {k.lower(): v for k, v in handler.headers.items()}
        with self._lock:
            self.requests.append(Seen(handler.command, handler.path, headers))
        url = urlparse(handler.path)
        path = unquote(url.path)

        def send(status: int, body: bytes = b"", extra: dict[str, str] | None = None):
            handler.send_response(status)
            handler.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                handler.send_header(key, value)
            handler.end_headers()
            if handler.command != "HEAD":
                handler.wfile.write(body)

        def send_json(status: int, data, extra: dict[str, str] | None = None):
            send(
                status,
                json.dumps(data).encode(),
                {"Content-Type": "application/json", **(extra or {})},
            )

        if path.startswith("/__download/"):
            # like the signed storage address: it rejects a request that brings GitHub credentials along
            if "authorization" in headers:
                return send(400, b"Only one auth mechanism allowed")
            identifier = int(path.split("/")[2])
            file = self._ids.get(identifier)
            if file is None:
                return send(404, b"")
            return send(
                200, file.read_bytes(), {"Content-Type": "application/octet-stream"}
            )

        web = f"/{self.repo}/releases/"
        if path.startswith(web):
            if self.web_mode == "not_found" or self.token is not None:
                return send(404, b"Not Found")
            if self.web_mode == "server_error":
                return send(503, b"")
            if self.web_mode == "too_many":
                return send(429, b"", {"Retry-After": "60"})
            visible = [r for r in self.releases if r not in self.drafts]
            rest = path[len(web) :]
            if rest == "latest" and self.web_mode == "latest_ok":
                return send(200, b"<html>no redirect</html>")
            if rest == "latest":
                stable = [
                    r for r in visible if "-" not in self._release_json(r)["tag_name"]
                ]
                if not stable:
                    return send(404, b"Not Found")
                tag = self._release_json(stable[-1])["tag_name"]
                where = (
                    self.latest_location
                    if self.latest_location is not None
                    else f"{self.url}/{self.repo}/releases/tag/{tag}"
                )
                return send(302, b"", {"Location": where})
            if rest.startswith("tag/"):
                return send(
                    200, b"<html>a release page</html>", {"Content-Type": "text/html"}
                )
            if rest.startswith("download/"):
                if self.web_mode == "download_error":
                    return send(503, b"")
                tag, _, name = rest[len("download/") :].partition("/")
                for release in visible:
                    if self._release_json(release)["tag_name"] == tag:
                        file = release / name
                        if file.is_file():
                            identifier = self._asset_id(file)
                            return send(
                                302,
                                b"",
                                {
                                    "Location": f"{self.url}/__download/{identifier}/{name}"
                                },
                            )
            return send(404, b"Not Found")

        if self.mode == "not_found":
            return send_json(404, {"message": "Not Found"})
        if self.mode == "server_error":
            return send_json(503, {"message": "Service Unavailable"})
        if self.mode == "unauthorized":
            return send_json(401, {"message": "Bad credentials"})
        if self.mode == "rate_limit":
            reset = str(int(time.time()) + 900)
            return send_json(
                403,
                {"message": "API rate limit exceeded for 203.0.113.9."},
                {
                    "X-RateLimit-Limit": "60",
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": reset,
                },
            )
        if self.mode == "secondary_limit":
            return send_json(
                403,
                {"message": "You have exceeded a secondary rate limit."},
                {"Retry-After": "120"},
            )
        if self.mode == "too_many":
            return send_json(429, {"message": "slow down"}, {"Retry-After": "60"})

        if self.token is not None:
            given = headers.get("authorization")
            if given is None:
                return send_json(404, {"message": "Not Found"})
            if given != f"Bearer {self.token}":
                return send_json(401, {"message": "Bad credentials"})

        prefix = f"/repos/{self.repo}/releases"
        visible = [r for r in self.releases if r not in self.drafts]
        if path == prefix:
            body = json.dumps([self._release_json(r) for r in visible]).encode()
            etag = '"' + hashlib.sha1(body).hexdigest() + '"'
            if headers.get("if-none-match") == etag:
                return send(304, b"", {"ETag": etag})
            return send(200, body, {"Content-Type": "application/json", "ETag": etag})
        if path == prefix + "/latest":
            stable = [
                r for r in visible if "-" not in self._release_json(r)["tag_name"]
            ]
            if not stable:
                return send_json(404, {"message": "Not Found"})
            return send_json(200, self._release_json(stable[-1]))
        if path.startswith(prefix + "/tags/"):
            tag = path.rsplit("/", 1)[1]
            for release in visible:
                if self._release_json(release)["tag_name"] == tag:
                    return send_json(200, self._release_json(release))
            return send_json(404, {"message": "Not Found"})
        if path.startswith(prefix + "/assets/"):
            identifier = int(path.rsplit("/", 1)[1])
            file = self._ids.get(identifier)
            if file is None:
                return send_json(404, {"message": "Not Found"})
            if "octet-stream" in headers.get("accept", ""):
                return send(
                    302,
                    b"",
                    {"Location": f"{self.url}/__download/{identifier}/{file.name}"},
                )
            return send_json(200, {"id": identifier, "name": file.name})
        return send_json(404, {"message": "Not Found"})
