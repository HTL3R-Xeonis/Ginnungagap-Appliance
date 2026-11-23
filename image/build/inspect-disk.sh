#!/bin/bash
# Root-owned helper. sudoers permits this path, never a caller-chosen script.
# Only RUN_ID is accepted; all input files live in the fixed service runs root.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset PYTHONPATH PYTHONHOME BASH_ENV ENV CDPATH
umask 077
if [ "$EUID" -ne 0 ] || [ "$#" -ne 1 ] ||
   ! [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?-[0-9]{8}T[0-9]{6}Z$ ]]; then
  echo '{"passed":false,"checks":[{"name":"inspection helper arguments","passed":false,"required":true}]}' >&2
  exit 2
fi
RUN_ID=$1
RUNS=/var/lib/ginnungagap-image-build/runs
PINNED=$(cd -- "$(dirname -- "$0")" && pwd -P)
[ -f "$PINNED/inspect_image.py" ] && [ ! -L "$PINNED/inspect_image.py" ] && [ "$(stat -c %u "$PINNED/inspect_image.py")" = 0 ] || exit 2
CODE_MODE=$(stat -c %a "$PINNED/inspect_image.py")
(( (8#$CODE_MODE & 8#022) == 0 )) || exit 2
SCRATCH=/var/lib/ginnungagap-image-build-inspect
[ -d "$SCRATCH" ] && [ ! -L "$SCRATCH" ] && [ "$(stat -c %u "$SCRATCH")" = 0 ] || exit 2
SCRATCH_MODE=$(stat -c %a "$SCRATCH")
(( (8#$SCRATCH_MODE & 8#022) == 0 )) || exit 2
WORK=$(mktemp -d "$SCRATCH/run.XXXXXXXX")
LOOP=
MOUNTED=0
cleanup() {
  if [ "$MOUNTED" = 1 ]; then umount "$WORK/root" || true; fi
  if [ -n "$LOOP" ]; then losetup --detach "$LOOP" || true; fi
  case "$WORK" in /var/lib/ginnungagap-image-build-inspect/run.*) rm -rf -- "$WORK";; esac
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

# All path traversal is openat + O_NOFOLLOW; an unprivileged user cannot swap a
# checked path into a symlink between validation and a privileged read.
/usr/bin/python3 -I - "$RUNS" "$RUN_ID" "$WORK" <<'PY'
import os,stat,sys
root,run,work=sys.argv[1:]
flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
fd=os.open("/",flags)
try:
    for part in root.strip("/").split("/")+[run]:
        new=os.open(part,flags,dir_fd=fd);os.close(fd);fd=new
    req=os.open("inspection-request.json",os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
    try:
        info=os.fstat(req)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size>1024*1024 or info.st_nlink!=1:
            raise ValueError("unsafe inspection request")
        with os.fdopen(req,"rb",closefd=False) as src,open(work+"/request.json","xb") as dst:
            dst.write(src.read())
    finally: os.close(req)
    exp=os.open("export",flags,dir_fd=fd)
    try:
        candidates=[name for name in os.listdir(exp) if name.endswith(".ova")]
        if len(candidates)!=1: raise ValueError("expected exactly one OVA")
        disk=os.open(candidates[0],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=exp)
        try:
            info=os.fstat(disk)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise ValueError("unsafe OVA")
            with os.fdopen(disk,"rb",closefd=False) as src,open(work+"/image.ova","xb") as dst:
                for block in iter(lambda:src.read(1024*1024),b""): dst.write(block)
        finally: os.close(disk)
    finally: os.close(exp)
finally: os.close(fd)
PY

/usr/bin/python3 -I "$PINNED/inspect_image.py" --extract-ova "$WORK/image.ova" --destination "$WORK"
rm -f -- "$WORK/image.ova"
qemu-img info --output=json "$WORK/disk.vmdk" | /usr/bin/python3 -I -c 'import json,shutil,sys; size=json.load(sys.stdin).get("virtual-size",0); available=shutil.disk_usage(sys.argv[1]).free; sys.exit(0 if isinstance(size,int) and 0 < size <= available-1024**3 else "Not enough disk space for offline inspection")' "$WORK"
qemu-img convert -f vmdk -O raw "$WORK/disk.vmdk" "$WORK/disk.raw"
rm -f -- "$WORK/image.ova" "$WORK/disk.vmdk"
LOOP=$(losetup --read-only --partscan --find --show "$WORK/disk.raw")
[[ "$LOOP" =~ ^/dev/loop[0-9]+$ ]] || exit 2
[ "$(blockdev --getro "$LOOP")" = 1 ] || exit 2
mkdir "$WORK/root"
FOUND=0
while read -r device fstype; do
  if [ -z "$fstype" ]; then fstype=$(blkid -p -s TYPE -o value "$device" 2>/dev/null || true); fi
  [ "$fstype" = ext4 ] || continue
  [[ "$device" == "$LOOP" || "$device" == "$LOOP"p[0-9]* ]] || exit 2
  mount -t ext4 -o ro,noload,nodev,nosuid,noexec "$device" "$WORK/root"
  MOUNTED=1
  if [ -f "$WORK/root/etc/shadow" ] && [ ! -L "$WORK/root/etc/shadow" ]; then
    FOUND=1
    break
  fi
  umount "$WORK/root"
  MOUNTED=0
done < <(lsblk --noheadings --raw --output PATH,FSTYPE "$LOOP")
[ "$FOUND" = 1 ] || { echo "No ext4 appliance root filesystem found" >&2; exit 2; }
/usr/bin/python3 -I "$PINNED/inspect_image.py" \
  --tree-root "$WORK/root" --raw-disk "$WORK/disk.raw" --request "$WORK/request.json"
