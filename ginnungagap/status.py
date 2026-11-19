"""
One health view over every service, asked through the gateway (the one entry point), so it checks what a user would reach.

Each service already answers /health on its own; this only asks them all and says what is wrong in plain words.
"""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

# state values: ok | degraded | down | login | external | off
Fetch = Callable[[str], tuple[int | None, str, str | None]]


@dataclass(frozen=True)
class Row:
    name: str
    state: str
    detail: str


def base_url(config: dict[str, Any], override: str | None = None) -> str:
    """Where the gateway answers on this machine."""
    if override:
        return override.rstrip("/")
    appliance = config["appliance"]
    if appliance["tls"]["mode"] == "self-signed":
        return f"https://127.0.0.1:{appliance['https_port']}"
    return f"http://127.0.0.1:{appliance['http_port']}"


def make_fetch(
    user: str | None = None, password: str | None = None, timeout: float = 8.0
) -> Fetch:
    """A GET that returns (status, body, error); a self-signed certificate on this machine's own gateway is accepted."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    headers = {}
    if user is not None and password is not None:
        headers["Authorization"] = (
            "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        )

    def fetch(url: str) -> tuple[int | None, str, str | None]:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=context
            ) as response:
                return response.status, response.read().decode("utf-8", "replace"), None
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace"), None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return None, "", str(getattr(exc, "reason", exc))

    return fetch


def _json(body: str) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _row(
    name: str,
    status: int | None,
    body: str,
    error: str | None,
    summary: Callable[[dict[str, Any]], str],
) -> Row:
    if status is None:
        return Row(name, "down", f"gateway not reachable ({error})")
    if status == 401:
        return Row(name, "login", "the gateway wants a login (use --user)")
    data = _json(body)
    if status == 200:
        if not data:
            # every service answers /health with a JSON object; anything else did not come from the service (a gateway
            # with no site for the address it was asked on answers 200 with nothing)
            return Row(
                name,
                "down",
                "answered 200 without the service's own health data - not the service (wrong host name?)",
            )
        return Row(name, "ok", summary(data))
    if status == 503:
        problems = data.get("problems") or [
            data.get("detail", "service reports a problem")
        ]
        return Row(name, "degraded", "; ".join(str(p) for p in problems))
    if status in (502, 504):
        return Row(name, "down", "the gateway gets no answer from the service")
    return Row(name, "down", f"unexpected answer {status}")


def check(config: dict[str, Any], base: str | None, fetch: Fetch) -> list[Row]:
    """Probe through the gateway, or directly on the Compose network when base is None."""
    services = config["services"]
    rows: list[Row] = []

    def probe(name: str, path: str, port: int = 8000):
        return fetch(
            f"{base}{path}" if base is not None else f"http://{name}:{port}/health"
        )

    status, body, error = probe("ginnungagap", "/health", 8123)
    rows.append(
        _row(
            "ginnungagap",
            status,
            body,
            error,
            lambda d: "up" + (f" - note: {d['warning']}" if d.get("warning") else ""),
        )
    )
    for key, label, path, summary in (
        (
            "template_esxi",
            "template-esxi",
            "/templates/esxi/health",
            lambda d: f"{d.get('templates')} template(s)",
        ),
        (
            "template_gns3",
            "template-gns3",
            "/templates/gns3/health",
            lambda d: (
                f"{d.get('templates')} template(s) from {d.get('servers')} server(s)"
            ),
        ),
        (
            "generator",
            "generator",
            "/generator/health",
            lambda d: f"{d.get('servers')} LLM server(s)",
        ),
    ):
        if not services[key]:
            rows.append(
                Row(label, "off", "not running here (configured to run elsewhere)")
            )
            continue
        status, body, error = probe(label, path)
        rows.append(_row(label, status, body, error, summary))
    if services["ova_store"]:
        port = config["templates"]["ova_port"]
        # not behind the gateway (it may ask for a login, an ESXi host cannot): asked on its own port, on the gateway's host
        host = urlparse(base).hostname if base is not None else None
        url = f"http://{host}:{port}/health" if host else "http://ova-store:80/health"
        status, body, error = fetch(url)
        if status is None:
            rows.append(
                Row("ova-store", "down", f"not reachable on port {port} ({error})")
            )
        else:
            rows.append(
                _row(
                    "ova-store",
                    status,
                    body,
                    error,
                    lambda d: (
                        f"serving the OVAs of the template store to ESXi hosts on port {port} (no login)"
                    ),
                )
            )
    else:
        rows.append(
            Row(
                "ova-store",
                "external",
                f"{config['templates']['ova_base_url']} (served elsewhere, not checked here)",
            )
        )
    return rows


def healthy(rows: list[Row]) -> bool:
    return all(row.state in ("ok", "external", "off") for row in rows)


def healthy_behind_login(rows: list[Row]) -> bool:
    """
    Everything that could be checked is fine and the rest sits behind the gateway's login. `apply` only knows the password's
    hash, so it can not look behind it - that is not a failure (`ginnungagap status --user` does the full check).
    """
    return any(row.state == "login" for row in rows) and all(
        row.state in ("ok", "external", "off", "login") for row in rows
    )


def render_table(rows: list[Row]) -> str:
    width = max(len(row.name) for row in rows)
    return "\n".join(
        f"  {row.name.ljust(width)}  {row.state.ljust(8)}  {row.detail}" for row in rows
    )
