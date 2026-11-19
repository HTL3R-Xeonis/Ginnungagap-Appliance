#!/usr/bin/env bash
# Prepares THIS machine to become the appliance image, as the last step before it is shut down and exported
# (image/export_ova.py). It builds the images of every service (so the image starts without internet and without a build)
# and removes every Docker image that the current compose project does not use (a service of an older bundle must not stay in
# the image), and everything that belongs to one particular machine: the configuration, the services' data, the SSH host keys,
# the machine id, every authorized SSH key, passwordless sudo rules, shell histories and logs, and the admin's generated
# initial SSH password, the login account's password hash and the template's root password hash (root is locked), and the
# journal of every build boot. The sshd drop-in stays for the next first boot.
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

build_images() { ginnungagap build-images --remove-other-images; }

stop_setup() {
  systemctl stop ginnungagap-setup-apply.path ginnungagap-setup-apply.service
}

remove_services() {
  docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" | xargs -r docker rm -f >/dev/null
  docker volume ls -q | grep "^${PROJECT}_" | xargs -r docker volume rm -f >/dev/null || true
}

remove_build_leftovers() {
  docker builder prune -af >/dev/null 2>&1 || true
  docker image prune -f >/dev/null 2>&1 || true  # layers of earlier builds
}

remove_configuration() {
  rm -f /etc/ginnungagap/ginnungagap.yml /etc/ginnungagap/ginnungagap.yml.new /etc/ginnungagap/ginnungagap.yml.rejected
  rm -rf /opt/ginnungagap/app/rendered
  rm -f /var/lib/ginnungagap/firstboot.done /var/lib/ginnungagap/firstboot.problems /etc/issue.d/ginnungagap.issue
  # the admin's generated initial password and the decision that it was generated: every copy of the image makes its own
  rm -f /var/lib/ginnungagap/ssh-access.json
}

remove_staged_configuration() {
  local staging_dir=""
  if [ -r /etc/ginnungagap/install.env ]; then
    staging_dir=$(sed -n 's/^GINNUNGAGAP_SETUP_STAGING_DIR=//p' /etc/ginnungagap/install.env)
  fi
  staging_dir=${staging_dir:-/var/lib/ginnungagap/setup}
  case "$staging_dir" in /*) ;; *) die "setup staging directory must be absolute" ;; esac
  rm -f -- "$staging_dir/apply.request" "$staging_dir/ginnungagap.yml" \
    "$staging_dir/ginnungagap.yml.new" "$staging_dir/ginnungagap.yml.rejected"
}

remove_identity() {
  rm -f /etc/ssh/ssh_host_*
  : >/etc/machine-id
  [ -L /var/lib/dbus/machine-id ] || rm -f /var/lib/dbus/machine-id  # Debian links it to /etc/machine-id
  rm -f /var/lib/dhcp/*.leases /var/lib/NetworkManager/*.lease 2>/dev/null || true
}

remove_access() {
  local account rule
  # Match the UID used by firstboot. Replace the hash entirely so it cannot be recovered by unlocking the account.
  if account=$(getent passwd 1000); then
    usermod --password '!' "${account%%:*}"
  fi
  # root keeps the template's own password hash otherwise; no copy of the image should open a root login with it
  usermod --password '!' root
  # usermod keeps the previous shadow file; Debian can also make a daily backup.
  rm -f /etc/shadow- /var/backups/shadow.bak
  find /root /home -name authorized_keys -type f -delete 2>/dev/null || true
  # passwordless sudo is for the person who built the image, not for everybody who gets a copy
  for rule in /etc/sudoers.d/*; do
    if [ -f "$rule" ] && [ "${rule##*/}" != README ] && grep -q NOPASSWD "$rule"; then
      rm -f -- "$rule"
    fi
  done
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

clean_journal() {
  # journalctl only vacuums the directory of the CURRENT machine id, never the ones of earlier builds, and it keeps the
  # active files, so the logs of every build boot would stay. Stop the journal (and its sockets, or the next message starts
  # it again), then delete everything; the next boot of a copy creates it from scratch. Nothing logs to it afterwards.
  journalctl --rotate >/dev/null 2>&1 || true
  journalctl --vacuum-time=1s >/dev/null 2>&1 || true
  systemctl stop systemd-journald.socket systemd-journald-dev-log.socket systemd-journald-audit.socket systemd-journald.service >/dev/null 2>&1 || true
  rm -rf /var/log/journal/* /run/log/journal/* 2>/dev/null || true
}

zero_free_space() {
  dd if=/dev/zero of=/zero.fill bs=1M status=none 2>/dev/null || true
  sync
  rm -f /zero.fill
}

act "stop the setup watcher" stop_setup
# the containers go first: an image that a container still uses cannot be removed
act "remove the containers and volumes of the project '$PROJECT'" remove_services
act "build and pull the images of every service and remove every image that is not part of the current compose project" build_images
act "remove the build cache and untagged image layers" remove_build_leftovers
act "remove the configuration, the rendered files, the first-boot marker and the generated SSH password" remove_configuration
act "remove staged and rejected setup files" remove_staged_configuration
act "remove the SSH host keys, the machine id and DHCP leases" remove_identity
act "remove the login password hash, shadow backups, authorized SSH keys, passwordless sudo rules and shell histories" remove_access
act "clean logs, package caches and temporary files" clean_logs
act "stop the journal and delete the logs of every build boot (/var/log/journal)" clean_journal
[ "$ZERO" = 1 ] && act "fill the free disk space with zeros" zero_free_space
if [ "$DRY" = 0 ]; then
  sync
  echo
  echo "Ready. Shut this machine down now (systemctl poweroff) and export it with image/export_ova.py."
  echo "Do not boot it again before the export: that would create new keys and a new machine id inside the image."
fi
