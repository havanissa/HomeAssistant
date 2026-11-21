#!/usr/bin/env bash
# End-to-end stack for the real Smart Occupancy Detection image, without Home Assistant OS.
#
#   HA_URL=http://172.30.32.1:18123 HA_TOKEN=<long-lived token> DATA_DIR=<dir> ./stack.sh up
#   ./stack.sh down | stop-app | start-app
#
# It shares the docker network "sl-hassio-test" (172.30.32.0/23, gateway 172.30.32.1) with Smart Comfort's e2e
# stack, which uses the Supervisor address 172.30.32.2; this stack's fake Supervisor runs at 172.30.33.250
# (alias sod-supervisor), so both Apps run side by side. The App container runs the real image (S6, run script,
# backend); only the address of the Supervisor proxy and of Ingress are passed in (SOD_HA_WS_URL,
# SOD_ALLOWED_CLIENTS). Ingress is published on 127.0.0.1:18300 only:
#   http://127.0.0.1:18300/hassio/ingress/sod
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(cd ../.. && pwd)"
NET=sl-hassio-test
IMAGE=local/sod:e2e
SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-sod-e2e-supervisor-token-0123456789}"
INGRESS_TOKEN="${INGRESS_TOKEN:-sodIngressToken_XyZ789}"

case "${1:-}" in
  down) docker rm -f sod-test-app sod-test-supervisor >/dev/null 2>&1 || true; exit 0 ;;
  stop-app) docker stop -t 10 sod-test-app >/dev/null; exit 0 ;;
  start-app) docker start sod-test-app >/dev/null ;;
  up)
    : "${HA_URL:?set HA_URL}" "${HA_TOKEN:?set HA_TOKEN}" "${DATA_DIR:?set DATA_DIR}"
    docker rm -f sod-test-app sod-test-supervisor >/dev/null 2>&1 || true
    # SOURCE_REF: build the image of an earlier release from git (upgrade tests), else the working tree.
    if [[ -n "${SOURCE_REF:-}" ]]; then
      SRC="$(mktemp -d)"
      git -C "$APP_DIR/.." archive "$SOURCE_REF" smart_occupancy_detection | tar -x -C "$SRC"
      docker build -q --build-arg BUILD_ARCH=amd64 -t "$IMAGE" "$SRC/smart_occupancy_detection" >/dev/null
      rm -rf "$SRC"
    else
      docker build -q --build-arg BUILD_VERSION=0.2.0 --build-arg BUILD_ARCH=amd64 -t "$IMAGE" "$APP_DIR" >/dev/null
    fi
    docker network inspect "$NET" >/dev/null 2>&1 \
      || docker network create --driver bridge --subnet 172.30.32.0/23 --gateway 172.30.32.1 "$NET" >/dev/null
    docker run -d --name sod-test-supervisor --network "$NET" --ip 172.30.33.250 --network-alias sod-supervisor \
      -p 127.0.0.1:18300:80 -v "$PWD:/e2e:ro" --entrypoint python3 \
      -e SUPERVISOR_TOKEN="$SUPERVISOR_TOKEN" -e HA_URL="$HA_URL" -e HA_TOKEN="$HA_TOKEN" \
      -e APP_URL=http://sod-test-app:8099 -e INGRESS_TOKEN="$INGRESS_TOKEN" \
      "$IMAGE" /e2e/fake_supervisor.py >/dev/null
    mkdir -p "$DATA_DIR"
    [ -f "$DATA_DIR/options.json" ] || echo '{"log_level": "info"}' > "$DATA_DIR/options.json"
    docker run -d --name sod-test-app --network "$NET" -v "$DATA_DIR:/data" \
      -e SUPERVISOR_TOKEN="$SUPERVISOR_TOKEN" -e SOD_HA_WS_URL=ws://sod-supervisor/core/websocket \
      -e SOD_ALLOWED_CLIENTS=172.30.33.250 "$IMAGE" >/dev/null
    ;;
  *) echo "usage: $0 up|down|stop-app|start-app" >&2; exit 2 ;;
esac
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:18300/api/hassio_ingress/$INGRESS_TOKEN/api/health" >/dev/null 2>&1; then
    echo "stack up: http://127.0.0.1:18300/hassio/ingress/sod"
    exit 0
  fi
  sleep 1
done
echo "the App did not become ready" >&2
docker logs sod-test-app >&2 || true
exit 1
