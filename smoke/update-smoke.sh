#!/usr/bin/env bash
# Smoke test of `ginnungagap update` on a machine with Docker: the real engine, the real `docker compose build/up/tag`, a small
# throw-away compose project (its own name, volume and no published ports) and fake releases whose services are a few lines of
# Python. It checks the things only a real Docker shows:
#   - an update to release B rebuilds and recreates ONLY the services whose component changed (the others keep their container),
#   - the data volume is untouched (a marker file survives),
#   - a release whose service does not build is rolled back automatically (exit 2) and the old containers are still running,
#   - `--rollback` after a good update brings the old version back, from the local rollback images, with no build.
# Releases come from folders (`--offline-dir`): no GitHub, no token. Everything is removed afterwards (KEEP=1 keeps it).
#
#   smoke/update-smoke.sh        # needs Docker with compose and python3 (the base image python:3.12-slim is pulled once;
#                                # FAKE_BASE_IMAGE=<image with python> uses another one)
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
PROJECT=ginnupdsmoke
FAILED=0
export PYTHONPATH="$HERE"
export GINNUNGAGAP_UPDATE_DIR="$WORK/state/update"
export GINNUNGAGAP_FAKE_INSTALL_LOG="$WORK/install.log"
export REPOS_DIR="$WORK/state/components"
unset GINNUNGAGAP_INSTALL_ARGS || true

say() { printf '\n== %s\n' "$*"; }
ok() { printf '   ok   %s\n' "$*"; }
bad() { printf '   FAIL %s\n' "$*"; FAILED=1; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (wanted '$3', got '$2')"; fi; }

cleanup() {
  if [ "${KEEP:-0}" != "1" ]; then
    (cd "$WORK/app" 2>/dev/null && docker compose --project-name "$PROJECT" --env-file rendered/compose.env down -v --remove-orphans >/dev/null 2>&1) || true
    docker images --format '{{.Repository}}:{{.Tag}}' | grep "^${PROJECT}-" | xargs -r docker image rm -f >/dev/null 2>&1 || true
    rm -rf "$WORK"
  else
    echo "kept: $WORK (project $PROJECT)"
  fi
}
trap cleanup EXIT

compose() { (cd "$WORK/app" && docker compose --project-name "$PROJECT" --env-file rendered/compose.env "$@"); }
container_of() { compose ps -q "$1"; }
inside() { compose exec -T "$1" cat /VERSION; }
update() { python3 -c 'import sys; sys.path[:0] = sys.argv[1:3]; from smoke_driver import main; sys.exit(main(sys.argv[3:]))' "$WORK/driver" "$HERE" "$@"; }

say "prepare: fake releases, a small compose project, the driver"
mkdir -p "$WORK/app/rendered" "$WORK/driver" "$WORK/etc" "$WORK/state"
python3 - "$WORK" <<'PY'
import sys
from pathlib import Path
from tests.helpers import fake_release

work = Path(sys.argv[1])
rel = work / "releases"
fake_release.make_release(rel / "a", appliance="1.3.0")
fake_release.make_release(rel / "b", appliance="1.3.1", components={"docker-server-files": "1.0.1"})
fake_release.make_release(rel / "broken", appliance="1.3.2", components={"docker-server-files": "1.0.2"}, break_service="template-esxi")
fake_release.make_release(rel / "c", appliance="1.3.3", components={"docker-server-files": "1.0.3"})
PY
cat >"$WORK/app/compose.yml" <<'YML'
name: ginnupdsmoke
x-probe: &probe
  interval: 2s
  timeout: 3s
  retries: 10
  start_period: 1s
  test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
services:
  ginnungagap:
    profiles: [ginnungagap]
    build: ${REPOS_DIR}/TopologyOperator
    healthcheck: *probe
    volumes: ["data:/data"]
  template-esxi:
    profiles: [template-esxi]
    build: ${REPOS_DIR}/Docker-Server-Files/yaml-api
    healthcheck: *probe
  template-gns3:
    profiles: [template-gns3]
    build:
      context: ${REPOS_DIR}/Docker-Server-Files/gns3-template-api
      dockerfile: gns3_api.Dockerfile
    healthcheck: *probe
  generator:
    profiles: [generator]
    build:
      context: ${REPOS_DIR}/topology-generator
      dockerfile: topology.Dockerfile
    healthcheck: *probe
volumes:
  data:
YML
printf 'COMPOSE_PROJECT_NAME=%s\nCOMPOSE_FILE=compose.yml\nCOMPOSE_PROFILES=ginnungagap,template-esxi,template-gns3,generator\n' "$PROJECT" >"$WORK/app/rendered/compose.env"
cp "$HERE/ginnungagap.example.yml" "$WORK/etc/ginnungagap.yml"
cat >"$WORK/driver/smoke_driver.py" <<'PY'
"""Runs `ginnungagap update` for real, with the outside faked where it would touch the machine (root, backup, health URL)."""
import subprocess
import sys
import time
from pathlib import Path

from ginnungagap import update

def main(argv):
    import os

    work = Path(os.environ["SMOKE_WORK"])
    bundle = work / "app"

    def gate(config_file, bundle_dir, generator_config, **kwargs):
        # the final health gate: every service of the small project is healthy
        for _ in range(60):
            out = subprocess.run(
                ["docker", "compose", "--project-name", "ginnupdsmoke", "--env-file", "rendered/compose.env", "ps", "--format", "{{.Health}}"],
                cwd=bundle, capture_output=True, text=True,
            ).stdout.split()
            if out and all(x == "healthy" for x in out):
                return 0
            time.sleep(1)
        return 1

    def backup(config_file, bundle_dir, destination, **kwargs):
        folder = Path(destination) / "ginnungagap-backup-smoke"
        folder.mkdir(parents=True)
        (folder / "data.tgz").write_bytes(b"")
        return folder

    env = update.Env(
        is_root=lambda: True,
        confirm=lambda question: True,
        apply=gate,
        backup=backup,
        restore=lambda *a, **k: 0,
        fetch=lambda url: (200, '{"warning": null, "status": "ok"}', None),
        reachable=lambda host: True,
        docker_root=None,
        free_bytes=lambda path: 1 << 40,
        journal=lambda text: None,
    )
    layout = update.Layout.from_env()
    # the program is not installed here, so "the new program continues" is this process continuing (the real one is a new process)
    env.reexec = lambda tx_id, config: update.run_update(
        update.Options(action="continue", continue_id=tx_id), config, env, layout, bundle
    )
    options = update.Options(action=argv[0], yes=True)
    args = argv[1:]
    if "--offline-dir" in args:
        options.offline_dir = args[args.index("--offline-dir") + 1]
    return update.run_update(options, work / "etc" / "ginnungagap.yml", env, layout, bundle)
PY
export SMOKE_WORK="$WORK"

say "install release A on an empty machine (nothing to adopt: a fresh install)"
update apply --offline-dir "$WORK/releases/a" && ok "update to A" || bad "update to A failed"
check "template-esxi runs 1.0.0" "$(inside template-esxi)" "1.0.0"
docker run --rm -v "${PROJECT}_data:/data" --entrypoint sh "$(docker inspect --format '{{.Config.Image}}' "$(container_of ginnungagap)")" -c 'echo keep-me > /data/marker'
ID_GEN=$(container_of generator); ID_GNS=$(container_of template-gns3); ID_ESXI=$(container_of template-esxi)

say "update to B: only the Docker-Server-Files services change"
update apply --offline-dir "$WORK/releases/b" && ok "update to B" || bad "update to B failed"
check "template-esxi runs 1.0.1" "$(inside template-esxi)" "1.0.1"
check "template-gns3 runs 1.0.1" "$(inside template-gns3)" "1.0.1"
check "the generator container was not recreated" "$(container_of generator)" "$ID_GEN"
[ "$(container_of template-esxi)" != "$ID_ESXI" ] && ok "template-esxi was recreated" || bad "template-esxi was not recreated"
check "the data volume is untouched" "$(docker run --rm -v "${PROJECT}_data:/data" --entrypoint cat "$(docker inspect --format '{{.Config.Image}}' "$(container_of ginnungagap)")" /data/marker)" "keep-me"
ID_ESXI=$(container_of template-esxi)

say "a release whose service does not build: rolled back by itself (exit 2)"
set +e; update apply --offline-dir "$WORK/releases/broken"; CODE=$?; set -e
check "exit code" "$CODE" "2"
check "template-esxi still runs 1.0.1" "$(inside template-esxi)" "1.0.1"
check "and in the same container" "$(container_of template-esxi)" "$ID_ESXI"
check "the link is back on 1.0.1" "$(readlink "$WORK/state/components/Docker-Server-Files")" "../releases/Docker-Server-Files/1.0.1"
[ ! -e "$GINNUNGAGAP_UPDATE_DIR/transaction.json" ] && ok "no transaction left" || bad "transaction.json is still there"

say "a good update C, then --rollback (no build, no network)"
update apply --offline-dir "$WORK/releases/c" && ok "update to C" || bad "update to C failed"
check "template-esxi runs 1.0.3" "$(inside template-esxi)" "1.0.3"
update rollback && ok "rollback" || bad "rollback failed"
check "template-esxi runs 1.0.1 again" "$(inside template-esxi)" "1.0.1"
check "the recorded release is 1.3.1" "$(python3 -c "import json,os; print(json.load(open(os.environ['GINNUNGAGAP_UPDATE_DIR']+'/installed.json'))['appliance']['version'])")" "1.3.1"

say "the audit log"
check "started/ok pairs and the rolled-back one" "$(python3 -c "
import json,os
print(','.join(f\"{r['action']}:{r['result']}\" for r in map(json.loads, open(os.environ['GINNUNGAGAP_UPDATE_DIR']+'/audit.log'))))")" "apply:started,apply:ok,apply:started,apply:ok,apply:started,apply:rolled_back,apply:started,apply:ok,rollback:started,rollback:ok"

echo
if [ "$FAILED" = 0 ]; then echo "update smoke test passed"; else echo "update smoke test FAILED"; exit 1; fi
