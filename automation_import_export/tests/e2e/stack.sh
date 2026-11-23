#!/usr/bin/env bash
# End-to-end stack for the real Automation Import/Export Manager image, without Home Assistant OS.
#
#   HA_URL=http://172.30.32.1:18123 HA_TOKEN=<long-lived token> DATA_DIR=<dir> [HA_CONFIG=<config dir>] \
#     [TEST_HOOKS=1] ./stack.sh up
#   ./stack.sh down | stop-app | kill-app | start-app
#
# HA_CONFIG is the disposable Home Assistant's configuration folder, mounted read-only at /homeassistant like
# the App's "map: homeassistant_config" on Home Assistant OS. TEST_HOOKS=1 enables the App's test pauses
# (data/test_hooks.json) that the interruption checks use; never set on a real installation.
#
# It shares the docker network "sl-hassio-test" (172.30.32.0/23, gateway 172.30.32.1) with the other Apps' e2e
# stacks; this stack's fake Supervisor runs at 172.30.33.249 (alias aie-supervisor), so all of them can run side by
# side. The App container runs the real image (S6, run script, backend); only the addresses of the Supervisor
# proxy and of Ingress are passed in (AIE_HA_WS_URL, AIE_HA_API_URL, AIE_ALLOWED_CLIENTS). Ingress is published on
# 127.0.0.1:18410 only:
#   http://127.0.0.1:18410/hassio/ingress/aie
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(cd ../.. && pwd)"
NET=sl-hassio-test
IMAGE=local/automation_import_export:e2e
SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-aie-e2e-supervisor-token-0123456789}"
INGRESS_TOKEN="${INGRESS_TOKEN:-aieIngressToken_Qw3rty}"

case "${1:-}" in
  down) docker rm -f aie-test-app aie-test-supervisor >/dev/null 2>&1 || true; exit 0 ;;
  stop-app) docker stop -t 10 aie-test-app >/dev/null; exit 0 ;;
  kill-app) docker kill aie-test-app >/dev/null; exit 0 ;;
  start-app) docker start aie-test-app >/dev/null ;;
  up)
    : "${HA_URL:?set HA_URL}" "${HA_TOKEN:?set HA_TOKEN}" "${DATA_DIR:?set DATA_DIR}"
    docker rm -f aie-test-app aie-test-supervisor >/dev/null 2>&1 || true
    docker build -q --build-arg BUILD_VERSION=0.2.0 --build-arg BUILD_ARCH=amd64 -t "$IMAGE" "$APP_DIR" >/dev/null
    docker network inspect "$NET" >/dev/null 2>&1 \
      || docker network create --driver bridge --subnet 172.30.32.0/23 --gateway 172.30.32.1 "$NET" >/dev/null
    docker run -d --name aie-test-supervisor --network "$NET" --ip 172.30.33.249 --network-alias aie-supervisor \
      -p 127.0.0.1:18410:80 -v "$PWD:/e2e:ro" --entrypoint python3 \
      -e SUPERVISOR_TOKEN="$SUPERVISOR_TOKEN" -e HA_URL="$HA_URL" -e HA_TOKEN="$HA_TOKEN" \
      -e APP_URL=http://aie-test-app:8099 -e INGRESS_TOKEN="$INGRESS_TOKEN" \
      "$IMAGE" /e2e/fake_supervisor.py >/dev/null
    mkdir -p "$DATA_DIR"
    [ -f "$DATA_DIR/options.json" ] || echo '{"log_level": "info"}' > "$DATA_DIR/options.json"
    MOUNT=()
    [ -n "${HA_CONFIG:-}" ] && MOUNT=(-v "$HA_CONFIG:/homeassistant:ro")
    docker run -d --name aie-test-app --network "$NET" -v "$DATA_DIR:/data" "${MOUNT[@]}" \
      -e AIE_TEST_HOOKS="${TEST_HOOKS:-}" -e SUPERVISOR_TOKEN="$SUPERVISOR_TOKEN" -e AIE_HA_WS_URL=ws://aie-supervisor/core/websocket \
      -e AIE_HA_API_URL=http://aie-supervisor/core/api -e AIE_ALLOWED_CLIENTS=172.30.33.249 "$IMAGE" >/dev/null
    ;;
  *) echo "usage: $0 up|down|stop-app|kill-app|start-app" >&2; exit 2 ;;
esac
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:18410/api/hassio_ingress/$INGRESS_TOKEN/api/health" >/dev/null 2>&1; then
    echo "stack up: http://127.0.0.1:18410/hassio/ingress/aie"
    exit 0
  fi
  sleep 1
done
echo "the App did not become ready" >&2
docker logs aie-test-app >&2 || true
exit 1
