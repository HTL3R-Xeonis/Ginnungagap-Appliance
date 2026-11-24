#!/bin/bash
# Install pinned image-build tooling. Deliberately enables no service, path unit or timer (also not the optional status page).
set -euo pipefail
umask 022

source_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
target_root=""
builder=""
packages=true
make_venv=true
replace_bootstrap=false
usage() {
    echo "Usage: sudo image/install-build-host.sh [--builder PATH] [--no-packages] [--no-venv] [--root STAGING_ROOT] [--replace-bootstrap]"
}
while (($#)); do
    case "$1" in
        --builder) builder=${2:?missing builder path}; shift 2 ;;
        --root) target_root=${2:?missing staging root}; shift 2 ;;
        --no-packages) packages=false; shift ;;
        --no-venv) make_venv=false; shift ;;
        --replace-bootstrap) replace_bootstrap=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
if [[ -n "$target_root" ]]; then
    [[ "$target_root" = /* && "$target_root" != / ]] || { echo 'staging root must be an absolute non-root path' >&2; exit 2; }
    [[ "$packages" = false && "$make_venv" = false ]] || { echo 'staging requires --no-packages --no-venv' >&2; exit 2; }
else
    [[ $(id -u) = 0 ]] || { echo 'run the installer as root' >&2; exit 2; }
    if command -v docker >/dev/null; then
        echo 'install this service on its dedicated build VM, not a Docker appliance or the testbox' >&2
        exit 2
    fi
fi
for file in build_image.py build/inspect-disk.sh build/inspect_image.py build/build-host.example.yml; do
    [[ -f "$source_root/image/$file" ]] || { echo "missing pinned tooling: image/$file" >&2; exit 2; }
done
if [[ "$make_venv" = true ]]; then
    [[ -n "$builder" ]] || builder="$source_root/../TopologyBuilder"
    [[ -f "$builder/pyproject.toml" || -f "$builder" ]] || { echo 'provide the trusted, pinned Mjonyr source or release tarball with --builder' >&2; exit 2; }
fi
if [[ "$packages" = true ]]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv python3-pip python3-yaml openssh-client nfs-common qemu-utils sudo ca-certificates
fi
base="$target_root/opt/ginnungagap-image-build"
config="$target_root/etc/ginnungagap-image-build"
state="$target_root/var/lib/ginnungagap-image-build"
install -d -m 0755 "$base/versions" "$target_root/usr/local/bin" "$target_root/etc/systemd/system" "$target_root/etc/sudoers.d" "$target_root/etc/tmpfiles.d"
install -d -m 0750 "$config"
install -d -m 0700 "$config/secrets" "$state" "$state/queue" "$state/runs" "$state/builds"
install -d -m 0750 "$target_root/var/lib/ginnungagap-image-build-inspect"
touch "$state/lock"
install -d -m 0755 "$target_root/mnt/esxi_templates"

# Include every tooling file in its fingerprint; never replace a pinned snapshot in place.
source_hash=$(tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --exclude=__pycache__ --exclude='*.pyc' -C "$source_root" -cf - image ginnungagap release schema pyproject.toml | sha256sum)
builder_hash=staging-without-venv
if [[ "$make_venv" = true ]]; then
    if [[ -d "$builder" ]]; then
        builder_hash=$(tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --exclude=.git --exclude=.venv --exclude=.env --exclude=__pycache__ --exclude='*.pyc' --exclude='*.egg-info' --exclude=build --exclude=dist -C "$builder" -cf - . | sha256sum)
    else
        builder_hash=$(sha256sum "$builder")
    fi
fi
hash=$(printf '%s\n%s\n' "$source_hash" "$builder_hash" | sha256sum | cut -c1-16)
snapshot="$base/versions/bootstrap-$hash"
if [[ ! -d "$snapshot" ]]; then
    scratch=$(mktemp -d "$base/versions/.install-XXXXXX")
    chmod 0755 "$scratch"
    trap '[[ -z "${scratch:-}" ]] || rm -rf -- "$scratch"' EXIT
    mkdir "$scratch/appliance"
    tar --exclude=__pycache__ --exclude='*.pyc' -C "$source_root" -cf - image ginnungagap release schema pyproject.toml README.md | tar -xf - -C "$scratch/appliance"
    chmod -R go-w "$scratch/appliance"
    chmod 0755 "$scratch/appliance/image/build/inspect-disk.sh"
    mv -- "$scratch" "$snapshot"
    scratch="$snapshot"
    if [[ "$make_venv" = true ]]; then
        python3 -m venv "$snapshot/venv"
        "$snapshot/venv/bin/python" -m pip install "$builder"
        "$snapshot/venv/bin/python" -I "$snapshot/appliance/image/build_image.py" --help >/dev/null
    fi
    touch "$snapshot/.ready"
    scratch=""
fi
[[ -f "$snapshot/.ready" ]] || { echo 'the pinned snapshot is incomplete; inspect it before retrying installation' >&2; exit 2; }
if [[ ! -e "$base/current" && ! -L "$base/current" ]]; then
    ln -s "versions/$(basename -- "$snapshot")" "$base/current"
elif [[ $(readlink "$base/current") != "versions/$(basename -- "$snapshot")" ]]; then
    if [[ "$replace_bootstrap" = true && $(readlink "$base/current") = versions/bootstrap-* ]]; then
        ln -s "versions/$(basename -- "$snapshot")" "$base/.current-new"
        mv -Tf -- "$base/.current-new" "$base/current"
    else
        echo 'a different pinned tooling snapshot is installed; use signed self-update (or explicit --replace-bootstrap for development snapshots only)' >&2
        exit 2
    fi
fi
for pair in 'tooling:current/appliance' 'venv:current/venv'; do
    name=${pair%%:*}; destination=${pair#*:}
    if [[ ! -L "$base/$name" ]]; then ln -s "$destination" "$base/$name"; fi
done
if [[ ! -e "$config/build-host.yml" ]]; then
    install -m 0640 "$source_root/image/build/build-host.example.yml" "$config/build-host.yml"
fi
if [[ ! -e "$config/allowed_signers" ]]; then
    install -m 0644 "$source_root/release/allowed_signers" "$config/allowed_signers"
fi
cat >"$target_root/usr/local/bin/ginnungagap-image-build" <<'WRAPPER'
#!/bin/sh
exec /opt/ginnungagap-image-build/venv/bin/python -I /opt/ginnungagap-image-build/tooling/image/build_image.py "$@"
WRAPPER
chmod 0755 "$target_root/usr/local/bin/ginnungagap-image-build"
install -m 0644 "$source_root"/image/build/systemd/* "$target_root/etc/systemd/system/"
minutes=$(python3 - "$config/build-host.yml" <<'PY'
import sys, yaml
with open(sys.argv[1]) as file:
    value = (yaml.safe_load(file) or {}).get('poll_minutes', 15)
if type(value) is not int or value < 1:
    raise SystemExit('poll_minutes must be a positive integer')
print(value)
PY
)
timer_dropin="$target_root/etc/systemd/system/ginnungagap-image-build-poll.timer.d"
install -d -m 0755 "$timer_dropin"
printf '[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=%smin\n' "$minutes" >"$timer_dropin/interval.conf"
cat >"$target_root/etc/sudoers.d/ginnungagap-image-build.new" <<'SUDOERS'
# One root-owned helper, which accepts only a strict run id below its fixed state directory.
imgbuild ALL=(root) NOPASSWD: /opt/ginnungagap-image-build/tooling/image/build/inspect-disk.sh *
SUDOERS
chmod 0440 "$target_root/etc/sudoers.d/ginnungagap-image-build.new"
mv -f -- "$target_root/etc/sudoers.d/ginnungagap-image-build.new" "$target_root/etc/sudoers.d/ginnungagap-image-build"
cat >"$target_root/etc/tmpfiles.d/ginnungagap-image-build.conf" <<'TMPFILES'
d /var/lib/ginnungagap-image-build-inspect 0750 root root -
TMPFILES
if [[ -z "$target_root" ]]; then
    getent group imgbuild >/dev/null || groupadd --system imgbuild
    id imgbuild >/dev/null 2>&1 || useradd --system --gid imgbuild --home-dir /var/lib/ginnungagap-image-build --shell /usr/sbin/nologin imgbuild
    chown -R root:root "$snapshot"
    chown root:imgbuild "$config" "$config/build-host.yml"
    chown -R imgbuild:imgbuild "$state"
    for unit in ginnungagap-image-build.service ginnungagap-image-build-poll.service ginnungagap-image-build-janitor.service; do
        dropin="$target_root/etc/systemd/system/$unit.d"
        install -d -m 0755 "$dropin"
        printf '[Service]\n' >"$dropin/credentials.conf"
        for pair in github-token:github-token sharekey:share-key; do
            credential=${pair%%:*}; filename=${pair#*:}
            if [[ -f "$config/secrets/$filename" ]]; then
                printf 'LoadCredential=%s:/etc/ginnungagap-image-build/secrets/%s\n' "$credential" "$filename" >>"$dropin/credentials.conf"
            fi
        done
    done
    visudo -cf "$target_root/etc/sudoers.d/ginnungagap-image-build"
    systemd-tmpfiles --create "$target_root/etc/tmpfiles.d/ginnungagap-image-build.conf"
    systemctl daemon-reload
fi
echo 'Installed pinned image-build tooling. No units were enabled.'
echo 'Configure build-host.yml, credentials, share mount and trusted signers; run a dry run before enabling the path and timer.'
echo 'Optional read-only status page: ginnungagap-image-build-status.service is installed but not enabled. Set status_http: true in build-host.yml, then run: sudo systemctl enable --now ginnungagap-image-build-status.service'
