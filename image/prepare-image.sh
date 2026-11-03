#!/usr/bin/env bash
# Prepares THIS machine to become the appliance image, as the last step before it is shut down and exported
# (image/export_ova.py). It builds the images of every service (so the image starts without internet and without a build)
# and removes everything that belongs to one particular machine: the configuration, the services' data, the SSH host keys,
# the machine id, every authorized SSH key, passwordless sudo rules, shell histories and logs.
#
# Run it on a machine on which `install.sh --install-packages --image --no-start` was run. It is DESTRUCTIVE for what it
# removes, so it asks for --yes; --dry-run only lists what it would do.
#
#   sudo image/prepare-image.sh --yes [--zero-free-space] [--project NAME]
#   image/prepare-image.sh --dry-run
#
# --zero-free-space  fill the free disk space with zeros first (the exported disk compresses better; takes a while)
# --project NAME     the compose project whose containers and volumes are removed (default ginnungagap)
set -euo pipefail

YES=0
DRY=0
ZERO=0
PROJECT=ginnungagap

die() { echo "prepare-image.sh: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --zero-free-space) ZERO=1; shift ;;
    --project) PROJECT=${2:?}; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done

if [ "$DRY" = 0 ]; then
  [ "$YES" = 1 ] || die "this removes the configuration, data, SSH keys and logs of this machine; run it again with --yes"
  [ "$(id -u)" = 0 ] || die "run it as root"
  command -v ginnungagap >/dev/null 2>&1 || die "the ginnungagap command is missing: run install.sh --image first"
fi

act() {  # description, then the function to run
  if [ "$DRY" = 1 ]; then echo "would: $1"; else echo "== $1"; "$2"; fi
}

build_images() { ginnungagap build-images; }

remove_services() {
  docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" | xargs -r docker rm -f >/dev/null
  docker volume ls -q | grep "^${PROJECT}_" | xargs -r docker volume rm -f >/dev/null || true
  docker builder prune -af >/dev/null 2>&1 || true
  docker image prune -f >/dev/null 2>&1 || true  # layers of earlier builds
}

remove_configuration() {
  rm -f /etc/ginnungagap/ginnungagap.yml /etc/ginnungagap/ginnungagap.yml.rejected
  rm -rf /opt/ginnungagap/app/rendered
  rm -f /var/lib/ginnungagap/firstboot.done /var/lib/ginnungagap/firstboot.problems /etc/issue.d/ginnungagap.issue
}

remove_identity() {
  rm -f /etc/ssh/ssh_host_*
  : >/etc/machine-id
  [ -L /var/lib/dbus/machine-id ] || rm -f /var/lib/dbus/machine-id  # Debian links it to /etc/machine-id
  rm -f /var/lib/dhcp/*.leases /var/lib/NetworkManager/*.lease 2>/dev/null || true
}

remove_access() {
  find /root /home -name authorized_keys -type f -delete 2>/dev/null || true
  # passwordless sudo is for the person who built the image, not for everybody who gets a copy
  grep -l NOPASSWD /etc/sudoers.d/* 2>/dev/null | grep -v README | xargs -r rm -f
  rm -f /root/.bash_history /home/*/.bash_history /root/.viminfo /home/*/.viminfo
}

clean_logs() {
  journalctl --rotate >/dev/null 2>&1 || true
  journalctl --vacuum-time=1s >/dev/null 2>&1 || true
  find /var/log -type f \( -name '*.log' -o -name '*.1' -o -name '*.gz' \) -delete 2>/dev/null || true
  find /var/log -maxdepth 1 -type f \( -name wtmp -o -name btmp -o -name lastlog \) | xargs -r truncate -s 0
  apt-get clean >/dev/null 2>&1 || true
  rm -rf /tmp/* /var/tmp/* 2>/dev/null || true
}

zero_free_space() {
  dd if=/dev/zero of=/zero.fill bs=1M status=none 2>/dev/null || true
  sync
  rm -f /zero.fill
}

act "build and pull the images of every service" build_images
act "remove the containers and volumes of the project '$PROJECT'" remove_services
act "remove the configuration, the rendered files and the first-boot marker" remove_configuration
act "remove the SSH host keys, the machine id and DHCP leases" remove_identity
act "remove authorized SSH keys, passwordless sudo rules and shell histories" remove_access
act "clean logs, package caches and temporary files" clean_logs
[ "$ZERO" = 1 ] && act "fill the free disk space with zeros" zero_free_space
if [ "$DRY" = 0 ]; then
  sync
  echo
  echo "Ready. Shut this machine down now (systemctl poweroff) and export it with image/export_ova.py."
  echo "Do not boot it again before the export: that would create new keys and a new machine id inside the image."
fi
