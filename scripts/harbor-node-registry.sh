#!/usr/bin/env bash
# Make the k3s node's containerd trust (and optionally authenticate to) the
# in-cluster Harbor. Run as root ON EACH NODE, once, after Harbor is healthy:
#
#   sudo scripts/harbor-node-registry.sh                       # public projects
#   sudo scripts/harbor-node-registry.sh 'robot$proj+name' PASS # private + robot
#
# It writes:
#   /etc/hosts                       192.168.10.51 harbor.k3s.local
#   /etc/rancher/k3s/harbor-ca.crt   ca.crt from secret harbor-tls (internal CA)
#   /etc/rancher/k3s/registries.yaml configs.<host>.tls.ca_file [+ auth]
# and then restarts k3s so containerd reloads. Running pods are not restarted;
# the API/containerd are briefly unavailable.
#
# Requires kubectl (admin) on the node to fetch the CA — or copy
# /etc/rancher/k3s/harbor-ca.crt there yourself and set SKIP_CA_FETCH=1.
set -euo pipefail

HOST_NAME="${HARBOR_HOST:-harbor.k3s.local}"
HARBOR_IP="${HARBOR_IP:-192.168.10.51}"
ROBOT_USER="${1:-}"
ROBOT_PASS="${2:-}"
CA_FILE=/etc/rancher/k3s/harbor-ca.crt
REGISTRIES=/etc/rancher/k3s/registries.yaml

if [ "$(id -u)" != "0" ]; then echo "run as root (sudo)" >&2; exit 1; fi

echo "==> /etc/hosts"
if ! grep -qE "[[:space:]]${HOST_NAME//./\\.}([[:space:]]|$)" /etc/hosts; then
  echo "$HARBOR_IP $HOST_NAME" >> /etc/hosts
fi

echo "==> $CA_FILE"
if [ "${SKIP_CA_FETCH:-0}" != "1" ]; then
  kubectl -n harbor get secret harbor-tls -o jsonpath='{.data.ca\.crt}' | base64 -d > "$CA_FILE"
fi
chmod 644 "$CA_FILE"
openssl x509 -in "$CA_FILE" -noout -subject -dates 2>/dev/null || { echo "not a valid CA file" >&2; exit 1; }

echo "==> $REGISTRIES"
mkdir -p /etc/rancher/k3s
if [ -f "$REGISTRIES" ]; then
  cp -a "$REGISTRIES" "${REGISTRIES}.bak.$(date +%Y%m%d%H%M%S)"
fi
{
  echo "configs:"
  echo "  ${HOST_NAME}:"
  if [ -n "$ROBOT_USER" ]; then
    echo "    auth:"
    echo "      username: \"${ROBOT_USER}\""
    echo "      password: \"${ROBOT_PASS}\""
  fi
  echo "    tls:"
  echo "      ca_file: ${CA_FILE}"
} > "$REGISTRIES"
chmod 600 "$REGISTRIES"

echo "==> restarting k3s"
systemctl restart k3s
for _ in $(seq 1 60); do
  if kubectl get --raw /readyz >/dev/null 2>&1; then echo "    k3s ready"; break; fi
  sleep 2
done

echo
echo "Verify with an image that exists in Harbor, e.g.:"
echo "  sudo k3s crictl pull ${HOST_NAME}/library/busybox:1.36"
