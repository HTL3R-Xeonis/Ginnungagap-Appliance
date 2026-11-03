# Ginnungagap appliance

Runs the Ginnungagap services together on one VM, configured by **one file** (`ginnungagap.yml`). Hosts (an ESXi host and
its GNS3 VM) are not part of that file: they stay profiles that you create in the app.

**Status: first version, run with Docker.** `smoke/smoke-test.sh` builds every image and runs it against a throw-away project, a
fake template store and a host that does not exist: each service alone, then all together through the gateway, then the
operating commands (`status`, `apply` on an unchanged file recreates nothing, `backup`, `restore`) and a switch to self-signed
TLS. It passes on a Docker host (Docker 29, Compose 5). What it does not cover: an actual OVA import by the OVA deploy
service (it needs a real ESXi host and password) and basic auth. The installer (root, real systemd unit, reboot) and the
appliance image (OVA: deployed on the lab ESXi, first boot from guestinfo, NFS template store, reboot) were verified on
Debian 13 VMs in the lab; see `image/README.md`.

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
`../Docker-Server-Files`, `../topology-generator` and, for the OVA deploy service, `../TopologyBuilderServices` (set `REPOS_DIR`
for another place).

**OVA deploy service.** `services.ova_deploy` is `true` (own container `ova-deploy`, built from the TopologyBuilderServices
repository as it is, with a `/health` endpoint added around it), `external` (the existing one, `ova_deploy.external_url`) or
`off`. With several ESXi hosts each has its own password: the container looks the password up by the requested host's
address in the host profiles the app stores (read-only, on every request, so a host added in the app works at once).
`ova_deploy.esxi_password` is only the fallback for a host without a stored password and may stay empty; if set it is written
to `rendered/ova-deploy/esxi_password` (mode 0600) and mounted as a secret file, never put into an environment variable or
another file. The service reads the OVAs from the same template store as the ESXi template API (read-only).

## Operate

```bash
python -m ginnungagap apply ginnungagap.yml        # validate, render, docker compose up, wait until everything is healthy
python -m ginnungagap status ginnungagap.yml       # one health table over every service, asked through the gateway
python -m ginnungagap backup ginnungagap.yml --to /var/backups
python -m ginnungagap restore /var/backups/ginnungagap-backup-20261102-123005 --yes
```

* `apply` stops at the first invalid setting without touching anything. It recreates only the services whose own rendered
  file changed (the gateway for the `Caddyfile`, the generator for its three files); the app and its data are never
  recreated by a change elsewhere. `--no-start` only renders, `--no-build` skips rebuilding the images.
  The OVA deploy service is recreated when its fallback password changes (it reads it once at its start), and the template
  store's volume is recreated when the store changes (NFS to a local folder, or another export): Docker never changes an
  existing volume's options, so without that the services would keep reading the old store. It is read-only, nothing is lost.
  On an appliance image without internet use `apply --no-build` (the image carries every service image; a build downloads
  the Python packages again).
* `status` exits non-zero if anything is down or degraded, so it can be used from a monitor or a script. Behind a login
  (`appliance.auth`) pass `--user NAME`; the password is asked for or read from `GINNUNGAGAP_PASSWORD`.
* `backup` writes a folder (mode 0700) with a copy of the config file and `data.tgz`, the app's data volume (host profiles,
  projects, designs, notes). `restore` stops the app, replaces that volume and starts it again; it needs `--yes`.

## Install on a machine

```bash
sudo ./install.sh --repos-dir /srv/repos      # program in /opt/ginnungagap, command `ginnungagap`, config in /etc/ginnungagap
sudo $EDITOR /etc/ginnungagap/ginnungagap.yml
sudo ginnungagap apply /etc/ginnungagap/ginnungagap.yml
```

It needs Docker with the compose plugin, `python3` (3.12+) with `python3-yaml`, and the four sibling repositories the images
are built from. A systemd unit (`ginnungagap.service`) applies the configuration at boot; the containers restart by
themselves, so this only picks up an edited file. Running `install.sh` again updates the program and keeps your
configuration; `sudo ./install.sh --uninstall` removes program, command and unit and keeps the configuration and the data.

## TLS

`appliance.tls.mode: self-signed` (with an `https://` `public_url`) makes the gateway serve HTTPS with a certificate from its own
CA and send plain HTTP to HTTPS on the published port. A browser warns until the CA is trusted; take its certificate out and
import it where needed:

```bash
cd /opt/ginnungagap/app && docker compose --env-file rendered/compose.env cp gateway:/data/caddy/pki/authorities/local/root.crt ginnungagap-ca.crt
```

`status` accepts that certificate on this machine's own gateway. Certificates from a file or from ACME are not available yet.

## Appliance image (OVA)

A VM that needs no installation: import the `.ova`, tell it its configuration, power it on. The image is built from a Debian 13
VM in three steps, all from this repository (details and the export scripts: `image/README.md`):

```bash
sudo ./install.sh --repos-dir /srv/repos --install-packages --image --no-start   # on the Debian VM that becomes the image
sudo image/prepare-image.sh --yes --zero-free-space                              # builds every image, removes everything of this machine
sudo systemctl poweroff                                                          # then, from a machine that can reach ESXi:
python image/export_ova.py --host <esxi> --vm <vm> --out <folder>                # ESXI_PASSWORD from the environment
```

The image has **no configuration, no SSH host keys, no machine id and no authorized SSH keys**; every image of every service is
already in the VM's image store, so it starts without internet and without a build. At its first boot
(`ginnungagap-firstboot.service`, `ginnungagap firstboot`) it reads what the deployer put into the VM's *guestinfo* (on ESXi:
VM options > Advanced > configuration parameters, or `image/set_guestinfo.py`):

| key (all `guestinfo.ginnungagap.`) | meaning |
|---|---|
| `config` | the whole `ginnungagap.yml`, base64 (`config.encoding = plain` for plain text) |
| `hostname` | optional, the VM's host name |
| `ssh_authorized_key` | optional, one public key for the login user |
| `admin_password_hash` | optional, a crypt hash (`openssl passwd -6`) for the login user - never the password itself |
| `admin_sudo_nopasswd` | optional, `true` lets the login user use `sudo` without a password (automation; the image is delivered without it) |

It creates the SSH host keys, creates the template folder of a local store, validates the configuration (an invalid one is kept as `ginnungagap.yml.rejected`, the reasons
are shown at the console login and in `/var/lib/ginnungagap/firstboot.problems`, and the next boot tries again), and
`ginnungagap.service` then applies it. A configuration that is already in `/etc/ginnungagap/` wins over guestinfo. With no
configuration at all the VM boots, shows at the console what to do and starts no service.

The login of the underlying Debian image is the one of the template it was built from; **change it** (guestinfo
`admin_password_hash`, or `ssh_authorized_key` and then switch password login off) before the VM is reachable by others.

Reaching it: the gateway answers for the name in `appliance.public_url`, for `localhost`, and for **any other name or address**
(so `https://<the VM's IP>` shows the web interface too; the certificate is the gateway's own either way). Without that
catch-all Caddy would answer an unknown name with an empty page.

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

The **OVA deploy API** is a service too (`ova-deploy`, `/ova/health`), built from the TopologyBuilderServices repository when
`services.ova_deploy` is `true`; with `external` the existing one (`ova_deploy.external_url`) is reached through the gateway at `/ova/`.

`smoke/smoke-test.sh` runs the whole sequence - each service alone (degraded until its data is there), then the gateway
alone (502 for what is not running), the app with a host that does not answer, then everything together - and removes
everything afterwards (`KEEP=1` keeps it). It needs only Docker and the sibling repositories.

`tests/test_compose.py` enforces the separation (own profile per service, no `depends_on`, only the gateway publishes
ports, read-only mounts for the template store and the host profiles).

## Not part of this solution

**Monitoring** (Prometheus, Grafana, exporters) is deliberately not part of the one-VM solution. It keeps running where it runs
today; `services.monitoring` in the config is refused with that explanation rather than silently ignored.

## Not available yet

The file browser (the config refuses `true` instead of ignoring it),
TLS from a file or ACME, single sign-on (basic auth at the gateway exists in the config but is neither wanted nor tested)
and an aggregate health endpoint (`ginnungagap status` is the one view over every service). The design and the decisions
still open are in the project's handoff repository.
