#!/usr/bin/env bash
# Installs the Ginnungagap appliance on this machine: the program under PREFIX, the `ginnungagap` command, a configuration
# folder (your ginnungagap.yml stays where it is on a re-run) and a systemd unit that applies the configuration at boot.
# The services themselves run as containers, so Docker with compose has to be installed already, and so do the sibling
# repositories the images are built from (TopologyOperator, TopologyBuilder, Docker-Server-Files, topology-generator).
#
#   sudo ./install.sh --repos-dir /srv/repos           # install (or update) and start
#   sudo ./install.sh --uninstall                      # remove program, command and unit; config and data are kept
#
# Options: --prefix DIR (/opt/ginnungagap)  --config-dir DIR (/etc/ginnungagap)  --bin-dir DIR (/usr/local/bin)
#          --unit-dir DIR (/etc/systemd/system)  --repos-dir DIR  --no-systemd  --no-start
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
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done

UNIT="$UNIT_DIR/ginnungagap.service"
# systemctl is only touched when installing into the real unit folder of a running systemd (not for a test --unit-dir)
REAL_SYSTEMD=0
if [ "$SYSTEMD" = 1 ] && [ "$UNIT_DIR" = /etc/systemd/system ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  REAL_SYSTEMD=1
fi
WRAPPER="$BIN_DIR/ginnungagap"
APP="$PREFIX/app"

if [ "$UNINSTALL" = 1 ]; then
  [ "$REAL_SYSTEMD" = 1 ] && systemctl disable ginnungagap.service >/dev/null 2>&1 || true
  rm -f "$UNIT" "$WRAPPER"
  rm -rf "$APP"
  [ "$REAL_SYSTEMD" = 1 ] && systemctl daemon-reload || true
  echo "Removed the program, the command and the unit. Kept: $CONFIG_DIR (your configuration) and the containers' data volumes."
  echo "Stop the containers with: docker compose --project-name ginnungagap down"
  exit 0
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
if [ ! -f "$CONFIG_DIR/ginnungagap.yml" ]; then
  install -m 0600 "$HERE/ginnungagap.example.yml" "$CONFIG_DIR/ginnungagap.yml"
  CREATED=1
else
  CREATED=0
fi
cat >"$CONFIG_DIR/install.env" <<ENV
# Written by install.sh; where the program and the repositories it builds from are.
PYTHONPATH=$APP
REPOS_DIR=$REPOS_DIR
GINNUNGAGAP_GENERATOR_CONFIG=$REPOS_DIR/topology-generator/config
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
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=600
ExecStart=$WRAPPER apply $CONFIG_DIR/ginnungagap.yml --no-build
ExecReload=$WRAPPER apply $CONFIG_DIR/ginnungagap.yml --no-build

[Install]
WantedBy=multi-user.target
UNITFILE
  if [ "$REAL_SYSTEMD" = 1 ]; then
    systemctl daemon-reload
    systemctl enable ginnungagap.service >/dev/null 2>&1 || true
  fi
fi

echo "Installed: program in $APP, command $WRAPPER, configuration in $CONFIG_DIR."
if [ "$CREATED" = 1 ]; then
  echo "A new $CONFIG_DIR/ginnungagap.yml was created from the example (readable by root only). Edit it, then run:"
  echo "  sudo ginnungagap apply $CONFIG_DIR/ginnungagap.yml"
  exit 0
fi
if [ "$START" = 1 ]; then
  "$WRAPPER" apply "$CONFIG_DIR/ginnungagap.yml"
fi
