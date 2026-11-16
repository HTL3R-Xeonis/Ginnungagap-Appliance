"""
Bringing the appliance up before any `ginnungagap.yml` exists at all.

Normally `ginnungagap apply` both writes `rendered/compose.env` + `rendered/Caddyfile` *and* starts the containers - but
the very first file to render comes FROM the one configuration file, and before it exists there is nothing to render.
Without this module a fresh VM (or one whose guestinfo config did not validate, see firstboot.py) would have no gateway,
no Caddyfile and nothing listening on 80/443 at all - a closed box, console-only.

This writes a fixed, static bootstrap environment instead (no configuration object involved: everything here is a
constant) that brings up only the gateway and the setup wizard, with the gateway's Caddyfile pointing at the wizard for
every request. It uses the exact same `rendered/` folder `apply` later renders into: when the wizard's submission is
applied (see setup_watcher.py), `render.render()` overwrites both `compose.env` and `Caddyfile` with the real ones -
profiles grow to the full set, the Caddyfile gains the app's real routes and auth - and Caddy (already in FILE_OWNERS)
is recreated to pick it up. The setup wizard's own container needs no such swap: it is always part of the normal
profile list too (render.py::profiles), so it simply keeps running.

install.sh wires `ginnungagap bootstrap` to run at boot, but only while no configuration exists yet
(`ginnungagap-bootstrap.service`'s `ConditionPathExists=!.../ginnungagap.yml`); once one does, this is never reached
again at boot (ginnungagap.service takes over), and the live hand-off from the wizard never goes through systemd at
all - it calls `operate.apply()` directly.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ginnungagap import operate as operate_module
from ginnungagap import render as render_module

Runner = Callable[[list[str]], int]

# Deliberately plain HTTP, no TLS and no auth: nothing worth protecting exists yet, and the real public_url (needed for
# a certificate name) is not known until the wizard's submission is applied. Every request, on any host header, goes to
# the wizard - there is nothing else to route to.
BOOTSTRAP_CADDYFILE = (
    render_module.HEADER.rstrip("\n")
    + "\n\n"
    + "{\n\tauto_https off\n}\n\n"
    + ":80 {\n"
    + "\tencode zstd gzip\n"
    + "\thandle {\n\t\treverse_proxy setup:8000\n\t}\n"
    + "}\n"
)


def bootstrap_env() -> str:
    return render_module._env_file(
        {
            "COMPOSE_PROJECT_NAME": render_module.project_name(),
            "COMPOSE_FILE": "compose.yml",
            "COMPOSE_PROFILES": "gateway,setup",
            "TZ": "UTC",
            "HTTP_PORT": "80",
            "HTTPS_PORT": "443",
            "LOG_LEVEL": "INFO",
            **render_module.host_paths(),
        }
    )


def run(
    bundle: Path,
    runner: Runner | None = None,
    build: bool = True,
    out: Callable[[str], None] = print,
) -> int:
    """Writes the bootstrap `rendered/` files and starts just the gateway and the setup wizard. Idempotent: running it
    again (e.g. every boot before a configuration exists) only recreates what actually changed, same as `apply`."""
    runner = runner or (lambda args: operate_module.default_runner(args, cwd=bundle))
    rendered = bundle / "rendered"
    rendered.mkdir(parents=True, exist_ok=True)
    (rendered / "compose.env").write_text(bootstrap_env(), encoding="utf-8")
    (rendered / "compose.env").chmod(0o644)
    (rendered / "Caddyfile").write_text(BOOTSTRAP_CADDYFILE, encoding="utf-8")
    (rendered / "Caddyfile").chmod(0o644)
    up = operate_module.compose_args(bundle, "up", "-d", "--remove-orphans")
    if build:
        up.append("--build")
    if runner(up) != 0:
        out("docker compose up failed (gateway + setup wizard)")
        return 1
    out(
        "gateway and the setup wizard are up; open the VM's address in a browser to finish setup"
    )
    return 0
