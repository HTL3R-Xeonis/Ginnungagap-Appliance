# Ginnungagap appliance

Runs the Ginnungagap services together on one VM, configured by **one file** (`ginnungagap.yml`). Hosts (an ESXi host and
its GNS3 VM) are not part of that file: they stay profiles that you create in the app.

**Status: first version, run with Docker.** `smoke/smoke-test.sh` builds every image and runs it against a throw-away project, a
fake template store and a host that does not exist: each service alone, then all together through the gateway, then the
operating commands (`status`, `apply` on an unchanged file recreates nothing, `backup`, `restore`) and a switch to self-signed
TLS. It passes on a Docker host (Docker 29, Compose 5). What it does not cover: an actual OVA import by an ESXi host
(it needs a real host and its login; the smoke test only checks what the OVA server answers) and basic auth. The installer (root, real systemd unit, reboot) and the
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
`../Docker-Server-Files` and `../topology-generator` (set `REPOS_DIR` for another place).

### OVA server for the ESXi hosts

TopologyBuilder does not upload an OVA any more: it reads the OVF descriptor over HTTP and the **ESXi host pulls the disks
itself**. So the hosts need an HTTP address that serves the OVAs of the template store as `<address>/<folder>/<file>.ova`
(`<folder>` and `<file>` are the template's `_source_dir` and `file` in the ESXi template API, which is the layout of the
store itself). The `ova-store` container does that: `services.ova_store: true` (the default), published on
**`templates.ova_port` (default 8088/TCP)**.

* **Setting.** The address the ESXi hosts use goes to the app as `OVA_BASE_URL` (written into `operator.env`; the app's
  profile `api:` section value `ova_base_url`). It is `templates.ova_base_url` when set, otherwise this appliance's own host
  name or address from `appliance.public_url` with `ova_port`, e.g. `http://10.20.20.181:8088`. Set it yourself when the hosts
  reach the appliance under another address; with `services.ova_store: false` (the files are served by something else that
  keeps the same `<folder>/<file>` layout) it is required. The old `services.ova_deploy` and the `ova_deploy` section were
  removed; a configuration that still has them is refused with a message telling you to delete them.
* **What it serves.** Plain HTTP, read-only (the store is mounted `:ro`), `GET` and `HEAD` only, **only `*.ova`** (the
  descriptors and every other file answer 404, so do names or folders that start with a dot), no directory listing, and `Range`
  requests work (they are what the pull needs). Caddy runs it with the rendered `ova-store/Caddyfile`.
* **No login, LAN only.** An ESXi host cannot send credentials, so the OVAs can be read without a login by everything that
  can reach that port. Open it to the ESXi hosts of the lab network only, never to the internet or the user network, and do
  not put it behind the gateway's `appliance.auth`.
* **Which port the ESXi hosts need.** TCP `templates.ova_port` (8088 unless changed) from each ESXi host to this appliance,
  nothing else (the ESXi host opens the connection, so the host's own firewall must allow outgoing HTTP: its `httpClient`
  ruleset). A firewall between the hosts and the appliance must allow that one port; the
  console login text and `ginnungagap status` (row `ova-store`) show the address and the port. The setup wizard says the same
  behind the information icon of "OVA server for the ESXi hosts".

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
  The template store's volume is recreated when the store changes (NFS to a local folder, or another export): Docker never changes an
  existing volume's options, so without that the services would keep reading the old store. It is read-only, nothing is lost.
  On an appliance image without internet use `apply --no-build` (the image carries every service image; a build downloads
  the Python packages again).
  A local template store (`templates.store.type: local`) is a bind mount, and Docker refuses to mount a folder that does not
  exist - so `apply` creates a missing store folder (empty, mode 0755; the default is `/srv/templates`) before it starts anything.
  "Which service needs recreating" is decided against what the last *successful* apply started (`rendered/.applied-digests.json`),
  not against the previous run: an apply that fails half-way (for example `docker compose up`) cannot make the next one believe
  nothing changed and leave the gateway on an old `Caddyfile`. With `appliance.auth` set, `apply` only knows the password's hash,
  so it can not look behind the login: a gateway that answers 401 counts as success (with a note to run `status --user`) as long
  as nothing else is down.
* `status` exits non-zero if anything is down or degraded, so it can be used from a monitor or a script. Behind a login
  (`appliance.auth`) pass `--user NAME`; the password is asked for or read from `GINNUNGAGAP_PASSWORD`.
* `GET /appliance/health` through Caddy reports the bundled services as JSON. It returns HTTP 200 when healthy
  and HTTP 503 before setup or when a bundled service is down or degraded. Services configured to run elsewhere
  are listed without probing them. This endpoint follows the site's Basic Auth setting.
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

It also installs three more units for the setup wizard below: `ginnungagap-bootstrap.service` (brings the wizard up
before a configuration exists), `ginnungagap-setup-apply.path` and `.service` (apply what the wizard staged). `--staging-dir
DIR` (default `/var/lib/ginnungagap/setup`) is the one folder the wizard's own container may write to.

## Setup wizard

A fresh appliance (or one whose guestinfo configuration did not validate) has no `ginnungagap.yml` at all - instead of a
terminal and `ginnungagap.example.yml`, open the VM's address in a browser: a plain HTML form (`setup`, no JavaScript)
covering the same fields as the YAML file, grouped the same way. It builds the file, checks it with the exact same
`ginnungagap validate` logic (never a second copy of the rules), shows the same problem list on failure, and offers
"Download the YAML instead" at any point for config-as-code. It also hashes a typed password with `caddy hash-password`
server-side - the one step that otherwise needs a terminal.

**How a fresh VM gets from nothing to a running appliance** (the chicken-and-egg this solves: the gateway's Caddyfile is
normally *rendered from* the configuration, so before one exists there is nothing to tell the gateway to route anywhere -
not even to the wizard itself):

1. **Before any configuration exists**, `ginnungagap bootstrap` (`ginnungagap-bootstrap.service`, boots whenever
   `/etc/ginnungagap/ginnungagap.yml` does not exist) writes a fixed, static `Caddyfile` - plain HTTP, no auth, every
   request routed to the wizard - and a minimal `compose.env` (`COMPOSE_PROFILES=gateway,setup`) into the same
   `rendered/` folder `apply` normally renders into, then starts just those two containers. The wizard container itself
   stays unprivileged throughout: no Docker socket, no sudo, no other host path mounted.
2. The admin opens the VM's address (shown in the console hint at `/etc/issue.d/ginnungagap.issue`) and fills in the form.
   After the wizard starts, bootstrap checks the address again and asks Debian's login console to refresh the prompt.
   If invalid guestinfo configuration was supplied, the console keeps its validation errors.
   When choosing an external service, enter its actual URL in the revealed field.
   Under **Template store**, select the local path or NFS export and enable **Allow admins to manage ESXi templates in
   the Admin Panel** to let admins import OVAs and edit their YAML metadata. For NFS, use an export writable by the
   Appliance. External catalog services should read the same share if they are used.
3. On submit the wizard's container writes the validated YAML and a marker file into its one writable path (a folder
   it shares with the host, nothing else) and tells the admin it is being applied.
4. A systemd path unit on the host (`ginnungagap-setup-apply.path`) notices the marker, triggers
   `ginnungagap-setup-apply.service`, which **re-validates independently** (never trusts the container), writes
   `/etc/ginnungagap/ginnungagap.yml` and runs the real `ginnungagap apply` - the same `render()`/`docker compose up`
   pipeline as always. This overwrites both `compose.env` (the real profile list) and `Caddyfile` (the real routes, the
   real TLS/auth); the gateway is recreated because its own file changed (see `apply`'s file-ownership check above), and
   the app and the other services come up for the first time. No reboot is needed, and resubmitting the wizard later
   works exactly the same way.
5. Browser reconfiguration at `/setup/` is available when `appliance.auth.mode` is `basic`. Both Caddy and the wizard
   enforce those credentials. This also protects the wizard while Caddy switches from bootstrap routing. If Basic Auth
   is disabled, use the configuration file and `ginnungagap apply` for later changes.

Appliances installed with `--image` use the prebuilt images for both startup and Wizard submissions. Docker Compose
skips pulls and builds in this mode. Build the images on the preparation VM before exporting the OVA, and use
`ginnungagap build-images` to refresh them on an installed appliance with network access.

Out of scope (see the plan in the project's handoff repository): it never touches TopologyOperator's own user store -
the appliance's `admin`/`admin` bootstrap login is unrelated and mentioned only in the console hint once setup is done.
ESXi/GNS3 host profiles are not configured here either; that is the app's own "Design a host" dialog, after the first
login.

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
| `ssh_authorized_key` | optional, one public key for the login user (then no initial password is made and the template's own password is locked) |
| `admin_password_hash` | optional, a crypt hash (`openssl passwd -6`) for the login user - never the password itself (then no initial password is made) |
| `admin_sudo_nopasswd` | optional, `true` lets the login user use `sudo` without a password (automation; the image is delivered without it) |

It creates the SSH host keys, creates the template folder of a local store, validates the configuration (an invalid one is kept as `ginnungagap.yml.rejected`, the reasons
are shown at the console login and in `/var/lib/ginnungagap/firstboot.problems`, and the next boot tries again), and
`ginnungagap.service` then applies it. A configuration that is already in `/etc/ginnungagap/` wins over guestinfo. With no
configuration at all the VM boots, the console hint gives its address to open in a browser (the setup wizard, see below -
`ginnungagap-bootstrap.service` brings the gateway and the wizard up for exactly this case), and nothing else starts yet.

### Admin SSH access

The image comes with a preconfigured admin SSH access and shows it on the VM console:

```
Ginnungagap appliance
SSH: ssh junioradmin@10.20.20.57 -p 22
Password: <16 random characters> (you must change it at the first login)
```

- **Who**: the image's login user (the first ordinary account, `junioradmin` in the current image). No extra account.
- **The password** is made at the first boot of every VM (random, 16 letters and digits, set with `chpasswd` through its standard
  input, never logged) and expired (`chage -d 0`), so SSH asks for a new one at the first login. The console shows it only
  while it is still the initial one; afterwards the SSH line stays without a password. It is kept in
  `/var/lib/ginnungagap/ssh-access.json` (mode 0600) so a later boot does not make a new one.
- **Address and port** come from the machine itself (the address on its default route, the first `Port` of the sshd
  configuration, 22 by default). `ginnungagap-issue.service` rewrites the text at every boot once the network is up, and
  bootstrap, `apply`, the setup wizard's hand-off and the first boot rewrite it too (`ginnungagap issue` does it by hand), so a DHCP
  address that changed is shown right.
- **Text console**: `install.sh --image` sets the default boot target to `multi-user.target`. The Debian template ships a
  desktop login (GDM), and `/etc/issue.d` is only shown on text consoles, so without this the console would show a graphical
  login with no address. `systemctl set-default graphical.target` brings the desktop back.
- **After a password change** `ginnungagap-issue.path` (`PathChanged=/etc/shadow`) starts `ginnungagap-issue-changed.service`
  (`ginnungagap issue --clear-screens`): the text is rewritten without the password and the idle login screens are wiped, so
  the initial password does not stay readable above the new text. At boot nothing is wiped.
- **Deployer-set access**: with `admin_password_hash` or `ssh_authorized_key` in guestinfo nothing is generated and the console
  says `(password/key set by the deployer)`. With only a key, the password of the login user is locked, otherwise the
  template's own Debian password would still open SSH.
- **Password login** is switched on by the drop-in `/etc/ssh/sshd_config.d/10-ginnungagap.conf` (`PasswordAuthentication yes`)
  that `install.sh --image` writes; sshd takes the first value it reads, so it wins over a later `50-cloud-init.conf`.
  `image/prepare-image.sh` removes the generated password (and the decision that it was generated) but keeps the drop-in.

Change the password at the first login, or switch password login off once a key is in place.

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
| gateway (Caddy: one entry point, optional TLS and basic auth) | `gateway` | 80, 443 | - |
| ginnungagap (the app with TopologyBuilder) | `ginnungagap` | `ginnungagap:8123` | `/health` |
| ESXi template API | `template-esxi` | `template-esxi:8000` | `/health` |
| ESXi template collector | `collector-esxi` | - (writes `templates.yml`) | - |
| GNS3 template API | `template-gns3` | `template-gns3:8000` | `/health` |
| GNS3 template collector (hosts from the app's profiles) | `collector-gns3` | - (writes `gns3_templates.yml`) | - |
| topology generator | `generator` | `generator:8000` | `/health` |
| OVA server for the ESXi hosts (Caddy, read-only, `*.ova` only, no login) | `ova-store` | 8088 (published, `templates.ova_port`) | `/health` |
| setup wizard (see "Setup wizard" above) | `setup` | `setup:8000`, at `/setup/` with Basic Auth after configuration | `/health` |

`smoke/smoke-test.sh` runs the whole sequence - each service alone (degraded until its data is there), then the gateway
alone (502 for what is not running), the app with a host that does not answer, then everything together - and removes
everything afterwards (`KEEP=1` keeps it). It needs only Docker and the sibling repositories.

`tests/test_compose.py` enforces the separation (own profile per service, no `depends_on`, only the gateway and the OVA
server publish ports, read-only mounts for the template store and the host profiles).

## Not part of this solution

**Monitoring** (Prometheus, Grafana, exporters) is deliberately not part of the one-VM solution. It keeps running where it runs
today; `services.monitoring` in the config is refused with that explanation rather than silently ignored.

## Not available yet

The remaining items are the file browser, TLS from a file or ACME, and single sign-on.
The design and open decisions are in the project's handoff repository.
