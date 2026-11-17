#!/usr/bin/env bash
# End-to-end test stack for the App container, without Home Assistant OS.
#
#   HA_URL=http://172.30.32.1:18123 HA_TOKEN=<long-lived token> ./stack.sh up
#   ./stack.sh down
#
# Network "sl-hassio-test" uses the real HAOS addressing: the fake Supervisor
# (fake_supervisor.py) runs at 172.30.32.2 with the alias "supervisor"; the
# App container runs the real image (S6, run script, backend) with no
# published port. Ingress is published on 127.0.0.1:18200 only:
#   http://127.0.0.1:18200/hassio/ingress/smart_lighting   (sidebar-panel iframe)
#   http://127.0.0.1:18200/api/hassio_ingress/$INGRESS_TOKEN/
set -euo pipefail
cd "$(dirname "$0")"
APP_DIR="$(cd ../.. && pwd)"
NET=sl-hassio-test
IMAGE=local/smart_lighting:e2e
SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-e2e-supervisor-token-0123456789abcdef}"
INGRESS_TOKEN="${INGRESS_TOKEN:-e2eIngressToken_AbC123}"
DATA_DIR="${DATA_DIR:-$(mktemp -d)}"

down() {
  docker rm -f sl-test-app sl-test-supervisor >/dev/null 2>&1 || true
}

if [[ "${1:-}" == "down" ]]; then
  down
  exit 0
fi

: "${HA_URL:?set HA_URL (Home Assistant reachable from the test network)}"
: "${HA_TOKEN:?set HA_TOKEN (long-lived access token of that Home Assistant)}"

down
docker build -q --build-arg BUILD_VERSION=0.2.0 --build-arg BUILD_ARCH=amd64 -t "$IMAGE" "$APP_DIR" >/dev/null
docker network inspect "$NET" >/dev/null 2>&1 \
  || docker network create --driver bridge --subnet 172.30.32.0/23 --gateway 172.30.32.1 "$NET" >/dev/null

docker run -d --name sl-test-supervisor --network "$NET" --ip 172.30.32.2 --network-alias supervisor \
  -p 127.0.0.1:18200:80 -v "$PWD:/e2e:ro" --entrypoint python3 \
  -e SUPERVISOR_TOKEN="$SUPERVISOR_TOKEN" -e HA_URL="$HA_URL" -e HA_TOKEN="$HA_TOKEN" \
  -e APP_URL=http://sl-test-app:8099 -e INGRESS_TOKEN="$INGRESS_TOKEN" \
  "$IMAGE" /e2e/fake_supervisor.py >/dev/null

mkdir -p "$DATA_DIR"
echo '{"log_level": "info"}' > "$DATA_DIR/options.json"
docker run -d --name sl-test-app --network "$NET" -v "$DATA_DIR:/data" \
  -e SUPERVISOR_TOKEN="$SUPERVISOR_TOKEN" "$IMAGE" >/dev/null

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:18200/api/hassio_ingress/$INGRESS_TOKEN/api/health" >/dev/null 2>&1; then
    echo "stack up: http://127.0.0.1:18200/hassio/ingress/smart_lighting"
    exit 0
  fi
  sleep 1
done
echo "the App did not become ready" >&2
docker logs sl-test-app >&2 || true
exit 1
