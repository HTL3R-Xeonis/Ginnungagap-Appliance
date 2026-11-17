"""
The setup wizard as a FastAPI app: a thin shim around ginnungagap.setup_wizard (the form-to-config logic, the YAML and
the hand-off file - all of it importable and testable without FastAPI). This container is deliberately powerless beyond
that: it mounts the config folder read-only (to show that a configuration already exists) and one staging folder
read-write (to hand a new one to the host) - no Docker socket, no sudo, nothing else writable. See
ginnungagap/setup_watcher.py for what happens on the host side of that staging folder, and install.sh for the systemd
path unit that watches it.

The first-boot Caddyfile reaches this service before a configuration exists. Once the config has been written, the
service requires its Basic Auth credentials on every setup form request, including while Caddy switches away from bootstrap
routing. A configured appliance without Basic Auth has no web setup route.
"""

from __future__ import annotations

import os
import subprocess
from base64 import b64decode
from binascii import Error
from hmac import compare_digest
from pathlib import Path
from typing import Annotated

import bcrypt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ginnungagap import setup_wizard, status
from ginnungagap.config import ConfigError, normalise

# fixed in-container mount points (compose.yml binds the real host paths onto these two); the test-only overrides let
# tests of this module run without mounting anything
CONFIG_DIR = Path(os.environ.get("SETUP_WIZARD_CONFIG_DIR", "/etc/ginnungagap"))
STAGING_DIR = Path(os.environ.get("SETUP_WIZARD_STAGING_DIR", "/staging"))

app = FastAPI()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/appliance/health")
def appliance_health() -> JSONResponse:
    config = setup_wizard.existing_config(CONFIG_DIR)
    if config is None:
        return JSONResponse({"status": "unconfigured", "services": {}}, status_code=503)
    rows = status.check(config, None, status.make_fetch(timeout=2.0))
    ready = status.healthy(rows)
    return JSONResponse(
        {
            "status": "ok" if ready else "degraded",
            "services": {row.name: row.state for row in rows},
        },
        status_code=200 if ready else 503,
    )


def _hash_password(plaintext: str) -> str:
    result = subprocess.run(
        ["caddy", "hash-password", "--plaintext", plaintext],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _authorised_config(request: Request) -> dict | None:
    existing = setup_wizard.existing_config(CONFIG_DIR)
    if existing is None and not (CONFIG_DIR / "ginnungagap.yml").exists():
        return None
    auth = existing["appliance"]["auth"] if existing is not None else {}
    if auth.get("mode") != "basic":
        raise HTTPException(404)
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "basic":
        try:
            username, sep, password = b64decode(encoded, validate=True).partition(b":")
            if (
                sep
                and compare_digest(username, auth["user"].encode())
                and bcrypt.checkpw(password, auth["password_hash"].encode())
            ):
                return existing
        except (Error, ValueError):
            pass
    raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="Ginnungagap"'})


@app.get("/", response_class=HTMLResponse)
def form(existing: Annotated[dict | None, Depends(_authorised_config)]) -> str:
    return setup_wizard.render_page(
        setup_wizard.prefill_from(existing), has_existing=existing is not None
    )


@app.post("/", response_class=HTMLResponse)
async def submit(
    request: Request, existing: Annotated[dict | None, Depends(_authorised_config)]
) -> Response:
    form_data = await request.form()
    values = {key: str(value) for key, value in form_data.items()}
    action = values.get("action", "apply")
    has_existing = existing is not None
    raw = setup_wizard.keep_existing_secrets(
        setup_wizard.build_raw_config(values, hash_password=_hash_password), existing
    )
    try:
        normalised = normalise(raw)
    except ConfigError as exc:
        return HTMLResponse(
            setup_wizard.render_page(
                values, problems=exc.problems, has_existing=has_existing
            ),
            status_code=400,
        )

    yaml_text = setup_wizard.render_yaml(normalised)
    if action == "download":
        return Response(
            yaml_text,
            media_type="application/yaml",
            headers={
                "Content-Disposition": 'attachment; filename="ginnungagap.yml"',
                "Cache-Control": "no-store",
            },
        )

    setup_wizard.write_staged(STAGING_DIR, yaml_text)
    return HTMLResponse(
        setup_wizard.render_page(
            values,
            message=(
                "Configuration accepted. The appliance is applying it now - this can "
                "take up to a minute, and the page you are on may stop answering for a "
                "moment while the gateway restarts. Reload this address, or the public "
                "URL you configured, once it is done."
            ),
            has_existing=has_existing,
        )
    )
