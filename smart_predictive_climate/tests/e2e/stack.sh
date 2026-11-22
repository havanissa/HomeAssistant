#!/usr/bin/env bash
# End-to-end stack for the real Smart Adaptive & Predictive Climate image, without Home Assistant OS.
#
#   HA_URL=http://172.30.32.1:18123 HA_TOKEN=<long-lived token> DATA_DIR=<dir> ./stack.sh up
#   ./stack.sh down | stop-app | start-app
#
# It shares the docker network "sl-hassio-test" (172.30.32.0/23, gateway 172.30.32.1) with the other Apps' e2e
# stacks (Smart Comfort's Supervisor address is 172.30.32.2); this stack's fake Supervisor runs at 172.30.33.252
# (alias spc-supervisor), so the Apps run side by side. The App container runs the real image (S6, run script,
# backend); only the address of the Supervisor proxy and of Ingress are passed in (SPC_HA_WS_URL,
# SPC_ALLOWED_CLIENTS). Ingress is published on 127.0.0.1:18500 only:
#   http://127.0.0.1:18500/hassio/ingress/spc
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(cd ../.. && pwd)"
NET=sl-hassio-test
IMAGE=local/spc:e2e
SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-spc-e2e-supervisor-token-0123456789}"
INGRESS_TOKEN="${INGRESS_TOKEN:-spcIngressToken_XyZ789}"

case "${1:-}" in
  down) docker rm -f spc-test-app spc-test-supervisor >/dev/null 2>&1 || true; exit 0 ;;
  stop-app) docker stop -t 10 spc-test-app >/dev/null; exit 0 ;;
  start-app) docker start spc-test-app >/dev/null ;;
  up)
    : "${HA_URL:?set HA_URL}" "${HA_TOKEN:?set HA_TOKEN}" "${DATA_DIR:?set DATA_DIR}"
    docker rm -f spc-test-app spc-test-supervisor >/dev/null 2>&1 || true
    # SOURCE_REF: build the image of an earlier release from git (upgrade tests), else the working tree.
    if [[ -n "${SOURCE_REF:-}" ]]; then
      SRC="$(mktemp -d)"
      git -C "$APP_DIR/.." archive "$SOURCE_REF" smart_predictive_climate | tar -x -C "$SRC"
      docker build -q --build-arg BUILD_ARCH=amd64 -t "$IMAGE" "$SRC/smart_predictive_climate" >/dev/null
      rm -rf "$SRC"
    else
      docker build -q --build-arg BUILD_VERSION=0.1.0 --build-arg BUILD_ARCH=amd64 -t "$IMAGE" "$APP_DIR" >/dev/null
    fi
    docker network inspect "$NET" >/dev/null 2>&1 \
      || docker network create --driver bridge --subnet 172.30.32.0/23 --gateway 172.30.32.1 "$NET" >/dev/null
    docker run -d --name spc-test-supervisor --network "$NET" --ip 172.30.33.252 --network-alias spc-supervisor \
      -p 127.0.0.1:18500:80 -v "$PWD:/e2e:ro" --entrypoint python3 \
      -e SUPERVISOR_TOKEN="$SUPERVISOR_TOKEN" -e HA_URL="$HA_URL" -e HA_TOKEN="$HA_TOKEN" \
      -e APP_URL=http://spc-test-app:8099 -e INGRESS_TOKEN="$INGRESS_TOKEN" \
      "$IMAGE" /e2e/fake_supervisor.py >/dev/null
    mkdir -p "$DATA_DIR"
    [ -f "$DATA_DIR/options.json" ] || echo '{"log_level": "info"}' > "$DATA_DIR/options.json"
    docker run -d --name spc-test-app --network "$NET" -v "$DATA_DIR:/data" \
      -e SUPERVISOR_TOKEN="$SUPERVISOR_TOKEN" -e SPC_HA_WS_URL=ws://spc-supervisor/core/websocket \
      -e SPC_ALLOWED_CLIENTS=172.30.33.252 "$IMAGE" >/dev/null
    ;;
  *) echo "usage: $0 up|down|stop-app|start-app" >&2; exit 2 ;;
esac
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:18500/api/hassio_ingress/$INGRESS_TOKEN/api/health" >/dev/null 2>&1; then
    echo "stack up: http://127.0.0.1:18500/hassio/ingress/spc"
    exit 0
  fi
  sleep 1
done
echo "the App did not become ready" >&2
docker logs spc-test-app >&2 || true
exit 1
