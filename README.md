# Ginnungagap appliance

Runs the Ginnungagap services together on one VM, configured by **one file** (`ginnungagap.yml`). Hosts (an ESXi host and
its GNS3 VM) are not part of that file: they stay profiles that you create in the app.

**Status: first version, not yet run with Docker.** The configuration, the renderer and the structure of the compose
project are tested (`python -m pytest`); `docker compose` itself has not been run against it yet.

## Use

```bash
cp ginnungagap.example.yml ginnungagap.yml      # edit; keep it readable by root only, it holds API keys
python -m ginnungagap validate ginnungagap.yml  # every problem at once, nothing is changed on error
python -m ginnungagap render ginnungagap.yml --out rendered --generator-config ../topology-generator/config
docker compose --env-file rendered/compose.env up -d --build
```

`render` writes each service's own configuration into `rendered/` (git-ignored): `compose.env` (which services run, ports,
the template store), `Caddyfile`, `operator.env`, `generator/`, `generator.env` (the only file with API keys, mode 0600),
`collector-gns3/`. Files are rewritten in place because Docker mounts single files by inode.

The compose file builds the services from the sibling checkouts `../TopologyOperator`, `../TopologyBuilder`,
`../Docker-Server-Files` and `../topology-generator` (set `REPOS_DIR` for another place).

## Every service stays separate

One container and image per service, its own rendered configuration, its own health check and its own compose profile.
**No service depends on another**; they only know each other by the URLs written into their configuration. So each one can
be started and tested alone:

```bash
docker compose --env-file rendered/compose.env up -d --build template-esxi   # just this service
docker compose --env-file rendered/compose.env ps
```

| Service | Profile | Inside the network | Health |
|---|---|---|---|
| gateway (Caddy: one entry point, optional TLS and basic auth) | `gateway` | 80, 443 (the only published ports) | - |
| ginnungagap (the app with TopologyBuilder) | `ginnungagap` | `ginnungagap:8123` | `/health` |
| ESXi template API | `template-esxi` | `template-esxi:8000` | `/health` |
| ESXi template collector | `collector-esxi` | - (writes `templates.yml`) | - |
| GNS3 template API | `template-gns3` | `template-gns3:8000` | `/health` |
| GNS3 template collector (hosts from the app's profiles) | `collector-gns3` | - (writes `gns3_templates.yml`) | - |
| topology generator | `generator` | `generator:8000` | `/health` |

The **OVA deploy API** is a service too, but its source is not in our repositories, so it is not built here: it stays
external (`ova_deploy.external_url`, reached through the gateway at `/ova/`).

`tests/test_compose.py` enforces the separation (own profile per service, no `depends_on`, only the gateway publishes
ports, read-only mounts for the template store and the host profiles).

## Not available yet

Monitoring and the file browser (the config refuses `true` instead of ignoring it), the OVA deploy service as a container,
TLS from a file or ACME, single sign-on, an aggregate health endpoint, and the installer / appliance image. The design
and the decisions still open are in the project's handoff repository.
