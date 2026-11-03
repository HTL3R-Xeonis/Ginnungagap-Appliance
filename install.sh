#!/usr/bin/env bash
# Installs the Ginnungagap appliance on this machine: the program under PREFIX, the `ginnungagap` command, a configuration
# folder (your ginnungagap.yml stays where it is on a re-run) and a systemd unit that applies the configuration at boot.
# The services themselves run as containers, so Docker with compose has to be installed already, and so do the sibling
# repositories the images are built from (TopologyOperator, TopologyBuilder, Docker-Server-Files, topology-generator).
#
#   sudo ./install.sh --repos-dir /srv/repos           # install (or update) and start
#   sudo ./install.sh --uninstall                      # remove program, command and units; config and data are kept
#
# Options: --prefix DIR (/opt/ginnungagap)  --config-dir DIR (/etc/ginnungagap)  --bin-dir DIR (/usr/local/bin)
#          --unit-dir DIR (/etc/systemd/system)  --repos-dir DIR  --no-systemd  --no-start
#          --install-packages   first install what is needed with apt (Debian 13 / Ubuntu: Docker, compose, python3-yaml, ...)
#          --image              for building an appliance image: also enable the first-boot unit (see image/prepare-image.sh)
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
PREFIX=/opt/ginnungagap
CONFIG_DIR=/etc/ginnungagap
BIN_DIR=/usr/local/bin
UNIT_DIR=/etc/systemd/system
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
    --repos-dir) REPOS_DIR=${2:?}; shift 2 ;;
    --no-systemd) SYSTEMD=0; shift ;;
    --no-start) START=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --install-packages) PACKAGES=1; shift ;;
    --image) IMAGE=1; shift ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done

UNIT="$UNIT_DIR/ginnungagap.service"
FIRSTBOOT_UNIT="$UNIT_DIR/ginnungagap-firstboot.service"
# systemctl is only touched when installing into the real unit folder of a running systemd (not for a test --unit-dir)
REAL_SYSTEMD=0
if [ "$SYSTEMD" = 1 ] && [ "$UNIT_DIR" = /etc/systemd/system ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  REAL_SYSTEMD=1
fi
WRAPPER="$BIN_DIR/ginnungagap"
APP="$PREFIX/app"

if [ "$UNINSTALL" = 1 ]; then
  [ "$REAL_SYSTEMD" = 1 ] && systemctl disable ginnungagap.service ginnungagap-firstboot.service >/dev/null 2>&1 || true
  rm -f "$UNIT" "$FIRSTBOOT_UNIT" "$WRAPPER"
  rm -rf "$APP"
  rmdir "$PREFIX" 2>/dev/null || true  # only when nothing else lives there
  [ "$REAL_SYSTEMD" = 1 ] && systemctl daemon-reload || true
  echo "Removed the program, the command and the unit. Kept: $CONFIG_DIR (your configuration) and the containers' data volumes."
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
[ -n "$REPOS_DIR" ] || REPOS_DIR=$(cd "$HERE/.." && pwd)
REPOS_DIR=$(cd "$REPOS_DIR" 2>/dev/null && pwd) || die "repos dir does not exist"
for repo in TopologyOperator TopologyBuilder Docker-Server-Files topology-generator; do
  [ -d "$REPOS_DIR/$repo" ] || die "missing $REPOS_DIR/$repo (the images are built from it; set --repos-dir)"
done

# ---- program: replace the code, keep rendered/ (the running project's files)
install -d -m 0755 "$APP"
rm -rf "$APP/ginnungagap" "$APP/docker"
cp -r "$HERE/ginnungagap" "$HERE/docker" "$APP/"
find "$APP" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "$HERE/compose.yml" "$HERE/compose.legacy-ports.yml" "$HERE/ginnungagap.example.yml" "$APP/"

# ---- configuration folder: never overwrite what the operator has edited
install -d -m 0750 "$CONFIG_DIR"
install -m 0644 "$HERE/ginnungagap.example.yml" "$CONFIG_DIR/ginnungagap.example.yml"  # a starting point for the first boot's hint
CREATED=0
if [ ! -f "$CONFIG_DIR/ginnungagap.yml" ] && [ "$IMAGE" = 0 ]; then  # an image has none: the first boot brings it
  install -m 0600 "$HERE/ginnungagap.example.yml" "$CONFIG_DIR/ginnungagap.yml"
  CREATED=1
fi
cat >"$CONFIG_DIR/install.env" <<ENV
# Written by install.sh; where the program and the repositories it builds from are.
PYTHONPATH=$APP
REPOS_DIR=$REPOS_DIR
GINNUNGAGAP_GENERATOR_CONFIG=$REPOS_DIR/topology-generator/config
GINNUNGAGAP_CONFIG_DIR=$CONFIG_DIR
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
  if [ "$REAL_SYSTEMD" = 1 ]; then
    systemctl daemon-reload
    if [ "$IMAGE" = 1 ]; then
      systemctl enable ginnungagap-firstboot.service >/dev/null 2>&1 || true
      systemctl enable ginnungagap.service >/dev/null 2>&1 || true  # it only runs once a configuration exists
    fi
    # the unit applies the configuration at boot: not before the example file has been edited (a boot with it would only fail)
    if [ "$CREATED" = 0 ]; then
      systemctl enable ginnungagap.service >/dev/null 2>&1 || true
    fi
  fi
fi

echo "Installed: program in $APP, command $WRAPPER, configuration in $CONFIG_DIR."
if [ "$IMAGE" = 1 ]; then
  echo "Prepared for an appliance image: the first-boot unit is enabled, there is no configuration yet (image/prepare-image.sh is the next step)."
  exit 0
fi
if [ "$CREATED" = 1 ]; then
  echo "A new $CONFIG_DIR/ginnungagap.yml was created from the example (readable by root only). Edit it, then run:"
  echo "  sudo ginnungagap apply $CONFIG_DIR/ginnungagap.yml"
  [ "$SYSTEMD" = 1 ] && echo "and, so that it also starts at boot:  sudo systemctl enable ginnungagap.service"
  exit 0
fi
if [ "$START" = 1 ]; then
  "$WRAPPER" apply "$CONFIG_DIR/ginnungagap.yml"
fi
