#!/usr/bin/env bash
# Prepares THIS machine to become the appliance image, as the last step before it is shut down and exported
# (image/export_ova.py). It builds the images of every service (so the image starts without internet and without a build)
# and removes every Docker image that the current compose project does not use (a service of an older bundle must not stay in
# the image), and everything that belongs to one particular machine: the configuration, the services' data, the SSH host keys,
# the machine id, every authorized SSH key, passwordless sudo rules, shell histories and logs, and the admin's generated
# initial SSH password, the login account's password hash and the template's root password hash (root is locked), and the
# journal of every build boot, the image builder's own leftovers (the log of this very run, other *.log, old-repos-listing and
# *.bak in /root, the known leftovers in the home of the login account) and the host name of the base VM (reset to
# `ginnungagap`; guestinfo.ginnungagap.hostname sets the real one at the first boot). The sshd drop-in stays for the next
# first boot. Nothing else in a home folder is touched.
#
# The image keeps the RELEASE it was built from (`ginnungagap update`: installed.json, the unpacked releases and the links the
# images are built from), so a fresh appliance can check for and install updates at once. A machine that was installed with
# install.sh --repos-dir (not by `ginnungagap update --apply`) is recorded as the release it is first (`update --adopt`).
# What belongs to this machine's update history goes: the access token (an update token is optional and must never be
# baked into an image), the journal, history, downloads, audit log, pre-update backups, rollback image tags and every
# unpacked release that is not installed.
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
DEFAULT_HOSTNAME=ginnungagap

die() { echo "prepare-image.sh: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --zero-free-space) ZERO=1; shift ;;
    --project) PROJECT=${2:?}; shift 2 ;;
    -h|--help) sed -n '2,/^# --project/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

state_dir() {  # the folder of the update state: GINNUNGAGAP_UPDATE_DIR from install.env, else the default
  local dir=""
  if [ -r /etc/ginnungagap/install.env ]; then
    dir=$(sed -n 's/^GINNUNGAGAP_UPDATE_DIR=//p' /etc/ginnungagap/install.env)
  fi
  dir=${dir:-/var/lib/ginnungagap/update}
  case "$dir" in /*) ;; *) die "update state directory must be absolute" ;; esac
  printf '%s\n' "$dir"
}

record_release() {
  # an image built with `ginnungagap update --apply` has installed.json already; any other one is adopted as the release it is
  if [ ! -f "$(state_dir)/installed.json" ]; then
    ginnungagap update --adopt --yes
  fi
}

remove_update_leftovers() {
  local dir base token_file link keep rel
  dir=$(state_dir)
  base=$(dirname "$dir")
  # the access token: the default place, and the place the configuration names (it is removed with the configuration below)
  rm -f /etc/ginnungagap/update-token
  token_file=$(python3 - <<'PY' 2>/dev/null || true
import yaml
try:
    with open("/etc/ginnungagap/ginnungagap.yml", encoding="utf-8") as handle:
        print((yaml.safe_load(handle) or {}).get("update", {}).get("token_file") or "")
except Exception:
    pass
PY
)
  case "$token_file" in /*) rm -f -- "$token_file" ;; esac
  rm -rf -- "$dir/downloads"
  rm -f -- "$dir/transaction.json" "$dir/audit.log" "$dir/available.json" "$dir/available-offline.json" "$dir/web-status.json" "$dir/lock"
  rm -f -- "$base/update-requests"/* 2>/dev/null || true  # a request the web panel left (it names who asked)
  rm -rf -- "$base/backups"/* 2>/dev/null || true  # pre-update backups: the configuration (API keys) and the data of this machine
  # the installed release stays but has nothing to go back to: a copy starts with no "previous" release. Of the history only
  # the record installed.json itself names (`entry`: the file of the installed state) is kept - the first update records the
  # installed state as the one to go back to by that name, and every other record is of this machine's own past.
  if [ -f "$dir/installed.json" ]; then
    python3 - "$dir/installed.json" "$dir/history" <<'PY'
import json, os, shutil, sys
path, history = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as handle:
    state = json.load(handle)
state["previous"] = None
state["previous_entry"] = None
state["backup"] = None
entry = state.get("entry")
keep = {entry} if isinstance(entry, str) and entry == os.path.basename(entry) else set()
if os.path.isdir(history) and not os.path.islink(history):
    for name in os.listdir(history):
        if name in keep:
            continue
        target = os.path.join(history, name)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target, ignore_errors=True)
        else:
            os.unlink(target)
    if not os.listdir(history):
        os.rmdir(history)
tmp = path + ".new"
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(state, handle, indent=2)
    handle.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
  else
    rm -rf -- "$dir/history"
  fi
  # unpacked releases that no link of components/ points to (older versions kept for a rollback)
  if [ -d "$base/releases" ]; then
    keep=" "
    for link in "$base"/components/*; do
      [ -L "$link" ] && keep="$keep$(readlink -f -- "$link") "
    done
    for rel in "$base"/releases/*/*; do
      [ -d "$rel" ] || continue
      case "$keep" in *" $(readlink -f -- "$rel") "*) ;; *) rm -rf -- "$rel" ;; esac
    done
  fi
  # rollback image tags: the images stay under their normal names, only the extra names go
  docker image ls --format '{{.Repository}}:{{.Tag}}' | grep ':rollback-' | xargs -r docker image rm >/dev/null 2>&1 || true
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

reset_hostname() {
  # the image carries the name of the base VM until the first boot sets one from guestinfo (firstboot.py)
  local old
  old=$(tr -d '[:space:]' </etc/hostname 2>/dev/null || true)
  printf '%s\n' "$DEFAULT_HOSTNAME" >/etc/hostname
  if [ -f /etc/hosts ]; then
    if grep -q '^127\.0\.1\.1[[:space:]]' /etc/hosts; then
      sed -i "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1\t$DEFAULT_HOSTNAME/" /etc/hosts
    elif [ -n "$old" ] && [ "$old" != "$DEFAULT_HOSTNAME" ]; then
      sed -i "s/\b${old//./\\.}\b/$DEFAULT_HOSTNAME/g" /etc/hosts 2>/dev/null || true
    fi
  fi
  # a pretty name that hostnamectl may have stored names the base VM as well
  [ -f /etc/machine-info ] && sed -i '/^PRETTY_HOSTNAME=/d' /etc/machine-info
  hostname "$DEFAULT_HOSTNAME" 2>/dev/null || true
}

remove_known_files() {  # DIR NAME... : files of that name directly in DIR (never folders, never links)
  local dir=$1 name
  shift
  [ -d "$dir" ] && [ ! -L "$dir" ] || return 0
  for name in "$@"; do
    find "$dir" -maxdepth 1 -type f -name "$name" -delete 2>/dev/null || true
  done
}

login_home() {  # the home folder of the login account (UID 1000, as firstboot), empty when there is none
  local home
  home=$(getent passwd 1000 2>/dev/null | cut -d: -f6) || true
  case "$home" in /|""|/root) ;; /*) printf '%s\n' "$home" ;; esac
}

remove_image_builder_files() {
  local home
  # root's home: the logs and listings of whoever built the image (this run's own log too, if it was tee'd to /root)
  remove_known_files /root '*.log' '*.bak' old-repos-listing .zsh_history .lesshst .python_history .wget-hsts
  # the folder of the same name that the base of an older image carries (only that one folder, never a link)
  [ -d /root/old-repos-listing ] && [ ! -L /root/old-repos-listing ] && rm -rf -- /root/old-repos-listing
  # registry logins of whoever built the image (docker login writes them here); a copy of the image must carry none
  remove_known_files /root/.docker config.json
  # the login account: only the known build artifacts; its Desktop and every other file stay
  home=$(login_home)
  if [ -n "$home" ]; then
    remove_known_files "$home" prepare.log old-repos-listing .zsh_history .lesshst .python_history .wget-hsts \
      .sudo_as_admin_successful .xsession-errors .xsession-errors.old
    remove_known_files "$home/.docker" config.json
  fi
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
act "record the installed release for 'ginnungagap update' (adopt the checkouts unless the image was built by an update)" record_release
act "remove the update access token, history, downloads, audit log, backups, rollback image tags and unpacked releases that are not installed (the installed release stays)" remove_update_leftovers
act "remove the build cache and untagged image layers" remove_build_leftovers
act "remove the configuration, the rendered files, the first-boot marker and the generated SSH password" remove_configuration
act "remove staged and rejected setup files" remove_staged_configuration
act "remove the SSH host keys, the machine id and DHCP leases" remove_identity
act "reset the host name of the base VM to '$DEFAULT_HOSTNAME' (guestinfo sets the real one at the first boot)" reset_hostname
act "remove the login password hash, shadow backups, authorized SSH keys, passwordless sudo rules and shell histories" remove_access
act "clean logs, package caches and temporary files" clean_logs
act "stop the journal and delete the logs of every build boot (/var/log/journal)" clean_journal
# last before the zero-fill: the log of this very run (/root/prepare.log when it was tee'd there) must not stay in the image either
act "remove the image builder's leftovers (*.log, *.bak, old-repos-listing in /root, known build files in the login account's home)" remove_image_builder_files
[ "$ZERO" = 1 ] && act "fill the free disk space with zeros" zero_free_space
if [ "$DRY" = 0 ]; then
  sync
  echo
  echo "Ready. Shut this machine down now (systemctl poweroff) and export it with image/export_ova.py."
  echo "Do not boot it again before the export: that would create new keys and a new machine id inside the image."
fi
