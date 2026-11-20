#!/usr/bin/env bash
# Installs the Ginnungagap appliance on this machine: the program under PREFIX, the `ginnungagap` command, a configuration
# folder (your ginnungagap.yml stays where it is on a re-run) and a systemd unit that applies the configuration at boot.
# The services themselves run as containers, so Docker with compose has to be installed already, and so do the sibling
# repositories the images are built from (Igdrasyl, TopologyBuilder, Hugyn-and-Munyn, Mimyr).
#
#   sudo ./install.sh --repos-dir /srv/repos           # install (or update) and start
#   sudo ./install.sh --uninstall                      # remove program, command and units; config and data are kept
#
# Options: --prefix DIR (/opt/ginnungagap)  --config-dir DIR (/etc/ginnungagap)  --bin-dir DIR (/usr/local/bin)
#          --unit-dir DIR (/etc/systemd/system)  --repos-dir DIR  --staging-dir DIR (/var/lib/ginnungagap/setup)
#          --sshd-dropin-dir DIR (/etc/ssh/sshd_config.d)
#          --state-dir DIR (/var/lib/ginnungagap)  where `ginnungagap update` keeps its state, the unpacked releases and backups
#          --manifest FILE      the components.json of the release this program comes from (`ginnungagap update` passes it)
#          --no-systemd  --no-start
#          --install-packages   first install what is needed with apt (Debian 13 / Ubuntu: Docker, compose, python3-yaml, ...)
#          --image              for building an appliance image: also enable the first-boot unit and switch SSH password login
#                               on (the admin's random initial password is shown on the console; see image/prepare-image.sh)
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
PREFIX=/opt/ginnungagap
CONFIG_DIR=/etc/ginnungagap
BIN_DIR=/usr/local/bin
UNIT_DIR=/etc/systemd/system
STATE_DIR=/var/lib/ginnungagap
STAGING_DIR=
MANIFEST=
SSHD_DROPIN_DIR=/etc/ssh/sshd_config.d
REPOS_DIR=
SYSTEMD=1
START=1
UNINSTALL=0
PACKAGES=0
IMAGE=0

die() { echo "install.sh: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX=${2:?}; shift 2 ;;
    --config-dir) CONFIG_DIR=${2:?}; shift 2 ;;
    --bin-dir) BIN_DIR=${2:?}; shift 2 ;;
    --unit-dir) UNIT_DIR=${2:?}; shift 2 ;;
    --staging-dir) STAGING_DIR=${2:?}; shift 2 ;;
    --state-dir) STATE_DIR=${2:?}; shift 2 ;;
    --manifest) MANIFEST=${2:?}; shift 2 ;;
    --sshd-dropin-dir) SSHD_DROPIN_DIR=${2:?}; shift 2 ;;
    --repos-dir) REPOS_DIR=${2:?}; shift 2 ;;
    --no-systemd) SYSTEMD=0; shift ;;
    --no-start) START=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --install-packages) PACKAGES=1; shift ;;
    --image) IMAGE=1; shift ;;
    -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done

STAGING_DIR=${STAGING_DIR:-$STATE_DIR/setup}
UPDATE_DIR="$STATE_DIR/update"
UPDATE_REQUEST_DIR="$STATE_DIR/update-requests"
COMPONENTS_DIR="$STATE_DIR/components"
TRUST_DIR="$CONFIG_DIR/update-trust"
UNIT="$UNIT_DIR/ginnungagap.service"
FIRSTBOOT_UNIT="$UNIT_DIR/ginnungagap-firstboot.service"
BOOTSTRAP_UNIT="$UNIT_DIR/ginnungagap-bootstrap.service"
SETUP_APPLY_PATH_UNIT="$UNIT_DIR/ginnungagap-setup-apply.path"
SETUP_APPLY_SERVICE_UNIT="$UNIT_DIR/ginnungagap-setup-apply.service"
ISSUE_UNIT="$UNIT_DIR/ginnungagap-issue.service"
ISSUE_PATH_UNIT="$UNIT_DIR/ginnungagap-issue.path"
ISSUE_CHANGE_UNIT="$UNIT_DIR/ginnungagap-issue-changed.service"
UPDATE_CHECK_SERVICE_UNIT="$UNIT_DIR/ginnungagap-update-check.service"
UPDATE_CHECK_TIMER_UNIT="$UNIT_DIR/ginnungagap-update-check.timer"
UPDATE_RESUME_UNIT="$UNIT_DIR/ginnungagap-update-resume.service"
UPDATE_REQUEST_PATH_UNIT="$UNIT_DIR/ginnungagap-update-request.path"
UPDATE_REQUEST_SERVICE_UNIT="$UNIT_DIR/ginnungagap-update-request.service"
SSHD_DROPIN="$SSHD_DROPIN_DIR/10-ginnungagap.conf"
# systemctl is only touched when installing into the real unit folder of a running systemd (not for a test --unit-dir)
REAL_SYSTEMD=0
if [ "$SYSTEMD" = 1 ] && [ "$UNIT_DIR" = /etc/systemd/system ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  REAL_SYSTEMD=1
fi
WRAPPER="$BIN_DIR/ginnungagap"
APP="$PREFIX/app"

if [ "$UNINSTALL" = 1 ]; then
  [ "$REAL_SYSTEMD" = 1 ] && systemctl disable ginnungagap.service ginnungagap-firstboot.service ginnungagap-bootstrap.service ginnungagap-setup-apply.path ginnungagap-issue.path ginnungagap-issue.service ginnungagap-update-check.timer ginnungagap-update-resume.service ginnungagap-update-request.path >/dev/null 2>&1 || true
  rm -f "$UNIT" "$FIRSTBOOT_UNIT" "$BOOTSTRAP_UNIT" "$SETUP_APPLY_PATH_UNIT" "$SETUP_APPLY_SERVICE_UNIT" "$ISSUE_UNIT" "$ISSUE_PATH_UNIT" "$ISSUE_CHANGE_UNIT" "$UPDATE_CHECK_SERVICE_UNIT" "$UPDATE_CHECK_TIMER_UNIT" "$UPDATE_RESUME_UNIT" "$UPDATE_REQUEST_PATH_UNIT" "$UPDATE_REQUEST_SERVICE_UNIT" "$SSHD_DROPIN" "$WRAPPER"
  rm -rf "$APP"
  rmdir "$PREFIX" 2>/dev/null || true  # only when nothing else lives there
  [ "$REAL_SYSTEMD" = 1 ] && systemctl daemon-reload || true
  echo "Removed the program, the command and the unit. Kept: $CONFIG_DIR (your configuration), $STAGING_DIR (the setup wizard's staging folder), $STATE_DIR (the update state, the unpacked releases and the backups) and the containers' data volumes."
  echo "Stop the containers with: docker compose --project-name ginnungagap down"
  exit 0
fi

# ---- packages (only when asked: this is the one step that installs software from the distribution)
if [ "$PACKAGES" = 1 ]; then
  [ "$(id -u)" = 0 ] || die "--install-packages needs root"
  command -v apt-get >/dev/null 2>&1 || die "--install-packages knows apt only (Debian, Ubuntu); install Docker with the compose plugin, python3-yaml and git yourself"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  compose_pkg=docker-compose  # Debian 13; Ubuntu calls the plugin package docker-compose-v2
  apt-cache show docker-compose-v2 >/dev/null 2>&1 && compose_pkg=docker-compose-v2
  apt-get install -y -qq docker.io "$compose_pkg" docker-buildx python3-yaml git curl ca-certificates open-vm-tools nfs-common  # nfs-common: for a template store on an NFS export
fi

# ---- checks, all before anything is changed
[ -f "$HERE/compose.yml" ] && [ -d "$HERE/ginnungagap" ] || die "run this from a checkout of the appliance repository"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || die "python 3.12 or newer is required"
python3 -c 'import yaml' 2>/dev/null || die "python3-yaml is required (Debian/Ubuntu: apt install python3-yaml)"
if [ "$START" = 1 ]; then
  command -v docker >/dev/null 2>&1 || die "docker is required (or use --no-start to only install)"
  docker compose version >/dev/null 2>&1 || die "the docker compose plugin is required"
fi
# the current folder name of each checkout, then the names it had before the code names (appliance images up to v1.2.x):
# those are read, never created
COMPONENT_REPOS="Igdrasyl:TopologyOperator TopologyBuilder:Mjonyr Hugyn-and-Munyn:Docker-Server-Files Mimyr:topology-generator:TopologyGenerator"
has_repos() {  # every checkout the images are built from is in folder $1 under its current name (a symlink to a folder counts)
  local entry
  for entry in $COMPONENT_REPOS; do [ -d "$1/${entry%%:*}" ] || return 1; done
}
repo_folder() {  # the folder in $1 that holds the checkout whose names are $2 (current first, then the old ones); empty: none
  local name
  for name in $(echo "$2" | tr ':' ' '); do
    if [ -d "$1/$name" ]; then echo "$name"; return 0; fi
  done
}
has_any_repos() {  # every checkout is in folder $1 under its current or an old name
  local entry
  for entry in $COMPONENT_REPOS; do [ -n "$(repo_folder "$1" "$entry")" ] || return 1; done
}
if [ -z "$REPOS_DIR" ]; then
  # no --repos-dir: a re-run keeps what the installation already uses; otherwise the folder `ginnungagap update` fills
  # (the update state's components folder, symlinks to the installed releases), and only then the folder next to this checkout
  recorded=$(sed -n 's/^REPOS_DIR=//p' "$CONFIG_DIR/install.env" 2>/dev/null | head -n1 || true)
  if [ -n "$recorded" ] && has_any_repos "$recorded"; then REPOS_DIR=$recorded
  elif has_repos "$COMPONENTS_DIR"; then REPOS_DIR=$COMPONENTS_DIR
  else REPOS_DIR=$(cd "$HERE/.." && pwd)
  fi
fi
REPOS_DIR=$(cd "$REPOS_DIR" 2>/dev/null && pwd) || die "repos dir does not exist"
# the checkouts may still carry the old names: that installs fine, but nothing can be built from them until
# `ginnungagap update --adopt` has copied them under the current names (it does not touch the old folders)
LEGACY_LAYOUT=0
for entry in $COMPONENT_REPOS; do
  found=$(repo_folder "$REPOS_DIR" "$entry")
  [ -n "$found" ] || die "missing $REPOS_DIR/${entry%%:*} (the images are built from it; set --repos-dir)"
  [ "$found" = "${entry%%:*}" ] || LEGACY_LAYOUT=1
done
GENERATOR_DIR=$(repo_folder "$REPOS_DIR" "Mimyr:topology-generator:TopologyGenerator")
[ -z "$MANIFEST" ] || [ -f "$MANIFEST" ] || die "--manifest: $MANIFEST is not a file"

# ---- program: replace the code, keep rendered/ (the running project's files)
install -d -m 0755 "$APP"
rm -rf "$APP/ginnungagap" "$APP/docker"
cp -r "$HERE/ginnungagap" "$HERE/docker" "$APP/"
find "$APP" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "$HERE/compose.yml" "$HERE/compose.legacy-ports.yml" "$HERE/compose.template-share.yml" "$HERE/ginnungagap.example.yml" "$APP/"
# which release this program is: the version of the appliance (ginnungagap/version.py) and, when `ginnungagap update` installs
# it, the components.json it came from. A leftover of an older install is removed first, it would describe the old program.
rm -f "$APP/VERSION" "$APP/components.json"
APP_VERSION=$(sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' "$HERE/ginnungagap/version.py" 2>/dev/null | head -n1 || true)
[ -z "$APP_VERSION" ] || printf '%s\n' "$APP_VERSION" >"$APP/VERSION"
[ -z "$MANIFEST" ] || install -m 0644 "$MANIFEST" "$APP/components.json"

# ---- configuration folder: never overwrite what the operator has edited
install -d -m 0750 "$CONFIG_DIR"
install -m 0644 "$HERE/ginnungagap.example.yml" "$CONFIG_DIR/ginnungagap.example.yml"  # a starting point for the first boot's hint
CREATED=0
if [ ! -f "$CONFIG_DIR/ginnungagap.yml" ] && [ "$IMAGE" = 0 ]; then  # an image has none: the first boot brings it
  install -m 0600 "$HERE/ginnungagap.example.yml" "$CONFIG_DIR/ginnungagap.yml"
  CREATED=1
fi
# the setup wizard's only writable path (its container mounts nothing else): a submitted configuration and the marker
# ginnungagap-setup-apply.path watches for, see ginnungagap/setup_wizard.py and ginnungagap/setup_watcher.py
install -d -m 0700 "$STAGING_DIR"

# ---- `ginnungagap update`: its state (installed release, history, journal, audit log, the lock), the unpacked releases,
# the symlinks the images are built from and the pre-update backups. All of it lives outside the program folder and the
# checkout, so replacing the program never touches it, and --uninstall keeps it. No token is created here: the access
# token is optional and only ever written by `ginnungagap update --set-token` (0600, never in the configuration).
install -d -m 0755 "$STATE_DIR"
install -d -m 0700 "$UPDATE_DIR" "$STATE_DIR/backups"
# the web panel's request folder (Igdrasyl, Updates): the one place its container may write to, and only one small request
# file at a time; ginnungagap-update-request.path hands it to `ginnungagap apply-update-request`, which re-validates it as root
install -d -m 0700 "$UPDATE_REQUEST_DIR"
install -d -m 0755 "$STATE_DIR/releases" "$COMPONENTS_DIR"
# the public key the release manifest is signed with. A file that still has no key in it (only comments) is not installed:
# `update.require_signature: auto` enforces the signature as soon as this file exists, and with no key every update would fail.
# An existing trust file that holds a key is the operator's (an added or replaced key, e.g. for their own test releases) and is
# never overwritten - not by a later install.sh run either, which includes the tooling step of every update. It is created when
# it is missing, and replaced when it has no key in it (only comments or blanks: nothing to lose).
install -d -m 0755 "$TRUST_DIR"
if [ -f "$HERE/release/allowed_signers" ] && grep -Eqv '^[[:space:]]*(#|$)' "$HERE/release/allowed_signers"; then
  if [ ! -s "$TRUST_DIR/allowed_signers" ] || ! grep -Eqv '^[[:space:]]*(#|$)' "$TRUST_DIR/allowed_signers"; then
    install -m 0644 "$HERE/release/allowed_signers" "$TRUST_DIR/allowed_signers"
  elif ! cmp -s "$HERE/release/allowed_signers" "$TRUST_DIR/allowed_signers"; then
    echo "Kept $TRUST_DIR/allowed_signers (it differs from the key list this release ships, release/allowed_signers): delete it and run install.sh again to get the shipped one."
  fi
fi

cat >"$CONFIG_DIR/install.env" <<ENV
# Written by install.sh; where the program and the repositories it builds from are.
PYTHONPATH=$APP
REPOS_DIR=$REPOS_DIR
GINNUNGAGAP_GENERATOR_CONFIG=$REPOS_DIR/$GENERATOR_DIR/config
GINNUNGAGAP_CONFIG_DIR=$CONFIG_DIR
GINNUNGAGAP_SETUP_STAGING_DIR=$STAGING_DIR
GINNUNGAGAP_UPDATE_DIR=$UPDATE_DIR
GINNUNGAGAP_UPDATE_REQUEST_DIR=$UPDATE_REQUEST_DIR
ENV

# ---- the command
install -d -m 0755 "$BIN_DIR"
cat >"$WRAPPER" <<WRAP
#!/bin/sh
# Runs the Ginnungagap command line with the settings written by install.sh.
set -a
. "$CONFIG_DIR/install.env"
set +a
exec python3 -m ginnungagap "\$@"
WRAP
chmod 0755 "$WRAPPER"

# ---- an image: SSH password login for the admin whose random initial password the console shows (see ginnungagap/console.py).
# A drop-in, so the distribution's own sshd_config stays untouched; sshd takes the FIRST value it reads, and the Debian
# main file includes its drop-ins before its own settings, so this wins over a later one (e.g. 50-cloud-init.conf).
if [ "$IMAGE" = 1 ]; then
  install -d -m 0755 "$SSHD_DROPIN_DIR"
  cat >"$SSHD_DROPIN" <<'SSHDDROPIN'
# Written by the Ginnungagap installer (--image): the admin logs in with the initial password shown on the VM console.
# root never logs in over SSH (Debian's default only allows keys; this keeps it so even if a key is added to root).
PasswordAuthentication yes
PermitRootLogin no
SSHDDROPIN
  chmod 0644 "$SSHD_DROPIN"
  [ "$SYSTEMD" = 1 ] && [ "$UNIT_DIR" = /etc/systemd/system ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] \
    && { systemctl reload ssh.service >/dev/null 2>&1 || true; }
fi

# ---- the unit: applies the configuration at boot (containers restart by themselves, this also picks up an edited file)
if [ "$SYSTEMD" = 1 ]; then
  install -d -m 0755 "$UNIT_DIR"
  cat >"$UNIT" <<UNITFILE
[Unit]
Description=Ginnungagap appliance
Requires=docker.service
After=docker.service network-online.target ginnungagap-firstboot.service
Wants=network-online.target
ConditionPathExists=$CONFIG_DIR/ginnungagap.yml

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=600
ExecStart=$WRAPPER apply $CONFIG_DIR/ginnungagap.yml --no-build
ExecReload=$WRAPPER apply $CONFIG_DIR/ginnungagap.yml --no-build

[Install]
WantedBy=multi-user.target
UNITFILE
  # first boot of an appliance image: the configuration from the hypervisor, the SSH host keys. Installed everywhere, but
  # only enabled for an image (a machine that is installed by hand has its configuration and its keys already).
  cat >"$FIRSTBOOT_UNIT" <<FIRSTBOOT
[Unit]
Description=Ginnungagap first boot (configuration from the hypervisor, SSH host keys)
ConditionPathExists=!/var/lib/ginnungagap/firstboot.done
After=open-vm-tools.service
Before=ssh.service ginnungagap.service

[Service]
Type=oneshot
ExecStart=$WRAPPER firstboot
# 1 = no usable configuration yet: a normal boot, the next one tries again
SuccessExitStatus=1

[Install]
WantedBy=multi-user.target
FIRSTBOOT
  # bootstrap: gateway + setup wizard only, reachable without a configuration at all - see ginnungagap/bootstrap.py.
  # Skipped at boot the moment a configuration exists (ginnungagap.service takes over); harmless to leave enabled.
  cat >"$BOOTSTRAP_UNIT" <<BOOTSTRAP
[Unit]
Description=Ginnungagap bootstrap (gateway + setup wizard, until a configuration exists)
Requires=docker.service
After=docker.service network-online.target ginnungagap-firstboot.service
Wants=network-online.target
ConditionPathExists=!$CONFIG_DIR/ginnungagap.yml
Before=ginnungagap.service

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=300
ExecStart=$WRAPPER bootstrap --no-build
ExecStartPost=-/sbin/agetty --reload

[Install]
WantedBy=multi-user.target
BOOTSTRAP
  # the setup wizard's hand-off: the moment its container stages a configuration (a marker file in $STAGING_DIR), this
  # path unit runs the service unit, which re-validates it and runs the real `apply` - see ginnungagap/setup_watcher.py.
  # Always armed, not only for a fresh appliance: resubmitting the wizard later works the same way, no reboot needed.
  cat >"$SETUP_APPLY_PATH_UNIT" <<SETUPPATH
[Unit]
Description=Watch for a configuration submitted through the setup wizard

[Path]
PathExists=$STAGING_DIR/apply.request

[Install]
WantedBy=multi-user.target
SETUPPATH
  wizard_image_flag=
  if [ "$IMAGE" = 1 ] || grep -Eq '^ExecStart=.*apply-staged .* --no-build$' "$SETUP_APPLY_SERVICE_UNIT" 2>/dev/null; then
    wizard_image_flag=" --no-build"
  fi
  cat >"$SETUP_APPLY_SERVICE_UNIT" <<SETUPSERVICE
[Unit]
Description=Apply a configuration submitted through the setup wizard
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
TimeoutStartSec=600
ExecStart=$WRAPPER apply-staged --staging-dir $STAGING_DIR --config-dir $CONFIG_DIR$wizard_image_flag
SETUPSERVICE
  # the console login text (SSH address and port, web interface) is rewritten at every boot, once the network is up: the
  # address of a DHCP machine can change between boots, and first boot runs before there is one.
  cat >"$ISSUE_UNIT" <<ISSUESERVICE
[Unit]
Description=Ginnungagap console login text (SSH address and port, web interface)
After=network-online.target ginnungagap-firstboot.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$WRAPPER issue

[Install]
WantedBy=multi-user.target
ISSUESERVICE
  # the initial SSH password stays on the console only until it is changed: a change rewrites /etc/shadow, which rewrites the text
  cat >"$ISSUE_PATH_UNIT" <<ISSUEPATH
[Unit]
Description=Rewrite the console login text when a password changes

[Path]
PathChanged=/etc/shadow
Unit=ginnungagap-issue-changed.service

[Install]
WantedBy=multi-user.target
ISSUEPATH
  cat >"$ISSUE_CHANGE_UNIT" <<ISSUECHANGED
[Unit]
Description=Ginnungagap console login text after a password change (also wipes the idle login screens)

[Service]
Type=oneshot
ExecStart=$WRAPPER issue --clear-screens
ISSUECHANGED
  # looking for a new version, once a day, never installing one. The timer is always armed; the service does nothing
  # unless the configuration says `update.check: daily` (ExecCondition), so the setting needs no systemctl. Exit code 10 of
  # `update --check` means "an update is available" and is not a failure.
  cat >"$UPDATE_CHECK_SERVICE_UNIT" <<UPDATECHECK
[Unit]
Description=Ginnungagap: look for a new version of the appliance components (never installs anything)
After=network-online.target
Wants=network-online.target
ConditionPathExists=$CONFIG_DIR/ginnungagap.yml

[Service]
Type=oneshot
TimeoutStartSec=300
SuccessExitStatus=10
# exit 1 here is "update.check is not daily": systemd skips the service without calling it a failure
ExecCondition=/bin/sh -c 'cd / && set -a && . $CONFIG_DIR/install.env && set +a && exec python3 -m ginnungagap.config update-check-daily $CONFIG_DIR/ginnungagap.yml'
ExecStart=$WRAPPER update --check
UPDATECHECK
  cat >"$UPDATE_CHECK_TIMER_UNIT" <<UPDATETIMER
[Unit]
Description=Ginnungagap: daily look for a new version (only when update.check is daily)

[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true
Unit=ginnungagap-update-check.service

[Install]
WantedBy=timers.target
UPDATETIMER
  # a power loss or reboot in the middle of an update leaves its journal (transaction.json): before anything starts, the
  # next boot finishes the job - discards what had not changed anything yet, rolls back what had
  cat >"$UPDATE_RESUME_UNIT" <<UPDATERESUME
[Unit]
Description=Ginnungagap: finish or roll back an update that was interrupted
Requires=docker.service
After=docker.service
Before=ginnungagap.service ginnungagap-setup-apply.path
ConditionPathExists=$UPDATE_DIR/transaction.json

[Service]
Type=oneshot
TimeoutStartSec=900
ExecStart=$WRAPPER update --resume

[Install]
WantedBy=multi-user.target
UPDATERESUME
  # the web panel's hand-off (Igdrasyl, Updates): the moment its container leaves a request file in $UPDATE_REQUEST_DIR, this path
  # unit runs the service unit as root. It does what the command line does (`update --check`, or `update --apply --yes` for the
  # one release an admin confirmed), re-validates the request first and never starts anything else - see ginnungagap/update_request.py.
  # Always armed, but nothing is ever installed without such a request: the check timer above only looks.
  cat >"$UPDATE_REQUEST_PATH_UNIT" <<UPDATEREQUESTPATH
[Unit]
Description=Watch for an update request from the web panel

[Path]
PathExists=$UPDATE_REQUEST_DIR/request.json

[Install]
WantedBy=multi-user.target
UPDATEREQUESTPATH
  cat >"$UPDATE_REQUEST_SERVICE_UNIT" <<UPDATEREQUESTSERVICE
[Unit]
Description=Ginnungagap: act on an update request from the web panel
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
TimeoutStartSec=3600
# exit 1 is a refused or failed request: the panel shows why (web-status.json), the unit is not a failure
SuccessExitStatus=1
ExecStart=$WRAPPER apply-update-request --requests-dir $UPDATE_REQUEST_DIR --config-dir $CONFIG_DIR
UPDATEREQUESTSERVICE
  if [ "$REAL_SYSTEMD" = 1 ]; then
    systemctl daemon-reload
    if [ "$IMAGE" = 1 ]; then
      # an appliance has no use for a desktop login: the text console is where /etc/issue.d (SSH address, port, password) is shown
      systemctl set-default multi-user.target >/dev/null 2>&1 || true
      systemctl enable ginnungagap-firstboot.service >/dev/null 2>&1 || true
      systemctl enable ginnungagap.service >/dev/null 2>&1 || true  # it only runs once a configuration exists
    fi
    # the unit applies the configuration at boot: not before the example file has been edited (a boot with it would only fail)
    if [ "$CREATED" = 0 ]; then
      systemctl enable ginnungagap.service >/dev/null 2>&1 || true
    fi
    systemctl enable ginnungagap-bootstrap.service ginnungagap-setup-apply.path ginnungagap-issue.service ginnungagap-issue.path ginnungagap-update-check.timer ginnungagap-update-resume.service ginnungagap-update-request.path >/dev/null 2>&1 || true
    # armed now, not only at the next boot: an update of the tooling itself installs this unit on a machine that keeps running
    systemctl start ginnungagap-update-request.path >/dev/null 2>&1 || true
  fi
fi

echo "Installed: program in $APP, command $WRAPPER, configuration in $CONFIG_DIR."
if [ "$LEGACY_LAYOUT" = 1 ]; then
  echo "The checkouts in $REPOS_DIR still carry the names of before the code names (TopologyOperator, Docker-Server-Files, topology-generator); the images are built from the current ones (Igdrasyl, TopologyBuilder, Hugyn-and-Munyn, Mimyr)."
  echo "Run  sudo ginnungagap update --adopt  (copies checkouts under the current names and leaves $REPOS_DIR alone; for the update state's own components folder it renames in place), then  sudo ginnungagap apply $CONFIG_DIR/ginnungagap.yml."
fi
if [ "$IMAGE" = 1 ]; then
  echo "Prepared for an appliance image: the first-boot unit is enabled, SSH password login is on, the console is text mode (systemctl set-default graphical.target brings the desktop back), there is no configuration yet (image/prepare-image.sh is the next step)."
  exit 0
fi
if [ "$CREATED" = 1 ]; then
  echo "A new $CONFIG_DIR/ginnungagap.yml was created from the example (readable by root only). Edit it, then run:"
  echo "  sudo ginnungagap apply $CONFIG_DIR/ginnungagap.yml"
  [ "$SYSTEMD" = 1 ] && echo "and, so that it also starts at boot:  sudo systemctl enable ginnungagap.service"
  exit 0
fi
if [ "$START" = 1 ] && [ "$LEGACY_LAYOUT" = 0 ]; then
  # not from this folder: python puts the current folder first on its path, so an update run inside the checkout would
  # apply the checkout's code and render into it, not the program that was just installed
  (cd / && "$WRAPPER" apply "$CONFIG_DIR/ginnungagap.yml")
fi
