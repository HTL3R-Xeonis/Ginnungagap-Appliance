"""
The setup wizard as a FastAPI app: a thin shim around ginnungagap.setup_wizard (the form-to-config logic, the YAML and
the hand-off file - all of it importable and testable without FastAPI). This container is deliberately powerless beyond
that: it mounts the config folder read-only (to show that a configuration already exists) and one staging folder
read-write (to hand a new one to the host) - no Docker socket, no sudo, nothing else writable. See
ginnungagap/setup_watcher.py for what happens on the host side of that staging folder, and install.sh for the systemd
path unit that watches it.

Gating: before a configuration exists this container is reached through a static bootstrap Caddyfile with no
authentication (nothing sensitive exists yet - see ginnungagap/bootstrap.py). Once a real configuration exists, the
rendered Caddyfile (ginnungagap/render.py::caddyfile) wraps the *entire* site, this service's /setup/ route included, in
the appliance's own basic_auth - so this app itself never has to know about authentication at all.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from ginnungagap import setup_wizard
from ginnungagap.config import ConfigError, normalise

# fixed in-container mount points (compose.yml binds the real host paths onto these two); the test-only overrides let
# tests of this module run without mounting anything
CONFIG_DIR = Path(os.environ.get("SETUP_WIZARD_CONFIG_DIR", "/etc/ginnungagap"))
STAGING_DIR = Path(os.environ.get("SETUP_WIZARD_STAGING_DIR", "/staging"))

app = FastAPI()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _hash_password(plaintext: str) -> str:
    result = subprocess.run(
        ["caddy", "hash-password", "--plaintext", plaintext],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _current_values() -> tuple[dict, bool]:
    existing = setup_wizard.existing_config(CONFIG_DIR)
    return setup_wizard.prefill_from(existing), existing is not None


@app.get("/", response_class=HTMLResponse)
def form() -> str:
    values, has_existing = _current_values()
    return setup_wizard.render_page(values, has_existing=has_existing)


@app.post("/", response_class=HTMLResponse)
async def submit(request: Request) -> Response:
    form_data = await request.form()
    values = {key: str(value) for key, value in form_data.items()}
    action = values.get("action", "apply")
    existing = setup_wizard.existing_config(CONFIG_DIR)
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
            headers={"Content-Disposition": 'attachment; filename="ginnungagap.yml"'},
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
