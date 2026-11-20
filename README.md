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

<!-- BEGIN code names -->
## The parts and their code names

The appliance runs, and updates, five components. **Ginnungagap** is the name of the whole product: this repository, the
appliance image, the `ginnungagap` command and everything you log into. The parts have code names, used in the READMEs, the
documentation and next to the plain role in the tables of `ginnungagap status`, `ginnungagap versions` and
`ginnungagap update --check` (`operator (Igdrasyl)`, `template-esxi (Hugyn)`); the landing page and error messages of the app do not use them.

| Code name | Plain role | Repository | Component key | Services |
|---|---|---|---|---|
| **Igdrasyl** | the web app (operator): GUI, gateway, backends | [`HTL3R-Xeonis/Igdrasyl`](https://github.com/HTL3R-Xeonis/Igdrasyl) (formerly TopologyOperator) | `operator` | `ginnungagap` |
| **Mjonyr** | the builder: deploy and orchestration library with its CLI | [`HTL3R-Xeonis/TopologyBuilder`](https://github.com/HTL3R-Xeonis/TopologyBuilder) (the repository keeps its name, the development branch is `Mjonyr`) | `builder` | `ginnungagap` (it is the app's library) |
| **Hugyn and Munyn** | the template services: **Hugyn** is the ESXi side, **Munyn** the GNS3 side | [`HTL3R-Xeonis/Hugyn-and-Munyn`](https://github.com/HTL3R-Xeonis/Hugyn-and-Munyn) (formerly Docker-Server-Files) | `template-services` | Hugyn: `template-esxi`, `collector-esxi`; Munyn: `template-gns3`, `collector-gns3` |
| **Mimyr** | the generator: LLM topology and configuration drafts | [`HTL3R-Xeonis/Mimyr`](https://github.com/HTL3R-Xeonis/Mimyr) (formerly TopologyGenerator, checkout folder `topology-generator`) | `generator` | `generator` |
| **Ginnungagap appliance** | the appliance itself: compose project, installer, wizard, image tooling | [`HTL3R-Xeonis/Ginnungagap-Appliance`](https://github.com/HTL3R-Xeonis/Ginnungagap-Appliance) (this repository) | `appliance` | `setup`, `gateway`, `ova-store` and the others of the compose file |

Two features of the appliance have code names of their own. They are not components, so they have no key and no repository of their own:

| Code name | Plain role | Where it lives |
|---|---|---|
| **Idynoun** | the updater: `ginnungagap update` (check, apply, roll back) and the update page of the admin panel | [Ginnungagap-Appliance](https://github.com/HTL3R-Xeonis/Ginnungagap-Appliance) (the engine and the host units), [Igdrasyl](https://github.com/HTL3R-Xeonis/Igdrasyl) (the admin page) |
| **Haymdal** | the watchman: the health view `ginnungagap status`, the `/health` endpoints and the app's "Connection lost" overlay | [Ginnungagap-Appliance](https://github.com/HTL3R-Xeonis/Ginnungagap-Appliance) (`status`), [Igdrasyl](https://github.com/HTL3R-Xeonis/Igdrasyl) (`/health`, overlay) |

The keys of the manifest, the configuration and compose are **roles**, never a repository or a code name, so a later rename of a
repository does not touch them. The identifiers inside (compose service and image names, systemd units, `TOPOLOGYOPERATOR_*` and
`XEONIS_*` variables, the markers the app writes on ESXi VMs, the image names `ginnungagap-appliance-v<version>`) keep their
names. What an appliance that was set up before the code names keeps working is described under "Names before the code names"
in "Updating components".
<!-- END code names -->

## Use

```bash
cp ginnungagap.example.yml ginnungagap.yml      # edit; keep it readable by root only, it holds API keys
python -m ginnungagap validate ginnungagap.yml  # every problem at once, nothing is changed on error
python -m ginnungagap render ginnungagap.yml --out rendered --generator-config ../Mimyr/config
docker compose --env-file rendered/compose.env up -d --build
```

`render` writes each service's own configuration into `rendered/` (git-ignored): `compose.env` (which services run, ports,
the template store), `Caddyfile`, `operator.env`, `generator/`, `generator.env` (the only file with API keys, mode 0600),
`collector-gns3/`. Files are rewritten in place because Docker mounts single files by inode.

The compose file builds the services from the sibling checkouts `../Igdrasyl`, `../TopologyBuilder`, `../Hugyn-and-Munyn` and `../Mimyr`
(set `REPOS_DIR` for another place; checkouts that still have the old folder names are recorded under the new ones by
`ginnungagap update --adopt`, see "Names before the code names").

### OVA server for the ESXi hosts

Mjonyr (the builder) does not upload an OVA any more: it reads the OVF descriptor over HTTP and the **ESXi host pulls the disks
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
* `status` prints the appliance release (`Ginnungagap appliance v1.3.0`, from `update/installed.json`, else the program's own
  version) as its first line, then the table. The same line heads the text at the VM's console login. It exits non-zero if
  anything is down or degraded, so it can be used from a monitor or a script. Behind a login
  (`appliance.auth`) pass `--user NAME`; the password is asked for or read from `GINNUNGAGAP_PASSWORD`.
* `GET /appliance/health` through Caddy reports the bundled services as JSON. It returns HTTP 200 when healthy
  and HTTP 503 before setup or when a bundled service is down or degraded. Services configured to run elsewhere
  are listed without probing them. This endpoint follows the site's Basic Auth setting.
* `backup` writes a folder (mode 0700) with a copy of the config file and `data.tgz`, the app's data volume (host profiles,
  projects, designs, notes, the accounts and the session secret, snippet topics, the audit logs and the app log). It runs a
  throw-away container of the Operator's own image, so it needs no network. `restore` stops the app, replaces that volume and
  starts it again; it needs `--yes`.
* **Everything the Operator keeps is on the `data` volume** (`render.py` sets every `TOPOLOGYOPERATOR_*` file and folder to a path under
  `/data`; the app log `logs/` in the image is a link to `/data/logs`). Nothing important is in the container's writable layer, so an
  update, `apply` or a new image that recreates the container loses no account and logs nobody out. An appliance installed
  before that (accounts in the container) is carried over once, by `apply` and by the update, **before** the container is recreated
  (`operate.migrate_operator_state`: `docker cp` out, written into the volume only where the file is missing, never overwritten;
  if it fails nothing is recreated). The first administrator is made by the Operator itself when `users.json` does not exist.
  Rolling back to a release from before this change brings the old render with it: the files stay on the volume but that
  Operator does not read them.

## Install on a machine

```bash
sudo ./install.sh --repos-dir /srv/repos      # program in /opt/ginnungagap, command `ginnungagap`, config in /etc/ginnungagap
sudo $EDITOR /etc/ginnungagap/ginnungagap.yml
sudo ginnungagap apply /etc/ginnungagap/ginnungagap.yml
```

It needs Docker with the compose plugin, `python3` (3.12+) with `python3-yaml`, and the four sibling repositories the images
are built from (Igdrasyl, TopologyBuilder, Hugyn-and-Munyn, Mimyr). A systemd unit (`ginnungagap.service`) applies the configuration at boot; the containers restart by
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

Out of scope: it never touches Igdrasyl's own user store -
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
- **Password login** is switched on by the drop-in `/etc/ssh/sshd_config.d/10-ginnungagap.conf` (`PasswordAuthentication yes`, `PermitRootLogin no`)
  that `install.sh --image` writes; sshd takes the first value it reads, so it wins over a later `50-cloud-init.conf`.
  `image/prepare-image.sh` removes the generated password (and the decision that it was generated), locks root's password and
  deletes the journal of the build boots, but keeps the drop-in.

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
| ginnungagap (the app, Igdrasyl, with Mjonyr) | `ginnungagap` | `ginnungagap:8123` | `/health` |
| ESXi template API (Hugyn) | `template-esxi` | `template-esxi:8000` | `/health` |
| ESXi template collector (Hugyn) | `collector-esxi` | - (writes `templates.yml`) | - |
| GNS3 template API (Munyn) | `template-gns3` | `template-gns3:8000` | `/health` |
| GNS3 template collector (Munyn; hosts from the app's profiles) | `collector-gns3` | - (writes `gns3_templates.yml`) | - |
| topology generator (Mimyr) | `generator` | `generator:8000` | `/health` |
| OVA server for the ESXi hosts (Caddy, read-only, `*.ova` only, no login) | `ova-store` | 8088 (published, `templates.ova_port`) | `/health` |
| setup wizard (see "Setup wizard" above) | `setup` | `setup:8000`, at `/setup/` with Basic Auth after configuration | `/health` |

`smoke/smoke-test.sh` runs the whole sequence - each service alone (degraded until its data is there), then the gateway
alone (502 for what is not running), the app with a host that does not answer, then everything together - and removes
everything afterwards (`KEEP=1` keeps it). It needs only Docker and the sibling repositories.

`tests/test_compose.py` enforces the separation (own profile per service, no `depends_on`, only the gateway and the OVA
server publish ports, read-only mounts for the template store and the host profiles).

<!-- BEGIN update configuration (the `update:` block, installer and image) -->
## Updating the components: the `update:` block

`ginnungagap update` (**Idynoun**, the updater) brings newer versions of the installed components (the appliance tooling, Igdrasyl,
Mjonyr, Hugyn-and-Munyn, Mimyr) onto a running appliance. The sources are the **public** GitHub releases of
`Ginnungagap-Appliance` (one release holds every component), or a folder with the files of one release. **No login and no token
are needed**; nothing is ever installed by itself (the optional daily timer only looks).

```yaml
update:
  source: github                 # github | directory
  repo: HTL3R-Xeonis/Ginnungagap-Appliance
  directory: ""                  # for source: directory: the absolute path of a folder with the files of ONE release
  channel: stable                # stable | prerelease (release candidates)
  token_file: /etc/ginnungagap/update-token   # OPTIONAL, see below; the file does not have to exist
  api: auto                      # auto | always | never: whether GitHub's rate-limited REST API is used, see "Updating components"
  check: off                     # off | daily  (the timer only looks for a new version, it never installs one)
  require_signature: auto        # auto | true | false
  keep_releases: 2               # unpacked versions kept per component; 2 = the current and the previous one (what a rollback needs)
  step_timeout_seconds: 90       # how long each restarted service gets to become healthy
```

Every key is optional; the block can be left out. `ginnungagap validate` checks it like the rest of the file. The setup wizard has no
fields for it and keeps a changed `update:` block when the form is submitted again.

* **Access token (optional).** Anonymous access is the default and enough for public repositories (the latest stable release is found
  without GitHub's API, whose limit is 60 anonymous requests per hour and address, see `update.api` below). A token is only for a
  private repository or for pre-release listings on a shared address that runs into that limit. It **never goes into `ginnungagap.yml`** (a `token:` key there is refused and the message says where
  it belongs): it lives in the file `update.token_file` names, readable by root only (mode 0600), and is handed over without
  showing it or putting it on a command line:

  ```bash
  read -rs -p 'GitHub token: ' T; printf '\n'; printf %s "$T" | sudo ginnungagap update --set-token; unset T
  ```

  No image carries a token: `image/prepare-image.sh` deletes the token file (also one that the configuration names), and the
  setup wizard never asks for one.
* **Signature.** `require_signature: auto` enforces the signature of `components.json` as soon as
  `/etc/ginnungagap/update-trust/allowed_signers` exists, which `install.sh` only creates when `release/allowed_signers`
  holds a key (until the first signing key exists, updates are checked by checksum only and the output says so). `true` always
  demands a valid signature, `false` never does.
* **Daily check.** `check: daily` makes `ginnungagap-update-check.timer` (always armed, once a day) run
  `ginnungagap update --check`, which only writes `/var/lib/ginnungagap/update/available.json`. With `check: off` the service is
  skipped, so the setting needs no `systemctl`. A look for updates that finds one is not an error (exit code 10).
* **State.** `install.sh` creates, outside the program folder and outside the checkouts (so reinstalling never touches them and
  `--uninstall` keeps them), under `--state-dir` (default `/var/lib/ginnungagap`): `update/` (what is installed, history,
  journal, audit log, the lock; mode 0700), `releases/` (the unpacked source of each installed version), `components/` (links the
  images are built from; this becomes `REPOS_DIR` on a new install that has no `--repos-dir`) and `backups/` (pre-update
  backups, 0700). `/etc/ginnungagap/install.env` names the folder as `GINNUNGAGAP_UPDATE_DIR`.
* **One update at a time.** The update and the setup wizard's apply (`apply-staged`) take the same lock
  (`update/lock`); a wizard submit during an update waits for it (up to the service's own 10 minutes) and is kept, not lost, if the
  update takes longer.
* **After a power loss.** `ginnungagap-update-resume.service` runs at boot when an update left its journal behind
  (`update/transaction.json`) and finishes or rolls back before anything else starts.
* **Installer options.** `--state-dir DIR`, and `--manifest FILE` (given by the update itself: the `components.json` of the release
  the program comes from is kept as `/opt/ginnungagap/app/components.json`, the version as `app/VERSION`).
* **Image.** `image/prepare-image.sh` keeps the installed release (`installed.json`, the unpacked releases and the links the images
  are built from), so a fresh appliance can check for and install updates at once; a machine that was installed by hand
  (`install.sh --repos-dir`) is recorded as the release it is first (`ginnungagap update --adopt`, which reads each component's version
  from the file its release workflow checks: Operator `app/version.py`, Builder `pyproject.toml`, Mimyr and Hugyn-and-Munyn `VERSION`, Appliance
  `ginnungagap/version.py`; an unreadable one stays `unknown` with the reason printed). It deletes everything that
  belongs to the build machine: the token, history, downloads, the journal, the audit log, pre-update backups (they hold the
  configuration with its API keys), rollback image tags and the unpacked versions that are not installed.
<!-- END update configuration -->

<!-- BEGIN updating components (usage of `ginnungagap update`) -->
## Updating components

```
ginnungagap update [--check | --apply | --rollback | --list | --versions | --history | --adopt | --set-token | --resume]
                   [--offline-dir DIR] [--version X.Y.Z] [--channel stable|prerelease] [--component NAME]...
                   [--yes] [--dry-run] [--no-backup] [--force] [--allow-downgrade] [--no-start]
                   [--wait SECONDS] [--restore-data] [--config FILE] [--json]
ginnungagap versions                      # the same as update --versions
```

| action | what it does |
|---|---|
| `--check` (default) | asks the source (GitHub Releases, no login needed, normally without the API; or `--offline-dir`), compares with what is installed, prints the table and writes `update/available.json`. Exit `10` = an update is available |
| `--apply` | downloads only the components whose version changed, verifies checksums (and the signature when a key is trusted), backs up the configuration and the app's data, unpacks, rebuilds only the services that use the changed components, restarts them one after the other (generator, collectors, template services, setup wizard, the app last) and waits for each to be healthy. Any failure rolls everything back by itself |
| `--rollback` | back to the previous release: links, the kept images (`rollback-<version>`), tooling, restarts; needs no internet. The data is restored from the pre-update backup only when that update was marked as migrating data (or with `--restore-data`) |
| `--list`, `--versions`, `--history` | the releases the source offers, the installed release with each component's version, every release installed so far |
| `--adopt` | records the components installed by hand (`install.sh --repos-dir /srv/repos`) as release `adopted` (copies, links, `install.env`); the first `--apply` does it itself. Nothing in the old folder is changed |
| `--set-token` | optional GitHub token from stdin into the token file (see the `update:` block); only needed for private repositories (the latest stable release is also found without the API, see `update.api`) |
| `--resume` | finishes or rolls back an update that was interrupted (the boot unit runs it): before anything that runs was touched it is discarded, afterwards it is rolled back. It first stops a `docker compose build` that the killed update left running (it would move the `latest` image tag again), and puts the old images back **by image id**. Also what to run after exit 3: it repeats the rollback until it works |

Exit codes: `0` ok / up to date, `10` update available (`--check`), `1` error before any change (no network, GitHub error or rate
limit - the message says when to try again, unknown release), `2` the update failed and was **rolled back**, `3` it failed and the
rollback failed too (the journal `update/transaction.json` and the printed steps say what is left), `4` refused by a
precondition (not root, another update or apply running, the appliance not healthy without `--force`, not enough disk, checksum or
signature wrong, an older release without `--allow-downgrade`, no confirmation).

Good to know:

* **"Healthy" means healthy.** A restarted service with a health check must report `healthy` (checked every 10 s), one without must stay up
  for 6 s, and a container that restarts by itself fails the step at once, not only the final 120 s check. After a rollback the
  Operator's reported version must be the old one again; if it is not the rollback is reported as failed (exit 3), not as done.
* **Rolling back the first update of a machine that was adopted** (the checkout in `/srv/repos` has an `install.sh` from before the
  updater): that script is not run (it does not know `--state-dir` and expects the old folder names); the previous program files are
  copied back. That program has no `update` command, so run the new release's `install.sh` again to get it back.
* **Anonymous by default.** No `Authorization` header is sent unless the token file exists; a token is only ever sent to the API host, never to the
  download address GitHub redirects to.
* **Without GitHub's API (`update.api`).** GitHub's REST API allows only 60 requests per hour and address without a token, and
  every appliance behind one NAT (and every GitHub runner) shares that. The latest **stable** release is therefore found without
  it: `https://github.com/<repo>/releases/latest` answers with a redirect to `.../releases/tag/<tag>`, which is read and **not
  followed**, and the files (`components.json`, `components.json.sig`, `SHA256SUMS`, the tarballs) are the plain download
  addresses `https://github.com/<repo>/releases/download/<tag>/<file>`; neither is an API call, so neither has that limit. The
  redirect is only accepted when it points at `https://github.com/<same owner>/<same repo>/releases/tag/<vX.Y.Z>` (a plain SemVer
  tag), so a hostile answer can not name other files; the downloads follow GitHub's redirect to the signed objects host without any
  credentials, and everything that was checked before is checked the same (size limits, `SHA256SUMS`, the signature, the
  checksums in `components.json`). `update.api` (or the environment variable `GINNUNGAGAP_UPDATE_API`, which wins) says who is asked:
  * `auto` (default): the web for the latest stable release when no token is stored; the **API** for a stored token (it is the only
    way into a private repository), for `--channel prerelease` and `--version` of a pre-release, for `--list` (which lists releases)
    and for a `--version` that is not the latest. It also works the other way round as a fallback: when the web side fails with
    anything but a clean 404 (a 404 means there is no stable release: "offers no release") the API is asked, and when the API
    answers with its rate limit while the latest stable release is wanted, the web is used instead. Only where nothing else can
    help (a listing, a pre-release) the message "GitHub's API rate limit is used up" names the time to try again and how to store a
    token.
  * `always`: the API only, as before (no fallback to the web).
  * `never`: no API call at all, a stored token is not used either: only the latest stable release can be checked and installed;
    a listing, a pre-release or an older `--version` is an error that says so.
* **Only what changed** is rebuilt and restarted; `--component NAME` (repeatable; the key such as `operator`, a code name such as `igdrasyl` or `hugyn-and-munyn`, or a key of before the code names) limits an update further and warns that this is not the combination that
  was tested together. `--no-start` unpacks, verifies and builds without restarting anything (for an appliance image). `--dry-run`
  shows the plan after checking the manifest and the signature and changes nothing.
* **Names before the code names.** Releases and machines from before the repositories got their code names keep working; the
  old names are only ever read, never written: (1) `--adopt` (and the first `--apply`) finds checkouts under the old folder names:
  old `TopologyOperator`, `Docker-Server-Files`, `topology-generator` or `TopologyGenerator` (the builder keeps its repository name, `TopologyBuilder`; a folder `Mjonyr` is read too); it records them under
  the current ones, also when only some of them still have the old name; the old folders stay as they were. (2) An update state that
  was written with the old component keys (`docker-server-files`, `topology-generator`) or old folder names under `releases/` and
  `components/` is renamed once, under the update lock, by the next `--apply`, `--rollback`, `--resume` or `--adopt`; the old name
  stays as a link to the new one so that the compose file of an older release still finds its sources. (3) A manifest of an
  older release (rollback, `--version X --allow-downgrade`) with the old keys and repository names is read as the roles they
  stand for. (4) A manifest is checked for consistency, not for a fixed repository name: the asset has to be
  `<repository>-<version>.tar.gz` of the repository the manifest names, so a later rename of a component repository does not
  lock an installed appliance out of updates. `install.sh` accepts a `--repos-dir` with the old folder names, tells you to run
  `ginnungagap update --adopt` and does not start anything until then.
* **Short downtime**: restarting the app drops the browser sessions for about 10-20 seconds; the admin panel's "Connection lost" overlay
  covers that gap and the page reloads by itself once the new version answers.
* **From the browser** (Igdrasyl's admin panel, section Updates; see "Updating from the web" below): the same check and the same apply,
  admin only, never automatic.
* **Building needs the internet** (pip, Docker Hub), as `apply` without `--no-build` always did; a folder source (`--offline-dir`) only
  removes the need for GitHub. A rollback needs no build while the rollback images are there.
* **Files**: `/var/lib/ginnungagap/update/{installed.json,history/,available.json,transaction.json,web-status.json,audit.log,lock}`; every apply, rollback,
  resume and token change is one JSON line in `audit.log` (who, from, to, result) and in the journal (`journalctl -t ginnungagap-update`).
* Tests: `tests/test_manifest.py`, `test_releases.py`, `test_release_web.py` (the web side: no API call, hostile redirects, the fallbacks), `test_update.py`, `test_update_failures.py` (the failure matrix) run against a
  fake appliance and a fake GitHub on localhost; `smoke/update-smoke.sh` does the real Docker part (build, recreate only what changed,
  automatic rollback, `--rollback`).
### Updating from the web

Igdrasyl's admin panel has a section **Updates**: installed and available version per component (with the code names), a
**Check for updates** button, and, once a check found a release, **Update now...** with a confirmation that names the release, the
services it restarts and the backup that is made first. Nothing in the browser installs anything by itself, and nothing is
automatic: a release is only installed after an admin clicked the confirmation, and the daily check timer (`update.check: daily`)
only looks.

The app's container has no Docker socket and no root; the update runs on the host, through the same hand-off as the setup wizard:

| | |
|---|---|
| what the app may read | the update state folder, **read-only** (`/var/lib/ginnungagap/update` mounted at `/update-state`): `installed.json`, `available.json`, `transaction.json` (the phase of a running update) and `web-status.json` (how the last request ended) - all written by the host |
| what the app may write | one request file `request.json` in `/var/lib/ginnungagap/update-requests` (mode 0700, mounted at `/update-requests`); nothing else is writable |
| who acts on it | `ginnungagap-update-request.path` (armed by `install.sh`, also on a running machine) starts `ginnungagap-update-request.service`, i.e. `ginnungagap apply-update-request`, as root |
| what a request may say | `check`, or `apply` of exactly the version the admin was shown with `confirmed: true`; the requester (`role: admin`) is recorded. The program reads the file without following links, caps its size, consumes it at once and refuses any other field - no source, folder, `--force`, `--no-backup`, downgrade or component selection can be asked for from the browser. It then runs `ginnungagap update --check`, or `--apply --yes --version X`, so the whole safety net applies: signature and checksums, backup, health gate, automatic rollback, the update lock |
| audit | the engine's own `audit.log` lines carry `web:<user name>`, the request adds `web-request` lines (accepted / refused / outcome), the Operator's audit log gets `update_check_requested`, `update_apply_requested` and `update_result`; the Operator's audit log now lives on the data volume (`/data/audit.log`) so the restart of an update does not erase it |
| result | `update/web-status.json`: state (`running`, `done`, `failed`, `refused`), outcome (`update_available`, `up_to_date`, `installed`, `rolled_back`, ...), the last lines of the output |

The panel keeps asking while the app restarts and shows the progress phase from `transaction.json`. Where the folders are not
mounted (an appliance whose app container was not recreated since the tooling update) the section says so; `ginnungagap apply` recreates
the app once and mounts them. Tests: `tests/test_update_request.py` (the request file, against the real engine with a fake Docker),
`tests/test_install.py`, `tests/test_compose.py`, `tests/test_render.py`.
<!-- END updating components -->

<!-- BEGIN releasing (for maintainers) -->
## Releasing (maintainers)

A release of the appliance is a GitHub release of **this** repository whose files are everything the appliance needs:
`components.json` (the manifest), `SHA256SUMS`, optionally `components.json.sig`, and one source tarball per component
(`Ginnungagap-Appliance-<v>.tar.gz`, `Igdrasyl-<v>.tar.gz`, `TopologyBuilder-<v>.tar.gz`, `Hugyn-and-Munyn-<v>.tar.gz`,
`Mimyr-<v>.tar.gz`, each with one top folder of its own name). The tooling lives in `release/`; **nothing in it
publishes or pushes on its own**.

1. Release the components that changed (their own repositories, tag `vX.Y.Z` = the version file inside: `app/version.py`,
   `pyproject.toml`, `VERSION`).
2. In this repository raise `ginnungagap/version.py` (a patch release for fixes, minor for a new component version or feature,
   major only for a breaking configuration or data change) and the tags in `release/components.in.yml` (the pin file; `data_migration:
   true` on a component whose new version changes persistent data in a way the old one can't read). A release candidate is
   `X.Y.Z-rc.N` in the version files and tags; it is only offered on `update.channel: prerelease`.
3. Look at what would be built, with nothing published (needs the checkouts or the components' releases):

   ```bash
   python3 release/build_release.py --pins release/components.in.yml --out /tmp/release --ref HEAD --fetch   # or --checkouts DIR / --sources DIR
   python3 release/build_release.py --verify /tmp/release                                                   # sizes, checksums, manifest
   ```

   The script refuses a tag that differs from the version file, a tarball with another top folder or a path leaving it, a
   missing component, and a non-empty `--out`. The same output is reproducible byte for byte (`--released` fixes the time).
4. Tag and push `vX.Y.Z` of this repository: `.github/workflows/release.yml` runs the tests and ruff, builds the files and creates the
   release (marked as pre-release when the tag contains a `-`). The workflow can also be started by hand with `dry_run` (the
   default), which keeps the files as a workflow artifact and creates nothing. `.github/workflows/ci.yml` runs the tests and
   ruff on every push.
5. **Signing (once).** `ssh-keygen -t ed25519 -N '' -C ginnungagap-release -f release_key`, put the line described in
   `release/allowed_signers` into that file and commit it, and store the private key as the repository secret
   `RELEASE_SIGNING_KEY` (the workflow signs with it; without the secret the release is unsigned). `COMPONENTS_READ_TOKEN`
   is an optional read-only token for fetching the other repositories' releases (anonymous access works while they are public: without
   the token `build_release.py --fetch` takes the tarballs and their `.sha256` from the plain download addresses
   `https://github.com/<repo>/releases/download/<tag>/<file>`, which are no API calls, so the workflow does not use up the API quota;
   with it the API is used, which also reads private repositories; `--api-mode always|never` or `GINNUNGAGAP_RELEASE_API` overrides).
   Locally: `--sign release_key`, and `--verify DIR --allowed-signers release/allowed_signers` to check.

The appliance image is built either from checkouts, as before (`image/README.md`), or from a release with `ginnungagap update --apply
--no-start`; the image name follows the appliance release version.
<!-- END releasing -->

## Not part of this solution

**Monitoring** (Prometheus, Grafana, exporters) is deliberately not part of the one-VM solution. It keeps running where it runs
today; `services.monitoring` in the config is refused with that explanation rather than silently ignored.

## Not available yet

The remaining items are the file browser, TLS from a file or ACME, and single sign-on.

## License

GNU General Public License v3.0 - see [LICENSE](LICENSE). Third-party components and their licenses are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
