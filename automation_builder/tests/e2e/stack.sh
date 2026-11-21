#!/usr/bin/env bash
# End-to-end stack for the real Automation Builder image, without Home Assistant OS.
#
#   HA_URL=http://172.30.32.1:18123 HA_TOKEN=<long-lived token> DATA_DIR=<dir> ./stack.sh up
#   ./stack.sh down | stop-app | start-app
#
# It shares the docker network "sl-hassio-test" (172.30.32.0/23, gateway 172.30.32.1) with the other Apps' e2e
# stacks; this stack's fake Supervisor runs at 172.30.33.251 (alias ab-supervisor), so all of them can run side by
# side. The App container runs the real image (S6, run script, backend); only the addresses of the Supervisor
# proxy and of Ingress are passed in (AB_HA_WS_URL, AB_HA_API_URL, AB_ALLOWED_CLIENTS). Ingress is published on
# 127.0.0.1:18400 only:
#   http://127.0.0.1:18400/hassio/ingress/ab
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(cd ../.. && pwd)"
NET=sl-hassio-test
IMAGE=local/automation_builder:e2e
SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-ab-e2e-supervisor-token-0123456789}"
INGRESS_TOKEN="${INGRESS_TOKEN:-abIngressToken_XyZ789}"

case "${1:-}" in
  down) docker rm -f ab-test-app ab-test-supervisor >/dev/null 2>&1 || true; exit 0 ;;
  stop-app) docker stop -t 10 ab-test-app >/dev/null; exit 0 ;;
  start-app) docker start ab-test-app >/dev/null ;;
  up)
    : "${HA_URL:?set HA_URL}" "${HA_TOKEN:?set HA_TOKEN}" "${DATA_DIR:?set DATA_DIR}"
    docker rm -f ab-test-app ab-test-supervisor >/dev/null 2>&1 || true
    docker build -q --build-arg BUILD_VERSION=0.2.0 --build-arg BUILD_ARCH=amd64 -t "$IMAGE" "$APP_DIR" >/dev/null
    docker network inspect "$NET" >/dev/null 2>&1 \
      || docker network create --driver bridge --subnet 172.30.32.0/23 --gateway 172.30.32.1 "$NET" >/dev/null
    docker run -d --name ab-test-supervisor --network "$NET" --ip 172.30.33.251 --network-alias ab-supervisor \
      -p 127.0.0.1:18400:80 -v "$PWD:/e2e:ro" --entrypoint python3 \
      -e SUPERVISOR_TOKEN="$SUPERVISOR_TOKEN" -e HA_URL="$HA_URL" -e HA_TOKEN="$HA_TOKEN" \
      -e APP_URL=http://ab-test-app:8099 -e INGRESS_TOKEN="$INGRESS_TOKEN" \
      "$IMAGE" /e2e/fake_supervisor.py >/dev/null
    mkdir -p "$DATA_DIR"
    [ -f "$DATA_DIR/options.json" ] || echo '{"log_level": "info"}' > "$DATA_DIR/options.json"
    docker run -d --name ab-test-app --network "$NET" -v "$DATA_DIR:/data" \
      -e SUPERVISOR_TOKEN="$SUPERVISOR_TOKEN" -e AB_HA_WS_URL=ws://ab-supervisor/core/websocket \
      -e AB_HA_API_URL=http://ab-supervisor/core/api -e AB_ALLOWED_CLIENTS=172.30.33.251 "$IMAGE" >/dev/null
    ;;
  *) echo "usage: $0 up|down|stop-app|start-app" >&2; exit 2 ;;
esac
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:18400/api/hassio_ingress/$INGRESS_TOKEN/api/health" >/dev/null 2>&1; then
    echo "stack up: http://127.0.0.1:18400/hassio/ingress/ab"
    exit 0
  fi
  sleep 1
done
echo "the App did not become ready" >&2
docker logs ab-test-app >&2 || true
exit 1
