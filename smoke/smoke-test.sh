#!/usr/bin/env bash
# Smoke test of the whole bundle on a machine with Docker: builds the images, starts every service ON ITS OWN and checks it,
# then all together through the gateway. It uses a throw-away project (its own name, volumes and ports), a fake template
# store and a host that does not exist, so nothing real is touched; it removes everything again (KEEP=1 keeps it).
#
#   smoke/smoke-test.sh            # needs Docker with compose, the sibling repositories (REPOS_DIR, default ..), no python
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
export REPOS_DIR=${REPOS_DIR:-$(cd "$HERE/.." && pwd)}
WORK=$(mktemp -d)
PROJECT=ginnsmoke
PORT=${SMOKE_PORT:-18080}
PROJ="$WORK/project"
FAILED=0

compose() { (cd "$PROJ" && COMPOSE_FILE=compose.yml:override.yml docker compose --project-name "$PROJECT" --env-file rendered/compose.env "$@"); }
say() { printf '\n== %s\n' "$*"; }
ok() { printf '   ok   %s\n' "$*"; }
bad() { printf '   FAIL %s\n' "$*"; FAILED=1; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (wanted '$3', got '$2')"; fi; }

# GET inside a service's own container (the images have python, not curl): prints "<status> <body>"
inside() {
  compose exec -T "$1" python -c "
import urllib.request, urllib.error
try:
    r = urllib.request.urlopen('$2', timeout=5); print(r.status, r.read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())"
}
status_of() { inside "$1" "$2" | cut -d' ' -f1; }
# start services; a failure to even create the container is shown instead of being swallowed
up() {
  compose up -d "$@" >"$WORK/up.log" 2>&1 || { bad "starting $* failed: $(tail -3 "$WORK/up.log" | tr '\n' ' ')"; return 1; }
}
wait_healthy() {  # service, seconds
  for _ in $(seq 1 "$2"); do
    [ "$(compose ps --format '{{.Health}}' "$1")" = "healthy" ] && return 0
    sleep 1
  done
  return 1
}
cleanup() {
  if [ "${KEEP:-0}" != "1" ]; then
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    # the data volume was first created by a plain `docker run` (without the compose labels), so `down -v` leaves it
    docker volume rm -f "${PROJECT}_data" >/dev/null 2>&1 || true
    docker images --format '{{.Repository}}' | grep "^${PROJECT}-" | sort -u | xargs -r docker image rm -f >/dev/null 2>&1 || true
    rm -rf "$WORK"
  else
    echo "kept: $WORK (project $PROJECT)"
  fi
}
trap cleanup EXIT

say "prepare: fake template store, test config, render"
mkdir -p "$PROJ" "$WORK/store/debian"
cp -r "$HERE/compose.yml" "$HERE/compose.legacy-ports.yml" "$HERE/docker" "$PROJ/"
cat > "$WORK/store/debian/debian.yml" <<'YML'
version: 1
templates:
  - {id: deb12, name: Debian Test 12, file: deb12.ova, category: linux, os: {family: debian, version: "12"}, cpu: 2, memory: 2048, disk: 20GB, hostname: deb12}
YML
printf 'FAKE-OVA-0123456789' > "$WORK/store/debian/deb12.ova"
cat > "$WORK/ginnungagap.yml" <<YML
version: 1
appliance: {public_url: http://localhost, timezone: UTC, tls: {mode: "off"}, http_port: $PORT, https_port: $((PORT + 1))}
templates: {store: {type: local, local: {path: $WORK/store}}, esxi_refresh_minutes: 1, gns3_refresh_minutes: 1}
generator: {llm_servers: [{name: test, url: "http://127.0.0.1:11434", model: m}]}
ova_deploy: {external_url: "http://127.0.0.1:9"}
YML
# the renderer runs in a throw-away python container, so the machine needs nothing but Docker
# (as your own user, so that the key file it writes - mode 0600 - stays readable for docker compose)
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e PYTHONPATH=/tmp/py -v "$HERE:/src:ro" -v "$WORK:/work" \
  -v "$REPOS_DIR/topology-generator/config:/gen:ro" -w /src python:3.12-slim \
  sh -c 'pip install -q --target /tmp/py pyyaml >/dev/null 2>&1 && python -m ginnungagap render /work/ginnungagap.yml --out /work/project/rendered --generator-config /gen >/dev/null'
printf 'esxi:\n  ip: 10.255.255.1\n  username: root\n  gns3_vm_name: TEST-VM\n' > "$WORK/app-settings.yml"
cat > "$PROJ/override.yml" <<YML
services:
  ginnungagap:
    environment:
      TOPOLOGYOPERATOR_SETTINGS: /app-settings.yml
    volumes:
      - $WORK/app-settings.yml:/app-settings.yml:ro
YML
ok "rendered into the throw-away project"

say "build every image"
compose build >/dev/null && ok "all images built" || { bad "build failed"; exit 1; }

say "template-esxi ALONE: degraded until a collector has run"
up template-esxi && sleep 8
check "only one container runs" "$(compose ps --format '{{.Service}}' | tr '\n' ' ')" "template-esxi "
check "/health is 503 without templates.yml" "$(status_of template-esxi http://127.0.0.1:8000/health)" "503"

say "collector-esxi: fills it from the directory, the API sees it without a restart"
up collector-esxi && sleep 10
check "/health is 200" "$(status_of template-esxi http://127.0.0.1:8000/health)" "200"
check "download straight from the store" "$(inside template-esxi http://127.0.0.1:8000/api/download/deb12 | cut -d' ' -f2-)" "FAKE-OVA-0123456789"

say "template-gns3 ALONE, then collector-gns3 with a host profile from the app's data volume"
up template-gns3 && sleep 6
check "/health is 503 before the collector" "$(status_of template-gns3 http://127.0.0.1:8000/health)" "503"
docker run --rm -v "${PROJECT}_data:/data" --entrypoint python "${PROJECT}-collector-gns3" -c "
import os
os.makedirs('/data/settings_profiles', exist_ok=True)
open('/data/settings_profiles/lab.yml', 'w').write('esxi:\n  ip: 10.255.255.1\n  username: root\n  gns3_vm_name: TEST-VM\n')"
up collector-gns3 && sleep 12
check "/health is 200 with one server" "$(inside template-gns3 http://127.0.0.1:8000/health | grep -c '"servers":1')" "1"

say "generator ALONE"
up generator && sleep 8
check "/health is 200" "$(status_of generator http://127.0.0.1:8000/health)" "200"

say "gateway ALONE: starts without the app, answers 502 for it, routes to what runs"
up gateway && sleep 4
check "/ is 502 while the app does not run" "$(curl -s -o /dev/null -w '%{http_code}' localhost:$PORT/)" "502"
check "/templates/esxi/health" "$(curl -s -o /dev/null -w '%{http_code}' localhost:$PORT/templates/esxi/health)" "200"
check "download through the gateway" "$(curl -s localhost:$PORT/templates/esxi/api/download/deb12)" "FAKE-OVA-0123456789"

say "ginnungagap (the app) with a host that does not answer: must start anyway"
up ginnungagap
wait_healthy ginnungagap 90 && ok "healthy within 90 s" || bad "app not healthy within 90 s"
check "landing page through the gateway" "$(curl -s -o /dev/null -w '%{http_code}' localhost:$PORT/)" "200"
check "it asks the template service over the compose network" "$(curl -s localhost:$PORT/templates | grep -c 'Debian Test 12')" "1"

say "everything together"
up && sleep 10
check "all seven services run" "$(compose ps --format '{{.State}}' | grep -c running)" "7"

echo
if [ "$FAILED" = "0" ]; then echo "SMOKE TEST PASSED"; else echo "SMOKE TEST FAILED"; exit 1; fi
