# Automated appliance image builds

Operator guide for `ginnungagap-image-build`, installed from this repository by `image/install-build-host.sh`.
The design is recorded in the Handoff repository's `plans/PLAN_automated_image_builds.md`; this guide describes the integrated
commands and their current limits.

## Current lab status and boundaries

Mandatory implementation and local RC commissioning are complete. The dedicated Debian worker is
**183-Debian-Ginn-Imagebuild**, **10.20.20.183**, on ESXi **10.20.20.83**. Reserved **.184** serves build guests;
**.185** serves sequential acceptance guests on **10.20.20.205**. Normal guards remain
`allowed_hosts: [10.20.20.205]` and `allowed_running_vms: [GNS3VM5]`.

Run `1.3.7-rc.20261122-20261122T151422Z` is **published**, with no failure or cleanup warning. Catalog template
`ginnungagap-appliance-v1.3.7-rc.20261122` has ID/SHA-256
`7F18B41C11A50B9FD33CA235E4DC810524E6171C1CCF9F43D06EB4AFF4326400`; its unchanged OVA is 2,631,383,040 bytes.
All **17 offline checks** and **37 acceptance checks (A6/B23/C8)** passed, including four distinct configured reboots with
nine running/healthy containers and no ordering cycles, exact-version upgrade, retained application/login state and exact
captured-base rollback. Independent catalog/HEAD200/range206/BUILD-INFO checks passed. Owned temporary VMs, folders,
private run keys and the current private export were removed; only GNS3VM5 remains on `.205`.

The real P13 SIGKILL/janitor/resume drill passed with an unchanged retained OVA. The worker reboot also passed: static
`.183`, key login/sudo, persistent NFSv4 writable by imgbuild and successful startup janitor. Evidence is under the run's
`evidence/` directory (`p13-crash-drill.json`, `p13-janitor-result.json`, `post-publication-audit.json`,
`worker-reboot-audit.json`). Latest validation: **1459 passed, 8 skipped**, Ruff 98 files and three Bash scripts clean,
source `3212e2e7`.

Only **startup janitor is enabled**. Poll timer/path remain **disabled**, and the imgbuild-owned mode-0600 **PAUSE** file
records the production hold. Local stable version/pin **v1.3.7** is prepared, but tag v1.3.7 is absent and the official release
is still **v1.3.6**. The user must commit/push/tag the new code; independently verify and run the signed stable release
before enabling normal scheduling. The published local unsigned RC does not satisfy that separate production gate.
Optional WP9 remains optional and unimplemented.

The daemon uses pinned root-owned tooling and protected systemd credentials; downloaded release code runs in the
throw-away guest rather than updating its verifier. Resume records the real owner/PID and preserves the original run start.
A powered-on unrelated VM normally defers a build. The one-time manual RC `--force-busy` exception does not extend normal
allow lists or bypass signature, artifact, inspection, acceptance or no-overwrite checks. Unrelated VMs and older templates
remain outside cleanup scope. Detailed corrected failures and measurements stay in the Handoff archive.

## Install and configure the worker

Use a dedicated Debian 13 VM with Python 3.12+ and enough disk space for exports and offline inspection. Do not install on
the appliance/testbox: the installer refuses a host where `docker` is already available. It installs host dependencies,
the `imgbuild` account, a pinned Appliance/Builder snapshot and venv, the CLI, units and the narrowly scoped root inspection
helper. It **enables no service, path unit or timer**.

Bootstrap from independently verified Appliance and Mjonyr (TopologyBuilder) release trees. Set these paths to the exact
component trees verified against the release manifest; their component versions may differ:

```bash
APPLIANCE_TREE=/srv/verified-release/Ginnungagap-Appliance-1.3.0
BUILDER_TREE=/srv/verified-release/TopologyBuilder-1.3.0
sudo "$APPLIANCE_TREE/image/install-build-host.sh" --builder "$BUILDER_TREE"
sudoedit /etc/ginnungagap-image-build/build-host.yml
```

The installer fingerprints both trees and never overwrites a pinned snapshot in place. `--replace-bootstrap` is only an
explicit replacement of an existing development/bootstrap snapshot, not a way to replace signed installed tooling. Normal
tooling upgrades use signed `self-update` below. `--no-packages` skips dependency installation on a prepared host;
`--root DIR --no-packages --no-venv` creates a staged filesystem for inspection/tests, not a runnable worker installation.
Do not use `--no-venv` for a normal bootstrap that needs its runnable pinned environment.

Copy values from [build-host.example.yml](build-host.example.yml), then choose the actual `.205` datastore and management
port group: both are required and must already exist. Keep `acceptance.upgrade_test: true`, `acceptance.extra_hosts: []` and
`github.channel: stable`. Set the catalog API, HTTP OVA base URL and share path to the same store. The default endpoints are
`http://10.20.20.171:8000`, `http://10.20.20.181:80` and `/mnt/esxi_templates`; confirm them for the lab before running.
`image.ssh_user` can explicitly select the base-image login when its catalog metadata does not declare one; there is no
silent login-name default. `image.install_method` is `update` by default, with `checkouts` available as a verified-release
installation strategy.

Keep `/etc/ginnungagap-image-build/build-host.yml` root-owned, group `imgbuild`, mode 0640. It holds paths, never secret
values. Install the trusted release signers in `/etc/ginnungagap-image-build/allowed_signers`; do not trust a signing key
downloaded beside the release it is supposed to verify.
Keep `paths.state_dir` at `/var/lib/ginnungagap-image-build`: the privileged offline-inspection helper deliberately accepts
only that fixed runs root, and installed units grant write access to the default paths.

### Credentials

Source credentials stay **root-owned, mode 0600**, inside the root-only mode-0700 `secrets` directory. Enter the ESXi
password interactively; do not put it in shell arguments, history or YAML:

```bash
sudo bash -c 'umask 077; read -r -s -p "ESXi .205 password: " secret; printf "\n" >&2; printf "%s\n" "$secret" > /etc/ginnungagap-image-build/secrets/esxi-205.password; unset secret'
sudo chmod 600 /etc/ginnungagap-image-build/secrets/esxi-205.password
```

The installed build and janitor units use
`LoadCredential=esxi205:/etc/ginnungagap-image-build/secrets/esxi-205.password`. systemd passes a private readable credential
copy to `imgbuild`; that user should not be given access to the source directory. A direct `sudo -u imgbuild ... run`
cannot read the root-only source password and is not the manual invocation shown below.

For private GitHub repositories, optionally create `secrets/github-token` the same way and set `github.token_file` to its
path. For `share.mode: ssh`, provide `share.ssh_host`, `share.ssh_user`, `share.ssh_key_file` and a root-only
`secrets/share-key`. The installed unit drop-ins load these as `github-token` and `sharekey` when they exist at installation;
if added later, add matching `LoadCredential=` entries to the build/poll/janitor unit drop-ins and run
`sudo systemctl daemon-reload`. The SSH account needs access to the configured store path; establish and verify host trust
before scheduling. NFS is the normal lab configuration.

### Mount the template share

Allow `10.20.20.183` read-write access on the NFS server and map ownership so `imgbuild` can create/rename files in the export.
Use the confirmed server/export rather than assuming that the storage dataset name is its NFS path:

```bash
NFS_SOURCE='<nfs-server>:/<export>'  # replace with the verified export
sudo mount -t nfs -o rw,_netdev "$NFS_SOURCE" /mnt/esxi_templates
findmnt --target /mnt/esxi_templates
sudo -u imgbuild test -w /mnt/esxi_templates
```

Make the verified mount persistent in `/etc/fstab` and test it before enabling scheduling. The runtime checks that an NFS
share path is really a mount; an absent mount defers the build instead of writing into the worker's local mount directory.
The HTTP OVA server must serve that same store, including byte ranges and `Content-Length`, and `.205` must be able to pull
from it. Do not put a login in front of the OVA endpoint. The preflight requires at least 25 GiB free on the worker,
50 GiB on the ESXi datastore and the greater of 10 GiB or three times the largest cataloged appliance OVA on the share;
offline raw-disk conversion also checks its actual free-space requirement.

### Reserved management addresses

The lab reserves `10.20.20.184` for temporary build guests and `10.20.20.185` for sequential acceptance guests. The worker remains `.183`. Enable `image.management_network` using the commented example in `build-host.example.yml` when DHCP is unavailable. Both guest addresses and the gateway must be distinct usable addresses in one subnet.

For older bases, the worker bootstraps through the hardware NIC's IPv6 link-local address and its injected SSH key, then verifies SSH and the assigned static IPv4. The worker must share the management layer-2 network; its local interface is selected from the configured IPv4 subnet. New images accept the optional JSON key `guestinfo.ginnungagap.network` during firstboot, including an otherwise unconfigured acceptance VM. No network override is applied when the key/configuration is absent.

The real DHCP failure also exposed an unsafe primary-address assumption: Tools reported Docker bridge `172.18.0.1`
while the hardware NIC had only link-local IPv6. IPv4 and link-local discovery now require a MAC match to an actual
virtual Ethernet card. This bootstrap supports one unambiguous hardware NIC on the same layer-2 management network
as the worker; bridge, unmatched, missing and ambiguous addresses are not accepted as a primary-address fallback.

Only a dedicated root-owned NetworkManager/networkd profile is created. Its exact contents are recorded privately under `/run`; preparation removes that matching profile without reconfiguring the live connection, before zeroing free space. The exported image therefore returns to its original DHCP configuration. Changed or unexpected profiles are refused. Automatic builds use the same reserved addresses and still serialize through the build lock.

The **1,220-passed / 8-skipped** full-suite checkpoint predates this static-network extension. Provisional post-change
evidence consists of **236 focused runtime tests** and **82 separate guest-network tests**; these are different subsets,
not a new combined full-suite total or proof of a complete live static-address build. The updated acceptance requirement
is **four configured reboot checks**, each with exactly **nine expected containers** and **zero systemd ordering cycles**.
That expanded acceptance has not yet been demonstrated by a complete real run.

### Machine identities and public test vectors

Preparation stops and temporarily masks fwupd activation, then removes only its generated `secret.key` and `client.pem`. Runtime masks disappear on the next boot; each deployed copy can create its own identity. The temporary guest network profile is removed before zeroing, while its current kernel address remains available until shutdown.

The pinned [public-test-keys.json](public-test-keys.json) policy identifies ten independently verified public self-test/example keys. Raw checks accept only their complete PEM fingerprints; filesystem checks additionally require the exact vendor path and full-file hash, including an equivalent single Docker-layer `diff` path. Unknown, truncated, modified or misplaced key material fails inspection. Machine identity paths are not made acceptable by copying a public fixture there.

If a vendor package changes, verify its authenticated Debian package and public upstream source independently before updating this policy through trusted tooling. Do not derive new allowances solely from the candidate image. Diagnostic reports contain file paths, file hashes and byte offsets, never PEM contents. Swap sanitization is not assumed from zeroing free root filesystem space; a remaining raw-only finding must be diagnosed before publication.

## Manual commands and first staged run

On the worker, define this helper in the current shell. It runs each command as `imgbuild` with the same ESXi credential
delivery as the installed service; it starts no automatic scheduling:

```bash
image_build() {
  sudo systemd-run --wait --pipe --collect \
    -p User=imgbuild -p Group=imgbuild \
    -p LoadCredential=esxi205:/etc/ginnungagap-image-build/secrets/esxi-205.password \
    /usr/local/bin/ginnungagap-image-build \
    --config /etc/ginnungagap-image-build/build-host.yml "$@"
}
image_build --help
image_build run --help
image_build status --json
```

If configured, also add `-p LoadCredential=github-token:/etc/ginnungagap-image-build/secrets/github-token` and
`-p LoadCredential=sharekey:/etc/ginnungagap-image-build/secrets/share-key` to this helper. The global `--config FILE` option
must come **before** the subcommand. `--pipe --wait` is convenient for a supervised run; use the installed worker service
for regular long-running builds and follow its journal over a separate SSH connection.

Use an actually available, signed stable version for the normal path:

```bash
image_build run --release v1.3.0 --dry-run
image_build run --release v1.3.0 --stop-after P8
image_build run --release v1.3.0
```

The dry run performs P0-P3 read-only external checks and verifies downloaded release assets, then describes P4-P16. It
creates no remote VM or share entry, but keeps local run evidence. `--stop-after P8` is a real build through preparation;
its normal cleanup removes temporary VMs. A later full run starts a fresh base. A successful dry run alone does not prove
the guest installation, disk inspection or acceptance will pass.

For the explicitly authorized initial busy-lab exception, add `--force-busy` to the one manual run, not the unit's
`ExecStart` or YAML allow list. A manually supplied RC/unsigned bundle requires both exceptions as applicable:

```bash
image_build run --release v1.3.0-rc.1 --release-dir /srv/release-rc \
  --allow-prerelease --allow-unsigned --stop-after P8
```

The local release directory must be readable by `imgbuild`, with the manifest, `SHA256SUMS` and every component archive.
Checksums, manifest versions and archive safety are still checked. An invalid signature never becomes valid through an
unsigned exception. These manual development runs are marked in the summary; they do not verify or publish an official
stable release and are never selected automatically.

| Run option | Effect |
|---|---|
| `--release vX.Y.Z` | Required exact release; an existing published template is not rebuilt or overwritten. |
| `--dry-run` | Read-only external preflight/release verification, later phases described. |
| `--stop-after P0` ... `P16` | Stop a real run after the named phase, then clean up; use `P8` for the first staged check. |
| `--keep` | Keep temporary VMs for diagnosis, normally protected from janitor for 24 hours; not a publication bypass. |
| `--base TEMPLATE` | Select an exact published appliance template instead of the newest older stable base. |
| `--release-dir DIR` | Use local release assets rather than fetching GitHub assets. |
| `--allow-prerelease`, `--allow-unsigned` | Explicit manual exceptions only; recorded in the result. |
| `--force-busy` | Ignore unrelated powered-on VMs for this manual run; other guards remain. |
| `--resume RUN_ID` | Continue at P12 when P0-P11 succeeded, or rerun P11 when P0-P10 succeeded and the recorded owned export still matches its validated size/SHA-256. No inspection gate is skipped. |
| `--refresh-catalog` | Explicit manual `--resume` review of unrelated catalog drift; preserves original P1 evidence and all base/target/no-overwrite guards. Never supplied by automatic stable jobs. |
| `--retry` | Accepted on a manual exact-release run; currently adds no behavior beyond that explicit retry. Poll suppression remains unchanged. |
| `--clean-base` | Currently blocks: clean Debian-base creation is not implemented. |
| `--json` | Print the redacted result as JSON. |

## Observe, pause and recover

```bash
image_build status --json
image_build list --json
image_build logs 1.3.0-20261120T120000Z
image_build show 1.3.0
sudo journalctl -u ginnungagap-image-build.service -f
sudo journalctl -u ginnungagap-image-build-poll.service -n 100 --no-pager

sudo -u imgbuild touch /var/lib/ginnungagap-image-build/PAUSE
image_build status
# Remove PAUSE only when builds may start again; it does not cancel a running build.
sudo -u imgbuild rm -- /var/lib/ginnungagap-image-build/PAUSE

image_build janitor
image_build run --release v1.3.0 --resume 1.3.0-20261120T120000Z
```

Use the run ID printed by your run, not the example timestamp. `status`, `list`, `logs` and `show` read saved state; an HTTP
status page exists only when you switch it on (see "Optional status page"). `status` adds `current` for a running
job alongside `latest` for the previous result; an older `latest` alone does not describe current progress. `logs` prints the saved run log;
use `journalctl -f` to follow a service live. Per-phase logs, diagnostics, `inspection.json` and `acceptance.json` live below
`/var/lib/ginnungagap-image-build/runs/<run-id>/`. Final summaries are in `builds/<version>/summary.json` and `latest.json`;
a successful published store folder also carries `BUILD-INFO.json`. `notify.command`, when set to an absolute script path,
receives the local summary path as its argument.

The pipeline retries transient errors once by default, from P4 with a fresh base VM; a transient error before P4 repeats
that phase. A permanent/blocked failure is not retried. Before cleanup it attempts diagnostics. The janitor runs before a
real build and can run explicitly after a crash; the lock prevents overlapping builds. Only owned `imgbuild-*`/
`imgaccept-*` VMs/datastore folders and recorded private resources are eligible. A staged share folder needs its run marker;
published templates are never removed by failure recovery. Kept runs expire after the configured keep period.

One tracked, hash-validated failed OVA is retained for diagnosis/resume; a partial or changed OVA cannot evict it. An
explicit resume prefers that run's valid artifact, even if older. A failed/pending/interrupted P11 can be rerun only after
P0-P10 succeeded and the owned export still matches the recorded P10 path, size and SHA-256. This reruns offline inspection;
secret residue, an invalid image or missing public-key evidence still fails. Earlier failed phases require a new run.
When P11 already passed, resume starts at P12 as before. Resume rechecks the artifact before staging and removes
the private export after publication. A fresh attempt removes its prior own export before rebuilding. A failure before
P11 completes requires a new run rather than resuming a half-installed VM. For a controlled service stop, use
`sudo systemctl stop ginnungagap-image-build.service`; SIGTERM runs cleanup. A forced kill needs a later janitor run.

### Review unrelated catalog drift during a manual resume

Long builds can overlap a legitimate change to another template. The default resume still refuses a changed catalog. Compare the current names/IDs with the run's original P1 catalog before choosing the explicit recovery option:

```bash
image_build run --release vX.Y.Z --resume "$RUN_ID" --refresh-catalog
```

Retain the original run's required release-directory/prerelease/unsigned options where applicable. This option requires `--resume`; automatic stable polling never passes it. It does not refresh a changed build input: the selected base name, ID, OVA filename and source directory must still match the original P3/P1 evidence. The target must remain absent, except for this run's own previously published exact artifact with matching run marker, YAML, filename, folder, size and SHA-256. A conflicting target is refused.

Original P1 evidence is retained. P12 records the before/current catalog plus added, removed and changed IDs in `catalog_refresh`; review this audit with the recovery result. Publication still refuses a catalog change during staging or acceptance. The option neither changes another template nor skips artifact, inspection or acceptance checks.

Public share/staging directories are explicitly **0755**, and public OVA/YAML files **0644**, even when the service uses UMask 0077. This allows the OVA HTTP server to read and range-serve the staged artifact before catalog publication. Private state, credentials and key evidence remain 0700/0600 as appropriate; they are not made public with the staging directory.

## Optional status page

A small read-only HTTP page for a later "Images" row in the Igdrasyl admin. It is **off by default** (`status_http: false`),
uses only the Python standard library and needs no extra package. Configuration (`build-host.yml`):

| Key | Default | Meaning |
|---|---|---|
| `status_http` | `false` | `true` allows `serve-status` to start; with `false` it refuses with exit code 2. |
| `status_bind` | `0.0.0.0` | IP address to listen on (an IPv6 address is accepted too). Prefer the management address. |
| `status_port` | `8183` | TCP port, 1024-65535. |

It serves exactly two paths, `GET`/`HEAD` only (every other method answers 405 with `Allow: GET, HEAD`, every other path 404;
there is no directory listing and the request path is never used as a file name): `/healthz` returns `{"ok": true}`, and
`/status.json` returns the document below. Answers carry `Cache-Control: no-store` and `Content-Type: application/json; charset=utf-8`,
and no CORS header (the consumer is a server, not a browser). **There is no authentication and no TLS: use it on the LAN only**
and do not forward the port. It never returns secrets, credential or key paths, ESXi addresses, run keys, log text or tracebacks:
the document is built only from the fields below, every text is cut to 200 characters, stripped of control characters, and the
`reason` is additionally scrubbed of URLs, IPv4 addresses, absolute paths and `password=...`-like pairs. An unknown or missing source
field becomes `null`; a missing or corrupt state file never breaks the page.

```json
{"schema": 1, "generated_at": "2026-11-24T10:15:00Z", "worker": "imgbuild",
 "state": "building",
 "paused": false,
 "active": {"run_id": "1.3.7-20261124T100000Z", "release": "1.3.7", "phase": "P13", "started_at": "2026-11-24T10:00:00Z"},
 "latest": {"version": "1.3.6", "result": "published", "started_at": "2026-11-20T09:00:00Z", "finished_at": "2026-11-20T10:30:00Z",
            "template": "ginnungagap-appliance-v1.3.6", "reason": null, "acceptance": {"passed": 42, "total": 42}},
 "builds": ["... up to 20 BUILD objects, newest first, the same shape as latest ..."]}
```

* `state`: `building` when a run with `state.json` status `running` exists (the rule of `status`), else `paused` when the `PAUSE`
  file exists, else `idle`. `paused` is true whenever `PAUSE` exists, also while a build runs. `active` is that run (`phase` is the
  running phase `P0`..`P16`, `null` between two phases), otherwise `null`.
* `latest` comes from `latest.json`, `builds` from `builds/<version>/summary.json` (at most 20). `result` is one of `published`,
  `failed`, `blocked`, `deferred`, `interrupted`, `stopped`, `dry-run`; an unknown value is shown as `failed`, never as a success.
  `template` is only set for a published image. `reason` is only set for another result. `acceptance` counts the checks of
  `acceptance.checks` (`passed` of `total`) and is `null` when the run recorded none.

The installer copies `ginnungagap-image-build-status.service` but never enables it. To try it:

```bash
# build-host.yml: status_http: true  (optionally status_bind / status_port), then
sudo systemctl enable --now ginnungagap-image-build-status.service
curl -s http://127.0.0.1:8183/healthz
curl -s http://127.0.0.1:8183/status.json | python3 -m json.tool
curl -si -X POST http://127.0.0.1:8183/status.json | head -n 3   # 405, Allow: GET, HEAD
# without the unit, in the foreground until Ctrl-C / SIGTERM:
image_build serve-status
```

With `status_http: false` the service stops at once with exit code 2 and is not restarted. Disable it again with
`sudo systemctl disable --now ginnungagap-image-build-status.service`.

## Enable scheduling only after the checks

The lab's local RC commissioning is complete, but normal activation remains on hold. Startup janitor is already enabled;
poll timer/path are disabled and PAUSE records why. The user must first commit/push/tag the prepared v1.3.7 implementation
and the resulting signed stable release must be independently verified. The current official v1.3.6 lacks these changes.
Then review the hold and permit a normal signed-stable manual run without unsigned/prerelease exceptions:

```bash
sudo -u imgbuild cat /var/lib/ginnungagap-image-build/PAUSE
# Only after the new signed stable release is verified and a stable test may start:
sudo -u imgbuild rm -- /var/lib/ginnungagap-image-build/PAUSE
image_build run --release v1.3.7
```

Keep poll/path disabled during that production check. If it fails, restore the PAUSE hold and diagnose; a published local
unsigned RC alone is not a signed-stable success. After the signed run passes all inspection, acceptance, publication and
cleanup checks, enable the normal consumer path and timer (the startup janitor command is idempotent):

```bash
sudo systemctl enable --now ginnungagap-image-build.path ginnungagap-image-build-janitor.service
sudo systemctl enable --now ginnungagap-image-build-poll.timer
sudo systemctl status ginnungagap-image-build.path ginnungagap-image-build-poll.timer --no-pager
sudo systemctl list-timers ginnungagap-image-build-poll.timer --all
# One poll now; the path unit consumes any queued release.
sudo systemctl start ginnungagap-image-build-poll.service
# Equivalent manual poll through the credential helper:
image_build poll
```

`poll` selects the newest unbuilt stable release and writes one queue item; it does not build or connect to ESXi itself.
The path unit invokes the internal `work` subcommand, which consumes the queue and performs full signature verification.
Do not invoke `work` as the normal manual interface; use `run --release`. The timer defaults to 15 minutes; the installer
renders its interval from `poll_minutes` (editing that YAML alone does not rewrite the existing timer drop-in).
Automatic selection skips blocked releases and releases at the configured failed-attempt limit per 24 hours. A busy-host
deferral clears its queue item so a later poll can reconsider it; it does not spin the service in a restart loop.

The optional status page is separate and independent of scheduling: set `status_http: true`, then
`sudo systemctl enable --now ginnungagap-image-build-status.service` (see "Optional status page").

To stop scheduling, create `PAUSE` and disable the triggers; a current build continues unless explicitly stopped:

```bash
sudo -u imgbuild touch /var/lib/ginnungagap-image-build/PAUSE
sudo systemctl disable --now ginnungagap-image-build-poll.timer ginnungagap-image-build.path
```

## Update the pinned tooling separately

Only root may run `self-update`. The **old installed verifier** checks the stable release signature, manifest and all
component archives against the locally trusted signers before switching the Appliance tooling and pinned Builder venv
together. It takes the build lock; it never runs automatically during a build and supports no unsigned/prerelease bypass.

```bash
sudo /usr/local/bin/ginnungagap-image-build \
  --config /etc/ginnungagap-image-build/build-host.yml self-update --release v1.3.0
# Or use a complete local signed bundle:
sudo /usr/local/bin/ginnungagap-image-build \
  --config /etc/ginnungagap-image-build/build-host.yml self-update --release v1.3.0 --release-dir /srv/release-verified
```

The two commands are alternatives. An existing tooling version is refused rather than overwritten. A private GitHub
source needs its configured root-readable token file. Review changed units/installer requirements separately: `self-update`
switches the tooling snapshot and venv, not installed systemd units or host configuration.

Offline inspection preserves only the exact current update-history record needed to seed the next rollback; unrelated,
substituted or additional runtime history is rejected. Docker digest aliases are accepted only when they resolve to the
same image ID as an allowed named image. Raw-disk token and private-key detection stays strict, including machine-specific
TLS private keys; preparation must remove those identities instead of weakening the scan.

## Failure table

| Finding | Behavior and next action |
|---|---|
| PAUSE, held lock, unrelated powered-on VM, missing NFS mount or insufficient space | Deferred (exit 3); fix the condition and poll/run again. Keep the normal `.205` allowance at `GNS3VM5`. |
| Wrong ESXi password, missing datastore/port group, host outside allow list | No unsafe fallback; check the credential source and exact config/inventory. |
| Missing/untrusted signature, bad manifest/SHA/version or unsafe archive | Blocked/permanent; obtain a correctly signed, consistent bundle. Do not weaken trusted signers. |
| Existing template/folder or incompatible base | No overwrite; inspect catalog/share state and choose a new release or supported published base. |
| SSH/bootstrap or transient import/network failure | Diagnostics and one fresh-base retry; check guest Tools/IP, SSH trust, OVA HTTP reachability and phase logs. |
| Preparation loses SSH before its verified completion marker | Failure, not assumed success; inspect prepare diagnostics. A retained multiplexed SSH session allows checking completion after key removal. |
| P11 root helper unavailable, no supported ext4 root, unknown Docker image-store layout, extra/rollback images, secret residue | Inspection fails closed; examine `inspection.json` and fix tooling/image preparation. LVM/encrypted/unsupported filesystems or a missing classic `repositories.json` index are not silently accepted. |
| `acceptance.extra_hosts` nonempty | Blocked: separate ESXi adapters for extra-host acceptance are not implemented. Keep `[]`; ESXi 8 acceptance is unverified. |
| Fresh boot, config/login guard or upgrade acceptance fails | No template YAML publication; inspect `acceptance.json` and diagnostics, then resume at P12 when P0-P11 succeeded. A P11 failure can rerun inspection only with successful P0-P10 and its unchanged recorded validated export. |
| Crash during export or a changed retained OVA | Unverified export removed; earlier verified evidence retained. Start a new build if P11 did not finish. |
| Crash after P14/catalog publication | Published store remains; janitor removes only private leftovers. A later reporting/cleanup error cannot unpublish the image. |
| `--clean-base` | Explicit block; use an existing supported appliance base. |

Run outcomes are `published`, `dry-run`, `stopped` (exit 0), `failed` (1), `blocked` (2), `deferred` (3) and `interrupted` (4).
CLI/config errors before the pipeline starts exit 2. Check the result and evidence, not only systemd's unit color.

## Implementation contracts

The modules import each other relatively: use `from image.build import pipeline` (tests) or `from . import pipeline` (inside the package).

| module | owns |
|---|---|
| `ports.py` | the Protocols `EsxiPort`, `RemotePort`, `ShareApiPort`, `TemplateApiPort`, `Clock`; the data they return (`VmInfo`, `CommandResult`, `RemoteTarget`, `ExportResult`, `HeadResult`, `TemplateEntry`); every error class; the read-only/mutating classification of each method |
| `names.py` | template/folder/stem, run id, `imgbuild-`/`imgaccept-` VM names, the annotation, `is_ours_*`, never-overwrite (`check_free`), `ova_url` |
| `config.py` | `build-host.yml` -> `BuildHostConfig` (`parse_config`, `load_config`), `read_secret` / `check_secret_file` |
| `pipeline.py` | the phase registry, `Pipeline.run`, `PhaseContext`, state.json, retry, cleanup, `Janitor`, `BuildLock`, dry run, stop-after, resume, `attempts_in_window` |
| `release.py` | P0-P3 selection, installed-tooling release verification, preflight and published-base selection |
| `esxi.py`, `remote.py`, `phases_install.py` | real ESXi/SSH adapters, guest bootstrap, upload, install/prepare and powered-off export |
| `inspect_image.py`, `inspect-disk.sh`, `acceptance.py` | P11 offline inspection and P13 throw-away guest acceptance |
| `publish.py`, `status.py` | staged OVA/metadata, atomic final YAML, catalog observation and redacted summaries |
| `tests/helpers/fake_esxi.py` | `FakeEsxi` (an `EsxiPort`), `FakeClock` |

## Register a phase

```python
from . import pipeline, names
from .ports import TransientError, PermanentError


@pipeline.phase(
    "P4",
    describe=lambda ctx: f"import the base OVA as {names.build_vm_name(ctx.run_id)}",
)
def deploy_base(ctx: pipeline.PhaseContext) -> dict | None:
    vm = names.build_vm_name(ctx.run_id)
    ctx.track_vm(vm)  # FIRST: cleanup by name, then create
    ctx.esxi.import_ova(
        ctx.results["P3"]["url"],
        vm,
        ctx.config.esxi.datastore,
        [ctx.config.esxi.network],
    )
    ctx.esxi.set_annotation(vm, names.annotation(ctx.run_id))
    return {"vm": vm}  # JSON facts -> state.json, later ctx.results["P4"]
```

* Ids are exactly `P0`..`P16` (`pipeline.PHASES`). `@pipeline.phase` registers in `pipeline.DEFAULT_REGISTRY`; a test (or the CLI)
  may build its own `PhaseRegistry` and give it to `Pipeline(registry=...)`. P15 (cleanup) and P16 (summary) have built-in defaults.
* `describe(ctx) -> str` is what a dry run prints for the phase (the command it would run); P0-P3 really run in a dry run, with
  READ-ONLY views of the ports (a mutating call raises `DryRunViolation`, `ctx.track_*` too).
* Return a mapping of JSON-able facts (paths and times are stored as text; resume restores them as text) or `None`. Secrets never.
* Raise by meaning: `TransientError` (retry once from P4), `PermanentError` (never), `Blocked` (loud, no retry), `Deferred`
  (not now, nothing created). Wrap library errors yourself; any other exception counts as a bug (permanent, traceback logged);
  only `TimeoutError`/`ConnectionError` are treated as transient as a last resort.
* Log only through `ctx.logger` (it redacts the ESXi password, registered secrets via `ctx.secret(v)`, token shapes and private
  keys); write only below `ctx.run_dir` (`ctx.subdir("release")`).
* Track every created thing BEFORE the call that creates it: `ctx.track_vm`, `track_datastore_folder`, `track_share_folder`
  (removed only when the run does NOT publish; the marker `.imgbuild-run` = run id proves it is ours), `track_path` (below the
  state dir only), `on_cleanup(kind, ref, fn)`. Entries are written to state.json, so the janitor finishes them after a crash.
  `ctx.add_diagnostic(label, fn(dir))` saves evidence into `runs/<id>/diag/` before a failed attempt is cleaned.
* A phase must be repeatable from its start after the cleanup of the failed attempt (a transient failure restarts at P4 with a
  new base VM; before P4 only the failed phase repeats). A failure after P14 is only a warning (the image stays published).
* `ctx.note(key, value)` adds a fact to the summary; `ctx.options` carries every CLI flag; `ctx.names`, `ctx.config`, `ctx.clock`
  (use `ctx.clock.sleep`, never `time.sleep`).

## The ports (method list)

* `EsxiPort`: `host`, `check_login`, `list_vms`, `get_vm`, `list_datastores`, `list_networks`, `datastore_free_bytes`,
  `list_datastore_folders`, `get_extra_config`, `wait_powered_off`, `wait_for_ip` (reads); `import_ova`, `set_annotation`,
  `set_extra_config`, `clear_guestinfo`, `power_on`, `power_off`, `shutdown_guest`, `screenshot`, `delete_vm`,
  `delete_datastore_folder`, `export_ova`, `close`.
* `RemotePort`: `generate_key`, `wait_ssh`, `run`, `run_detached`, `put`, `get`, `forget`.
* `ShareApiPort`: `free_bytes`, `list_folders`, `folder_files`, `read_text`, `size`, `sha256`, `url_for`, `head` (reads);
  `make_folder`, `put_file`, `put_text`, `remove_folder(marker_name, marker_text)`. (The share AND the `.181` web server.)
* `TemplateApiPort`: `check`, `list_templates` (reads); `refresh`. `Clock`: `now`, `monotonic`, `sleep` (`SystemClock`).

Everything is addressed by name, idempotent where cheap (`delete_vm`, `delete_datastore_folder`, `power_off`, `forget`), and
secret-free in arguments: the ESXi password lives in the adapter (it hands it to children only as `ESXI_PASSWORD`), guestinfo values
and command output are never logged, `RemotePort.run` argv is logged so secrets go through `stdin`/`env`. A new port method must be
added to exactly one of the `*_READ_ONLY` / `*_MUTATING` sets of `ports.py` (a test enforces it) and to the fakes.

## Write a fake

Implement the Protocol with plain Python in `tests/helpers/fake_<name>.py` and follow `fake_esxi.py`: in-memory state, a call
log (`Call(name, args, outcome)` that never holds a secret value), `inject(method, exc, skip=, times=, match=, side_effect=)` for
transient/permanent faults, helpers to seed the world (`add_vm`, `add_folder`), and `FakeClock` for waits (advance it instead of
sleeping). Keep the method names and parameter names identical to the Protocol (`test_fake_esxi_has_every_method_of_the_port...`
shows the check). For a quick stand-in `unittest.mock.create_autospec(RemotePort, instance=True)` works too.

## State on disk (`config.paths.state_dir`, default `/var/lib/ginnungagap-image-build`)

`lock` (flock), `PAUSE` (a file defers every real run), `runs/<run id>/{state.json, summary.json, run.log, NN-name.log, diag/, ...}`.
`state.json` schema 1 is documented in the `pipeline.py` docstring. Results: `published 0 | dry-run 0 | stopped 0 | failed 1 |
blocked 2 | deferred 3 | interrupted 4` (`RunOutcome.exit_code`). A deferral before a run directory exists (lock held, PAUSE, lab busy,
host not ready) creates no run directory; P16 runs once for every outcome that has one.

## Decisions to know

* A transient failure restarts at **P4**, not P5: P4 makes the base VM, and the retry needs a new one (the plan's "from P5" meant "after the checks").
* Lab busy = a powered-on VM that is neither on `esxi.allowed_running_vms` nor one of ours; replaceable through `PipelineHooks.lab_busy(esxi, config)`.
* The janitor runs at the start of every real run and in `Pipeline.run_janitor()` (both hold the lock). It touches only `imgbuild-`/`imgaccept-`
  VMs and datastore folders, and what a crashed run recorded in its own state.json (a share folder only with its marker, paths only below `runs/`).
* The config adds a few keys to plan section 10 that the phases need: `paths.state_dir`, `paths.allowed_signers`, `share.ssh_*`,
  `image.os_type`, `status_http`, `status_bind`, `status_port`, `limits.*`.
